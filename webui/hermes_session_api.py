"""JSON bridge to hermes' own session store — runs in HERMES' interpreter, not ours.

The webui and hermes live in separate venvs deliberately: they share most of
their packages and disagree on some, so importing `hermes_state` in-process
would shadow one venv's deps with the other's. Hence a subprocess:
`<hermes venv>/bin/python hermes_session_api.py`.

Why not parse `hermes sessions list`? It renders a fixed-width table — titles
truncated, times as "3d ago", CJK breaking the column alignment. `SessionDB` is
what that table is rendered FROM, so call it directly and emit JSON.

Read-only. Writes (delete/rename) stay on the `hermes sessions ...` CLI, which
is schema-aware (it also cleans the FTS index and related tables).
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

def _db():
    """hermes' session store. Imported HERE, not at module scope, so the pure
    shaping below can be exercised by tests -- which have no hermes. The module
    still runs only under hermes' interpreter; this just stops an unavailable
    dependency from making the transcript-shaping untestable, and that shaping
    is where the bug this file was last changed for actually lived."""
    from hermes_state import SessionDB  # hermes venv only

    return SessionDB()

# Shared with the LIVE path on purpose. A reload used to re-render the raw
# `{"output":…,"exit_code":…}` with no command above it -- the very shape the
# live fix removed, handed back the moment the reader refreshed. `turn_stream`
# is stdlib-only, so it imports under either venv.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from turn_stream import DETAIL_MAX, _detail_for, _invocation_text, _todo_items  # noqa: E402

# The LIVE path does not derive the command -- hermes hands it one, built by
# `build_tool_preview`. Deriving a second version here produced a different
# string for the same call the moment a tool took more than one argument:
# `terminal` adds a `timeout`, so a reload showed the whole JSON where the live
# row had shown `echo …`. Use hermes' own function, which is what live receives.
# Guarded like every other hermes import here: a version that moved it costs the
# nicer rendering, not the transcript.
try:
    from agent.display import build_tool_preview as _hermes_preview  # noqa: E402
except Exception:  # noqa: BLE001
    _hermes_preview = None


def _provisional_title(preview: str) -> str:
    """A short name from the opening line. Empty stays empty.

    The preview has already had its newlines flattened to spaces, so the break
    between what the reader actually asked and the prompt template that follows
    survives only as a RUN of spaces. Splitting on that recovers the question —
    "六道", not "六道  你是佛教典籍的检索助手，工作是…".
    """
    head = re.split(r"\s{2,}", preview.strip(), maxsplit=1)[0].strip()
    head = head.split("\n", 1)[0].strip()
    if len(head) > 24:
        head = head[:24].rstrip() + "\u2026"
    return head


def list_sessions(limit: int, include_empty: bool) -> list[dict]:
    # exclude_sources=["tool"] mirrors the CLI's default: hide third-party tool
    # sessions, which are not conversations anyone opened.
    #
    # include_children=False + project_compression_tips=True is the CLI's own
    # projection: context compression ROTATES the session id — the old row ends
    # with end_reason='compression' and the conversation continues under a new
    # child — and without the projection every link of that chain showed up as
    # its own sidebar row (five fragments of one conversation in production).
    # The projection collapses each chain to ONE row keyed to its tip, which is
    # where the messages actually live.
    #
    # The opposite shape was tried before and had a real failure: a chain whose
    # tip died mid-turn projected NOWHERE and vanished from the sidebar. But
    # that was the OLD projection; hermes' `list_sessions_rich` keeps the root
    # row when the tip row is missing, and the only true loss is a tip with
    # ZERO messages (compression flushed nothing yet) — covered below by
    # `resolve_resume_session_id`, which walks the chain to the first
    # descendant that holds messages.
    rows = _db().list_sessions_rich(
        source=None, exclude_sources=["tool"], limit=limit,
        include_children=False, project_compression_tips=True,
        order_by_last_active=True,
    )
    out = []
    for r in rows:
        # A session with no messages is a ghost — unless it is a compression
        # root whose messages live in a descendant (the tip flushed nothing
        # yet). `resolve_resume_session_id` (#15000) walks the chain forward;
        # still nothing anywhere, and the drop below is correct. Projected rows
        # carry `_lineage_root_id` when the tip differs from the root.
        if not include_empty and not r.get("message_count"):
            root = r.get("_lineage_root_id") or r.get("id")
            try:
                sid2 = _db().resolve_resume_session_id(root)
                if sid2 and sid2 != root:
                    r = {**r, "id": sid2,
                         "message_count": _db().message_count(sid2)}
            except Exception:  # noqa: BLE001 — an unmapped chain is just a ghost
                pass
        if not include_empty and not r.get("message_count"):
            continue
        # hermes titles a session asynchronously, so a conversation that is
        # minutes old and fifteen messages long can still have none — and the
        # sidebar was calling those "(未命名)" while holding the opening line in
        # `preview`. Stand in with the first thing the reader said, which is
        # what they would call it themselves, until the real title lands.
        title = (r.get("title") or "").strip()
        if not title:
            title = _provisional_title(r.get("preview") or "")
        out.append({
            "id": r.get("id"),
            # The row's own project mark, carried up verbatim. `source` is
            # where hermes stores `agent.platform`, and the webui writes the
            # project into it (hermes_agent.session_mark) precisely so that
            # the answer survives a compression id rotation: the projection
            # above hands back the TIP of a chain, and the tip was written by
            # the same agent as its root. The route turns it into `profile`.
            "source": r.get("source") or "",
            "title": title,
            "titleProvisional": not (r.get("title") or "").strip(),
            "preview": r.get("preview") or "",
            "lastActive": r.get("last_active") or r.get("started_at") or 0,
            "messageCount": r.get("message_count") or 0,
        })
    return out


def resolve_tip(session_id: str) -> str:
    """The id a conversation is listed under today, given an id it once had.

    Compression rotates the id; `list_sessions` projects each chain onto its
    tip. `resolve_resume_session_id` is hermes' own forward walk, and the
    sidebar's fallback for pre-mark sessions uses it to ask "which row is this
    old pin talking about now?". Returns the input unchanged when the chain
    has not moved, which is the common case.
    """
    if not session_id:
        return ""
    try:
        return _db().resolve_resume_session_id(session_id) or session_id
    except Exception:  # noqa: BLE001 -- an unwalkable chain is just an unmarked row
        return session_id


def _tool_calls(raw) -> list[dict]:
    """`tool_calls` comes back as a list, or as its repr. Accept both."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    for parse in (json.loads, ast.literal_eval):
        try:
            v = parse(raw)
            return v if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            continue
    return []


