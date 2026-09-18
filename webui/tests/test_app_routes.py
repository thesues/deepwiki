"""The API's contracts, over a real socket, with a fake agent behind them."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import hermes_agent as ha  # noqa: E402
import profiles as pr
import session_profiles as sp  # noqa: E402
from app_routes import build_app  # noqa: E402
from http_shell import serve  # noqa: E402
from turns import TurnManager  # noqa: E402


class FakeAgent:
    def __init__(self):
        self.stream_delta_callback = None
        self.reasoning_callback = None
        self.tool_progress_callback = None
        self.step_callback = None
        self.thinking_callback = None

    def interrupt(self, message=None):
        pass


class FakeSessions:
    def __init__(self):
        self.rows = [{"id": "s-old", "title": "昨天的问题", "messageCount": 4}]
        self.moved = []

    def list_sessions(self, limit, include_empty):
        return [dict(r) for r in self.rows]

    def history(self, sid, limit):
        self.moved.append(sid)
        return [{"kind": "history_user", "text": "before"}]


@pytest.fixture
def app_server(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "build_agent", lambda session_id, ep, profile=None: FakeAgent())
    (tmp_path / "index.html").write_text(
        '<html><link rel="stylesheet" href="/static/style.css">'
        '<script src="/static/app.js"></script></html>'
    )
    (tmp_path / "home.html").write_text(
        '<html><link rel="stylesheet" href="/static/style.css">'
        '<script src="/static/home.js"></script></html>'
    )
    (tmp_path / "home.js").write_text("console.log(1)")
    (tmp_path / "app.js").write_text("console.log(1)")   # the versioned URL must serve
    gate = threading.Event()
    gate.set()
    state = {"gate": gate, "sessions": FakeSessions()}

    def run(agent, **kw):
        state["gate"].wait(3)
        agent.stream_delta_callback("hello")
        return {}

    mgr = TurnManager(ha.AgentPool(), history=lambda sid: [], run=run)
    eps = [
        ha.Endpoint("dsv4", "DSV4", "m1", "http://a/v1", max_concurrent=1),
        ha.Endpoint("vision", "Vision", "m2", "http://b/v1", max_concurrent=1),
    ]
    pfs = pr.build_profiles([
        {"key": "buda", "label": "佛典检索"},
        {"key": "video", "label": "解说视频", "endpoints": ["vision"]},
    ])
    app = build_app(
        manager=mgr,
        endpoints=eps,
        profiles=pfs,
        static_dir=tmp_path,
        index_html=tmp_path / "index.html",
        sessions=state["sessions"],
        mcp={"name": "memory", "url": "http://mcp"},
        # Pinned into tmp_path: the default lands in HERMES_HOME, so without
        # this the suite writes its pins into the developer's real ~/.hermes.
        session_profiles=sp.SessionProfiles(tmp_path / "session_profiles.json"),
    )
    srv = serve(app, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, mgr, state
    srv.shutdown()


def _post(base, path, obj, cookie=""):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})},
    )
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(base, path, cookie=""):
    req = urllib.request.Request(base + path, headers={"Cookie": cookie} if cookie else {})
    r = urllib.request.urlopen(req, timeout=5)
    return json.loads(r.read())


def _drain(stream, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end and stream.running:
        time.sleep(0.005)


def _wait_idle(mgr, timeout=3.0):
    """Let every running turn finish. The manager refuses a second turn in a
    conversation that is still replying, so a test that sends twice to the
    same session has to wait between them."""
    for sid in list(mgr.running().values()):
        st = mgr.stream(sid)
        if st is not None:
            _drain(st, timeout)


# ── sending ─────────────────────────────────────────────────────────────────


def test_a_prompt_starts_a_turn_and_names_its_stream(app_server):
    base, mgr, _ = app_server
    code, body = _post(base, "/api/chat/start", {"text": "hi"})
    assert code == 200 and body["streamId"] and body["sessionId"]
    assert body["endpoint"] == "dsv4", "no endpoint named means the default one"
    _drain(mgr.stream(body["streamId"]))


def test_an_empty_prompt_is_refused_before_anything_starts(app_server):
    base, mgr, _ = app_server
    code, _ = _post(base, "/api/chat/start", {"text": "   "})
    assert code == 400 and mgr.running() == {}


def test_the_endpoint_named_by_the_client_is_the_one_used(app_server):
    """The picker is the whole multi-endpoint feature; if this is ignored the UI
    shows one model's name and another model answers."""
    base, mgr, _ = app_server
    code, body = _post(base, "/api/chat/start", {"text": "hi", "endpoint": "vision"})
    assert code == 200 and body["endpoint"] == "vision"
    _drain(mgr.stream(body["streamId"]))


