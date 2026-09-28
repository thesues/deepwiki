"""Two authenticated users must not access each other's resources."""
import json
import sqlite3
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_auth import authority, cookie
from auth import JWTAuthenticator
from app_routes import build_app
from http_shell import serve
from turns import TurnManager, Refused
from turn_stream import TurnStream
from session_profiles import SessionProfiles
import hermes_agent as ha
import hermes_session_api as hs


@pytest.fixture
def tenants(authority, tmp_path, monkeypatch):
    key, jwks_url, _ = authority
    class Sessions:
        def owner(self, sid):
            return {"s-a": "u_a", "s-b": "u_b"}.get(sid, "")
        def list_sessions(self, **kw):
            # Even an overbroad store response is filtered by the route layer.
            return [{"id": s, "title": s, "messageCount": 1} for s in ("s-a", "s-b")]
        def history(self, sid, **kw):
            return [{"kind": "history_user", "text": sid}]
        def resolve_tip(self, sid):
            return sid
    manager = TurnManager(ha.AgentPool(), history=lambda sid: [], run=lambda *a, **kw: {})
    for sid, owner in (("s-a", "u_a"), ("s-b", "u_b")):
        stream = TurnStream("stream-" + sid, sid, user_id=owner)
        stream.approval_key = sid
        manager._live[sid] = stream
        manager._streams[stream.stream_id] = stream
        directory = tmp_path / "artifacts" / sid
        directory.mkdir(parents=True)
        (directory / "file.txt").write_text(owner)
    (tmp_path / "artifacts" / "s-a" / "link.txt").symlink_to(tmp_path / "artifacts" / "s-b" / "file.txt")
    (tmp_path / "index.html").write_text("hello")
    (tmp_path / "home.html").write_text("hello")
    app = build_app(manager=manager, endpoints=[ha.Endpoint("x", "X", "m", "http://model", max_concurrent=8)],
                    static_dir=tmp_path, index_html=tmp_path / "index.html", artifacts_dir=tmp_path / "artifacts",
                    sessions=Sessions(), session_profiles=SessionProfiles(tmp_path / "profiles.json"),
                    authenticator=JWTAuthenticator(jwks_url, audience="deepwiki"))
    server = serve(app, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_port}"
    def call(path, user="u_a", body=None, headers=None):
        hdr = {"Cookie": cookie(key, sub=user), **(headers or {})}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, headers=hdr)
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, response.read(), response.headers
    yield call, manager
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("path,body", [
    ("/api/session/history?id=s-b", None),
    ("/api/session/delete", {"sessionId": "s-b"}),
    ("/api/chat/start", {"sessionId": "s-b", "text": "hello"}),
    ("/api/chat/stream?stream_id=stream-s-b", None),
    ("/api/session/stream?session_id=s-b", None),
    ("/api/chat/cancel", {"streamId": "stream-s-b"}),
    ("/api/approval/pending?session=s-b", None),
    ("/api/approval/answer", {"id": "s-b", "optionId": "once"}),
    ("/artifacts/s-b/file.txt", None),
    ("/artifacts/s-a/link.txt", None),
])
def test_cross_user_access_is_denied(tenants, path, body):
    call, _ = tenants
    status, _, _ = call(path, body=body)
    assert status == 404


def test_lists_and_history_only_show_current_user(tenants):
    call, _ = tenants
    scopes = {}
    for user, sid in (("u_a", "s-a"), ("u_b", "s-b")):
        status, data, _ = call("/api/sessions", user)
        assert status == 200
        result = json.loads(data)
        assert [s["id"] for s in result["sessions"]] == [sid]
        assert set(result["streaming"]) == {sid}
        assert len(result["identityScope"]) == 64
        scopes[user] = result["identityScope"]
        status, data, _ = call("/api/status", user)
        assert [s["session"] for s in json.loads(data)["turns"]] == [sid]
        status, data, _ = call("/api/session/history?id=" + sid, user)
        assert status == 200 and sid.encode() in data
    assert scopes["u_a"] != scopes["u_b"]
    status, data, headers = call("/artifacts/s-a/file.txt")
    assert status == 200 and data == b"u_a"
    assert headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert headers["Vary"] == "Cookie"


def test_authenticated_artifact_cache_keeps_ownership_and_api_is_uncached(tenants):
    call, _ = tenants
    status, _, headers = call("/artifacts/s-a/file.txt", headers={"Range": "bytes=0-1"})
    assert status == 206
    assert headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert headers["Vary"] == "Cookie"
    status, _, headers = call("/artifacts/s-a/file.txt", headers={"If-None-Match": headers["ETag"]})
    assert status == 304
    assert headers["Cache-Control"] == "private, max-age=31536000, immutable"
    assert headers["Vary"] == "Cookie"
    status, _, headers = call("/artifacts/s-b/file.txt")
    assert status == 404
    assert headers["Cache-Control"] == "private, no-store"
    status, _, headers = call("/api/sessions")
    assert status == 200
    assert headers["Cache-Control"] == "private, no-store"


