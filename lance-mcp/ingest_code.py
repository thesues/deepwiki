"""Index a Rust tree into filesystem-backed LanceDB `code` and `edges` tables.

    python ingest_code.py --fs-root ~/upstream --index autumn-rs \
        --db-path /mnt/autumn/lancedb/buda

A port of memory-mcp's indexer.rs, and meant to agree with it edge for edge:
the same tree-sitter-rust grammar (0.23.3; the Python runtime is 0.25 only
because that wheel is ABI 15), the same symbol ids
(`<relpath>::<qualname>`, relpath under --fs-root), the same CONTAINS/CALLS
resolution by short name with the same cap of 8 targets per name. The cap
makes the ORDER in which files are visited part of the answer, so the walk
reproduces read_dir's order (os.scandir is the same readdir) rather than
sorting.

Where memory-mcp walks a graph, this answers with a filter:
find_callers(id) is `edges WHERE type='CALLS' AND dst=id`, find_callees the
same on `src`.
"""
import argparse
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import tree_sitter_rust
from lancedb.index import FTS, BTree
from tree_sitter import Language, Node, Parser

from embed import Embedder
from store import DIM, connect, replace_files, rust_lines

MAX_CALLS_PER_NAME = 8
NESTED_ITEMS = {"function_item", "impl_item", "struct_item", "enum_item",
                "trait_item", "mod_item", "union_item"}
TYPE_KINDS = {"struct_item": "Struct", "enum_item": "Enum", "union_item": "Union",
              "type_item": "Type"}

CODE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("kind", pa.string()),
    pa.field("name", pa.string()),
    pa.field("qualname", pa.string()),
    pa.field("file", pa.string()),
    pa.field("start", pa.int32()),
    pa.field("end", pa.int32()),
    pa.field("text", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), DIM), nullable=True),
])
EDGE_SCHEMA = pa.schema([
    pa.field("src", pa.string()),
    pa.field("type", pa.string()),
    pa.field("dst", pa.string()),
])


@dataclass
class Def:
    id: str
    kind: str
    name: str
    qualname: str
    file: str
    start: int
    end: int
    src: str


@dataclass
class FileIndex:
    defs: list[Def] = field(default_factory=list)
    contains: list[tuple[str, str]] = field(default_factory=list)  # (container short name, member id)
    calls: list[tuple[str, str]] = field(default_factory=list)  # (caller id, callee short name)


def text(src: bytes, n: Node) -> str:
    return src[n.start_byte:n.end_byte].decode("utf-8", errors="replace")


def field_name(src: bytes, n: Node) -> str | None:
    c = n.child_by_field_name("name")
    return text(src, c) if c is not None else None


def type_name(src: bytes, n: Node) -> str | None:
    """First type_identifier under an impl's `type` (`impl Foo<T>` → Foo)."""
    if n.type == "type_identifier":
        return text(src, n)
    for c in n.children:
        if (t := type_name(src, c)) is not None:
            return t
    return None


def ident_of(src: bytes, n: Node | None) -> str | None:
    """Final identifier of a call target (`a::b::foo`, `x.foo`, `foo::<T>` → foo)."""
    if n is None:
        return None
    if n.type in ("identifier", "type_identifier", "field_identifier"):
        return text(src, n)
    sub = {"scoped_identifier": "name", "field_expression": "field",
           "generic_function": "function"}.get(n.type)
    return ident_of(src, n.child_by_field_name(sub)) if sub else None


def push_def(fi: FileIndex, relpath: str, scope: list[str], name: str, kind: str,
             node: Node, src: bytes) -> str:
    qual = "::".join([*scope, name])
    did = f"{relpath}::{qual}"
    fi.defs.append(Def(did, kind, name, qual, relpath, node.start_point[0] + 1,
                       node.end_point[0] + 1, text(src, node)))
    return did


def collect_calls(node: Node, src: bytes, caller: str, fi: FileIndex) -> None:
    for c in node.children:
        if c.type in NESTED_ITEMS:
            continue  # each nested item collects its own
        if c.type == "call_expression":
            if (name := ident_of(src, c.child_by_field_name("function"))) is not None:
                fi.calls.append((caller, name))
        collect_calls(c, src, caller, fi)


def walk(node: Node, src: bytes, relpath: str, scope: list[str], container: str | None,
         in_type: bool, fi: FileIndex) -> None:
    for c in node.children:
        k = c.type
        if k == "function_item":
            name = field_name(src, c) or "<anon>"
            did = push_def(fi, relpath, scope, name, "Method" if in_type else "Function", c, src)
            if container is not None:
                fi.contains.append((container, did))
            collect_calls(c, src, did, fi)
            walk(c, src, relpath, [*scope, name], name, False, fi)
        elif k in TYPE_KINDS:
            name = field_name(src, c) or "<anon>"
            did = push_def(fi, relpath, scope, name, TYPE_KINDS[k], c, src)
            if container is not None:
                fi.contains.append((container, did))
        elif k in ("trait_item", "mod_item"):
            name = field_name(src, c) or "<anon>"
            did = push_def(fi, relpath, scope, name, "Trait" if k == "trait_item" else "Module", c, src)
            if container is not None:
                fi.contains.append((container, did))
            walk(c, src, relpath, [*scope, name], name, k == "trait_item", fi)
        elif k == "impl_item":
            # Not a symbol itself; sets the container and scope for its methods.
            t = c.child_by_field_name("type")
            t = type_name(src, t) if t is not None else None
            if t is not None:
                walk(c, src, relpath, [*scope, t], t, True, fi)
            else:
                walk(c, src, relpath, scope, container, True, fi)
        else:
            walk(c, src, relpath, scope, container, in_type, fi)


