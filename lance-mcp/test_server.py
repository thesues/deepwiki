"""Smoke test for every MCP tool against an in-memory Lance database.

    python test_server.py

No Autumn cluster and no embedding server: `memory://` stands in for Autumn
(same LanceDB, a different object store), and a deterministic fake embedder
stands in for llama-embed. This checks the tools' wiring — argument handling,
result shape, the graph queries — not retrieval quality; that is eval_docs.py
and parity_code.py's job, against the real corpus.
"""
import hashlib

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.index import FTS

from ingest_code import CODE_SCHEMA, EDGE_SCHEMA
from ingest_docs import SCHEMA as DOCS_SCHEMA
from ingest_docs import TOKENIZERS
from server import Retriever, call_tool, handle_one
from store import DIM, FILES_SCHEMA


class FakeEmbedder:
    """Same shape as embed.Embedder: deterministic, so a query's nearest
    neighbour is predictable enough to assert on."""
    dim = DIM

    def embed(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "little")
        v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
        return v / np.linalg.norm(v)

    def embed_batch(self, texts):
        return [self.embed(t) for t in texts]


def build_db():
    db = lancedb.connect("memory://")
    emb = FakeEmbedder()

    docs = [
        {"id": "a.md#L1-L3", "file": "a.md", "name": "药师经", "headings": ["药师经"],
         "start": 1, "end": 3, "parent": "a.md",
         "text": "a.md › 药师经\n\n药师琉璃光如来本愿功德经，十二大愿，救众生病苦。",
         "vector": emb.embed("药师琉璃光")},
        {"id": "a.md#L5-L7", "file": "a.md", "name": "十二大愿", "headings": ["药师经", "十二大愿"],
         "start": 5, "end": 7, "parent": "a.md#L1-L3",
         "text": "a.md › 药师经 › 十二大愿\n\n第一大愿：愿我来世...",
         "vector": emb.embed("十二大愿 愿我来世")},
    ]
    db.create_table("docs", pa.Table.from_pylist(docs, schema=DOCS_SCHEMA),
                    on_bad_vectors="null").create_index(
        "text", config=FTS(base_tokenizer="ngram", ngram_min_length=1, ngram_max_length=2,
                           lower_case=True, stem=False, remove_stop_words=False,
                           ascii_folding=False), replace=True)

    code = [
        {"id": "lib.rs::add", "kind": "Function", "name": "add", "qualname": "add",
         "file": "lib.rs", "start": 1, "end": 3, "text": "fn add(a: i32, b: i32) -> i32 { a + b }",
         "vector": emb.embed("fn add")},
        {"id": "lib.rs::main", "kind": "Function", "name": "main", "qualname": "main",
         "file": "lib.rs", "start": 5, "end": 7, "text": "fn main() { add(1, 2); }",
         "vector": emb.embed("fn main calls add")},
    ]
    db.create_table("code", pa.Table.from_pylist(code, schema=CODE_SCHEMA),
                    on_bad_vectors="null").create_index(
        "text", config=FTS(base_tokenizer="simple", lower_case=True, stem=False,
                           remove_stop_words=False, ascii_folding=False), replace=True)

    edges = [{"src": "lib.rs::main", "type": "CALLS", "dst": "lib.rs::add"}]
    db.create_table("edges", pa.Table.from_pylist(edges, schema=EDGE_SCHEMA))

    files = [
        {"path": "a.md", "corpus": "docs", "name": "a.md", "lines": 7,
         "text": "# 药师经\n\n药师琉璃光如来本愿功德经，十二大愿，救众生病苦。\n\n## 十二大愿\n\n第一大愿：愿我来世...\n"},
        {"path": "lib.rs", "corpus": "code", "name": "lib.rs", "lines": 3,
         "text": "fn add(a: i32, b: i32) -> i32 { a + b }\n\nfn main() { add(1, 2); }\n"},
    ]
    db.create_table("files", pa.Table.from_pylist(files, schema=FILES_SCHEMA))
    return db, emb


