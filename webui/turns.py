"""Starting, tracking and stopping turns — the piece the HTTP layer talks to.

Two admission rules, and they refuse for different reasons because the client
does different things with each:

* **taken** — this CONVERSATION is already replying. The message was never read
  by a model, so the client must be able to put it back in the composer rather
  than believe it was sent.
* **busy** — this ENDPOINT is at capacity. A backstop: the client is told the
  limit by `/api/sessions` and blocks its own composer, so reaching here is a
  race, not the normal path.

The limit is per endpoint because the real ceiling is the model behind it. A
24 GB card running `--max-running-requests 1` and whatever serves the next
endpoint are different numbers, and one global limit can only be right for one
of them.

`run_conversation` blocks, so a turn occupies a thread for its whole life —
but it runs on `_TurnPool`, a fixed set of daemon workers sized to the
admission budget (Σ `max_concurrent`), not a fresh `threading.Thread` per
turn. Daemon on purpose: `concurrent.futures` workers are non-daemon and
joined at interpreter exit on 3.9+ (bpo-39812), and a rollout's SIGTERM must
drop an in-flight turn, not wait out the approval timeout it may be parked
in. Admission still counts live turns per endpoint — the pool buys reuse and
a named worker set, it is not the limit.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from hermes_agent import (
    PROCESS_WAKEUP_SOURCE,
    AgentPool,
    Endpoint,
    history_for,
    run_turn,
)
from profiles import AgentProfile
from turn_stream import EventSink, TurnStream

log = logging.getLogger("deepwiki.turns")


class Refused(Exception):
    """Admission refused. `reason` is what the client branches on."""

    def __init__(self, reason: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.extra = extra

    def as_json(self) -> dict:
        return {"error": self.message, self.reason: True, **self.extra}


# How long a permission card waits before the turn gives up on it. Long,
# because the person it is asking may be away from the tab; bounded, because a
# turn blocked forever holds its thread and its agent.
APPROVAL_TIMEOUT_S = int(
    os.environ.get("DEEPWIKI_APPROVAL_TIMEOUT_S",
                   os.environ.get("BUDA_APPROVAL_TIMEOUT_S", "600"))
)


@dataclass(eq=False)
class _SessionRoute:
    """Where a Hermes background-process completion should resume.

    `session_key` is captured when the terminal tool starts the process, while
    context compression can rotate the conversation id before that process
    exits.  Every old id therefore remains an alias and `session_id` tracks the
    current tip that the synthetic turn must actually continue.
    """

    session_id: str
    endpoint: Endpoint
    user_id: str = ""
    profile: AgentProfile | None = None
    aliases: set[str] = field(default_factory=set)
    pending: deque[dict] = field(default_factory=deque)
    pending_ids: set[str] = field(default_factory=set)


class _TurnPool:
    """Fixed daemon workers pulling from a queue.

    A hand-rolled pool, not `concurrent.futures.ThreadPoolExecutor`, for one
    property: these workers are daemon threads. Executor workers are joined at
    interpreter exit since 3.9 (bpo-39812), and SIGTERM here means "lose the
    turn" — a turn parked in a 600 s approval wait must not hold a rollout.

    Sized by the caller to the admission budget, so an admitted turn always
    finds a worker. `submit` still checks: if every worker is somehow busy
    (endpoints grew after boot), the turn spawns its own daemon thread rather
    than queueing behind a turn that may outlast the reader's patience.
    Liveness beats tidiness.
    """

    def __init__(self, workers: int, prefix: str = "turn") -> None:
        self._q: "queue.SimpleQueue[tuple[str, Any, tuple]]" = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._busy = 0
        self._workers = max(1, workers)
        for i in range(self._workers):
            threading.Thread(target=self._loop, name=f"{prefix}-{i}", daemon=True).start()

    def _loop(self) -> None:
        while True:
            name, target, args = self._q.get()
            with self._lock:
                self._busy += 1
            # Carry the turn's stream id into the thread name while it runs —
            # py-spy dumps and log lines keep pointing at a stream — then
            # restore it, because this worker outlives many turns.
            t = threading.current_thread()
            base = t.name
            try:
                t.name = name
                target(*args)
            except BaseException:  # noqa: BLE001 -- a dead worker must not die
                log.exception("turn worker crashed")
            finally:
                t.name = base
                with self._lock:
                    self._busy -= 1

    def submit(self, name: str, target: Any, *args: Any) -> None:
        """Run target(name-bearing) on a worker, or on a spare daemon thread
        if every worker is busy. Never blocks the caller, never queues behind
        a turn whose stream may already be abandoned."""
        with self._lock:
            if self._busy < self._workers:
                self._q.put((name, target, args))
                return
        log.warning("turn pool (%d workers) saturated; spawning a spare thread", self._workers)
        threading.Thread(target=target, args=args, name=name, daemon=True).start()


class TurnManager:
    def __init__(
        self,
        pool: AgentPool,
        *,
        history: Callable[[str], list] = history_for,
        run: Callable[..., dict] = run_turn,
        workers: int = 8,
    ) -> None:
        # 8 is a tests-and-small-deployments default; main() passes the exact
        # admission budget (Σ max_concurrent), which is the number a turn can
        # never exceed.
        self._turns = _TurnPool(workers)
        self._pool = pool
        self._history = history
        self._run = run
        self._lock = threading.Lock()
        # One contextvar token per live turn, so teardown resets exactly the
        # binding that turn made and not a later one's.
        self._approval_tokens: dict[str, Any] = {}
        # By SESSION, not one global: a reader looking at an idle conversation
        # while another streams must see THAT conversation's state. Deriving it
        # from a global is how a busy session's spinner lands on an idle one.
        self._live: dict[str, TurnStream] = {}
        self._streams: dict[str, TurnStream] = {}
        # Hermes' CLI/Gateway owns this routing in upstream.  This WebUI calls
        # AIAgent directly, so it must remember enough of the originating turn
        # to turn process_registry completion events into a later turn itself.
        self._routes: dict[str, _SessionRoute] = {}
        self._delivered_completions: set[str] = set()
        # A tombstone prevents a late process exit from recreating a
        # conversation the reader has just deleted.
        self._forgotten_sessions: set[str] = set()
        # Persistent browser discovery lives at the CONVERSATION scope, not
        # the turn scope.  A notify_on_complete continuation has no
        # /api/chat/start response for the tab to learn its new stream id
        # from, and the previous turn's SSE is already closed.  Each queue is
        # one open /api/session/stream EventSource; absent subscribers cost no
        # retained channel object.
        self._session_subscribers: dict[str, set[queue.Queue]] = {}
        # WHY THE LAST FAILURE OUTLIVES ITS STREAM.
        #
        # A turn that dies tells its reader through the stream — and the stream
        # is in memory, so that telling reaches only a reader who is watching
        # at that moment. Reload, or be looking at another conversation, and
        # the failure is gone: hermes persists the prompt and nothing else, so
        # the conversation reopens as a question with no answer and a 就绪
        # status, which reads as the app losing the reply rather than the
        # engine refusing it. Production: mm2 crash-looped for three hours
        # answering `503 model is still loading`, and the webui showed a blank
        # transcript with no hint that anything had gone wrong.
        #
        # Keyed by SESSION, not by stream, because that is what the reader
        # comes back to. Cleared when that conversation's next turn starts —
        # a successful retry must not leave a stale ghost above it.
        self._last_error: dict[str, dict] = {}

    # ── queries ────────────────────────────────────────────────────────────

    def stream(self, stream_id: str) -> TurnStream | None:
        with self._lock:
            return self._streams.get(stream_id)

    def live_for(self, session_id: str) -> TurnStream | None:
        with self._lock:
            s = self._live.get(session_id)
            return s if s is not None and s.running else None

    def profile_of(self, session_id: str) -> str | None:
        """The project whose agent is writing into `session_id`, or None.

        The one answer about a conversation that needs no store and no side
        table: the turn was admitted with a profile and still holds it. It
        exists for the id compression rotates a live turn onto — that id has
        no row yet and no pin under it, so every persisted answer about it is
        "unknown", which the sidebar reads as the DEFAULT project. A general
        conversation would appear in 佛典检索's sidebar for the rest of the
        turn, and a send from there would be answered by buda's agent.

        Not gated on `running`: a turn that just ended still answers for the
        conversation it was writing until `forget_finished` drops it, and the
        store row that replaces this appears in the same moment.
        """
        with self._lock:
            stream = self._live.get(session_id)
        return (getattr(stream, "profile_key", "") or None) if stream is not None else None

    def running(self, user_id: str = "") -> dict[str, str]:
        """session_id -> stream_id for every turn still going."""
        with self._lock:
            return {
                sid: s.stream_id
                for sid, s in self._live.items()
                if s.running and (not user_id or s.user_id == user_id)
            }

    def owner_of(self, session_id: str) -> str | None:
        with self._lock:
            stream = self._live.get(session_id)
            if stream is None:
                stream = next((s for s in self._streams.values() if session_id in s.session_ids), None)
        return stream.user_id if stream is not None else None

    def running_on(self, endpoint_key: str) -> int:
        with self._lock:
            return sum(
                1
                for s in self._live.values()
                if s.running and getattr(s, "endpoint_key", None) == endpoint_key
            )

    def subscribe_session(self, session_id: str) -> tuple[queue.Queue, dict | None]:
        """Subscribe to server-created turns and snapshot any live one.

        Registration and the live snapshot share ``_lock`` with ``start``.
        Thus a turn either appears in ``initial`` or is broadcast to the new
        queue; it cannot fall into the gap between those operations.  A
        duplicate at the boundary is harmless because stream ids are stable
        and the browser's attach path is idempotent.
        """
        subscriber: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._session_subscribers.setdefault(session_id, set()).add(subscriber)
            stream = self._live.get(session_id)
            initial = (
                self._session_turn_event(stream, recovered=True)
                if stream is not None and stream.running
                else None
            )
        return subscriber, initial

    def unsubscribe_session(self, session_id: str, subscriber: queue.Queue) -> None:
        with self._lock:
            listeners = self._session_subscribers.get(session_id)
            if listeners is None:
                return
            listeners.discard(subscriber)
            if not listeners:
                self._session_subscribers.pop(session_id, None)

    @staticmethod
    def _session_turn_event(stream: TurnStream, *, recovered: bool = False) -> dict:
        event = {
            "sessionId": stream.session_id,
            "streamId": stream.stream_id,
        }
        if recovered:
            event["recovered"] = True
        return event

    def _publish_session_turn_started(self, stream: TurnStream) -> int:
        """Fan a new server-created turn out to every tab on this session."""
        with self._lock:
            listeners = tuple(self._session_subscribers.get(stream.session_id, ()))
        event = ("server_turn_started", self._session_turn_event(stream))
        delivered = 0
        for subscriber in listeners:
            try:
                subscriber.put_nowait(event)
                delivered += 1
            except queue.Full:
                # There is only one meaningful state here: the current live
                # stream. Replace a stale unread announcement with the newest;
                # reconnect self-heals from the live snapshot as a backstop.
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(event)
                    delivered += 1
                except (queue.Empty, queue.Full):
                    pass
        return delivered

    def forget_finished(self, keep: int = 200) -> None:
        """Drop the oldest finished streams. A finished stream is kept so a
        reader who was away can still collect its tail; it is not kept forever."""
        with self._lock:
            done = sorted(
                (s for s in self._streams.values() if not s.running and s.finished_at),
                key=lambda s: s.finished_at or 0.0,
            )
            for s in done[: max(0, len(done) - keep)]:
                self._streams.pop(s.stream_id, None)
                if self._live.get(s.session_id) is s:
                    self._live.pop(s.session_id, None)

    # ── starting ───────────────────────────────────────────────────────────

    def start(
        self,
        *,
        session_id: str,
        text: str,
        endpoint: Endpoint,
        user_id: str = "",
        profile: AgentProfile | None = None,
        internal: bool = False,
        _completion_route: _SessionRoute | None = None,
    ) -> TurnStream:
        """Admit and launch one turn. Raises `Refused` if it cannot start."""
        with self._lock:
            if internal and (
                _completion_route is None
                or self._routes.get(session_id) is not _completion_route
                or session_id in self._forgotten_sessions
            ):
                raise Refused("gone", "这个会话已删除，后台完成通知不再续跑")
            current = self._live.get(session_id)
            if current is not None and current.running:
                raise Refused(
                    "taken",
                    "这个会话正在回复中（另一个窗口发起的），消息未发出",
                    sessionId=session_id,
                    streamId=current.stream_id,
                )
            running = sum(
                1
                for s in self._live.values()
                if s.running and getattr(s, "endpoint_key", None) == endpoint.key
            )
            if running >= endpoint.max_concurrent:
                raise Refused(
                    "busy",
                    f"{endpoint.label} 已有 {running} 个会话在回复，达到上限 "
                    f"{endpoint.max_concurrent}",
                    running=running,
                    maxConcurrent=endpoint.max_concurrent,
                )
            # This conversation is being tried again; whatever went wrong last
            # time is no longer what the reader needs to see.
            self._last_error.pop(session_id, None)
            stream = TurnStream(secrets.token_hex(8), session_id, user_id=user_id)
            # Which endpoint this turn is on, so the per-endpoint count above can
            # be taken without reaching back into the agent.
            stream.endpoint_key = endpoint.key  # type: ignore[attr-defined]
            # And which PROJECT is answering it. `_rotated` re-keys `_live`
            # under the id compression moved the conversation to, and that id
            # has neither a store row (hermes persists at the end of the turn)
            # nor a pin (the pin was written under the id chat/start was given).
            # Carried on the stream, the answer moves with the conversation.
            stream.profile_key = profile.key if profile is not None else ""  # type: ignore[attr-defined]
            # `stream.finish()` precedes approval/session-context teardown.
            # Completion dispatch must wait for the latter too, otherwise it
            # can acquire and rebind the same cached agent while the old turn
            # is still unwinding.
            stream.turn_settled = False  # type: ignore[attr-defined]
            self._streams[stream.stream_id] = stream
            self._live[session_id] = stream
            route = _completion_route if internal else self._routes.get(session_id)
            if route is None:
                route = _SessionRoute(session_id, endpoint, user_id, profile)
            route.session_id = session_id
            route.endpoint = endpoint
            route.user_id = user_id
            route.profile = profile
            route.aliases.add(session_id)
            for alias in route.aliases:
                self._routes[alias] = route
                self._forgotten_sessions.discard(alias)

        # The prompt is echoed into the log FIRST, so a reader attaching to this
        # stream sees the question above the answer even if they arrive late.
        if internal:
            stream.emit("note", text=f"后台任务已完成，agent 自动继续：\n\n{text}")
        else:
            stream.emit("user", text=text)

        # Only server-created turns need discovery. Browser-created turns get
        # the same stream id directly from /api/chat/start; broadcasting those
        # would race the optimistic user-message echo in send().
        if internal:
            self._publish_session_turn_started(stream)

        self._turns.submit(
            f"turn-{stream.stream_id}", self._run_turn,
            stream, session_id, text, endpoint, profile, user_id,
            PROCESS_WAKEUP_SOURCE if internal else None,
        )
        return stream

    def _install_approval(self, stream: TurnStream) -> None:
        """Route this turn's permission prompts to this turn's reader.

        Through hermes' GATEWAY path, not `terminal_tool.set_approval_callback`.
        That setter stores the callback in a `threading.local` — deliberately,
        it is a fix for concurrent ACP sessions sharing one slot — and hermes
        dispatches tools on threads of its own. The callback was therefore
        invisible to the thread that actually asked, hermes fell through to its
        `input()` fallback, and that spawns a daemon thread reading a stdin no
        one will ever type into. Measured: a turn stopped at
        `terminal / running`, emitted nothing for minutes, held the endpoint's
        only slot, and `interrupt()` could not touch it because interrupt sets
        a flag the conversation loop reads and the loop was never reached. The
        module's own comment calls this "an invisible 60s deadlock".

        The gateway path is module-global instead: the session key is a
        contextvar (inherited by threads hermes spawns), the pending entry goes
        into `_gateway_queues`, and `resolve_gateway_approval` unblocks it from
        whichever thread the HTTP handler happens to be on. This is what
        hermes-webui does, for the same reason.

        `HERMES_GATEWAY_SESSION` is the switch `_is_gateway_approval_context()`
        reads; `HERMES_INTERACTIVE` tells the non-interactive auto-approve path
        to stay out of it.
        """
        import os

        try:
            from tools import approval as ap

            os.environ["HERMES_GATEWAY_SESSION"] = "1"
            os.environ["HERMES_INTERACTIVE"] = "1"
            key = self._approval_key(stream)
            # Pinned. hermes queues approvals under HERMES_SESSION_KEY, which
            # compression does not move (it moves HERMES_SESSION_ID), so after a
            # rotation the queue, the notify and the teardown all stay here.
            stream.approval_key = key  # type: ignore[attr-defined]

            # Hermes 0.17 propagates contextvars to tool workers. A process-wide
            # HERMES_SESSION_KEY would cross users when turns run concurrently.
            if not stream.user_id:  # legacy mode compatibility
                os.environ["HERMES_SESSION_KEY"] = key
            self._approval_tokens[stream.stream_id] = ap.set_current_session_key(key)
            ap.register_gateway_notify(key, lambda data: self._notify_approval(stream, key, data))
            log.info("approval hook armed for %s (gateway)", key)
        except Exception:  # noqa: BLE001 -- a missing hook must not fail the turn
            log.warning("could not install the approval hook", exc_info=True)

    @staticmethod
    def _approval_key(stream: TurnStream) -> str:
        """One key per CONVERSATION, so "allow for this session" means what a
        reader thinks it means and a reconnect finds its own pending card."""
        return str(getattr(stream, "approval_key", None) or stream.session_id or stream.stream_id)

    def approval_key_for(self, session_id: str) -> str:
        """The gateway queue a conversation's approvals wait in. The session id
        itself — unless its live turn rotated, in which case the queue is still
        under the id the turn started with."""
        live = self.live_for(session_id)
        return self._approval_key(live) if live is not None else session_id

    def owner_of_approval_key(self, key: str) -> str | None:
        with self._lock:
            for stream in self._live.values():
                if self._approval_key(stream) == key:
                    return stream.user_id
        return None

    @staticmethod
    def _resolve_notified_approval(ap: Any, key: str, data: dict, choice: str) -> bool:
        """Resolve the exact gateway entry which produced ``data``.

        Hermes' public resolver is intentionally FIFO. That is right for a
        human pressing the one card displayed by the UI, but wrong for an
        automatic smart-DENY response: parallel tool calls can enqueue a
        normal request before the denied one, and resolving the queue head
        would answer somebody else's request. Hermes 0.19 passes the same
        payload object stored on ``_ApprovalEntry.data`` to the notify hook,
        so identity gives us an unambiguous correlation without exposing a
        new ID to the browser.

        Return False if the pinned 0.19 queue surface is unavailable. The
        caller then leaves the request for manual review; it must never fall
        back to a potentially wrong FIFO denial.
        """
        lock = getattr(ap, "_lock", None)
        queues = getattr(ap, "_gateway_queues", None)
        if lock is None or not isinstance(queues, dict):
            return False
        entry = None
        with lock:
            queue = queues.get(key) or []
            entry = next(
                (candidate for candidate in queue
                 if getattr(candidate, "data", None) is data),
                None,
            )
            if entry is None:
                return False
            queue.remove(entry)
            if not queue:
                queues.pop(key, None)
            entry.result = choice
        entry.event.set()
        return True

    def _notify_approval(self, stream: TurnStream, key: str, data: dict) -> None:
        """hermes has something to ask. Put it on this turn's stream.

        Runs on the agent's thread, inside the wait — emitting here is what
        makes the card appear while the turn is still blocked, which is the
        whole point.
        """
        title = str(data.get("description") or data.get("command") or "需要确认")
        if data.get("smart_denied"):
            try:
                from tools import approval as ap

                if self._resolve_notified_approval(ap, key, data, "deny"):
                    log.info("smart approval denied a request for %s", key)
                    stream.emit("note", text=f"智能审批已拒绝：{title}")
                    return
            except Exception:  # noqa: BLE001 -- fail to manual, never wrong FIFO
                log.warning("could not resolve smart denial for %s", key, exc_info=True)
            log.warning(
                "smart denial for %s could not be correlated; leaving it for manual review",
                key,
            )

        opts = [
            {"optionId": "once", "name": "允许一次"},
            {"optionId": "session", "name": "本次会话都允许"},
        ]
        if not stream.user_id and data.get("allow_permanent", True):
            opts.append({"optionId": "always", "name": "始终允许"})
        opts.append({"optionId": "deny", "name": "拒绝"})
        stream.emit(
            "approval",
            id=key,                      # resolve_gateway_approval keys on this
            title=title,
            command=str(data.get("command") or ""),
            options=opts,
        )
        status_note = f"需要你确认：{title}"
        stream.emit("note", text=status_note)

    def _release_approval(self, stream: TurnStream) -> None:
        """Drop the turn's approval wiring, and deny anything still pending.

        A turn that ended with a card still on screen would otherwise leave an
        entry in `_gateway_queues` that nothing will ever answer, and the next
        approval for this conversation would queue behind it.
        """
        try:
            from tools import approval as ap

            key = self._approval_key(stream)
            ap.resolve_gateway_approval(key, "deny", resolve_all=True)
            if hasattr(ap, "unregister_gateway_notify"):
                ap.unregister_gateway_notify(key)
            tok = self._approval_tokens.pop(stream.stream_id, None)
            if tok is not None:
                ap.reset_current_session_key(tok)
        except Exception:  # noqa: BLE001
            log.debug("approval teardown skipped", exc_info=True)

    def _rotated(self, stream: TurnStream, old: str, new: str) -> None:
        """hermes compressed the context and continued under a new session id.

        Everything keyed by the conversation moves with it: the live map (so
        the sidebar marks the new row, and a second prompt into it is `taken`)
        and the agent cache (so the next turn under the new id continues with
        the agent that rotated, reading the child session's history). The old
        row stays in the store as the parent; nothing here deletes it.
        """
        with self._lock:
            stream.session_ids.update((old, new))
            if self._live.get(old) is stream:
                self._live.pop(old, None)
                self._live[new] = stream
            route = self._routes.get(old)
            if route is not None:
                route.session_id = new
                route.aliases.update((old, new))
                self._routes[old] = route
                self._routes[new] = route
                self._forgotten_sessions.discard(new)
        self._pool.rename(old, new)
        log.info("session %s rotated to %s mid-turn (stream %s)", old, new, stream.stream_id)

    def _run_turn(
        self, stream: TurnStream, session_id: str, text: str,
        endpoint: Endpoint, profile: AgentProfile | None = None, user_id: str = "",
        user_source: str | None = None,
    ) -> None:
        agent = None
        session_tokens = None
        from profile_skills import enter, leave
        skill_profile = enter(profile.key if profile is not None else None)
        try:
            # Advertise a real async-delivery route even in legacy/basic-auth
            # mode, where user_id is empty.  Hermes otherwise disables the
            # notify_on_complete flag and tells the model to poll, so merely
            # adding a queue consumer would never receive an event there.
            try:
                from gateway.session_context import set_session_vars
                session_tokens = set_session_vars(
                    platform="deepwiki", user_id=user_id,
                    session_key=session_id, session_id=session_id,
                    async_delivery=True,
                )
            except ImportError:
                # Unit tests intentionally run without the Hermes package.
                # An authenticated production path already required this
                # module before this change, so preserve its fail-closed rule.
                if user_id:
                    raise
            agent = (self._pool.acquire(session_id, endpoint, profile, user_id=user_id)
                     if user_id else self._pool.acquire(session_id, endpoint, profile))
            self._pool.note_running(stream.stream_id, agent)
            self._install_approval(stream)
            # After the approval key is pinned: it stays the id the hook was
            # registered under even if hermes rotates the session.
            stream.follow(
                lambda: getattr(agent, "session_id", None),
                lambda old, new: self._rotated(stream, old, new),
            )
            from hermes_agent import bind_callbacks

            # EVERY turn, cached agent or fresh: a reused agent still carries the
            # previous turn's callbacks, which captured a stream nobody reads.
            bind_callbacks(agent, EventSink(stream))
            history = self._history(session_id)
            # Only the current turn's image markers are hydrated. Historical
            # messages remain lightweight object-key references until the
            # model explicitly calls input_image_open for one of them.
            from media_input import model_user_content, marker_keys, owned_key
            if user_id and any(not owned_key(k, user_id) for k in marker_keys(text)):
                raise ValueError("input image is not owned by this user")

            model_message = model_user_content(text)
            # The profile's brief, on every turn, for the same reason
            # CHAT_DIRECTIVE is: hermes replays the system prompt verbatim to
            # keep the upstream prompt cache warm, so a constant string costs
            # nothing, and a first-turn-only injection would be missing from
            # any session whose first turn predates it. `directive` is None
            # for a profile that declares none — run_turn then falls back to
            # CHAT_DIRECTIVE exactly as before.
            directive = profile.directive if profile is not None else None
            if user_id:
                from hermes_agent import CHAT_DIRECTIVE, _artifacts_root
                directive = (CHAT_DIRECTIVE if directive is None else directive) + (
                    f"\nStore generated artifacts under {_artifacts_root() / session_id}/. "
                    f"Link to them as /artifacts/{session_id}/<filename>. "
                    "Files outside this session directory are not published to the user."
                )
            self._run(
                agent, session_id=session_id, user_message=model_message, history=history,
                system_message=directive,
                persist_user_message=text,
                user_source=user_source,
            )
            stream.finish()
        except Exception as e:  # noqa: BLE001
            # The reader must be told. A turn that dies silently leaves the
            # composer locked and the spinner running until EventSource gives up.
            log.exception("turn %s failed", stream.stream_id)
            msg = str(e) or e.__class__.__name__
            stream.finish(error=msg)
            # Outlive the stream, so a reader who was not watching still finds
            # out. Bounded: one entry per session that failed, oldest dropped.
            with self._lock:
                self._last_error[session_id] = {"text": msg, "at": time.time()}
                while len(self._last_error) > 200:
                    self._last_error.pop(next(iter(self._last_error)))
        finally:
            leave(skill_profile)
            self._release_approval(stream)
            self._pool.clear_running(stream.stream_id)
            if session_tokens is not None:
                from gateway.session_context import clear_session_vars
                clear_session_vars(session_tokens)
            stream.turn_settled = True  # type: ignore[attr-defined]

    # ── background process completions ────────────────────────────────────

    @staticmethod
    def _completion_id(event: dict) -> str:
        # Hermes calls the background process id `session_id` in completion
        # events.  It is unrelated to the chat session in `session_key`.
        return str(event.get("session_id") or "")

    @staticmethod
    def _clear_completion_watcher(registry: Any, process_id: str) -> None:
        """Remove Hermes' now-satisfied async-delivery watcher, if exposed.

        Hermes 0.19 keeps this compatibility map for Gateway delivery.  The
        WebUI is the delivery loop here, so leaving entries in it would grow
        one stale watcher per notified process.  The attribute is private and
        has moved before, hence the deliberately defensive cleanup.
        """
        watchers = getattr(registry, "pending_watchers", None)
        if isinstance(watchers, dict):
            watchers.pop(process_id, None)
        elif isinstance(watchers, list):
            # Hermes 0.19 stores one dict per gateway watcher.  The terminal
            # tool appends before the process can complete, so by the time the
            # queue event exists an in-place filter cannot race that append.
            watchers[:] = [
                watcher for watcher in watchers
                if str(watcher.get("session_id") or "") != process_id
            ]

    @staticmethod
    def _completion_consumed(registry: Any, process_id: str) -> bool:
        check = getattr(registry, "is_completion_consumed", None)
        if not process_id or not callable(check):
            return False
        try:
            return bool(check(process_id))
        except Exception:  # noqa: BLE001 -- a compatibility check must not stop delivery
            log.debug("could not check completion consumption for %s", process_id, exc_info=True)
            return False

    def _queue_completion(self, event: dict, registry: Any) -> None:
        session_key = str(event.get("session_key") or "")
        process_id = self._completion_id(event)
        self._clear_completion_watcher(registry, process_id)
        if event.get("type") not in (None, "completion") or not session_key:
            log.warning("ignoring malformed process completion: %r", event)
            return
        if self._completion_consumed(registry, process_id):
            log.info("process %s completion was already consumed; not auto-resuming", process_id)
            return
        with self._lock:
            route = self._routes.get(session_key)
            if route is None or session_key in self._forgotten_sessions:
                log.info("no live route for process %s session %s; dropping completion",
                         process_id, session_key)
                return
            if process_id and (
                process_id in route.pending_ids
                or process_id in self._delivered_completions
            ):
                return
            route.pending.append(dict(event))
            if process_id:
                route.pending_ids.add(process_id)
        log.info("queued completion for process %s on session %s", process_id, session_key)

    def _pending_routes(self) -> list[_SessionRoute]:
        with self._lock:
            unique = {id(route): route for route in self._routes.values()}
            return [route for route in unique.values() if route.pending]

    def _dispatch_pending_completions(
        self,
        registry: Any,
        formatter: Callable[[dict], str],
    ) -> int:
        """Start every completion whose conversation and endpoint are idle.

        Refusal is normal: the originating turn may still be unwinding, or a
        different conversation may occupy the endpoint.  The event stays at
        the head of its per-conversation queue and a later dispatcher tick
        retries it, preserving completion order without interrupting a turn.
        """
        started = 0
        for route in self._pending_routes():
            with self._lock:
                if not route.pending or self._routes.get(route.session_id) is not route:
                    continue
                current = self._live.get(route.session_id)
                if current is not None and (
                    current.running or not getattr(current, "turn_settled", True)
                ):
                    continue
                event = route.pending[0]
                process_id = self._completion_id(event)
            if self._completion_consumed(registry, process_id):
                with self._lock:
                    if route.pending and route.pending[0] is event:
                        route.pending.popleft()
                        route.pending_ids.discard(process_id)
                continue
            try:
                text = formatter(event)
                stream = self.start(
                    session_id=route.session_id,
                    text=text,
                    endpoint=route.endpoint,
                    user_id=route.user_id,
                    profile=route.profile,
                    internal=True,
                    _completion_route=route,
                )
            except Refused:
                continue
            except Exception:  # noqa: BLE001 -- one bad event must not kill the dispatcher
                log.exception("could not dispatch process %s completion", process_id)
                continue
            with self._lock:
                if route.pending and route.pending[0] is event:
                    route.pending.popleft()
                    route.pending_ids.discard(process_id)
                if process_id:
                    self._delivered_completions.add(process_id)
                    # A long-lived pod should not retain an unbounded history;
                    # the registry's own consumed set remains the authority.
                    while len(self._delivered_completions) > 4096:
                        self._delivered_completions.pop()
            started += 1
            log.info("auto-resumed session %s for process %s on stream %s",
                     route.session_id, process_id, stream.stream_id)
        return started

    def run_completion_dispatcher(
        self,
        stop: threading.Event,
        *,
        registry: Any = None,
        formatter: Callable[[dict], str] | None = None,
        poll_s: float = 0.25,
    ) -> None:
        """Drain Hermes completions until `stop` is set.

        Dependency injection keeps the loop testable outside Hermes' venv;
        production resolves the exact 0.19 process registry used by terminal.
        """
        if registry is None or formatter is None:
            try:
                from tools.process_registry import format_process_notification, process_registry
            except Exception:  # noqa: BLE001 -- server stays up, log the lost capability
                log.exception("process completion dispatcher unavailable")
                return

            registry = process_registry if registry is None else registry
            formatter = format_process_notification if formatter is None else formatter
        completions = registry.completion_queue
        while not stop.is_set():
            try:
                event = completions.get(timeout=poll_s)
            except queue.Empty:
                event = None
            except Exception:  # noqa: BLE001 -- keep later notifications alive
                log.exception("process completion queue read failed")
                stop.wait(poll_s)
                event = None
            if isinstance(event, dict):
                self._queue_completion(event, registry)
            self._dispatch_pending_completions(registry, formatter)

    def start_completion_dispatcher(self, stop: threading.Event) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_completion_dispatcher,
            args=(stop,),
            name="process-completions",
            daemon=True,
        )
        thread.start()
        return thread

    def forget_sessions(self, session_ids: list[str]) -> bool:
        """Atomically retire idle conversations before deleting their rows.

        Returns False when any id is still running (including a completion
        turn that won the race with delete), so the caller can refuse deletion
        without corrupting a conversation being persisted.
        """
        with self._lock:
            routes = {
                self._routes[sid]
                for sid in session_ids
                if sid in self._routes
            }
            aliases = set(session_ids)
            for route in routes:
                # A just-created compression tip may not be visible to the DB
                # lineage query yet, while the manager already routes it.  Its
                # aliases are part of the same conversation and must take part
                # in the atomic running check.
                aliases.update(route.aliases)
            if any(
                (stream := self._live.get(sid)) is not None
                and (stream.running or not getattr(stream, "turn_settled", True))
                for sid in aliases
            ):
                return False
            for route in routes:
                route.pending.clear()
                route.pending_ids.clear()
            for alias in aliases:
                self._routes.pop(alias, None)
            self._forgotten_sessions.update(aliases)
            return True

    def last_error(self, session_id: str) -> dict | None:
        """How this conversation's most recent turn failed, if it did.

        Read when the transcript is loaded, so the failure is part of what the
        reader comes back to rather than something only a live watcher saw.
        None once the conversation has been tried again.
        """
        with self._lock:
            return self._last_error.get(session_id)

    # ── stopping ───────────────────────────────────────────────────────────

    def cancel(self, stream_id: str) -> bool:
        """Ask the turn behind `stream_id` to stop.

        `stopped` is set BEFORE interrupting so the death it causes is reported
        as a stop rather than a failure — the reader pressed the button and must
        be told it worked.
        """
        stream = self.stream(stream_id)
        if stream is None or not stream.running:
            return False
        stream.stopped = True
        # Deny anything this conversation is parked on FIRST. `interrupt` only
        # sets a flag the conversation loop reads, and a turn blocked waiting
        # for permission is not in that loop — it is parked on the approval
        # queue, which is exactly where a reader who pressed Stop most wants it
        # to stop. Releasing it lets the turn reach the loop and see the flag.
        try:
            from tools import approval as ap

            n = ap.resolve_gateway_approval(self._approval_key(stream), "deny", resolve_all=True)
            if n:
                stream.emit("note", text="已停止：拒绝了等待中的确认")
        except Exception:  # noqa: BLE001 -- a failed release must not fail the stop
            log.debug("no pending approval to release for %s", stream_id, exc_info=True)
        return self._pool.interrupt(stream_id, "user asked to stop")
