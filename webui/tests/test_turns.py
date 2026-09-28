"""Admission and lifecycle of a turn. Every case here is a client-visible one."""

from __future__ import annotations

import queue
import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import hermes_agent as ha  # noqa: E402
import media_input  # noqa: E402
from turns import Refused, TurnManager  # noqa: E402


class FakeAgent:
    def __init__(self):
        self.interrupted = threading.Event()
        self.stream_delta_callback = None
        self.reasoning_callback = None
        self.tool_progress_callback = None
        self.step_callback = None
        self.thinking_callback = None

    def interrupt(self, message=None):
        self.interrupted.set()


def _mgr(monkeypatch, run=None, history=None):
    monkeypatch.setattr(ha, "build_agent", lambda session_id, ep, profile=None, user_id="": FakeAgent())
    pool = ha.AgentPool()
    return TurnManager(
        pool,
        history=history or (lambda sid: []),
        run=run or (lambda agent, **kw: {"final_response": "ok"}),
    )


def _ep(key="dsv4", max_concurrent=4):
    return ha.Endpoint(key=key, label=key, model="m", base_url="http://x/v1",
                       max_concurrent=max_concurrent)


def _wait_done(stream, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not stream.running:
            return True
        time.sleep(0.005)
    return False


# ── the happy path ──────────────────────────────────────────────────────────


def test_the_prompt_is_in_the_log_before_the_answer(monkeypatch):
    """A reader attaching late must still see the question above the reply."""
    def run(agent, **kw):
        agent.stream_delta_callback("hi")
        return {}

    m = _mgr(monkeypatch, run=run)
    s = m.start(session_id="s1", text="who are you", endpoint=_ep())
    assert _wait_done(s)
    kinds = [(e["kind"], e.get("text")) for e in s.after(0)]
    assert kinds[0] == ("user", "who are you")
    assert ("delta", "hi") in kinds
    assert kinds[-1][0] == "end"


def test_the_history_read_is_what_the_agent_is_given(monkeypatch):
    """hermes does NOT load history itself: a turn handed nothing starts the
    conversation over, silently, and the model answers as if nothing was said."""
    seen = {}

    def run(agent, **kw):
        seen["history"] = kw["history"]
        return {}

    m = _mgr(monkeypatch, run=run, history=lambda sid: [{"role": "user", "content": "before"}])
    s = m.start(session_id="s1", text="and now", endpoint=_ep())
    assert _wait_done(s)
    assert seen["history"] == [{"role": "user", "content": "before"}]


def test_even_an_unauthenticated_turn_advertises_async_delivery(monkeypatch):
    """Basic-auth/legacy requests have no user_id, but still have a stable WebUI
    session route.  Without this flag Hermes silently disables
    notify_on_complete and no queue event is ever produced."""
    seen = {}
    gateway = types.ModuleType("gateway")
    context = types.ModuleType("gateway.session_context")

    def set_session_vars(**kwargs):
        seen.update(kwargs)
        return ("tokens",)

    context.set_session_vars = set_session_vars
    context.clear_session_vars = lambda tokens: seen.update(cleared=tokens)
    gateway.session_context = context
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)

    stream = _mgr(monkeypatch).start(session_id="s1", text="run", endpoint=_ep())
    assert _wait_done(stream)
    assert seen["session_key"] == "s1"
    assert seen["platform"] == "deepwiki"
    assert seen["async_delivery"] is True
    assert seen["cleared"] == ("tokens",)


