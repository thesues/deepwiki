"""The ceiling on one tool result — the thing hermes does not have."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tool_budget as tb  # noqa: E402


def _hits(*sizes, ident="python/src/lib.rs::BatchClient"):
    return [{"id": f"{ident}#{i}", "file": "src/lib.rs", "name": f"sym{i}",
             "kind": "Function", "score": 0.5, "source": "x" * n}
            for i, n in enumerate(sizes)]


def test_a_short_result_is_untouched():
    """The ceiling must be invisible until it is needed — the median hit in
    production is a few hundred characters."""
    small = json.dumps(_hits(200, 300))
    assert tb.shrink("search_code", small) == small
    assert tb.shrink("get_symbol", "fn main() {}") == "fn main() {}"


def test_the_giant_hit_is_trimmed_and_the_others_survive():
    """The real shape: one 17 KB symbol is 71% of the answer while the median
    hit is 485 chars. Lowering `k` would throw away the cheap, relevant hits
    and still admit the giant one; trimming BODIES bounds the worst case and
    keeps the list of what was found.
    """
    raw = json.dumps(_hits(17396, 150, 120, 80))
    out = tb.shrink("search_code", raw)
    doc = json.loads(out)
    assert len(doc) == 4, "every hit must survive; the list is what the model reasons over"
    assert len(doc[0]["source"]) < tb.MAX_SOURCE_CHARS + 200, "the giant body must be cut"
    assert doc[1]["source"] == "x" * 150, (
        "a body already under the cap must not be touched — at 200 chars that is "
        "a signature, which is what a shortlist entry is for"
    )
    assert "get_symbol" in doc[0]["source"], (
        "a trim must name the tool that fetches the rest, or the model just searches again"
    )
    assert len(out) <= tb.MAX_RESULT_CHARS + 400


def test_the_output_stays_parseable_json():
    """A truncated JSON document is worse than a truncated answer: the model
    parses this, so a blunt cut at N bytes would make the whole result
    unreadable rather than shorter."""
    out = tb.shrink("search_code", json.dumps(_hits(*([3000] * 12))))
    body = out.split("\n…")[0]
    doc = json.loads(body)          # must not raise
    assert isinstance(doc, list) and doc


def test_dropping_the_tail_is_declared():
    """When trimming bodies is not enough, the model must be told hits are
    missing — otherwise it assumes it saw everything and answers from a
    partial view."""
    out = tb.shrink("search_code", json.dumps(_hits(*([3000] * 40))))
    assert "未返回" in out and len(out) <= tb.MAX_RESULT_CHARS + 400


def test_a_non_json_result_is_cut_with_a_marker():
    out = tb.shrink("terminal", "y" * 50000)
    assert len(out) <= tb.MAX_RESULT_CHARS + 200
    assert "已截断" in out


def test_non_string_results_pass_through():
    """Middleware sits in front of EVERY tool. Anything it does not understand
    must come out exactly as it went in."""
    for value in ({"a": 1}, None, 42, ["x"]):
        assert tb.shrink("whatever", value) is value


def test_install_is_not_fatal_without_hermes():
    """No ceiling beats no chat: a hermes that moves the middleware registry
    must cost the ceiling, not the server."""
    assert tb.install() in (True, False)


def test_the_hermes_wrapper_shapes_are_recognised():
    """What arrives here is what HERMES made of the MCP reply, not what the
    server sent. It joins multiple content blocks with newlines, and wraps
    text + structuredContent in an object when a server returns both
    (tools/mcp_tool.py). Missing those shapes does not fail loudly — it
    silently falls back to the blunt cut, which leaves the model holding
    truncated JSON. That is how this shipped the first time.
    """
    hits = _hits(17396, 485, 400)
    arr = json.dumps(hits)

    # 1. the bare array
    assert tb._parse(arr) is not None
    # 2. hermes' content+structuredContent wrapper, text side
    assert tb._parse(json.dumps({"result": arr, "structuredContent": None})) is not None
    # 3. same wrapper, structured side already decoded
    assert tb._parse(json.dumps({"result": "", "structuredContent": hits})) is not None
    # 4. two content blocks joined with a newline: no longer one document
    assert tb._parse(arr + "\n" + json.dumps(_hits(100))) is not None
    # 5. genuinely not a hit list
    assert tb._parse("total 12\ndrwxr-xr-x 4 root root") is None


def test_a_wrapped_result_is_trimmed_structurally_not_chopped():
    """The point of recognising the wrapper: the trim stays structural, so
    what the model receives still parses."""
    out = tb.shrink("search_code",
                    json.dumps({"result": json.dumps(_hits(17396, 150)),
                                "structuredContent": None}))
    body = out.split("\n…")[0]
    doc = json.loads(body)
    assert isinstance(doc, list) and len(doc) == 2
    assert len(doc[0]["source"]) < tb.MAX_SOURCE_CHARS + 200
    assert doc[1]["source"] == "x" * 150


def test_a_wave_of_calls_shares_one_budget():
    """A per-result cap is not a budget.

    The model emits a BATCH of tool calls in one response — fifteen of them in
    the screenshot this came from — and hermes runs them concurrently. With
    only a per-result ceiling, ten calls at 4,000 chars each admit 40,000
    characters in ONE step, and the window fills exactly as before. The wave
    is identified exactly, not guessed: every tool call from one model
    response carries the same `api_request_id`.
    """
    tb._waves = tb._Waves()
    big = json.dumps(_hits(*([4000] * 6)))
    total = 0
    for _ in range(10):
        total += len(tb.shrink("search_code", big, wave="req-1"))
    assert total <= tb.MAX_WAVE_CHARS + 600, (
        f"one wave spent {total} chars; the budget is {tb.MAX_WAVE_CHARS}"
    )


def test_the_withheld_result_says_what_to_do():
    """A stub with no explanation teaches the model nothing; it retries the
    same batch. The thing it can act on is 'you asked for too much at once'."""
    tb._waves = tb._Waves()
    big = json.dumps(_hits(*([4000] * 6)))
    outs = [tb.shrink("search_code", big, wave="req-2") for _ in range(10)]
    assert any("一次少调用几个工具" in o for o in outs)


def test_a_new_wave_starts_fresh():
    """The budget is per model response, not per conversation: the next wave
    must not inherit the last one's exhaustion."""
    tb._waves = tb._Waves()
    big = json.dumps(_hits(*([4000] * 6)))
    for _ in range(10):
        tb.shrink("search_code", big, wave="req-3")
    first = tb.shrink("search_code", big, wave="req-4")
    assert len(first) > 1000, "a fresh wave gets its full budget"


def test_no_wave_id_still_gets_the_per_result_cap():
    """Not every caller supplies one. Falling back to per-result accounting
    beats lumping unrelated calls into a single shared budget."""
    tb._waves = tb._Waves()
    out = tb.shrink("search_code", json.dumps(_hits(*([4000] * 6))), wave="")
    assert len(out) <= tb.MAX_RESULT_CHARS + 600