def test_cross_user_stream_status_is_indistinguishable_from_unknown(tenants):
    call, _ = tenants
    status, data, _ = call("/api/chat/status?stream_id=stream-s-b", user="u_a")
    assert status == 200
    assert json.loads(data) == {"known": False}


def test_other_users_images_and_forged_markers_denied(tenants):
    call, _ = tenants
    key = "input/webui/u_b/draft/" + "a" * 32 + ".png"
    status, _, _ = call("/api/media/input?objectKey=" + key)
    assert status == 400
    status, _, _ = call("/api/chat/start", body={"text": f"[输入图片 object_key: {key}]"})
    assert status == 404


def test_origin_checked_and_user_admission_atomic(tenants):
    call, manager = tenants
    status, _, _ = call("/api/chat/cancel", body={"streamId": "stream-s-a"}, headers={"Origin": "https://foreign.test"})
    assert status == 403
    with pytest.raises(Refused) as refused:
        manager.start(session_id="s-a", text="x", endpoint=ha.Endpoint("y","Y","m","http://x",max_concurrent=8), user_id="u_a")
    assert refused.value.reason == "taken"
    stream = manager.stream("stream-s-a")
    manager._rotated(stream, "s-a", "s-child")
    assert manager.owner_of("s-a") == manager.owner_of("s-child") == "u_a"
    assert manager.owner_of_approval_key("s-a") == "u_a"


def test_hermes_lazy_and_compression_writes_receive_user_id():
    class DB:
        def create_session(self, **kw):
            self.last = kw
    raw = DB()
    wrapper = ha.UserSessionDB(raw, "u_a")
    wrapper.create_session(session_id="root", user_id=None)
    assert raw.last["user_id"] == "u_a"
    wrapper.create_session(session_id="child", parent_session_id="root")
    assert raw.last["user_id"] == "u_a"
    assert raw.last["parent_session_id"] == "root"


def test_mixed_lineage_is_not_owned():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, user_id TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [("root",None,"u_a"),("child","root","u_a"),("bad","child","u_b")])
    db = types.SimpleNamespace(_conn=conn)
    assert hs._owner_from_db(db, "child") == "u_a"
    assert hs._owner_from_db(db, "bad") == ""
    conn.close()


def test_authenticated_list_filters_roots_before_tip_projection(monkeypatch):
    """A compression tip from another identity must contribute no metadata."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, user_id TEXT)")
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [
        ("root-a", None, "u_a"), ("tip-a", "root-a", "u_a"),
        ("root-mixed", None, "u_a"), ("tip-mixed", "root-mixed", "u_b"),
        ("root-b", None, "u_b"),
    ])
    seen = []

    class DB:
        _conn = conn
        def list_sessions_rich(self, **kw):
            seen.append(kw)
            assert kw["project_compression_tips"] is False
            return [
                {"id": "root-a", "user_id": "u_a", "end_reason": "compression",
                 "message_count": 1, "title": "a-root", "preview": "a-root", "started_at": 1},
                {"id": "root-mixed", "user_id": "u_a", "end_reason": "compression",
                 "message_count": 1, "title": "must-not-leak", "preview": "must-not-leak", "started_at": 2},
                {"id": "root-b", "user_id": "u_b", "end_reason": None,
                 "message_count": 2, "title": "b", "preview": "b", "started_at": 3},
            ]
        def get_compression_tip(self, sid):
            return {"root-a": "tip-a", "root-mixed": "tip-mixed"}.get(sid, sid)
        def _get_session_rich_row(self, sid):
            return {
                "tip-a": {"id": "tip-a", "message_count": 3, "title": "a-tip",
                          "preview": "a-tip", "last_active": 11, "end_reason": None},
                "tip-mixed": {"id": "tip-mixed", "message_count": 4,
                              "title": "other-user-secret", "preview": "other-user-secret",
                              "last_active": 12, "end_reason": None},
            }.get(sid)

    db = DB()
    monkeypatch.setattr(hs, "_db", lambda: db)
    assert [r["id"] for r in hs.list_sessions(100, False, "u_a")] == ["tip-a"]
    assert [r["id"] for r in hs.list_sessions(100, False, "u_b")] == ["root-b"]
    assert all("other-user-secret" not in json.dumps(rows) for rows in (
        hs.list_sessions(100, False, "u_a"), hs.list_sessions(100, False, "u_b")
    ))
    assert seen
    conn.close()