def test_an_unknown_endpoint_falls_back_rather_than_failing(app_server):
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {"text": "hi", "endpoint": "nope"})
    assert body["endpoint"] == "dsv4"
    _drain(mgr.stream(body["streamId"]))


# ── profiles ─────────────────────────────────────────────────────────────────


def test_status_advertises_the_project_cards(app_server):
    """The homepage renders FROM this list. A deploy that declares none gets
    the built-in default — one card, no behaviour change."""
    base, _, _ = app_server
    j = _get(base, "/api/status")
    assert [p["key"] for p in j["profiles"]] == ["buda", "video"]
    assert j["defaultProfile"] == "buda"


def test_the_profile_named_by_the_client_is_the_one_echoed(app_server):
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {"text": "hi", "profile": "video"})
    assert body["profile"] == "video"
    _drain(mgr.stream(body["streamId"]))


def test_an_unknown_profile_falls_back_to_the_default(app_server):
    """Same stale-key rule as endpoints: a profile renamed in config while an
    old tab still sends the old key must land somewhere real."""
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {"text": "hi", "profile": "gone"})
    assert body["profile"] == "buda"   # the first entry is the default
    _drain(mgr.stream(body["streamId"]))


def test_a_pinned_profile_redirects_a_foreign_endpoint(app_server):
    """The video profile may only use the multimodal model. The redirect is
    echoed, so the client's picker follows what will actually answer."""
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {
        "text": "hi", "profile": "video", "endpoint": "dsv4",
    })
    assert body["endpoint"] == "vision"
    _drain(mgr.stream(body["streamId"]))


# ── the two refusals are different ──────────────────────────────────────────


def test_a_busy_conversation_is_409_taken_and_a_full_endpoint_is_429_busy(app_server):
    base, mgr, state = app_server
    state["gate"] = threading.Event()  # hold the turn open

    _, first = _post(base, "/api/chat/start", {"text": "one"})
    sid = first["sessionId"]

    code, body = _post(base, "/api/chat/start", {"text": "two", "sessionId": sid})
    assert code == 409 and body.get("taken") is True, "the same conversation"
    assert body["streamId"] == first["streamId"]

    code, body = _post(base, "/api/chat/start", {"text": "three"})
    assert code == 429 and body.get("busy") is True, "a different one, same full endpoint"
    assert body["maxConcurrent"] == 1

    # The other endpoint has its own budget.
    code, _ = _post(base, "/api/chat/start", {"text": "four", "endpoint": "vision"})
    assert code == 200

    state["gate"].set()
    for s in list(mgr.running().values()):
        _drain(mgr.stream(s))


# ── streaming ───────────────────────────────────────────────────────────────


def test_the_stream_carries_the_turn_and_ends(app_server):
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {"text": "hi"})
    r = urllib.request.urlopen(base + f"/api/chat/stream?stream_id={body['streamId']}", timeout=5)
    kinds = []
    for raw in r:
        line = raw.decode().strip()
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            kinds.append(ev["kind"])
            if ev["kind"] == "end":
                break
    assert kinds[0] == "user" and "delta" in kinds and kinds[-1] == "end"


def test_an_unknown_stream_is_404_not_an_empty_stream(app_server):
    """The client has to tell "that turn is gone" from "it has said nothing"."""
    base, _, _ = app_server
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/chat/stream?stream_id=nope", timeout=5)
    assert e.value.code == 404


def test_chat_status_reports_a_finished_turn(app_server):
    base, mgr, _ = app_server
    _, body = _post(base, "/api/chat/start", {"text": "hi"})
    _drain(mgr.stream(body["streamId"]))
    st = _get(base, f"/api/chat/status?stream_id={body['streamId']}")
    assert st["known"] is True and st["running"] is False and st["error"] is None
    assert _get(base, "/api/chat/status?stream_id=nope")["known"] is False


# ── reading is free ─────────────────────────────────────────────────────────


