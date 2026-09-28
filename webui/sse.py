"""Writing one `TurnStream` out as Server-Sent Events, on a request thread.

A faithful port of the asyncio writer. The mechanism changed — a blocking
`stream.wait()` instead of parking on an `asyncio.Event`, and `wfile.write`
instead of `await resp.write` — and nothing else did. Every rule below was
earned by a reader-visible failure, so each is restated here rather than left
to be rediscovered:

* **`id:` on every frame.** EventSource replays the last id it saw as
  `Last-Event-ID` when it reconnects on its own. Without it a reconnect reuses
  the URL's original `after_seq` and re-delivers the whole turn — which is how
  the same prompt ended up rendered twice.
* **A cursor we cannot honour is refused, not adopted.** Unparseable, or AHEAD
  of anything this stream emitted (a stale id from a previous process, another
  stream's), would skip every frame up to it — the terminal event included —
  and leave the reader stalled on keepalives. Replaying is cheap; a silent hole
  is not.
* **Drain, then drain again.** The pending list is fixed before the first write,
  so a turn that finishes during those writes lands its terminal event outside
  the pass. Park only when there is genuinely nothing left.
* **Every reader leaves having been told the turn ended.** Closing silently
  leaves the browser believing the turn is live, reconnecting every 750 ms with
  the composer locked.
"""

from __future__ import annotations

import json
import queue

SSE_HEARTBEAT_SEC = 15.0

# Chrome's default reconnect delay is 3 s. Everything here is one hop away, so a
# stall that long is all overhead.
RETRY_MS = 750

SSE_HEADERS = [
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Cache-Control", "no-cache, no-transform"),
    # Nginx buffers text/event-stream by default, which turns a live stream into
    # one delivery at the end.
    ("X-Accel-Buffering", "no"),
]


def frame(obj: dict) -> bytes:
    return (
        b"id: "
        + str(obj.get("seq", 0)).encode()
        + b"\ndata: "
        + json.dumps(obj, ensure_ascii=False).encode()
        + b"\n\n"
    )


def resolve_cursor(after: int, last_event_id: str | None, stream_seq: int) -> int:
    """Where this reader actually resumes.

    A browser reconnect carries where it got to; the query parameter is only the
    opening position of a fresh attach. Pure so the fail-closed rule can be
    tested without a socket.
    """
    if not last_event_id:
        return after
    try:
        cursor = int(last_event_id)
    except ValueError:
        return after
    return cursor if 0 <= cursor <= stream_seq else after


def write_stream(stream, write, after: int = 0, last_event_id: str | None = None) -> None:
    """Pump `stream` into `write` until the turn ends or the reader goes away.

    `write` takes bytes and may raise when the peer disconnects — that is the
    normal way this ends and the caller treats it as such. It must NOT be
    buffered past return: a reader watching a slow turn sees nothing otherwise.
    """
    after = resolve_cursor(after, last_event_id, stream.seq)

    write(f"retry: {RETRY_MS}\n\n".encode())

    if stream.gap_before(after):
        write(frame({"kind": "gap", "seq": after,
                     "text": "some output was dropped while disconnected"}))

    while True:
        pending = stream.after(after)
        if pending:
            for ev in pending:
                write(frame(ev))
                after = ev["seq"]
                if ev["kind"] == "end":
                    return
            continue

        if not stream.running:
            # Nothing pending and the turn is over. Because the branch above
            # returns the moment it writes `end`, reaching here means this
            # reader was sent no terminal frame — the ordinary reload, where the
            # turn finished while the page was away. Re-read once before
            # concluding: the list above was fixed before its writes, so a turn
            # that finished during them left its events behind this pass.
            if stream.after(after):
                continue
            write(frame({"kind": "end", "seq": stream.seq, "error": stream.error}))
            return

        if not stream.wait(after, timeout=SSE_HEARTBEAT_SEC):
            # A comment line: EventSource ignores it, proxies see traffic. A long
            # prefill can say nothing for minutes and a proxy will drop a
            # connection that says nothing at all.
            write(b": keepalive\n\n")


def named_frame(event: str, data: dict) -> bytes:
    """One named SSE event for the persistent session control channel."""
    return (
        b"event: " + event.encode("utf-8")
        + b"\ndata: " + json.dumps(data, ensure_ascii=False).encode("utf-8")
        + b"\n\n"
    )


def write_session_stream(
    subscriber,
    write,
    *,
    initial: dict | None = None,
    heartbeat_s: float = SSE_HEARTBEAT_SEC,
) -> None:
    """Write a long-lived session-scoped control stream.

    Unlike a turn stream this channel has no terminal frame: it survives the
    end of one turn so it can announce a later server-created continuation.
    ``initial`` is the atomic on-subscribe live snapshot and closes the only
    race where a broadcast could precede EventSource registration.
    """
    write(f"retry: {RETRY_MS}\n\n".encode())
    write(named_frame("initial", {}))
    if initial is not None:
        write(named_frame("server_turn_started", initial))
    while True:
        try:
            event, data = subscriber.get(timeout=heartbeat_s)
        except queue.Empty:
            write(b": keepalive\n\n")
            continue
        write(named_frame(str(event), data if isinstance(data, dict) else {}))