def collect_rs(d: Path, out: list[Path]) -> None:
    try:
        entries = list(os.scandir(d))
    except OSError:
        return
    for e in entries:
        p = Path(e.path)
        if p.is_dir():
            if not e.name.startswith(".") and e.name not in ("target", "node_modules"):
                collect_rs(p, out)
        elif p.suffix == ".rs":
            out.append(p)


def index(root: Path, base: Path) -> tuple[list[FileIndex], list[dict], list[dict], list[dict]]:
    parser = Parser(Language(tree_sitter_rust.language()))
    files: list[FileIndex] = []
    file_rows: list[dict] = []
    paths: list[Path] = []
    collect_rs(root, paths)
    for p in paths:
        try:
            src = p.read_bytes()
        except OSError:
            continue
        rel = p.relative_to(base).as_posix() if p.is_relative_to(base) else p.as_posix()
        fi = FileIndex()
        walk(parser.parse(src).root_node, src, rel, [], None, False, fi)
        files.append(fi)
        body = src.decode("utf-8", errors="replace")
        file_rows.append({"path": rel, "corpus": "code", "name": p.name,
                          "lines": len(rust_lines(body)), "text": body})

    by_name: dict[str, list[str]] = {}
    for fi in files:
        for d in fi.defs:
            by_name.setdefault(d.name, []).append(d.id)

    # A def whose id repeats (cfg twins, two impl blocks) overwrites the
    # earlier one in memory-mcp's store; keeping the last row matches that.
    symbols: dict[str, dict] = {}
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for fi in files:
        for d in fi.defs:
            symbols[d.id] = {"id": d.id, "kind": d.kind, "name": d.name, "qualname": d.qualname,
                             "file": d.file, "start": d.start, "end": d.end, "text": d.src,
                             "vector": None}
        for etype, pairs in (("CONTAINS", [(None, m, c) for c, m in fi.contains]),
                             ("CALLS", [(caller, None, callee) for caller, callee in fi.calls])):
            for caller, member, name in pairs:
                for tid in by_name.get(name, [])[:MAX_CALLS_PER_NAME]:
                    s, t = (tid, member) if etype == "CONTAINS" else (caller, tid)
                    if s == t or (etype, s, t) in seen:
                        continue
                    seen.add((etype, s, t))
                    edges.append({"src": s, "type": etype, "dst": t})
    return files, list(symbols.values()), edges, file_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fs-root", required=True, type=Path, help="ids are relative to this")
    ap.add_argument("--index", required=True, help="tree under --fs-root to index")
    ap.add_argument("--db-path", required=True, type=Path,
                    help="absolute LanceDB directory (production: below Autumn FUSE)")
    ap.add_argument("--embed-url")
    ap.add_argument("--embed-model", default="bge-m3")
    args = ap.parse_args()

    base = args.fs_root.expanduser().resolve()
    root = base / args.index
    if not root.is_dir():
        raise SystemExit(f"--index {args.index}: {root} is not a directory")
    t0 = time.monotonic()
    files, symbols, edges, file_rows = index(root, base)
    if not symbols:
        raise SystemExit(f"no Rust symbols under {root}: refusing to write empty tables")
    t_parse = time.monotonic() - t0

    t_e = time.monotonic()
    if args.embed_url:
        vecs = Embedder(args.embed_url, args.embed_model).embed_batch([s["text"] for s in symbols])
        for s, v in zip(symbols, vecs, strict=True):
            s["vector"] = v
    t_embed = time.monotonic() - t_e

    db = connect(args.db_path)
    t1 = time.monotonic()
    code = db.create_table("code", pa.Table.from_pylist(symbols, schema=CODE_SCHEMA), mode="overwrite", on_bad_vectors="null")
    et = db.create_table("edges", pa.Table.from_pylist(edges, schema=EDGE_SCHEMA), mode="overwrite")
    replace_files(db, "code", file_rows)
    t_write = time.monotonic() - t1

    t2 = time.monotonic()
    # Identifiers split on punctuation; snake_case stays one token, as it
    # would in a grep. The edge columns take a btree each — they are only
    # ever filtered by equality.
    code.create_index("text", config=FTS(
        base_tokenizer="simple", lower_case=True, stem=False, remove_stop_words=False,
        ascii_folding=False), replace=True)
    for col in ("src", "dst"):
        et.create_index(col, config=BTree(), replace=True)
    t_index = time.monotonic() - t2

    n_calls = sum(e["type"] == "CALLS" for e in edges)
    print(f"{len(files)} files → {len(symbols)} symbols, {len(edges)} edges "
          f"({n_calls} CALLS, {len(edges) - n_calls} CONTAINS)  "
          f"(parse {t_parse:.1f}s, embed {t_embed:.1f}s, write {t_write:.1f}s, index {t_index:.1f}s)")


if __name__ == "__main__":
    main()