def test_reading_a_transcript_does_not_disturb_a_running_turn(app_server):
    """Under ACP the only way to read one was `session/load`, which MOVED the
    single agent process — so looking at another conversation while one streamed
    yanked the agent out from under the turn in flight."""
    base, mgr, state = app_server
    state["gate"] = threading.Event()
    _, live = _post(base, "/api/chat/start", {"text": "one"})

    got = _get(base, "/api/session/history?id=s-old")
    assert got["events"][0]["text"] == "before"
    assert mgr.stream(live["streamId"]).running is True, "the live turn is untouched"

    state["gate"].set()
    _drain(mgr.stream(live["streamId"]))


def test_the_sidebar_marks_which_conversations_are_replying(app_server):
    base, mgr, state = app_server
    state["gate"] = threading.Event()
    _, live = _post(base, "/api/chat/start", {"text": "one"})

    body = _get(base, "/api/sessions")
    assert live["sessionId"] in body["streaming"]
    assert [e["key"] for e in body["endpoints"]] == ["dsv4", "vision"]

    state["gate"].set()
    _drain(mgr.stream(live["streamId"]))


def test_each_endpoint_reports_its_own_running_count(app_server):
    """The client's composer gates on the picker's CHOICE, so the counts must
    be per endpoint: one full endpoint greying out a send aimed at another is
    the single-endpoint behaviour wearing a multi-endpoint hat."""
    base, mgr, state = app_server
    state["gate"] = threading.Event()
    _, live = _post(base, "/api/chat/start", {"text": "one"})

    body = _get(base, "/api/sessions")
    counts = {e["key"]: e["running"] for e in body["endpoints"]}
    assert counts == {"dsv4": 1, "vision": 0}, counts

    state["gate"].set()
    _drain(mgr.stream(live["streamId"]))
    body = _get(base, "/api/sessions")
    assert {e["key"]: e["running"] for e in body["endpoints"]} == {"dsv4": 0, "vision": 0}, (
        "a finished turn must not keep the endpoint marked busy")