# ── the todo-injection block ──────────────────────────────────────


# After context compression, hermes re-injects the agent's active todo list as
# a USER message (`conversation_compression.py`: `compressed.append({"role":
# "user", "content": todo_snapshot})`). It is an instruction to the MODEL —
# nobody typed it — and rendered as speech it is a wall of `[>]` markers in a
# user bubble, which is exactly how it was reported. Parsed instead: the items
# become the same `todo` event the tool result emits, and the row renders as
# the checklist card it is describing.
TODO_INJECTION_MARK = "[your active task list was preserved across context compression]"
_TODO_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


def _parse_todo_injection(text: str):
    """A post-compression todo snapshot, as plain items — or None.

    Each item line hermes writes is `- [marker] id. content (status)`; the
    trailing `(status)` is the authoritative state and the marker is its
    fallback, because the content may itself end in a bracketed note. Lines
    that parse as nothing are not todos — a list with no items yields None,
    and the caller skips the row entirely.
    """
    t = (text or "").strip()
    if TODO_INJECTION_MARK not in t.lower():
        return None
    out = []
    marker_status = {"x": "completed", ">": "in_progress", "~": "cancelled"}
    for line in t.splitlines():
        m = re.match(r"^\s*- \[([^\]]+)\]\s+(.*)\s+\((\w+)\)\s*$", line)
        if not m:
            continue
        marker, content, status = m.group(1).strip(), m.group(2), m.group(3).lower()
        if status not in _TODO_STATUSES:
            continue
        # Split `id. content`: the id is the dot-delimited head, the rest —
        # which may contain its own dots — is the description.
        head, dot, tail = content.partition(". ")
        item_id = head.strip() if dot else ""
        out.append({
            "id": item_id or marker,
            "content": (tail if dot else content).strip(),
            "status": status,
        })
    return out or None


def _is_compaction_summary(text: str) -> bool:
    """True when a persisted message IS a context-compaction summary, not
    something a human or the model said.

    hermes compresses overflowing history into one message that starts
    `[CONTEXT COMPACTION — REFERENCE ONLY]…` (older builds: `[CONTEXT
    SUMMARY]:`), persists it as an ordinary user/assistant row, and the
    transcript faithfully rendered that instruction block to the reader as if
    it were speech. hermes has its own detector for exactly this — the
    compressor uses it to find summaries it wrote before — and it is preferred
    here so a renamed prefix keeps being recognised; the literal fallbacks
    cover the builds where the helper moved.
    """
    if not isinstance(text, str) or not text:
        return False
    try:
        from agent.context_compressor import ContextCompressor
        return bool(ContextCompressor._is_context_summary_content(text))
    except Exception:  # noqa: BLE001 — the helper moved; fall through to literals
        pass
    t = text.lstrip()
    return t.startswith("[CONTEXT COMPACTION") or t.startswith("[CONTEXT SUMMARY]:")


