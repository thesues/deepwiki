"""Bounded, keyset reads across Hermes' compression lineage."""
from __future__ import annotations

import base64
from contextlib import nullcontext
import json

PAGE_SIZE = 40
MAX_PAGE_SIZE = 80


def _cursor(link: str, row_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([link, row_id]).encode()).decode().rstrip("=")


def _decode(value: str, lineage: list[str]) -> tuple[str, int]:
    try:
        if len(value) > 1024:
            raise ValueError
        link, row_id = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if link not in lineage or type(row_id) is not int or row_id < 1:
            raise ValueError
        return link, row_id
    except Exception as exc:
        raise ValueError("invalid history cursor") from exc


def _rows(db, lineage, bound, newer, limit):
    """At most limit rows; no OFFSET and no full transcript materialization."""
    links = lineage if newer else list(reversed(lineage))
    if bound:
        links = links[links.index(bound[0]):]
    result = []
    for link in links:
        params = [link]
        predicate = ""
        if bound and link == bound[0]:
            predicate = " AND id " + (">" if newer else "<") + " ?"
            params.append(bound[1])
        params.append(limit - len(result))
        rows = db._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? AND active = 1"
            + predicate + " ORDER BY id " + ("ASC" if newer else "DESC") + " LIMIT ?",
            params,
        ).fetchall()
        result.extend(dict(row) for row in rows)
        if len(result) == limit:
            break
    return result


def history_page(sid: str, *, before: str = "", after: str = "", limit: int = PAGE_SIZE) -> dict:
    from hermes_session_api import _db, _history_events, _history_lineage

    if before and after:
        raise ValueError("use before or after, not both")
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
    db = _db()
    # Hermes shares a connection among HTTP threads and the agent writer.
    with getattr(db, "_lock", nullcontext()):
        lineage = _history_lineage(db, sid)
        bound = _decode(before or after, lineage) if before or after else None
        rows = _rows(db, lineage, bound, bool(after), limit)
        if not after:
            rows.reverse()
        start = (rows[0]["session_id"], rows[0]["id"]) if rows else None
        end = (rows[-1]["session_id"], rows[-1]["id"]) if rows else None
        more_before = bool(start and _rows(db, lineage, start, False, 1))
        more_after = bool(end and _rows(db, lineage, end, True, 1))
        # A page can begin with a result whose call was on the previous page.
        # Read only the nearest call batch, never the omitted message bodies.
        preceding = None
        if rows and rows[0]["role"] == "tool":
            preceding = db._conn.execute(
                "SELECT tool_calls FROM messages WHERE session_id = ? AND active = 1"
                " AND role = 'assistant' AND id < ? AND tool_calls IS NOT NULL"
                " ORDER BY id DESC LIMIT 1", start,
            ).fetchone()

    invocations = {}
    if preceding:
        _history_events([{"role": "assistant", "tool_calls": preceding[0]}], invocations)
    messages = []
    for row in rows:
        decode = getattr(db, "_decode_content", None)
        if decode and "content" in row:
            row["content"] = decode(row["content"])
        messages.append({
            "cursor": _cursor(row["session_id"], row["id"]),
            "events": _history_events([row], invocations),
        })
    return {
        "messages": messages,
        "before": _cursor(*start) if start else None,
        "after": _cursor(*end) if end else None,
        "hasMoreBefore": more_before,
        "hasMoreAfter": more_after,
    }
