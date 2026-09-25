"""Ingest a markdown corpus into a filesystem-backed LanceDB `docs` table.

    python ingest_docs.py --fs-root ~/Downloads/buda --index md \
        --db-path /mnt/autumn/lancedb/buda \
        --embed-url http://127.0.0.1:18080 --embed-model bge-m3

Rows carry the same id, breadcrumb and indexed text as memory-mcp's documents
(`<relpath>#L<a>-L<b>` with relpath under --fs-root, `relpath › h1 › h2`,
breadcrumb + body), so a hit from either server cites the same passage and
eval_docs.py can judge both alike. `parent` is the outline edge memory-mcp
stores as CONTAINS: the first chunk seen at the chunk's longest known heading
prefix, or the file itself.

Full-text search uses the ngram tokenizer at lengths 1-2. Unsegmented Chinese
has no spaces for the simple tokenizer to split on, so it finds nothing;
unigrams alone match 惠能 to 慧能 but rank 智慧能 alongside; bigrams alone
drop the 慧/惠 variant. Both lengths in one index keep the recall of the first
and let BM25 reward the second.

Without --embed-url the vector column is left null and the server serves
this corpus lexically; it never fills it with anything that would rank.
"""
import argparse
import os
import time
from pathlib import Path

import pyarrow as pa
from lancedb.index import FTS

from chunk import chunk_markdown
from embed import Embedder
from s3 import Gateway
from store import DIM, connect, lit, replace_files, rust_lines, s3_storage_options, table_names

DOC_EXTS = {".md", ".markdown", ".txt"}

SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("file", pa.string()),
    pa.field("name", pa.string()),
    pa.field("headings", pa.list_(pa.string())),
    pa.field("start", pa.int32()),
    pa.field("end", pa.int32()),
    pa.field("parent", pa.string()),
    pa.field("text", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), DIM), nullable=True),
])

# ngram-1-2 is the default; the others exist to be measured against it.
TOKENIZERS = {
    "ngram-1-2": dict(base_tokenizer="ngram", ngram_min_length=1, ngram_max_length=2),
    "ngram-1": dict(base_tokenizer="ngram", ngram_min_length=1, ngram_max_length=1),
    "ngram-2": dict(base_tokenizer="ngram", ngram_min_length=2, ngram_max_length=2),
    "icu": dict(base_tokenizer="icu"),
}


def collect(root: Path) -> list[Path]:
    """memory-mcp's collect_docs: no dot-directories, target or node_modules,
    and no dotted file whatever its extension — macOS tar writes `._x.md`
    AppleDouble sidecars that would otherwise be indexed as prose."""
    if root.is_file():
        return [root]
    out: list[Path] = []

    def walk(d: Path):
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            p = Path(e.path)
            if p.is_dir():
                if not e.name.startswith(".") and e.name not in ("target", "node_modules"):
                    walk(p)
            elif not e.name.startswith(".") and p.suffix in DOC_EXTS:
                out.append(p)

    walk(root)
    return sorted(out)


def is_indexable(relpath: str) -> bool:
    """collect()'s rules, applied to a path string so they hold for S3 keys
    exactly as for local files: no dot-directory, no target/node_modules, and
    no dotted file whatever its extension — macOS tar writes `._x.md`
    AppleDouble sidecars that would otherwise be indexed as prose."""
    for part in relpath.split("/")[:-1]:
        if part.startswith(".") or part in ("target", "node_modules"):
            return False
    name = relpath.rsplit("/", 1)[-1]
    return not name.startswith(".") and Path(name).suffix in DOC_EXTS


def rows_for_text(text: str, relpath: str, name: str) -> tuple[list[dict], dict | None]:
    """Chunk one file's text. `relpath` is the id's path — the same string a
    local-path ingest produced (`<fs-root-relative> › headings`), so an S3
    ingest of a FUSE-written corpus reproduces every id byte for byte."""
    chunks = chunk_markdown(text)
    if not chunks:
        return [], None
    rows = []
    path_owner: dict[tuple[str, ...], str] = {}
    for c in chunks:
        rid = f"{relpath}#L{c.start_line}-L{c.end_line}"
        breadcrumb = relpath if not c.headings else f"{relpath} › {' › '.join(c.headings)}"
        key = tuple(c.headings)
        parent = next((path_owner[key[:n]] for n in range(len(key) - 1, -1, -1)
                       if key[:n] in path_owner), relpath)
        path_owner.setdefault(key, rid)
        rows.append({
            "id": rid, "file": relpath,
            "name": c.headings[-1] if c.headings else name,
            "headings": c.headings, "start": c.start_line, "end": c.end_line,
            "parent": parent, "text": f"{breadcrumb}\n\n{c.body}", "vector": None,
        })
    file_row = {"path": relpath, "corpus": "docs", "name": name,
                "lines": len(rust_lines(text)), "text": text}
    return rows, file_row


def rows_for(path: Path, base: Path) -> tuple[list[dict], dict | None]:
    text = path.read_bytes().decode("utf-8", errors="replace")
    relpath = path.resolve().relative_to(base).as_posix()
    return rows_for_text(text, relpath, path.name)


def build(base: Path, index: str) -> tuple[list[dict], list[dict]]:
    root = base / index.lstrip("/")
    if not root.exists():
        raise FileNotFoundError(f"{index}: {root} does not exist")
    rows, files = [], []
    for p in collect(root):
        r, f = rows_for(p, base)
        if f:
            rows.extend(r)
            files.append(f)
    return rows, files


