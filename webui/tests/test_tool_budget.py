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
    raw = json.dumps(_hits(17396, 485, 400, 223))
    out = tb.shrink("search_code", raw)
    doc = json.loads(out)
    assert len(doc) == 4, "every hit must survive; the list is what the model reasons over"
    assert len(doc[0]["source"]) < 2000, "the giant body must be cut"
    assert doc[1]["source"] == "x" * 485, "a small body must not be touched"
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