def test_a_sessions_read_that_fails_does_not_take_the_app_down(app_server, monkeypatch):
    base, _, state = app_server
    monkeypatch.setattr(
        state["sessions"], "list_sessions",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    body = _get(base, "/api/sessions")
    assert body["sessions"] == [], "an unreadable sidebar must not 500 the page"


# ── per-browser position ────────────────────────────────────────────────────


def test_each_browser_keeps_its_own_position(app_server):
    """Shared, this leaked one person's position into another's page."""
    base, _, _ = app_server
    _post(base, "/api/session/open", {"sessionId": "s-a"}, cookie="deepwiki_cid=alice")
    _post(base, "/api/session/open", {"sessionId": "s-b"}, cookie="deepwiki_cid=bob")
    assert _get(base, "/api/status", cookie="deepwiki_cid=alice")["session"] == "s-a"
    assert _get(base, "/api/status", cookie="deepwiki_cid=bob")["session"] == "s-b"


def test_status_advertises_the_endpoints_the_client_can_pick(app_server):
    base, _, _ = app_server
    body = _get(base, "/api/status")
    assert body["defaultEndpoint"] == "dsv4"
    assert {e["key"]: e["maxConcurrent"] for e in body["endpoints"]} == {"dsv4": 1, "vision": 1}
    assert body["mcp"]["name"] == "memory"


# ── deleting a session ──────────────────────────────────────────────────────


class FakeDb:
    """The slice of SessionDB the delete route touches."""

    def __init__(self, deleted=True, error=None):
        self.deleted = deleted
        self.error = error
        self.calls = []

    def delete_session(self, sid, sessions_dir=None):
        self.calls.append((sid, sessions_dir))
        if self.error:
            raise self.error
        return self.deleted


def test_delete_runs_in_process_against_session_db(app_server, monkeypatch):
    """The delete must reach `SessionDB.delete_session` — the same method the
    CLI subprocess ran — with the sessions dir, and report ok."""
    base, _, state = app_server
    fake = FakeDb(deleted=True)
    monkeypatch.setattr(ha, "_Db", type("_Db", (), {"get": staticmethod(lambda: fake)}))
    code, body = _post(base, "/api/session/delete", {"sessionId": "s-old"})
    assert (code, body["ok"]) == (200, True)
    assert fake.calls and fake.calls[0][0] == "s-old"
    assert fake.calls[0][1] is not None   # transcripts dir passed through


def test_delete_is_idempotent_when_the_row_is_already_gone(app_server, monkeypatch):
    """An already-deleted id must read as success — a second tab racing the
    first's delete is the normal case, and the old CLI exited 0 on it."""
    base, _, state = app_server
    monkeypatch.setattr(ha, "_Db", type("_Db", (), {"get": staticmethod(lambda: FakeDb(deleted=False))}))
    code, body = _post(base, "/api/session/delete", {"sessionId": "s-old"})
    assert (code, body["ok"], body["found"]) == (200, True, False)


def test_delete_refuses_a_session_that_is_mid_reply(app_server, monkeypatch):
    """A turn writing into a deleted row is corruption the reader cannot see."""
    base, mgr, state = app_server
    monkeypatch.setattr(type(mgr), "running", lambda self: {"s-old": "stream-x"})
    monkeypatch.setattr(ha, "_Db", type("_Db", (), {"get": staticmethod(lambda: FakeDb())}))
    code, body = _post(base, "/api/session/delete", {"sessionId": "s-old"})
    assert code == 409
    assert "先停止再删除" in body["error"]


def test_a_live_turns_session_shows_in_the_sidebar_before_the_store_has_it(app_server):
    """hermes persists a session row only when the turn ENDS, so a first turn
    ran its whole life invisible to /api/sessions — "new sessions appear late,
    sometimes never". The route must synthesize the row from the live stream."""
    base, mgr, state = app_server
    state["gate"].clear()   # keep the turn running
    try:
        code, body = _post(base, "/api/chat/start",
                           {"text": "什么是缘起", "sessionId": "s-live", "endpoint": "dsv4"})
        assert code == 200, body
        rows = _get(base, "/api/sessions")["sessions"]
        row = next((r for r in rows if r["id"] == "s-live"), None)
        assert row is not None, "a running turn must be visible in the sidebar"
        assert row["is_streaming"] is True
        assert "什么是缘起" in row["title"]
    finally:
        state["gate"].set()


def test_the_real_row_shadows_the_synthesized_one(app_server, monkeypatch):
    """Once the store has the row (turn ended), the merge must not duplicate it."""
    base, mgr, state = app_server
    fake = FakeSessions()
    fake.rows.append({"id": "s-old", "title": "昨天的问题", "messageCount": 4})
    monkeypatch.setattr(mgr, "running", lambda: {})   # nothing live
    rows = _get(base, "/api/sessions")["sessions"]
    assert sum(1 for r in rows if r["id"] == "s-old") == 1


def test_index_versioned_the_static_urls(app_server):
    """A heuristically-cached, validator-less app.js cannot be revalidated —
    the no-cache fix itself never reached that browser. A URL that changes
    with every build is the only bust that works by construction."""
    base, _, _ = app_server
    # The home page is served at / (deepwiki: cards first); the chat page at
    # /<profile>/. Both are versioned the same way.
    home = urllib.request.urlopen(base + "/").read().decode()
    assert "home.js?v=" in home and "style.css?v=" in home, "statics must be versioned"
    assert "@@BUILD@@" not in home, "the build marker must be injected"
    chat = urllib.request.urlopen(base + "/buda/").read().decode()
    assert "app.js?v=" in chat and "style.css?v=" in chat, "statics must be versioned"
    # and the versioned URL still serves
    import re
    v = re.search(r"home\.js\?v=([0-9a-f]+)", home).group(1)
    urllib.request.urlopen(f"{base}/static/home.js?v={v}")


def test_a_changed_bundle_changes_the_version(app_server, tmp_path):
    """Most deploys change app.js and nothing else. Hashing only index.html
    kept `?v=` identical across them, so the cache bust did not bust."""
    import re
    base, _, _ = app_server
    get_v = lambda: re.search(
        r"home\.js\?v=([0-9a-f]+)", urllib.request.urlopen(base + "/").read().decode()
    ).group(1)
    before = get_v()
    (tmp_path / "home.js").write_text("console.log(2)")
    assert get_v() != before, "home.js changed but its versioned URL did not"


# ── compression chains collapse to one sidebar row ─────────────────────────

def test_list_sessions_projects_compression_chains(monkeypatch):
    """Context compression rotates the session id; each rotation used to show
    up as its own sidebar row (five fragments of one conversation). The bridge
    must use hermes' chain projection, keyed to the tip where messages live."""
    import hermes_session_api as hsa

    seen = {}

    class FakeDB:
        def list_sessions_rich(self, **kw):
            seen.update(kw)
            return [{
                "id": "tip", "end_reason": None, "message_count": 23,
                "title": "不是有那个第二只箭的故事吗", "preview": "…",
                "last_active": 22, "started_at": 1,
                "_lineage_root_id": "root",
            }]
        def resolve_resume_session_id(self, sid):
            return "tip" if sid == "root" else sid
        def message_count(self, sid):
            return 23 if sid == "tip" else 0

    monkeypatch.setattr(hsa, "_db", lambda: FakeDB())
    rows = hsa.list_sessions(limit=100, include_empty=False)
    assert seen["include_children"] is False
    assert seen["project_compression_tips"] is True
    assert seen["order_by_last_active"] is True
    assert len(rows) == 1 and rows[0]["id"] == "tip"
    assert rows[0]["messageCount"] == 23


def test_list_sessions_zero_message_root_resolves_through_the_chain(monkeypatch):
    """A compression root whose tip flushed nothing yet must not vanish (the
    '全没了' regression): walk the chain to the first descendant with messages."""
    import hermes_session_api as hsa

    class FakeDB:
        def list_sessions_rich(self, **kw):
            return [{"id": "root", "end_reason": None, "message_count": 0,
                     "title": "", "preview": "", "last_active": 1, "started_at": 1}]
        def resolve_resume_session_id(self, sid):
            return "tip" if sid == "root" else sid
        def message_count(self, sid):
            return 9 if sid == "tip" else 0

    monkeypatch.setattr(hsa, "_db", lambda: FakeDB())
    rows = hsa.list_sessions(limit=100, include_empty=False)
    assert len(rows) == 1 and rows[0]["id"] == "tip" and rows[0]["messageCount"] == 9


# ── the project a conversation belongs to ───────────────────────────────────


def test_the_project_mark_is_read_only_in_its_two_part_form():
    """`deepwiki:<key>`, never a bare source.

    The mark rides on `agent.platform`, which hermes writes into
    `sessions.source`. That column already held values from before the mark
    existed — and one of them is "buda", this app's platform name before the
    deepwiki repositioning, which is ALSO a profile key today. Reading a bare
    source as a project would hand every pre-mark session to that project by
    coincidence, and the coincidence would look like the feature working.
    """
    buda = pr.build_profiles([{"key": "buda"}])[0]
    assert ha.session_mark(buda) == "deepwiki:buda"
    assert ha.session_mark(None) == "deepwiki"
    assert ha.profile_of_source("deepwiki:buda") == "buda"
    assert ha.profile_of_source("deepwiki:code-autumn-rs") == "code-autumn-rs"
    for bare in ("deepwiki", "buda", "cli", "tool", "", None, "deepwiki:"):
        assert ha.profile_of_source(bare) is None, f"{bare!r} must not read as a project"


def test_a_compressed_conversation_keeps_its_project(app_server):
    """The bug this moved the mark down for, in the shape it shipped.

    Context compression ROTATES the session id: the old row ends with
    end_reason='compression' and the conversation continues under a new one.
    The sidebar lists each chain under its TIP. A pin recorded in the side
    table at chat/start is keyed by the id the conversation STARTED with, so
    after a compression it points at a dead row — and the tip, having no pin,
    filed under the DEFAULT project. In production a buda conversation had
    already lost its project this way; it only looked right because buda also
    happened to be the default.

    hermes stamps `agent.platform` onto the compression child too
    (`agent/conversation_compression.py`), so the mark is on the tip without
    anyone carrying it there. Here the tip carries the mark and NO pin.

    Ablation: read `profile` from the side table first and this goes red.
    """
    base, _, state = app_server
    state["sessions"].rows = [
        {"id": "tip-after-compression", "title": "六道", "source": "deepwiki:video",
         "messageCount": 9, "lastActive": 0},
    ]
    j = _get(base, "/api/sessions")
    row = next(r for r in j["sessions"] if r["id"] == "tip-after-compression")
    assert row["profile"] == "video", (
        "the tip of a compression chain must file under the project its own "
        "row is marked with, not under the default"
    )


def test_an_unmarked_row_still_falls_back_to_the_pin(app_server):
    """Rows written before the mark keep working.

    The mark only appears on sessions whose next turn has run under the new
    code. Everything already in the store carries a bare source and a pin in
    the side table, and must keep filing where it always did.
    """
    base, _, state = app_server
    state["sessions"].rows = [
        {"id": "old-one", "title": "旧会话", "source": "deepwiki",
         "messageCount": 3, "lastActive": 0},
    ]
    # As chat/start recorded it, before the mark existed.
    _, started = _post(base, "/api/chat/start",
                       {"text": "hi", "sessionId": "old-one", "profile": "video"})
    assert started["profile"] == "video"
    j = _get(base, "/api/sessions")
    row = next(r for r in j["sessions"] if r["id"] == "old-one")
    assert row["profile"] == "video", "an unmarked row still reads its pin"


def test_a_send_from_another_projects_page_cannot_move_the_conversation(app_server):
    """The corruption path, closed.

    The page carries its project in its URL and the composer sends it. A page
    can legitimately be SHOWING a conversation that belongs elsewhere — the
    view was restored per browser, so opening /video/ could reopen the buda
    conversation last read. Pressing 发送 there did two things, both silent:
    answered the turn with the wrong project's agent (its MCP servers, not the
    corpus this conversation had been talking to), and re-pinned the
    conversation to the sending page's project on the way through, because
    `record()` overwrites.

    Which project answers is the CONVERSATION's property. Asserted where it
    matters — the profile handed to the agent, not just the echo.

    Ablation: resolve the profile from the request body and this goes red.
    """
    base, mgr, state = app_server
    seen = []
    real = ha.AgentPool.acquire
    ha.AgentPool.acquire = lambda self, sid, ep, profile=None: (
        seen.append((sid, profile.key if profile else None)) or real(self, sid, ep, profile))
    try:
        # Born under buda, from buda's page.
        _, first = _post(base, "/api/chat/start", {"text": "六道是什么", "profile": "buda"})
        sid = first["sessionId"]
        assert first["profile"] == "buda"
        _wait_idle(mgr)
        # The same conversation, now sent from the video project's page.
        _, second = _post(base, "/api/chat/start",
                          {"text": "继续", "sessionId": sid, "profile": "video"})
        assert second["profile"] == "buda", (
            "the echo must name the conversation's own project, so the page "
            "that sent it does not go on believing it moved"
        )
        _wait_idle(mgr)
        assert seen[-1] == (sid, "buda"), (
            "the AGENT must be built for the conversation's project; answering "
            "with video's tool surface is the real damage, not the label"
        )
    finally:
        ha.AgentPool.acquire = real


def test_a_new_conversation_still_takes_the_page_it_was_started_from(app_server):
    """The other half: `profile` in the request decides a NEW conversation.

    Without this the rule above would collapse into "everything is the default
    project" — the resolver returns None for an id the store has never seen,
    and a new conversation is exactly that.
    """
    base, mgr, _ = app_server
    _, j = _post(base, "/api/chat/start", {"text": "hi", "profile": "video", "new": True})
    assert j["profile"] == "video"
    _wait_idle(mgr)


def test_a_pin_stranded_by_compression_is_walked_forward(app_server, monkeypatch):
    """The production casualty, repaired at read time.

    A buda conversation was compressed before the mark existed:
    `session_profiles.json` holds `<root> -> buda`, the store lists the chain
    under a different id, and nothing connects them — so it filed under the
    default project. It only LOOKED right because buda was also the default;
    reorder DEEPWIKI_PROFILES and every such conversation moves at once.

    No migration: the pin is walked forward through the store's own resume
    resolver, and the mark takes over for good on the conversation's next turn.
    """
    base, mgr, state = app_server
    fake = state["sessions"]
    fake.tips = {"root-id": "tip-id"}
    fake.resolve_tip = lambda sid: fake.tips.get(sid, sid)
    # The chain, as the sidebar sees it: the tip, unmarked, under a new id.
    fake.rows = [{"id": "tip-id", "title": "六道", "source": "deepwiki",
                  "messageCount": 12, "lastActive": 0}]
    # The pin, as chat/start wrote it under the id the conversation started with.
    _, j = _post(base, "/api/chat/start",
                 {"text": "hi", "sessionId": "root-id", "profile": "video"})
    assert j["profile"] == "video"
    _wait_idle(mgr)

    rows = _get(base, "/api/sessions")["sessions"]
    row = next(r for r in rows if r["id"] == "tip-id")
    assert row["profile"] == "video", (
        "the pin belongs to the chain, not to the id it was written under"
    )
