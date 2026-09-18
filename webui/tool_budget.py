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
import threading
from collections import OrderedDict

log = logging.getLogger("deepwiki.tool_budget")

# The whole result, after trimming. ~1K tokens.
MAX_RESULT_CHARS = int(os.environ.get("DEEPWIKI_MAX_TOOL_CHARS", "4000"))

# WHAT ONE WAVE OF TOOL CALLS MAY SPEND, ALL RESULTS TOGETHER.
#
# A per-result cap is not a budget. The model emits a BATCH of tool calls in
# one response — fifteen of them in the screenshot this came from — and hermes
# runs the batch concurrently, so a 8,000-char ceiling per result admitted
# 120,000 characters in a single step. Everything downstream then inherits it:
# the window fills, compression fires, and compression is the thing that was
# timing out. One result being reasonable says nothing about what the step cost.
#
# `api_request_id` is the wave: hermes passes it to the middleware, and every
# tool call produced by one model response carries the same one
# (agent/tool_executor.py). So the budget is per wave, exactly, with no
# guessing from timestamps.
MAX_WAVE_CHARS = int(os.environ.get("DEEPWIKI_MAX_TOOL_WAVE_CHARS", "12000"))
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


def _parse(result: str):
    """The hit list inside a tool result, or None.

    Not just `json.loads`: what arrives here is whatever hermes made of the
    MCP reply, and that is not always the bare document the server sent. It
    joins multiple content blocks with newlines, and when a server returns
    `structuredContent` alongside the text it wraps both in
    `{"result": …, "structuredContent": …}` (tools/mcp_tool.py). A shape this
    does not recognise is not a crisis — it falls back to the plain cut — but
    it IS worth knowing about, because the structural trim is the one that
    keeps the result parseable, so an unrecognised shape is logged with its
    head rather than silently degrading.
    """
    for candidate in (result, result.strip()):
        try:
            doc = json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(doc, dict):
            # hermes' content+structuredContent wrapper: the hits are inside.
            for key in ("result", "structuredContent", "content"):
                inner = doc.get(key)
                if isinstance(inner, list):
                    return inner
                if isinstance(inner, str):
                    try:
                        nested = json.loads(inner)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(nested, list):
                        return nested
            return None
        return doc if isinstance(doc, list) else None

    # Several content blocks, joined with newlines: N documents, not one. Take
    # each line that is a hit list and concatenate them — spanning from the
    # first '[' to the last ']' would instead produce a string that is two
    # arrays and parses as neither.
    merged: list = []
    for line in result.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            part = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(part, list) and all(isinstance(h, dict) for h in part):
            merged.extend(part)
    if merged:
        return merged

    log.info("tool result is not a shape the trimmer knows (%d chars); head=%r",
             len(result), result[:160])
    return None


class _Waves:
    """How much each wave of tool calls has spent so far.

    Keyed by `api_request_id`. Bounded and ordered, so finished waves fall off
    the end rather than accumulating for the life of the process; a wave is
    over as soon as the next model response starts a new one, and nothing
    needs to tell us when.
    """

    def __init__(self, keep: int = 32) -> None:
        self._spent: "OrderedDict[str, int]" = OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def take(self, wave: str, want: int) -> int:
        """Reserve up to `want` characters for this wave; return what is left
        for it. A wave with no id gets the per-result cap and no wave
        accounting — better than lumping unrelated calls into one budget."""
        if not wave:
            return want
        with self._lock:
            spent = self._spent.get(wave, 0)
            grant = max(0, min(want, MAX_WAVE_CHARS - spent))
            self._spent[wave] = spent + grant
            self._spent.move_to_end(wave)
            while len(self._spent) > self._keep:
                self._spent.popitem(last=False)
            return grant


_waves = _Waves()


def shrink(tool_name: str, result, wave: str = ""):
    """Bound one tool result. Anything not a long string is returned as-is.

    Order matters: trim the bodies first and keep every hit, because the LIST
    is the part the model reasons over. Only if that is still too large does
    the tail get dropped — and it is told how many, so it can narrow the query
    rather than assume it saw everything.
    """
    if not isinstance(result, str):
        return result

    # What this result may spend: its own cap, further reduced by whatever the
    # rest of its wave already took.
    budget = _waves.take(wave, min(len(result), MAX_RESULT_CHARS))
    if len(result) <= budget:
        return result
    if budget <= 0:
        # The wave is spent. Say so instead of returning a stub with no
        # explanation — the model asked for too much AT ONCE, and that is the
        # thing it can do something about.
        log.info("tool %s: wave %s exhausted; %d chars withheld", tool_name, wave[:8], len(result))
        return ("（本轮工具调用返回的内容已超出上限，这一条未返回。"
                "一次少调用几个工具，或缩小查询范围后重试。）")

    before = len(result)
    doc = _parse(result)

    if isinstance(doc, list) and doc and all(isinstance(h, dict) for h in doc):
        cut = sum(_trim_body(h) for h in doc)
        out = json.dumps(doc, ensure_ascii=False)
        if len(out) > budget:
            # Still too big: drop from the tail, which is the low-scoring end.
            kept = doc
            while len(kept) > 1 and len(json.dumps(kept, ensure_ascii=False)) > budget:
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

    out = result[:budget] + (
        f"\n…（已截断 {before - budget} 字符，超出单次工具输出上限）"
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
            # `api_request_id` is the wave: every tool call produced by one
            # model response carries the same one.
            return shrink(tool_name, next_call(args), str(ctx.get("api_request_id") or ""))

        get_plugin_manager()._middleware.setdefault(
            TOOL_EXECUTION_MIDDLEWARE, []
        ).append(_mw)
    except Exception:  # noqa: BLE001 -- no ceiling beats no chat
        log.warning("could not install the tool output ceiling", exc_info=True)
        return False
    log.info("tool output ceiling: %d chars per result, %d per wave, %d per hit body",
             MAX_RESULT_CHARS, MAX_WAVE_CHARS, MAX_SOURCE_CHARS)
    return True
