"""Media publication and replay over real HTTP, including failed S3 writes."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request

import pytest
from PIL import Image
from botocore import UNSIGNED
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import artifact_store as store
import hermes_agent as ha
from http_shell import App, serve
from media_input import artifact_tool_result
from turns import TurnManager


@pytest.fixture
def storage(monkeypatch):
    state = SimpleNamespace(objects={}, requests=[], fail_put=False, fail_read=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", **headers):
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), str(v))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_PUT(self):
            assert self.headers.get("Authorization") is None
            state.requests.append(("PUT", self.path))
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if state.fail_put:
                return self.reply(503)
            expected = base64.b64encode(hashlib.md5(body).digest()).decode()
            if self.headers.get("Content-MD5") not in (None, expected):
                return self.reply(400)
            if self.headers.get("If-None-Match") == "*" and self.path in state.objects:
                return self.reply(412)
            state.objects[self.path] = body
            self.reply(200)

        def do_GET(self):
            assert self.headers.get("Authorization") is None
            state.requests.append((self.command, self.path))
            if state.fail_read:
                return self.reply(503)
            body = state.objects.get(self.path)
            if body is None:
                return self.reply(404)
            etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            if self.headers.get("If-Match", etag) != etag:
                return self.reply(412)
            if self.headers.get("Range"):
                first, last = self.headers["Range"].removeprefix("bytes=").split("-")
                first, last = int(first), int(last)
                return self.reply(206, body[first:last+1], ETag=etag,
                                  Content_Range=f"bytes {first}-{last}/{len(body)}")
            self.reply(200, body, ETag=etag, Last_Modified="Mon, 12 Oct 2026 12:00:00 GMT")

        do_HEAD = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("AUTUMN_S3_ENDPOINT", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("ARTIFACT_S3_PREFIX", raising=False)
    yield state
    server.shutdown()
    server.server_close()


def png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 32), "red").save(path)
    return path.read_bytes()


def get(url, **headers):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15)


@pytest.fixture
def browser(tmp_path):
    class Auth:
        def authenticate(self, cookie):
            return SimpleNamespace(user_id=cookie or "alice")

    app = App(artifacts_dir=tmp_path, authenticator=Auth())
    app.artifact_authorizer = lambda req, sid: sid == "session-a" and req.user_id == "alice"
    server = serve(app, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_publish_then_replay_without_local_media(storage, browser, tmp_path):
    path = tmp_path / "session-a" / "run" / "图片.png"
    body = png(path)
    url = store.publish_session(tmp_path, "session-a")[0]
    assert not path.exists()
    assert list(storage.objects.values()) == [body]
    with get(browser + url) as response:
        assert response.read() == body
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["Vary"] == "Cookie"
        assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
    result = artifact_tool_result(url, "inspect", tmp_path, lambda sid: sid == "session-a")
    assert result["meta"]["width"] == 64
    assert not path.exists()  # opening for the model must not restore permanent media


def test_remote_video_ranges_validators_and_no_full_buffer(storage, browser, tmp_path, monkeypatch):
    path = tmp_path / "session-a" / "clip.mp4"
    path.parent.mkdir()
    body = bytes(range(256)) * 8192
    path.write_bytes(body)
    url = store.publish_file(tmp_path, path)
    original = store.open_media
    reads = []

    def open_checked(*args, **kwargs):
        response = original(*args, **kwargs)
        read = response["Body"].read

        def bounded(size=-1):
            assert 0 < size <= store.CHUNK
            reads.append(size)
            return read(size)

        response["Body"].read = bounded
        return response

    monkeypatch.setattr(store, "open_media", open_checked)
    with get(browser + url) as response:
        assert response.read() == body
        etag = response.headers["ETag"]
        assert response.headers["Content-Length"] == str(len(body))
        assert response.headers["Transfer-Encoding"] is None
        assert response.headers["Content-Type"] == "video/mp4"
    assert len(reads) >= 8
    for range_value, expected in [("bytes=20-39", body[20:40]), ("bytes=-17", body[-17:])]:
        with get(browser + url, Range=range_value, **{"If-Range": etag}) as response:
            assert response.status == 206 and response.read() == expected
            assert response.headers["ETag"] == etag
    for validator in ['"old"', "W/" + etag]:
        with get(browser + url, Range="bytes=20-39", **{"If-Range": validator}) as response:
            assert response.status == 200 and response.read() == body
    with pytest.raises(urllib.error.HTTPError) as error:
        get(browser + url, **{"If-None-Match": etag})
    assert error.value.code == 304
    with pytest.raises(urllib.error.HTTPError) as error:
        get(browser + url, Range=f"bytes={len(body)}-")
    assert error.value.code == 416
    assert error.value.headers["Content-Range"] == f"bytes */{len(body)}"


def test_foreign_session_and_traversal_never_reach_storage(storage, browser, tmp_path):
    png(tmp_path / "session-a" / "pic.png")
    for path in ["/artifacts/session-a/pic.png", "/artifacts/session-b/pic.png",
                 "/artifacts/session-b/../session-a/pic.png",
                 "/artifacts/session-b/%2e%2e/session-a/pic.png",
                 "/artifacts/%2e%2e/private.png"]:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(browser + path, Cookie="bob")
        assert error.value.code == 404
    with pytest.raises(ValueError, match="not owned"):
        artifact_tool_result("/artifacts/session-a/missing.png", "inspect", tmp_path, lambda _: False)
    assert storage.requests == []


def test_failed_put_and_idempotent_retry_keep_bytes_safe(storage, tmp_path):
    path = tmp_path / "session-a" / "pic.png"
    body = png(path)
    storage.fail_put = True
    with pytest.raises(ClientError):
        store.publish_session(tmp_path, "session-a")
    assert path.read_bytes() == body
    storage.fail_put = False
    store.publish_session(tmp_path, "session-a")
    path.write_bytes(body)  # replay after a lost successful PUT response
    store.publish_session(tmp_path, "session-a")
    assert not path.exists()
    path.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="different media"):
        store.publish_session(tmp_path, "session-a")
    assert path.read_bytes() == b"replacement"
    assert list(storage.objects.values()) == [body]


@pytest.mark.parametrize("changed_source", [True, False])
def test_changed_source_and_bad_head_are_not_deleted(storage, tmp_path, monkeypatch, changed_source):
    path = tmp_path / "session-a" / "pic.png"
    png(path)
    original = store.head

    def changed(relative):
        if changed_source:
            path.write_bytes(b"changed while uploading")
            return original(relative)
        return {"ContentLength": 0}

    monkeypatch.setattr(store, "head", changed)
    with pytest.raises(RuntimeError, match="changed during|verification failed"):
        store.publish_file(tmp_path, path)
    assert path.exists()


def test_legacy_files_still_readable_and_remote_errors_retryable(storage, browser, tmp_path):
    path = tmp_path / "session-a" / "old.png"
    body = png(path)
    with get(browser + "/artifacts/session-a/old.png") as response:
        assert response.read() == body
    storage.fail_read = True
    with get(browser + "/artifacts/session-a/old.png") as response:
        assert response.read() == body
    with pytest.raises(urllib.error.HTTPError) as error:
        get(browser + "/artifacts/session-a/remote.png")
    assert error.value.code == 503
    assert "no-store" in error.value.headers["Cache-Control"]


def test_publication_between_storage_lookup_and_local_open(storage, browser, tmp_path, monkeypatch):
    path = tmp_path / "session-a" / "pic.png"
    body = png(path)
    original = App._serve_immutable_artifact

    def publish_during_request(*args, **kwargs):
        store.publish_file(tmp_path, path)
        return original(*args, **kwargs)

    monkeypatch.setattr(App, "_serve_immutable_artifact", staticmethod(publish_during_request))
    with get(browser + "/artifacts/session-a/pic.png") as response:
        assert response.read() == body
    assert not path.exists()


def test_only_completed_media_in_current_session_is_published(storage, tmp_path):
    directory = tmp_path / "session-a"
    png(directory / "complete.png")
    (directory / "video.mp4.part").write_bytes(b"unfinished")
    (directory / "diagram.html").write_text("diagram")
    png(directory / ".hidden.png")
    png(tmp_path / "session-b" / "other.png")
    (directory / "alias.png").symlink_to(tmp_path / "session-b" / "other.png")
    assert store.publish_session(tmp_path, "session-a") == ["/artifacts/session-a/complete.png"]
    assert (directory / "video.mp4.part").exists()
    assert (directory / "diagram.html").exists()
    assert (tmp_path / "session-b" / "other.png").exists()


def test_legacy_files_excluded_but_failed_new_batch_retries(storage, tmp_path):
    old = tmp_path / "session-a" / "old.png"
    png(old)
    baseline = store.existing_media(tmp_path, "session-a")
    new = tmp_path / "session-a" / "new.png"
    later = tmp_path / "session-a" / "later.png"
    png(new)
    png(later)
    storage.fail_put = True
    with pytest.raises(ClientError):
        store.publish_session(tmp_path, "session-a", exclude=baseline)
    storage.fail_put = False
    next_turn_baseline = store.existing_media(tmp_path, "session-a")
    store.publish_session(tmp_path, "session-a", exclude=next_turn_baseline)
    assert old.exists() and not new.exists() and not later.exists()
    assert len(storage.objects) == 2
    assert not list(old.parent.glob("*.s3-pending"))


@pytest.mark.parametrize("fail", [False, True])
def test_turn_publishes_unviewed_media_before_end(storage, tmp_path, monkeypatch, fail):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ha, "build_agent", lambda *a, **kw: SimpleNamespace())
    storage.fail_put = fail
    path = tmp_path / "artifacts" / "session-a" / "pic.png"
    old = path.with_name("legacy.png")
    png(old)

    def run(agent, **kwargs):
        png(path)
        return {}

    manager = TurnManager(ha.AgentPool(), history=lambda _: [], run=run)
    ep = ha.Endpoint(key="test", label="test", model="m", base_url="http://unused")
    stream = manager.start(session_id="session-a", text="draw", endpoint=ep)
    deadline = time.monotonic() + 15
    while stream.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not stream.running
    assert path.exists() == fail
    assert bool(storage.objects) != fail
    assert old.exists()
    assert bool(stream.error) == fail
    assert bool(stream.after(0)[-1].get("error")) == fail


def test_browser_input_uses_unsigned_boto3_even_with_ambient_credentials(storage, tmp_path, monkeypatch):
    from media_input import endpoint, put_object, get_object
    from s3_client import client
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "unused-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unused-test-secret")
    key = "input/webui/session-a/0123456789abcdef0123456789abcdef.png"
    body = png(tmp_path / "input.png")
    put_object(endpoint(), key, body, "image/png")
    assert get_object(endpoint(), key) == (body, "image/png")
    assert client(endpoint()).meta.config.signature_version == UNSIGNED
    assert client(endpoint())._request_signer._credentials is None


def test_standalone_h3_input_uses_boto3(storage, tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "hermes/skills/media/vllm-omni-h3/scripts/h3_client.py"
    spec = importlib.util.spec_from_file_location("h3_client", script)
    h3 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h3)
    path = tmp_path / "input.png"
    body = png(path)
    key, uploaded, mime = h3.persist_input(path, "session-a", store.endpoint())
    assert uploaded == body and mime == "image/png"
    assert h3.load_input(key, store.endpoint()) == (body, mime)