def main() -> None:
    db, emb = build_db()
    r = Retriever(db, emb, fs_root=None)

    assert r.modes("docs") == ["lexical", "vector", "hybrid"]
    assert r.modes("code") == ["lexical", "vector", "hybrid"]

    for mode in ("lexical", "vector", "hybrid", "auto"):
        hits = r.search("docs", "十二大愿", mode, k=5)
        assert hits, mode
        assert hits[0]["id"] == "a.md#L5-L7", (mode, hits)
        assert "source" not in hits[0], "a search hit must carry no body"
        assert hits[0]["headings"] == ["药师经", "十二大愿"]
        assert "score" in hits[0]
    print("search_docs: all 4 modes agree on the top hit, no body attached — ok")

    # Both bodies mention "add" (one defines it, one calls it) — check the
    # defining symbol is found and carries no body, not that it ranks first.
    hits = r.search("code", "add", "lexical", k=5)
    assert {h["id"] for h in hits} == {"lib.rs::add", "lib.rs::main"}, hits
    assert all("source" not in h for h in hits)
    print("search_code: lexical finds both `add` symbols, no body attached — ok")

    sym = r.get_symbol("lib.rs::add")
    assert sym["source"] == "fn add(a: i32, b: i32) -> i32 { a + b }", sym
    assert r.get_symbol("a.md#L1-L3")["source"].startswith("a.md ›")
    assert r.get_symbol("nope") is None
    print("get_symbol: code and doc ids both resolve, unknown id is null — ok")

    rf = r.read_file("lib.rs", 1, 1)
    assert rf == {"file": "lib.rs", "start": 1, "end": 1, "total_lines": 3,
                  "truncated": False, "text": "fn add(a: i32, b: i32) -> i32 { a + b }"}, rf
    rf_whole = r.read_file("lib.rs", None, None)
    assert rf_whole["end"] == 3 and rf_whole["total_lines"] == 3
    try:
        r.read_file("lib.rs", 3, 1)
        assert False, "inverted range must raise"
    except Exception as e:
        assert "after end" in str(e)
    try:
        r.read_file("lib.rs", 99, 100)
        assert False, "start past the end must raise"
    except Exception as e:
        assert "past the end" in str(e)
    try:
        r.read_file("nope.rs", None, None)
        assert False, "unknown path must raise"
    except Exception as e:
        assert "not a file" in str(e)
    print("read_file: line ranges, whole-file, inverted range and unknown path — ok")

    assert [n["id"] for n in r.far_briefs("lib.rs::add", "in")] == ["lib.rs::main"]
    assert [n["id"] for n in r.far_briefs("lib.rs::main", "out")] == ["lib.rs::add"]
    assert r.far_briefs("lib.rs::add", "out") == []
    print("find_callers / find_callees — ok")

    trace = r.traverse("lib.rs::main", "out", "CALLS", 6, 200)
    assert [n["id"] for n in trace] == ["lib.rs::main", "lib.rs::add"]
    assert [n["depth"] for n in trace] == [0, 1]
    print("trace_call_path — ok")

    docs_list = r.documents()
    assert [d["id"] for d in docs_list] == ["a.md"], docs_list
    print("list_documents — ok")

    outline = r.traverse("a.md", "out", "CONTAINS-DOC", 8, 500)
    assert [n["id"] for n in outline] == ["a.md", "a.md#L1-L3", "a.md#L5-L7"], outline
    assert [n["depth"] for n in outline] == [0, 1, 2]
    print("document_outline — ok")

    # Same path through the MCP envelope: tools/list, tools/call, an unknown
    # method, and a notification (no id) that must get no reply.
    tool_names = {t["name"] for t in handle_one(r, {"jsonrpc": "2.0", "id": 1,
                                                    "method": "tools/list"})["result"]["tools"]}
    assert {"search_docs", "search_code", "find_callers", "document_outline"} <= tool_names
    resp = handle_one(r, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "find_callees", "arguments": {"id": "lib.rs::main"}}})
    assert resp["result"]["content"][0]["text"] == '[{"id": "lib.rs::add", "kind": "Function", ' \
        '"name": "add", "file": "lib.rs", "start": 1}]', resp
    assert handle_one(r, {"jsonrpc": "2.0", "id": 3, "method": "bogus"})["error"]["code"] == -32601
    assert handle_one(r, {"jsonrpc": "2.0", "method": "ping"}) is None  # no id: no reply
    bad = call_tool(r, "get_symbol", {"id": "nope"})
    assert bad["content"][0]["text"] == "null"
    err = call_tool(r, "search_docs", {"query": "x", "mode": "nonsense"})
    assert err["isError"] and "unknown mode" in err["content"][0]["text"]
    print("MCP envelope: tools/list, tools/call, unknown method, notification, tool error — ok")

    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