def test_only_the_current_image_marker_is_hydrated(monkeypatch):
    seen = {}
    old = "[输入图片 object_key: input/webui/s/00000000000000000000000000000000.png]"
    current = "[输入图片 object_key: input/webui/s/11111111111111111111111111111111.png]"
    history = [{"role": "user", "content": old}]
    live = [
        {"type": "text", "text": current},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    monkeypatch.setattr(media_input, "model_user_content", lambda text: live)

    def run(agent, **kw):
        seen.update(kw)
        return {}

    m = _mgr(monkeypatch, run=run, history=lambda sid: history)
    s = m.start(session_id="s1", text=current, endpoint=_ep())
    assert _wait_done(s)
    assert seen["user_message"] is live
    assert seen["persist_user_message"] == current
    assert seen["history"] == history
    assert "base64" not in str(seen["history"])


# ── admission ───────────────────────────────────────────────────────────────


def test_a_second_prompt_into_a_replying_conversation_is_refused_as_taken(monkeypatch):
    """`taken`, not `busy`: the text was never read by a model, so the client
    has to be able to put it back in the composer."""
    gate = threading.Event()
    m = _mgr(monkeypatch, run=lambda agent, **kw: gate.wait(3) and {})
    first = m.start(session_id="s1", text="one", endpoint=_ep())
    with pytest.raises(Refused) as e:
        m.start(session_id="s1", text="two", endpoint=_ep())
    assert e.value.reason == "taken"
    assert e.value.as_json()["streamId"] == first.stream_id
    gate.set()
    assert _wait_done(first)


def test_the_limit_is_per_endpoint_not_global(monkeypatch):
    """The ceiling is the model behind the endpoint. One global number can only
    be right for one of them."""
    gate = threading.Event()
    m = _mgr(monkeypatch, run=lambda agent, **kw: gate.wait(3) and {})
    small = _ep("dsv4", max_concurrent=1)
    other = _ep("vision", max_concurrent=1)

    a = m.start(session_id="s1", text="x", endpoint=small)
    with pytest.raises(Refused) as e:
        m.start(session_id="s2", text="y", endpoint=small)
    assert e.value.reason == "busy"
    assert e.value.as_json()["maxConcurrent"] == 1

    # A different endpoint has its own budget and is unaffected.
    b = m.start(session_id="s3", text="z", endpoint=other)
    gate.set()
    assert _wait_done(a) and _wait_done(b)


def test_one_user_can_start_another_session_while_the_first_replies(monkeypatch):
    """A fresh conversation follows endpoint capacity, even for one signed-in user."""
    gate = threading.Event()
    m = _mgr(monkeypatch, run=lambda agent, **kw: gate.wait(3) and {})
    endpoint = _ep(max_concurrent=2)

    first = m.start(session_id="s1", text="one", endpoint=endpoint, user_id="reader")
    second = m.start(session_id="s2", text="two", endpoint=endpoint, user_id="reader")
    assert first.running and second.running
    with pytest.raises(Refused) as e:
        m.start(session_id="s3", text="three", endpoint=endpoint, user_id="reader")
    assert e.value.reason == "busy"

    gate.set()
    assert _wait_done(first) and _wait_done(second)


def test_a_finished_turn_frees_its_conversation(monkeypatch):
    m = _mgr(monkeypatch)
    first = m.start(session_id="s1", text="one", endpoint=_ep())
    assert _wait_done(first)
    second = m.start(session_id="s1", text="two", endpoint=_ep())
    assert _wait_done(second)


# ── failure and cancellation ────────────────────────────────────────────────


def test_a_turn_that_raises_still_tells_the_reader(monkeypatch):
    """Dying silently leaves the composer locked and the spinner running until
    EventSource gives up on its own."""
    def boom(agent, **kw):
        raise RuntimeError("model went away")

    m = _mgr(monkeypatch, run=boom)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    end = [e for e in s.after(0) if e["kind"] == "end"][0]
    assert "model went away" in end["error"]


def test_a_failed_turn_does_not_wedge_its_conversation(monkeypatch):
    m = _mgr(monkeypatch, run=lambda agent, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    first = m.start(session_id="s1", text="one", endpoint=_ep())
    assert _wait_done(first)
    m.start(session_id="s1", text="two", endpoint=_ep())  # must not raise Refused


def test_cancel_reaches_the_running_agent_and_marks_the_stop(monkeypatch):
    """`stopped` is set before interrupting so the death it causes reads as a
    stop, not a failure — the reader pressed the button and must be told it
    worked."""
    started, release = threading.Event(), threading.Event()

    def run(agent, **kw):
        started.set()
        release.wait(3)
        return {}

    m = _mgr(monkeypatch, run=run)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert started.wait(2)
    assert m.cancel(s.stream_id) is True
    assert s.stopped is True
    release.set()
    assert _wait_done(s)


def test_cancelling_a_finished_turn_is_not_an_error(monkeypatch):
    m = _mgr(monkeypatch)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    assert m.cancel(s.stream_id) is False
    assert m.cancel("never-existed") is False


# ── bookkeeping ─────────────────────────────────────────────────────────────


def test_a_finished_stream_stays_readable_for_a_reader_who_was_away(monkeypatch):
    m = _mgr(monkeypatch)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    assert m.stream(s.stream_id) is s, "the tail must still be collectable"
    assert m.live_for("s1") is None, "but the conversation is no longer replying"


def test_finished_streams_are_eventually_dropped(monkeypatch):
    m = _mgr(monkeypatch)
    ids = []
    for i in range(5):
        s = m.start(session_id=f"s{i}", text="x", endpoint=_ep())
        assert _wait_done(s)
        ids.append(s.stream_id)
    m.forget_finished(keep=2)
    assert [i for i in ids if m.stream(i) is not None] == ids[-2:]


# ── approvals ───────────────────────────────────────────────────────────────


def _fake_approval(monkeypatch):
    """Stand in for `tools.approval`, which lives in hermes' venv.

    Models the GATEWAY surface — a module-global queue keyed by session, a
    notify callback, and a resolve that unblocks from any thread — because that
    is the surface this code uses. The thread-local
    `terminal_tool.set_approval_callback` is NOT modelled: it is what this
    replaced, and modelling it would let the old bug pass.
    """
    import sys
    import threading as _t
    import types

    ap = types.ModuleType("tools.approval")
    class Entry:
        def __init__(self, data):
            self.data = data
            self.event = _t.Event()
            self.result = None

    ap._lock = _t.RLock()
    ap._gateway_queues = {}
    ap.queues = ap._gateway_queues  # concise alias used by these tests
    ap.notify = {}
    ap._key = {"v": "default"}

    ap.bound = []           # every key this turn bound, in order

    def set_current_session_key(k):
        prev = ap._key["v"]
        ap._key["v"] = k
        ap.bound.append(k)
        return prev

    def reset_current_session_key(tok):
        ap._key["v"] = tok

    def register_gateway_notify(k, cb):
        ap.notify[k] = cb

    ap.unregistered = []

    def unregister_gateway_notify(k):
        ap.unregistered.append(k)

    def resolve_gateway_approval(k, choice, resolve_all=False):
        with ap._lock:
            q = ap.queues.get(k) or []
            targets = list(q) if resolve_all else q[:1]
            for entry in targets:
                q.remove(entry)
            if not q:
                ap.queues.pop(k, None)
        for t in targets:
            t.result = choice
            t.event.set()
        return len(targets)

    def ask(k, data):
        """What hermes does on the agent thread: queue, notify, block."""
        entry = Entry(data)
        with ap._lock:
            ap.queues.setdefault(k, []).append(entry)
        cb = ap.notify.get(k)
        if cb:
            cb(data)
        entry.event.wait(timeout=10)
        return entry.result or "deny"

    ap.set_current_session_key = set_current_session_key
    ap.reset_current_session_key = reset_current_session_key
    ap.register_gateway_notify = register_gateway_notify
    ap.unregister_gateway_notify = unregister_gateway_notify
    ap.resolve_gateway_approval = resolve_gateway_approval
    ap.ask = ask

    tools = sys.modules.get("tools") or types.ModuleType("tools")
    tools.approval = ap
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", ap)
    return ap


def test_the_approval_hook_is_the_gateway_one_not_the_thread_local(monkeypatch):
    """`terminal_tool.set_approval_callback` stores into a `threading.local`.

    hermes dispatches tools on its own threads, so a callback registered on the
    turn's thread is invisible where the question is actually asked; hermes
    then falls through to an `input()` that nothing will ever answer. Measured
    on the cluster: `terminal / running`, no events for minutes, the endpoint's
    only slot held, and `interrupt()` unable to help because it sets a flag the
    conversation loop reads and the loop was never reached.

    So the turn must bind the session key (a contextvar, inherited by threads)
    and register a gateway notify — not the thread-local callback.

    Ablation: go back to `terminal_tool.set_approval_callback` and this fails.
    """
    ap = _fake_approval(monkeypatch)
    m = _mgr(monkeypatch)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    # Checked as a HISTORY, not a current value: teardown resets the contextvar
    # when the turn ends, which is correct and would make a live read useless.
    assert "s1" in ap.bound, "the session key was never bound for this turn"
    assert "s1" in ap.notify, "no gateway notify was registered"


def test_a_permission_request_reaches_the_reader_and_the_turn_waits(monkeypatch):
    import threading

    ap = _fake_approval(monkeypatch)
    m = _mgr(monkeypatch)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)

    out = {}
    t = threading.Thread(target=lambda: out.setdefault(
        "r", ap.ask("s1", {"command": "rm -rf /tmp/x", "description": "delete a path"})))
    t.start()

    ev = None
    for _ in range(300):
        ev = next((e for e in s.after(0) if e["kind"] == "approval"), None)
        if ev:
            break
        time.sleep(0.01)
    assert ev, "no approval card was emitted"
    assert ev["title"] == "delete a path"
    assert [o["optionId"] for o in ev["options"]] == ["once", "session", "always", "deny"]
    assert t.is_alive(), "the turn carried on without an answer"

    ap.resolve_gateway_approval("s1", "session")
    t.join(timeout=5)
    assert out["r"] == "session"


def test_a_smart_denial_is_rejected_without_showing_an_override_card(monkeypatch):
    ap = _fake_approval(monkeypatch)
    result = {}

    def run(agent, **kw):
        result["choice"] = ap.ask("s1", {
            "command": "rm -rf /",
            "description": "recursive delete of a system path",
            "smart_denied": True,
            "allow_permanent": False,
        })
        return {}

    m = _mgr(monkeypatch, run=run)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    assert result["choice"] == "deny"
    assert not [e for e in s.after(0) if e["kind"] == "approval"]
    notes = [e["text"] for e in s.after(0) if e["kind"] == "note"]
    assert notes == ["智能审批已拒绝：recursive delete of a system path"]


def test_a_smart_denial_resolves_its_exact_entry_not_the_fifo_head(monkeypatch):
    """Parallel tool calls can queue a normal prompt before Smart DENY.

    The public Hermes resolver answers the oldest request, so using it from
    the notify callback would deny the normal prompt and leave the dangerous
    request waiting. Correlate by the payload object stored on the entry.
    """
    ap = _fake_approval(monkeypatch)
    hold_turn = threading.Event()
    m = _mgr(monkeypatch, run=lambda agent, **kw: hold_turn.wait(5) and {})
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    for _ in range(300):
        if "s1" in ap.notify:
            break
        time.sleep(0.01)

    normal_data = {"command": "sudo true", "description": "manual review"}
    smart_data = {
        "command": "rm -rf /", "description": "smart rejected",
        "smart_denied": True, "allow_permanent": False,
    }
    choices = {}
    normal = threading.Thread(
        target=lambda: choices.setdefault("normal", ap.ask("s1", normal_data))
    )
    normal.start()
    for _ in range(300):
        if ap.queues.get("s1"):
            break
        time.sleep(0.01)

    denied = threading.Thread(
        target=lambda: choices.setdefault("smart", ap.ask("s1", smart_data))
    )
    denied.start(); denied.join(timeout=3)
    assert choices.get("smart") == "deny"
    assert normal.is_alive(), "the older manual request was denied by mistake"
    assert len(ap.queues.get("s1") or []) == 1
    assert ap.queues["s1"][0].data is normal_data

    ap.resolve_gateway_approval("s1", "once")
    normal.join(timeout=3)
    assert choices["normal"] == "once"
    cards = [e for e in s.after(0) if e["kind"] == "approval"]
    assert len(cards) == 1 and cards[0]["title"] == "manual review"
    hold_turn.set()
    assert _wait_done(s)


def test_stop_releases_a_turn_parked_on_an_approval(monkeypatch):
    """Interrupt alone cannot reach it.

    `interrupt` sets a flag the conversation loop reads; a turn waiting for
    permission is parked on the approval queue and never reaches that loop.
    Pressing Stop with a card on screen is exactly when a reader most wants it
    to stop, so cancel denies what is pending first.

    The turn is held RUNNING here on purpose — cancel returns early for a
    finished stream, and a finished turn is not the situation this is about.

    Ablation: drop the `resolve_gateway_approval` call from `cancel` and the
    waiter never returns.
    """
    import threading

    ap = _fake_approval(monkeypatch)
    parked = threading.Event()
    released = {}

    def blocking_run(agent, **kw):
        # What hermes does: ask, and do not come back until answered.
        parked.set()
        released["choice"] = ap.ask("s1", {"command": "rm -rf /", "description": "dangerous"})
        return {"final_response": "stopped"}

    m = _mgr(monkeypatch, run=blocking_run)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert parked.wait(timeout=5), "the turn never reached the approval"
    for _ in range(300):
        if ap.queues.get("s1"):
            break
        time.sleep(0.01)
    assert s.running, "the turn should still be running while it waits"

    m.cancel(s.stream_id)
    assert _wait_done(s), "the turn did not finish after stop"
    assert released.get("choice") == "deny", "stop did not deny the parked approval"




def test_the_session_key_is_visible_from_a_thread_hermes_starts(monkeypatch):
    """A contextvar does not cross a `threading.Thread`.

    A new thread begins with an EMPTY context, not a copy of its parent's, and
    hermes dispatches tools on threads of its own. So the key bound with
    `set_current_session_key` was invisible where the tool actually asked:
    hermes queued the approval under `"default"`, looked for a notify callback
    under `"default"`, found none, and the turn waited forever with no card on
    screen. `get_current_session_key()` falls back to HERMES_SESSION_KEY, which
    is process-wide and therefore does cross.

    Ablation: drop the `os.environ["HERMES_SESSION_KEY"]` line and this fails.
    """
    import os
    import threading

    _fake_approval(monkeypatch)
    m = _mgr(monkeypatch)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    s = m.start(session_id="s-thread", text="x", endpoint=_ep())
    assert _wait_done(s)

    seen = {}
    t = threading.Thread(target=lambda: seen.setdefault("key", os.environ.get("HERMES_SESSION_KEY")))
    t.start(); t.join(timeout=5)
    assert seen["key"] == "s-thread", (
        "a thread hermes starts cannot see the session key, so it will ask "
        "under 'default' and no card will ever be shown"
    )


def test_the_gateway_context_switch_is_on(monkeypatch):
    """`_is_gateway_approval_context()` reads HERMES_GATEWAY_SESSION. Without
    it hermes takes the CLI branch — the thread-local callback and then the
    `input()` that never returns."""
    import os

    _fake_approval(monkeypatch)
    m = _mgr(monkeypatch)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    s = m.start(session_id="s1", text="x", endpoint=_ep())
    assert _wait_done(s)
    assert os.environ.get("HERMES_GATEWAY_SESSION") == "1"


# ── the turn pool ───────────────────────────────────────────────────────────


def test_turn_runs_on_a_named_pool_thread(monkeypatch):
    """A reader of py-spy output or logs must see turn-<stream id>, not turn-3."""
    seen = {}

    def run(agent, **kw):
        seen["name"] = threading.current_thread().name
        return {}

    m = _mgr(monkeypatch, run=run)
    s = m.start(session_id="s1", text="who", endpoint=_ep())
    assert _wait_done(s)
    assert seen["name"] == f"turn-{s.stream_id}"


def test_pool_workers_outlive_turns_and_are_daemon(monkeypatch):
    """Workers persist across turns (that is the point of a pool) and are
    daemon — SIGTERM must drop an in-flight turn, not join it at exit."""
    m = _mgr(monkeypatch)
    s = m.start(session_id="s1", text="warm", endpoint=_ep())
    assert _wait_done(s)
    workers = [t for t in threading.enumerate() if t.name.startswith("turn-")]
    assert workers and all(t.daemon for t in workers)


def test_saturated_pool_still_runs_the_turn(monkeypatch):
    """Admission budget larger than the pool must never stall a turn: the
    spare thread path exists so mis-sizing degrades to today's behaviour."""
    gate = threading.Event()

    def run(agent, **kw):
        gate.wait(3)
        return {}

    m = _mgr(monkeypatch, run=run)
    m._turns._workers = 1  # simulate drift: endpoints grew after boot
    a = m.start(session_id="s1", text="x", endpoint=_ep("e1", 1))
    b = m.start(session_id="s2", text="y", endpoint=_ep("e2", 1))
    gate.set()
    assert _wait_done(a) and _wait_done(b)


# ── session id rotation ─────────────────────────────────────────────────────
#
# hermes' context compression ROTATES the session mid-turn
# (`agent/conversation_compression.py`, 0.17): it ends the old session, sets
# `agent.session_id` to a fresh id, creates a child row and persists the rest
# of the turn there. The stream used to keep stamping the id it was created
# with, `_live` and the agent cache stayed keyed by it, and the next send went
# out under the OLD id — handing the agent the pre-compression history while it
# wrote to the new session.


class _RotatingAgent(FakeAgent):
    def __init__(self, session_id):
        super().__init__()
        self.session_id = session_id


def _rotating_mgr(monkeypatch, run, history=None):
    built = []

    def build(session_id, ep, profile=None):
        a = _RotatingAgent(session_id)
        built.append(a)
        return a

    monkeypatch.setattr(ha, "build_agent", build)
    return TurnManager(ha.AgentPool(), history=history or (lambda sid: []), run=run), built


def test_a_rotation_mid_turn_is_carried_on_the_stream(monkeypatch):
    """Frames after the rotation, and the end, name the NEW session — that is
    what the client follows. Ablation: drop the stream's session source and the
    later frames keep the old id."""
    gate = threading.Event()

    def run(agent, **kw):
        agent.stream_delta_callback("before")
        agent.session_id = "s1-rot"          # compression fired
        agent.stream_delta_callback("after")
        gate.wait(3)
        return {}

    _fake_approval(monkeypatch)
    m, _ = _rotating_mgr(monkeypatch, run)
    s = m.start(session_id="s1", text="long", endpoint=_ep())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not any(e.get("text") == "after" for e in s.after(0)):
        time.sleep(0.005)
    by_text = {e.get("text"): e["session"] for e in s.after(0) if e["kind"] == "delta"}
    assert by_text == {"before": "s1", "after": "s1-rot"}, by_text
    # Live bookkeeping follows it: the sidebar marks the new row, and a second
    # prompt into the continuation is refused as taken rather than run twice.
    assert m.running() == {"s1-rot": s.stream_id}
    assert m.live_for("s1-rot") is s and m.live_for("s1") is None
    with pytest.raises(Refused) as r:
        m.start(session_id="s1-rot", text="again", endpoint=_ep())
    assert r.value.reason == "taken"
    # A reader polling approvals for the rotated conversation must find the
    # queue the turn registered, which is still under the id it started with.
    assert m.approval_key_for("s1-rot") == "s1"
    assert m.approval_key_for("elsewhere") == "elsewhere"
    gate.set()
    assert _wait_done(s)
    assert s.after(0)[-1] == {**s.after(0)[-1], "kind": "end", "session": "s1-rot"}


def test_the_next_turn_continues_the_rotated_session_with_its_own_agent(monkeypatch):
    """After a rotation the conversation lives under the new id: sending there
    must reuse the agent that rotated (cache re-keyed) and read THAT session's
    history. Ablation: drop the pool rename and a fresh agent is built."""
    seen = []

    def run(agent, **kw):
        seen.append((kw["session_id"], agent.session_id))
        if agent.session_id == "s1":
            agent.session_id = "s1-rot"
        agent.stream_delta_callback("x")
        return {}

    histories = []
    m, built = _rotating_mgr(monkeypatch, run, history=lambda sid: histories.append(sid) or [])
    assert _wait_done(m.start(session_id="s1", text="one", endpoint=_ep()))
    assert _wait_done(m.start(session_id="s1-rot", text="two", endpoint=_ep()))
    assert len(built) == 1, "the rotated conversation was handed a freshly built agent"
    assert histories == ["s1", "s1-rot"]
    assert seen[-1] == ("s1-rot", "s1-rot")


def test_a_rotation_does_not_strand_the_approval_wiring(monkeypatch):
    """The approval hook is registered under the key the turn STARTED with;
    teardown must release that key, not the rotated one, or the old entry is
    left in hermes' gateway queue for the next approval to queue behind."""
    ap = _fake_approval(monkeypatch)

    def run(agent, **kw):
        agent.session_id = "s1-rot"
        agent.stream_delta_callback("x")
        return {}

    m, _ = _rotating_mgr(monkeypatch, run)
    assert _wait_done(m.start(session_id="s1", text="x", endpoint=_ep()))
    assert ap.unregistered == ["s1"], f"teardown released {ap.unregistered}, not the key it registered"


# ── background process completion delivery ────────────────────────────────


class _CompletionRegistry:
    def __init__(self):
        self.completion_queue = queue.Queue()
        self.pending_watchers = []
        self.consumed = set()

    def is_completion_consumed(self, process_id):
        return process_id in self.consumed


def _completion(process_id="proc-1", session_key="s1"):
    return {
        "type": "completion",
        "session_id": process_id,
        "session_key": session_key,
        "command": "long-job",
        "exit_code": 0,
        "output": "done",
    }


def _start_dispatcher(manager, registry):
    stop = threading.Event()
    thread = threading.Thread(
        target=manager.run_completion_dispatcher,
        kwargs={
            "stop": stop,
            "registry": registry,
            "formatter": lambda e: (
                f"[IMPORTANT: Background process {e['session_id']} completed] "
                f"{e['output']}"
            ),
            "poll_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    return stop, thread


def test_completion_waits_for_the_originating_turn_then_auto_resumes(monkeypatch):
    """The notification may arrive while the model is still answering the turn
    that launched it.  It must become the NEXT turn, never splice into that
    turn or get lost when the first stream ends."""
    first_gate = threading.Event()
    second_started = threading.Event()
    calls = []

    def run(agent, **kw):
        calls.append(kw)
        if len(calls) == 1:
            first_gate.wait(3)
        else:
            second_started.set()
        return {}

    manager = _mgr(monkeypatch, run=run)
    first = manager.start(session_id="s1", text="launch it", endpoint=_ep())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not calls:
        time.sleep(0.005)
    registry = _CompletionRegistry()
    registry.pending_watchers.append({"session_id": "proc-1", "session_key": "s1"})
    stop, thread = _start_dispatcher(manager, registry)
    try:
        registry.completion_queue.put(_completion())
        time.sleep(0.05)
        assert len(calls) == 1, "completion interrupted the turn that launched it"
        subscriber, initial = manager.subscribe_session("s1")
        assert initial == {
            "sessionId": "s1", "streamId": first.stream_id, "recovered": True,
        }
        first_gate.set()
        assert _wait_done(first)
        assert second_started.wait(3), "idle conversation was not auto-resumed"
        assert calls[1]["session_id"] == "s1"
        assert calls[1]["user_source"] == ha.PROCESS_WAKEUP_SOURCE
        assert calls[1]["persist_user_message"].startswith(
            "[IMPORTANT: Background process proc-1 completed]"
        )
        resumed = next(
            stream for stream in manager._streams.values()
            if stream is not first
        )
        assert resumed.after(0)[0]["kind"] == "note"
        assert "agent 自动继续" in resumed.after(0)[0]["text"]
        event_name, event = subscriber.get(timeout=1)
        assert event_name == "server_turn_started"
        assert event == {"sessionId": "s1", "streamId": resumed.stream_id}
        manager.unsubscribe_session("s1", subscriber)
        assert not registry.pending_watchers
    finally:
        first_gate.set()
        stop.set()
        thread.join(1)


def test_completion_waits_for_endpoint_capacity_and_follows_rotation(monkeypatch):
    """An idle origin can still be blocked by another session on its endpoint;
    after capacity frees, resume the compression tip rather than the captured
    pre-compression session_key."""
    busy_gate = threading.Event()
    resumed = threading.Event()
    calls = []

    def run(agent, **kw):
        calls.append(kw["session_id"])
        if kw["session_id"] == "s1":
            agent.session_id = "s1-tip"
            agent.stream_delta_callback("rotated")
        elif kw["session_id"] == "other":
            busy_gate.wait(3)
        elif kw["session_id"] == "s1-tip":
            resumed.set()
        return {}

    manager, _ = _rotating_mgr(monkeypatch, run)
    endpoint = _ep(max_concurrent=1)
    assert _wait_done(manager.start(session_id="s1", text="launch", endpoint=endpoint))
    other = manager.start(session_id="other", text="occupy", endpoint=endpoint)
    registry = _CompletionRegistry()
    stop, thread = _start_dispatcher(manager, registry)
    try:
        registry.completion_queue.put(_completion(session_key="s1"))
        time.sleep(0.05)
        assert not resumed.is_set(), "completion bypassed endpoint admission"
        busy_gate.set()
        assert _wait_done(other)
        assert resumed.wait(3)
        assert calls[-1] == "s1-tip"
    finally:
        busy_gate.set()
        stop.set()
        thread.join(1)


def test_consumed_duplicate_and_deleted_completions_do_not_resume(monkeypatch):
    calls = []
    manager = _mgr(monkeypatch, run=lambda agent, **kw: calls.append(kw) or {})
    assert _wait_done(manager.start(session_id="s1", text="launch", endpoint=_ep()))
    registry = _CompletionRegistry()
    registry.consumed.add("already-read")
    stop, thread = _start_dispatcher(manager, registry)
    try:
        registry.completion_queue.put(_completion("already-read"))
        registry.completion_queue.put(_completion("once"))
        registry.completion_queue.put(_completion("once"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(calls) < 2:
            time.sleep(0.005)
        assert len(calls) == 2, "one unconsumed process should create exactly one turn"
        while time.monotonic() < deadline and any(
            not getattr(stream, "turn_settled", True)
            for stream in manager._streams.values()
        ):
            time.sleep(0.005)

        route = manager._routes["s1"]
        assert manager.forget_sessions(["s1"])
        with pytest.raises(Refused) as refused:
            manager.start(
                session_id="s1", text="late", endpoint=_ep(), internal=True,
                _completion_route=route,
            )
        assert refused.value.reason == "gone"
        registry.completion_queue.put(_completion("after-delete"))
        time.sleep(0.05)
        assert len(calls) == 2, "a late completion recreated a deleted conversation"
    finally:
        stop.set()
        thread.join(1)


def test_delete_guard_includes_a_rotated_tip_not_yet_seen_by_the_store(monkeypatch):
    gate = threading.Event()

    def run(agent, **kw):
        agent.session_id = "s1-tip"
        agent.stream_delta_callback("rotated")
        gate.wait(3)
        return {}

    manager, _ = _rotating_mgr(monkeypatch, run)
    stream = manager.start(session_id="s1", text="long", endpoint=_ep())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and manager.live_for("s1-tip") is None:
        time.sleep(0.005)
    try:
        # Simulate the DB lineage query still returning only the old/root id.
        assert manager.forget_sessions(["s1"]) is False
    finally:
        gate.set()
    assert _wait_done(stream)