def _history_lineage(db, sid: str) -> list[str]:
    """Session ids in one compressed conversation, oldest first.

    Hermes compression rotates a session id and links the child through
    ``parent_session_id``. The sidebar deliberately shows only the newest tip,
    but reading that row alone loses every pre-compression message. Hermes has
    a public forward walk to the tip, not the inverse walk needed here, so use
    its SQLite connection just as the delete path does. A malformed/older
    database safely falls back to the requested row.
    """
    if not sid:
        return []
    try:
        rows = db._conn.execute(
            """
            WITH RECURSIVE ancestors(id, parent, depth) AS (
                SELECT id, parent_session_id, 0 FROM sessions WHERE id = ?
              UNION ALL
                SELECT s.id, s.parent_session_id, ancestors.depth + 1
                  FROM sessions s JOIN ancestors ON s.id = ancestors.parent
                 WHERE ancestors.depth < 63
            )
            SELECT id FROM ancestors ORDER BY depth DESC
            """,
            (sid,),
        ).fetchall()
        ids = [row[0] for row in rows]
        return ids or [sid]
    except Exception:  # noqa: BLE001 -- pre-lineage schemas are one-row sessions
        return [sid]


def history(sid: str, limit: int) -> list[dict]:
    """One session's transcript, in the UI's own event shape.

    The point of this command is that it does NOT go through ACP. `session/load`
    is what MOVES the single agent process to a session, and it was the only way
    to read a transcript — so looking at another conversation while one was
    streaming meant yanking the agent out from under the turn in flight, which
    is why the UI had to refuse. Reading from the store instead decouples the
    two: viewing is free, and the agent is moved only when something is sent.

    The mapping mirrors `_to_event` in the webui server, which is what the
    client already renders. Kept lossy in the same way and for the same reason:
    this is a chat box, so assistant text and a one-line trace of tool activity
    is all of it.
    """
    db = _db()
    # The displayed id is normally the compression tip. Replay its ancestors
    # first so a reader sees the one conversation they started, rather than a
    # post-compression fragment beginning with Hermes' private summary.
    msgs = []
    for link in _history_lineage(db, sid):
        msgs.extend(db.get_messages(link) or [])
    if limit and len(msgs) > limit:
        msgs = msgs[-limit:]
    out: list[dict] = []
    # call id -> what it was invoked with, so the result row can show the
    # command above its output the same way a live row does.
    invocations: dict[str, str] = {}
    for m in msgs:
        role = m.get("role")
        text = m.get("content") or ""
        # A compaction summary is an instruction block hermes addressed to the
        # MODEL, persisted as an ordinary message row. Rendered as speech it is
        # a wall of highlighted prose nobody wrote — replace it with a note and
        # skip the block. Only whole-summary rows are skipped here; a summary
        # hermes MERGED into a real user message keeps that message (the prefix
        # rides along, the rarer shape).
        if role in ("user", "assistant") and _is_compaction_summary(text):
            out.append({"kind": "note", "text": "（此前的对话已压缩为上下文摘要）"})
            continue
        if role == "user":
            if text:
                # The todo-injection row is model-facing state, not speech.
                # With items it becomes the checklist card it describes; even
                # without them it is never rendered as something a human said.
                injected = _parse_todo_injection(text)
                if injected is not None:
                    out.append({"kind": "todo", "items": injected})
                    continue
                if TODO_INJECTION_MARK in text.lower():
                    continue
                out.append({"kind": "history_user", "text": text})
        elif role == "assistant":
            if text:
                out.append({"kind": "delta", "text": text, "thought": False})
            for tc in _tool_calls(m.get("tool_calls")):
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                cid = tc.get("id") or tc.get("call_id")
                # `arguments` is persisted as a JSON STRING; unparsed it is
                # noise, and it is the only record of what was run.
                raw = fn.get("arguments")
                call_args = raw
                if isinstance(raw, str):
                    try:
                        call_args = json.loads(raw)
                    except ValueError:
                        call_args = raw
                inv = ""
                if _hermes_preview is not None and isinstance(call_args, dict):
                    try:
                        inv = _hermes_preview(fn.get("name") or "", call_args) or ""
                    except Exception:  # noqa: BLE001
                        inv = ""
                if not inv:
                    inv = _invocation_text(None, call_args)
                if cid:
                    invocations[cid] = inv
                detail = _detail_for(inv, None)
                # A `todo` CALL is the write; the list itself lives in the
                # result and is what the checklist card is drawn from. The
                # call row is not where the plan belongs — the args ARE the
                # JSON, and "$ {…}" is exactly the shape a plan must not
                # take. Named here; summarised on the result row below.
                if (fn.get("name") or "") == "todo":
                    detail = "任务清单"
                out.append({
                    "kind": "tool",
                    "id": cid,
                    "title": fn.get("name") or tc.get("name") or "tool",
                    "status": "pending",
                    "detail": detail[:DETAIL_MAX], "detailFull": len(detail),
                })
        elif role == "tool":
            # The result arrives as its own row and carries the id the call was
            # announced under, so the client merges it into that same line.
            cid = m.get("tool_call_id")
            tool_name = m.get("tool_name") or ""
            detail = _detail_for(invocations.get(cid, ""), text)
            # A `todo` result IS the task list — summarised on the row, replayed
            # whole as the `todo` event the client's checklist card is drawn
            # from, the same shape the live sink emits.
            items = _todo_items(tool_name, text)
            if items is not None:
                done = sum(1 for t in items if t.get("status") == "completed")
                detail = f"任务清单：{len(items)} 项，{done} 已完成"
            # `is_error` is NOT persisted, so a replayed row cannot reproduce
            # hermes' own verdict. An explicit `error` in the payload is a
            # failure by any reading and is honoured; everything else stays
            # `completed` with the exit code visible in the detail. Claiming
            # "completed" is a smaller lie than inventing a failure.
            status = "completed"
            try:
                parsed = json.loads(text) if text.lstrip().startswith("{") else None
                if isinstance(parsed, dict) and parsed.get("error"):
                    status = "failed"
            except (ValueError, AttributeError):
                pass
            out.append({
                "kind": "tool",
                "id": cid,
                "title": tool_name,
                "status": status,
                # `detailFull` is a LENGTH, not a second copy of the text --
                # the client computes "还有 N 字" from it. Sending the string
                # made that arithmetic NaN on every replayed tool result.
                "detail": detail[:DETAIL_MAX], "detailFull": len(detail),
            })
            if items is not None:
                out.append({"kind": "todo", "items": items})
    # A call no `tool` row ever answered. It happens — captured in production:
    # a `mcp_memory_search_docs` call followed directly by the final answer —
    # and `pending` made the client time it as running forever. `incomplete`
    # says what the store knows; `completed` would claim a result it never had.
    answered = {e.get("id") for e in out if e.get("kind") == "tool" and e.get("status") != "pending"}
    for e in out:
        if e.get("kind") == "tool" and e.get("status") == "pending" and e.get("id") not in answered:
            e["status"] = "incomplete"
    return out