def split_index(index: str) -> tuple[str, str]:
    """`--index` is `<bucket>/<prefix>` in S3 mode: bucket = fs/'s first level.
    A missing bucket is an error, not a default — the same rule the gateway's
    URL mapping applies."""
    bucket, _, prefix = index.lstrip("/").partition("/")
    if not bucket or not prefix:
        raise SystemExit(f"--index must be <bucket>/<prefix> in S3 mode: {index!r}")
    return bucket, prefix


def build_s3(gateway: Gateway, index: str) -> tuple[list[dict], list[dict]]:
    """Read the corpus over the gateway instead of a filesystem. The ids and
    file paths are `<bucket>/<key>` — the same strings a FUSE-mount ingest of
    the same tree produced — so re-ingesting is never needed to move a table
    between the two access paths."""
    bucket, prefix = split_index(index)
    rows, files = [], []
    for key in gateway.list(bucket, prefix):
        relpath = f"{bucket}/{key}"
        if not is_indexable(relpath):
            continue
        text = gateway.get(bucket, key).decode("utf-8", errors="replace")
        r, f = rows_for_text(text, relpath, key.rsplit("/", 1)[-1])
        if f:
            rows.extend(r)
            files.append(f)
    return rows, files


def embed_rows(rows: list[dict], emb: Embedder) -> None:
    vecs = emb.embed_batch([r["text"] for r in rows])
    for r, v in zip(rows, vecs, strict=True):
        r["vector"] = v


def write(db, rows: list[dict], files: list[dict], tokenizer: str = "ngram-1-2",
          table: str = "docs", under: str | None = None) -> None:
    """Replace the whole table, or — with `under` — only the files below that
    path, so re-ingesting one directory drops chunks whose spans moved rather
    than leaving them beside their replacements."""
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    if under is None or table not in table_names(db):
        t = db.create_table(table, tbl, mode="overwrite", on_bad_vectors="null")
    else:
        t = db.open_table(table)
        t.delete(f"starts_with(file, {lit(under)})")
        t.add(tbl, on_bad_vectors="null")
    t.create_index("text", config=FTS(
        # CJK has no case, stems or stop words; the defaults would only damage
        # the Latin transliterations (Dharma, sutra) scattered through the corpus.
        lower_case=True, stem=False, remove_stop_words=False, ascii_folding=False,
        **TOKENIZERS[tokenizer],
    ), replace=True)
    if table == "docs":
        replace_files(db, "docs", files, under=under)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fs-root", type=Path, help="ids are relative to this; corpus on a local "
                    "filesystem (omitted in S3 mode)")
    ap.add_argument("--s3-endpoint", help="autumn-s3 gateway URL; corpus and db are read "
                    "through it instead of a filesystem")
    ap.add_argument("--index", required=True,
                    help="file/directory under --fs-root, or <bucket>/<prefix> in S3 mode")
    ap.add_argument("--db-path", required=True,
                    help="absolute LanceDB directory, or s3://bucket/prefix in S3 mode")
    ap.add_argument("--table", default="docs")
    ap.add_argument("--tokenizer", choices=TOKENIZERS, default="ngram-1-2")
    ap.add_argument("--embed-url")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--marker", help="with --ensure: success marker. A filesystem path in "
                    "local mode, s3://bucket/key in S3 mode")
    ap.add_argument("--ensure", action="store_true",
                    help="skip when --marker exists; write it last, after a successful "
                    "ingest, so a replacement overwrites tables left by an interrupted one")
    args = ap.parse_args()

    s3 = Gateway(args.s3_endpoint) if args.s3_endpoint else None
    if args.ensure:
        if not args.marker:
            ap.error("--ensure needs --marker")
        if s3 is not None:
            if not args.marker.startswith("s3://"):
                ap.error("--marker must be s3://bucket/key in S3 mode")
            mbucket, _, mkey = args.marker[len("s3://"):].partition("/")
            done = s3.exists(mbucket, mkey)
        else:
            done = Path(args.marker).expanduser().exists()
        if done:
            print(f"marker {args.marker} present — index already written, nothing to do")
            return

    t0 = time.monotonic()
    if s3 is not None:
        rows, files = build_s3(s3, args.index)
    else:
        if not args.fs_root:
            ap.error("--fs-root is required without --s3-endpoint")
        base = args.fs_root.expanduser().resolve()
        rows, files = build(base, args.index)
    if not rows:
        raise SystemExit(f"no chunks under {args.index}: refusing to write an empty table")
    t_chunk = time.monotonic() - t0

    t1 = time.monotonic()
    if args.embed_url:
        embed_rows(rows, Embedder(args.embed_url, args.embed_model))
    t_embed = time.monotonic() - t1

    db = connect(args.db_path, storage_options=s3_storage_options(args.s3_endpoint) if s3 else None)
    t2 = time.monotonic()
    write(db, rows, files, args.tokenizer, args.table)
    t_write = time.monotonic() - t2

    n = db.open_table(args.table).count_rows()
    if n != len(rows):
        raise SystemExit(f"wrote {len(rows)} rows but the table reports {n}")
    if args.ensure:
        if s3 is not None:
            s3.put(mbucket, mkey)
        else:
            m = Path(args.marker).expanduser()
            m.parent.mkdir(parents=True, exist_ok=True)
            m.touch()
    print(f"{len(files)} files → {n} chunks  (chunk {t_chunk:.1f}s, embed {t_embed:.1f}s, "
          f"embed {t_embed:.1f}s, write+fts {t_write:.1f}s)  {args.db_path}/{args.table} "
          f"tokenizer={args.tokenizer} "
          f"vectors={'bge-m3 via ' + args.embed_url if args.embed_url else 'none'}")


if __name__ == "__main__":
    main()
