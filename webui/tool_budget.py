"""A ceiling on what one tool call can spend of the model's context.

hermes has none: a tool result goes into the conversation whole, however big
it is. That is fine until a tool is backed by a corpus. Measured against the
autumn-rs index, `search_code` answers a single query with:

    query='extent node replication'  7 hits, 24,608 chars of source
        largest hit 17,396 chars — 71% of the answer, by itself
        median hit     485 chars

and dsv4-flash's ceiling is 62,080 tokens for prompt AND generation together.
Six ordinary searches exhaust the window, after which every turn is context
compression — which rotates the session id and loses accuracy — instead of an
answer. Reported as a diagram request that never arrived: fifteen searches,
all `completed`, no reply.

WHY THIS TRIMS EACH HIT RATHER THAN DROPPING HITS. The obvious lever is `k`,
and it is the wrong one. The median hit is a few hundred characters; the cost
is a long tail of enormous symbols, and `k=1` can still land on the 17 KB one.
Cutting `k` throws away the cheap, relevant hits and does not bound the
expensive case. Trimming the SOURCE of each hit bounds the worst case and
keeps the list of what was found — which is what the next call needs, since
`get_symbol` can fetch any body in full.

The trim is structural, not a chop at N bytes: the result is JSON the model
parses, and a truncated JSON document is worse than a truncated answer — it is
an unreadable one. Every trim says what it did and which tool retrieves the
rest, or the model simply searches again and pays twice.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("deepwiki.tool_budget")

# The whole result, after trimming. ~2K tokens: a handful of these still leaves
# room to think in a 54K window.
MAX_RESULT_CHARS = int(os.environ.get("DEEPWIKI_MAX_TOOL_CHARS", "8000"))
# One hit's body. Enough for a signature and the shape of a function; the rest
# is one get_symbol away.
MAX_SOURCE_CHARS = int(os.environ.get("DEEPWIKI_MAX_HIT_SOURCE_CHARS", "900"))

# The field the corpus tools return a symbol's body in, and the id that fetches
# it whole.
_BODY_KEYS = ("source", "text", "content", "body")


def _trim_body(hit: dict) -> bool:
    """Trim one hit's body in place. True if anything was cut."""
    for key in _BODY_KEYS:
        body = hit.get(key)
        if isinstance(body, str) and len(body) > MAX_SOURCE_CHARS:
            ident = hit.get("id") or hit.get("name") or ""
            hint = f"get_symbol id={ident!r}" if ident else "the id above"
            hit[key] = (
                body[:MAX_SOURCE_CHARS]
                + f"\n…（已截断 {len(body) - MAX_SOURCE_CHARS} 字符；"
                + f"需要完整内容用 {hint}）"
            )
            return True
    return False


def shrink(tool_name: str, result):
    """Bound one tool result. Anything not a long string is returned as-is.

    Order matters: trim the bodies first and keep every hit, because the LIST
    is the part the model reasons over. Only if that is still too large does
    the tail get dropped — and it is told how many, so it can narrow the query
    rather than assume it saw everything.
    """
    if not isinstance(result, str) or len(result) <= MAX_RESULT_CHARS:
        return result

    before = len(result)
    try:
        doc = json.loads(result)
    except Exception:  # noqa: BLE001 -- not JSON: fall through to the plain cut
        doc = None

    if isinstance(doc, list) and doc and all(isinstance(h, dict) for h in doc):
        cut = sum(_trim_body(h) for h in doc)
        out = json.dumps(doc, ensure_ascii=False)
        if len(out) > MAX_RESULT_CHARS:
            # Still too big: drop from the tail, which is the low-scoring end.
            kept = doc
            while len(kept) > 1 and len(json.dumps(kept, ensure_ascii=False)) > MAX_RESULT_CHARS:
                kept = kept[:-1]
            dropped = len(doc) - len(kept)
            out = json.dumps(kept, ensure_ascii=False)
            if dropped:
                out += (
                    f"\n…（另有 {dropped} 条命中未返回：结果超出单次工具输出上限。"
                    f"缩小查询范围，或对已知 id 用 get_symbol）"
                )
        log.info("tool %s: %d -> %d chars (%d bodies trimmed)",
                 tool_name, before, len(out), cut)
        return out

    out = result[:MAX_RESULT_CHARS] + (
        f"\n…（已截断 {before - MAX_RESULT_CHARS} 字符，超出单次工具输出上限）"
    )
    log.info("tool %s: %d -> %d chars (plain cut)", tool_name, before, len(out))
    return out


def install() -> bool:
    """Register `shrink` as hermes tool-execution middleware.

    hermes' public route to this is a plugin; reaching the manager's list
    directly is the same surgical access `main` already uses for the context
    floor, and it is guarded: a hermes that moves this must cost a ceiling, not
    the server.
    """
    try:
        from hermes_cli.middleware import TOOL_EXECUTION_MIDDLEWARE
        from hermes_cli.plugins import get_plugin_manager

        def _mw(tool_name: str, args, next_call, **ctx):
            return shrink(tool_name, next_call(args))

        get_plugin_manager()._middleware.setdefault(
            TOOL_EXECUTION_MIDDLEWARE, []
        ).append(_mw)
    except Exception:  # noqa: BLE001 -- no ceiling beats no chat
        log.warning("could not install the tool output ceiling", exc_info=True)
        return False
    log.info("tool output ceiling: %d chars per result, %d per hit body",
             MAX_RESULT_CHARS, MAX_SOURCE_CHARS)
    return True
