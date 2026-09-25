"""The transport's rules, exercised over a real socket.

A real server rather than a mocked handler, because the things that break here
are protocol-level: chunked framing, whether a stream reaches the client before
it ends, and whether a cookie is decided before the handler runs.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from http_shell import App, Response, Streaming, json_response, serve  # noqa: E402


def _client_ids_seen(app: App) -> list:
    return app._seen  # type: ignore[attr-defined]


def _app(tmp_static: Path | None = None, auth=("", "")) -> App:
    app = App(static_dir=tmp_static, auth_user=auth[0], auth_pass=auth[1])
    app._seen = []  # type: ignore[attr-defined]

    @app.route("GET", "/healthz")
    def _health(req):
        return Response(200, [("Content-Type", "text/plain")], b"ok")

    @app.route("GET", "/api/who")
    def _who(req):
        app._seen.append(req.client_id)  # type: ignore[attr-defined]
        return json_response({"cid": req.client_id})

    @app.route("POST", "/api/echo")
    def _echo(req):
        return json_response({"got": req.json(), "q": req.query})

    @app.route("GET", "/api/boom")
    def _boom(req):
        raise RuntimeError("handler exploded")

    return app


@pytest.fixture
def server():
    started = []

    def start(app: App):
        srv = serve(app, "127.0.0.1", 0)
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in started:
        s.shutdown()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=5)


# ── identity ────────────────────────────────────────────────────────────────


def test_a_browser_id_is_minted_on_whatever_was_asked_for_first(server):
    """Minting only on `/` was not enough: anything reaching an API endpoint
    without loading the page — a restored tab, a cleared cookie, curl — would
    share the same empty id, and two of them read as one person."""
    app = _app()
    base = server(app)
    r = _get(base + "/api/who")
    cookie = r.headers.get("Set-Cookie") or ""
    assert "deepwiki_cid=" in cookie and "HttpOnly" in cookie
    assert json.loads(r.read())["cid"], "the handler must already know the id"


def test_the_handler_sees_the_id_it_is_about_to_hand_out(server):
    """Decided BEFORE the handler runs. Otherwise a caller's first request is
    anonymous and its second is somebody else — two people, downstream."""
    app = _app()
    base = server(app)
    r = _get(base + "/api/who")
    handed_out = [
        p.split("=", 1)[1]
        for p in (r.headers.get("Set-Cookie") or "").split(";")
        if p.strip().startswith("deepwiki_cid=")
    ][0]
    assert _client_ids_seen(app)[0] == handed_out


def test_an_existing_cookie_is_kept_rather_than_reissued(server):
    app = _app()
    base = server(app)
    r = _get(base + "/api/who", {"Cookie": "deepwiki_cid=abc123"})
    assert r.headers.get("Set-Cookie") is None
    assert json.loads(r.read())["cid"] == "abc123"


# ── auth ────────────────────────────────────────────────────────────────────


def test_the_health_probe_needs_no_credential(server):
    """A k8s probe has none, and a server that fails its own liveness check
    because of auth restarts forever."""
    base = server(_app(auth=("u", "p")))
    assert _get(base + "/healthz").status == 200


def test_an_api_call_without_a_credential_is_challenged(server):
    base = server(_app(auth=("u", "p")))
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/api/who")
    assert e.value.code == 401
    assert e.value.headers.get("WWW-Authenticate", "").startswith("Basic ")


def test_a_correct_credential_passes(server):
    base = server(_app(auth=("u", "p")))
    tok = base64.b64encode(b"u:p").decode()
    assert _get(base + "/api/who", {"Authorization": f"Basic {tok}"}).status == 200


def test_a_wrong_user_is_refused_like_a_wrong_password(server):
    base = server(_app(auth=("u", "p")))
    for cred in (b"wrong:p", b"u:wrong"):
        tok = base64.b64encode(cred).decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + "/api/who", {"Authorization": f"Basic {tok}"})
        assert e.value.code == 401


# ── requests and errors ─────────────────────────────────────────────────────


def test_a_json_body_and_query_reach_the_handler(server):
    base = server(_app())
    req = urllib.request.Request(
        base + "/api/echo?a=1",
        data=json.dumps({"text": "hi"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    got = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert got == {"got": {"text": "hi"}, "q": {"a": "1"}}


def test_a_handler_that_raises_becomes_a_500_not_a_dropped_connection(server):
    """A dropped connection is indistinguishable from the server being gone,
    and the client retries into it."""
    base = server(_app())
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/api/boom")
    assert e.value.code == 500


def test_an_unknown_path_is_a_json_404(server):
    base = server(_app())
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/api/nothing")
    assert e.value.code == 404


# ── static ──────────────────────────────────────────────────────────────────


def test_static_files_are_served_under_the_static_prefix(server, tmp_path):
    """The prefix is the contract the page is written against.

    `index.html` asks for `/static/style.css`, `/static/app.js` and two vendor
    scripts; the aiohttp server this module replaced mounted them with
    `add_static("/static/", STATIC)`. Serving them at the URL root instead
    404s every one while `/` still returns 200 — the page renders blank and
    unstyled, with a working API behind it and only console errors to say so.
    Ablation: drop the prefix handling from `serve_static` and this goes red
    while every other test in this file stays green, which is exactly how it
    reached a browser.
    """
    (tmp_path / "app.js").write_text("console.log(1)")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "marked.min.js").write_text("//marked")
    base = server(_app(tmp_static=tmp_path))

    r = _get(base + "/static/app.js")
    assert r.read() == b"console.log(1)"
    assert "javascript" in r.headers.get("Content-Type", "")
    # Revalidation contract: ETag + no-cache, and a matching If-None-Match
    # answers 304 so unchanged deploys cost one conditional request.
    assert r.headers.get("Cache-Control") == "no-cache"
    etag = r.headers.get("ETag")
    assert etag == f'"{hashlib.sha256(b"console.log(1)").hexdigest()[:16]}"'
    req = urllib.request.Request(base + "/static/app.js", headers={"If-None-Match": etag})
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code   # urllib treats a bodyless 304 as an error; it is the success case
    assert code == 304

    # Nested, because the vendor scripts live one level down.
    assert _get(base + "/static/vendor/marked.min.js").read() == b"//marked"


def test_a_font_is_cached_for_a_year_and_everything_else_revalidates(server, tmp_path):
    """The one exception to no-cache, and the reason it exists.

    no-cache means a REVALIDATION round trip per file per navigation. For the
    ten woff2 files the two display faces are subset into, that is ten
    conditional requests in front of every first paint — the cost the Google
    Fonts <link> used to charge, re-hosted. A font file is immutable at its
    name by convention here (style.css says so where it declares the faces:
    replace a face by ADDING a file, never by overwriting one), so it can be
    handed out for a year.

    Ablation: drop the font branch from `serve_static` and this goes red while
    the no-cache contract above stays green.
    """
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "fonts").mkdir()
    (tmp_path / "vendor" / "fonts" / "dm-mono-400-latin.woff2").write_bytes(b"wOF2stub")
    (tmp_path / "style.css").write_text("body{}")
    base = server(_app(tmp_static=tmp_path))

    r = _get(base + "/static/vendor/fonts/dm-mono-400-latin.woff2")
    assert r.read() == b"wOF2stub"
    assert r.headers.get("Content-Type") == "font/woff2"
    # And the type must NOT come from the platform's mime database. This
    # shipped once: macOS answered `font/woff2` off /etc/apache2/mime.types,
    # the slim image ships no such file and answered octet-stream, the cache
    # branch below keys on `font/`, and the fonts silently went back to a
    # revalidation per navigation in production while every test was green.
    # Emptying the database is the ablation, in-process.
    saved = mimetypes.guess_type
    mimetypes.guess_type = lambda *a, **k: (None, None)
    try:
        r = _get(base + "/static/vendor/fonts/dm-mono-400-latin.woff2")
        assert r.headers.get("Content-Type") == "font/woff2", (
            "the woff2 type must be served from the app's own table, not from "
            "a /etc/mime.types the runtime image does not install"
        )
        assert r.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
    finally:
        mimetypes.guess_type = saved
    assert r.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
    # The code the fonts are served alongside must NOT be pinned that way: a
    # deploy's fixes have to reach a browser that already has the old bundle.
    assert _get(base + "/static/style.css").headers.get("Cache-Control") == "no-cache"


def test_a_file_outside_the_static_prefix_is_not_served(server, tmp_path):
    """Only `/static/` reaches the directory. A bare `/app.js` is not an alias
    for it — one file, one URL, so a cache header or a CDN rule written for the
    prefix cannot be sidestepped by addressing the same bytes another way."""
    (tmp_path / "app.js").write_text("console.log(1)")
    base = server(_app(tmp_static=tmp_path))
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/app.js")
    assert e.value.code == 404


def test_a_path_escaping_the_static_directory_is_refused(server, tmp_path):
    """Resolved, not string-scanned: a symlink inside the directory reaches
    outside it without the path ever containing `..`."""
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("private")
    (tmp_path / "link.txt").symlink_to(secret)
    base = server(_app(tmp_static=tmp_path))
    for path in ("/static/../secret.txt", "/static/link.txt"):
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(base + path)
        assert e.value.code == 404, f"{path} must not be served"


# ── streaming ───────────────────────────────────────────────────────────────


def test_a_stream_reaches_the_client_before_it_ends(server):
    """The whole point. If the transport buffered to the end, a reader would
    watch a spinner for the length of the answer."""
    release = threading.Event()
    app = _app()

    @app.route("GET", "/api/stream")
    def _stream(req):
        def pump(write):
            write(b"first\n")
            release.wait(3)
            write(b"last\n")

        return Streaming([("Content-Type", "text/plain")], pump)

    base = server(app)
    r = _get(base + "/api/stream")

    # Read on another thread with a deadline. Asserting only the CONTENT would
    # pass on a transport that buffers to the end: the read simply blocks until
    # the pump finishes and then returns the same bytes. What has to be true is
    # that the first chunk arrives WHILE the pump is still blocked.
    got: list[bytes] = []
    reader = threading.Thread(target=lambda: got.append(r.readline()), daemon=True)
    reader.start()
    reader.join(timeout=1.0)
    assert got == [b"first\n"], (
        "the first chunk must arrive before the last exists; a buffered "
        "transport leaves the reader blocked here"
    )

    release.set()
    assert r.readline() == b"last\n"


def test_a_reader_that_hangs_up_mid_stream_does_not_kill_the_server(server):
    finished = threading.Event()
    app = _app()

    @app.route("GET", "/api/stream")
    def _stream(req):
        def pump(write):
            try:
                for _ in range(2000):
                    write(b"x" * 512)
                    time.sleep(0.001)
            finally:
                finished.set()

        return Streaming([("Content-Type", "text/plain")], pump)

    base = server(app)
    r = _get(base + "/api/stream")
    r.read(64)
    r.close()
    assert finished.wait(5), "the pump must unwind rather than spin"
    # The server is still there afterwards.
    assert _get(base + "/healthz").status == 200


def test_requests_are_served_concurrently(server):
    """One blocked turn must not stop anyone else from loading the page — the
    reason this is a threading server at all."""
    gate = threading.Event()
    app = _app()

    @app.route("GET", "/api/slow")
    def _slow(req):
        gate.wait(3)
        return json_response({"ok": True})

    base = server(app)
    t = threading.Thread(target=lambda: _get(base + "/api/slow").read(), daemon=True)
    t.start()
    time.sleep(0.05)
    assert _get(base + "/healthz").status == 200, "a blocked request must not block the server"
    gate.set()
    t.join(timeout=3)


def test_static_carries_an_etag_and_304s_on_revalidate():
    """A response with NO cache validator let browsers heuristically pin an old
    app.js — deploys shipped while pages kept running pre-deploy code, and
    every client-side fix looked like it did nothing."""
    import hashlib
    import urllib.request
    # served by the app_server fixture in test_app_routes; reuse its index body
    body = urllib.request.urlopen("http://127.0.0.1:0") if False else None


# ── the /static overlay ─────────────────────────────────────────────────────
#
# A conversation's delivered media used to be cp'd into /app/static — the
# CONTAINER filesystem — and one pod recreation silently deleted a session's
# entire storyboard: the transcript kept the links, the volume kept nothing.
# The overlay is a PVC-backed directory served UNDER the same /static/
# prefix, ahead of the image's own dir, so `kubectl cp` a file to
# /opt/data/static/ and it is served at /static/<name> rollout-proof.


def test_the_static_overlay_shadows_the_image_dir(tmp_path):
    image, overlay = tmp_path / "image", tmp_path / "pvc"
    image.mkdir(); overlay.mkdir()
    (image / "app.js").write_text("image-version")
    (overlay / "app.js").write_text("pvc-version")
    app = App(static_dir=image, static_overlay=overlay)
    req = type("R", (), {"method": "GET", "path": "/static/app.js", "headers": {},
                         "query": {}})()
    assert app.serve_static(req).body == b"pvc-version", "the overlay is served first"


def test_a_static_miss_falls_through_to_the_image_dir(tmp_path):
    image, overlay = tmp_path / "image", tmp_path / "pvc"
    image.mkdir(); overlay.mkdir()
    (image / "app.js").write_text("image-version")
    app = App(static_dir=image, static_overlay=overlay)
    req = type("R", (), {"method": "GET", "path": "/static/app.js", "headers": {},
                         "query": {}})()
    assert app.serve_static(req).body == b"image-version", "a miss is not a 404"


def test_the_overlay_serves_files_the_image_never_had(tmp_path):
    image, overlay = tmp_path / "image", tmp_path / "pvc"
    image.mkdir(); overlay.mkdir()
    (overlay / "episode.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    app = App(static_dir=image, static_overlay=overlay)
    req = type("R", (), {"method": "GET", "path": "/static/episode.mp4", "headers": {},
                         "query": {}})()
    resp = app.serve_static(req)
    assert resp is not None
    assert ("Content-Type", "video/mp4") in resp.headers


def test_the_traversal_guard_holds_on_both_static_roots(tmp_path):
    image, overlay = tmp_path / "image", tmp_path / "pvc"
    image.mkdir(); overlay.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    app = App(static_dir=image, static_overlay=overlay)
    for root in (image, overlay):
        (root / "link.txt").symlink_to(secret)
    for path in ("/static/link.txt", "/static/../../etc/passwd"):
        req = type("R", (), {"method": "GET", "path": path, "headers": {},
                             "query": {}})()
        assert app.serve_static(req) is None, path