def serve() -> int:
    """Answer JSON-line requests on stdin until it closes.

    The reason this mode exists is measured: a one-shot invocation costs ~310 ms
    of which ~270 ms is `import hermes_state`, and the webui pays that twice on
    every session switch — once for the transcript and once for the list. Held
    open, the import happens at startup and each answer is a SQLite query.

    One request per line, `{"cmd": ..., ...}`; one response per line, either
    `{"ok": <payload>}` or `{"error": "..."}`. Errors are returned rather than
    raised so a bad request cannot take the process — and with it every
    subsequent request — down with it.
    """
    sys.stdout.write(json.dumps({"ok": "ready"}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "list":
                out = list_sessions(int(req.get("limit", 200)),
                                    bool(req.get("include_empty", False)))
            elif cmd == "history":
                out = history(req["id"], int(req.get("limit", 2000)))
            else:
                raise ValueError(f"unknown cmd {cmd!r}")
            resp = {"ok": out}
        except Exception as e:  # noqa: BLE001
            resp = {"error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="list sessions as JSON")
    p_list.add_argument("--limit", type=int, default=200)
    p_list.add_argument("--include-empty", action="store_true")
    p_hist = sub.add_parser("history", help="one session's transcript as JSON")
    p_hist.add_argument("--id", required=True)
    p_hist.add_argument("--limit", type=int, default=2000)
    sub.add_parser("serve", help="stay open and answer JSON lines on stdin")
    args = ap.parse_args()

    if args.cmd == "list":
        json.dump(list_sessions(args.limit, args.include_empty), sys.stdout, ensure_ascii=False)
        return 0
    if args.cmd == "history":
        json.dump(history(args.id, args.limit), sys.stdout, ensure_ascii=False)
        return 0
    if args.cmd == "serve":
        return serve()
    return 2


if __name__ == "__main__":
    sys.exit(main())
