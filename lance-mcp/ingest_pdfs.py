"""Ingest a fortune-telling corpus (PDF / .doc / .txt / .md) into LanceDB.

    python ingest_pdfs.py --source ~/Downloads/fortune \
        --s3-endpoint http://127.0.0.1:9100 --db-path s3://lancedb/mayi \
        --embed-url http://127.0.0.1:8080 --embed-model bge-m3 \
        --marker s3://lancedb/.mayi-docs-v1-ready --ensure

Rows land in the SAME `docs`/`files` schema ingest_docs.py writes, so the
server's search_docs / list_documents / document_outline / read_file work on
this corpus unchanged. The one structural difference is the citation
coordinate: a PDF has pages, not markdown headings, so every page becomes its
own section — `id = <relpath>#p<a>-p<b>` with start = end = the page number
and headings = ["第N页"]. A hit therefore cites a page, and read_page turns a
page range back into text.

Images: `pdfimages -png -p` lifts every embedded figure with its page number.
Watermarks and decoration are dropped by the rule plan-b measured on the
eight-mansions book: identical bytes repeated on >= 50% of a document's pages
is a watermark (its figures never repeat beyond 2). Survivors go into the
`page_images` table — `file | page | kind | width | height | image` with the
PNG bytes IN the table (large_binary), so the cluster serves them without a
filesystem; page_images(file, a, b) hands them to the agent as MCP image
content.

.doc is converted with macOS textutil; its embedded pictures are NOT lifted
(textutil gives text only). Those files are ingested as plain text, chunked by
the markdown chunker like .txt/.md.
"""
import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pyarrow as pa
from lancedb.index import FTS

from embed import Embedder, batches
from ingest_docs import DOC_EXTS, SCHEMA, TOKENIZERS
from ingest_docs import rows_for_text as chunk_plain_text
from s3 import Gateway
from store import DIM, connect, replace_files, rust_lines, s3_storage_options, table_names

PAGE_IMAGES_SCHEMA = pa.schema([
    pa.field("file", pa.string()),
    pa.field("page", pa.int32()),
    pa.field("kind", pa.string()),      # "figure" today; "page_render" reserved
    pa.field("width", pa.int32()),
    pa.field("height", pa.int32()),
    pa.field("sha", pa.string()),
    pa.field("image", pa.large_binary()),
])

MIN_W, MIN_H = 64, 64          # smaller than this it is a rule, not a figure
MAX_IMAGE_BYTES = 4 << 20      # a figure over 4 MiB is a scan gone wrong
# Page-level chunking wins NVIDIA's cross-dataset benchmark (E2E accuracy 0.648
# vs 0.60-0.65 for token splits at 128/256/512/1024/2048); a CJK page in these
# old books runs 300-1200 chars ≈ 300-1200 tokens, which sits exactly in the
# 512-1024 sweet spot. Pages beyond SOFT_PAGE are split at the last sentence
# boundary so the citation stays on the page (start=end=page for every chunk).
SOFT_PAGE = 800
HARD_PAGE = 1100
PAGE_OVERLAP = 80              # ~10% overlap at soft splits
SENT_BREAK = re.compile(r"[。！？；\n]")
MIN_PAGE_CHARS = 20            # plan-b: pages under this are headers/noise


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------- PDF text ----------

def pdf_pages(pdf: Path) -> list[str]:
    """1-based page texts, whitespace-normalized (plan-b: CJK has no spaces,
    and pdftotext's layout padding would just fatten the chunks)."""
    out = run(["pdftotext", str(pdf), "-"])
    if out.returncode != 0:
        raise RuntimeError(f"pdftotext {pdf}: {out.stderr.strip()[:200]}")
    return [re.sub(r"\s+", "", p) for p in out.stdout.split("\f")]


def pdf_page_count(pdf: Path) -> int:
    out = run(["pdfinfo", str(pdf)])
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    if not m:
        raise RuntimeError(f"pdfinfo {pdf}: no page count")
    return int(m.group(1))


def chunk_page(text: str) -> list[str]:
    """Page-level chunks. A short page is one chunk; a long page is split at
    a sentence boundary under SOFT_PAGE chars so citations stay on page.

    Scanned-image pages with NO body text but with OCR labels from figures
    (e.g. 离火/震三东/感情线) still get a chunk — those label words are the
    only thing the retrieval can match on.
    """
    if len(text) < 10:
        return []
    if len(text) <= SOFT_PAGE:
        return [text]
    out: list[str] = []
    i = 0
    while i < len(text):
        if len(text) - i <= SOFT_PAGE:
            tail = text[i:]
            if len(tail) >= 10:
                out.append(tail)
            break
        # last sentence break inside the soft window, else hard cut
        cut = -1
        for m in SENT_BREAK.finditer(text, i, i + SOFT_PAGE):
            cut = m.end()
        if cut <= i:
            cut = i + HARD_PAGE
        piece = text[i:cut]
        if len(piece) >= 10:
            out.append(piece)
        i = max(cut - PAGE_OVERLAP, i + 1)
    return out


def doc_to_text(path: Path) -> str:
    """macOS textutil for the legacy .doc binaries (no antiword dependency)."""
    out = run(["textutil", "-convert", "txt", "-stdout", str(path)])
    if out.returncode != 0:
        raise RuntimeError(f"textutil {path}: {out.stderr.strip()[:200]}")
    return out.stdout


# ---------- PDF images ----------

def png_size(data: bytes) -> tuple[int, int]:
    # PNG IHDR: width/height are fixed 4-byte big-endian at offsets 16/20.
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def pdf_figures(pdf: Path, pages_text: list[str]) -> list[dict]:
    """Every surviving figure of one PDF: deduped, watermarks dropped.

    Returns figure rows with an added `ocr` field (Vision OCR of label text in
    the figure — hand/face charts carry Chinese labels like 感情线/山根 that
    are the real retrieval signal). OCR is best-effort: failures return "".

    `pdfimages -p` names its output <prefix>-<page>-<n>.png with the page
    number zero-padded; the width of that field follows the document's page
    count, so the page is recovered by stripping the known prefix rather than
    assuming a field width.
    """
    from ocr import ocr_png
    npages = len(pages_text)
    with tempfile.TemporaryDirectory(prefix="pdfimg-") as td:
        out = run(["pdfimages", "-png", "-p", str(pdf), f"{td}/img"])
        if out.returncode != 0:
            raise RuntimeError(f"pdfimages {pdf}: {out.stderr.strip()[:200]}")
        seen: dict[str, dict] = {}
        freq: dict[str, int] = {}
        ocr_by_page: dict[int, list[str]] = {}
        rows: list[dict] = []
        for p in sorted(Path(td).glob("*.png")):
            m = re.match(r"img-(\d+)-\d+\.png", p.name)
            if not m:
                continue
            page = int(m.group(1))
            data = p.read_bytes()
            if len(data) > MAX_IMAGE_BYTES:
                continue
            try:
                w, h = png_size(data)
            except ValueError:
                continue
            if w < MIN_W or h < MIN_H:
                continue
            sha = hashlib.sha1(data).hexdigest()
            txt = (ocr_png(data) or "").strip()
            if txt:
                ocr_by_page.setdefault(page, []).append(txt)
            if sha in seen:
                freq[sha] += 1
                continue
            seen[sha] = {"file": "", "page": page, "kind": "figure",
                         "width": w, "height": h, "sha": sha, "image": data}
            freq[sha] = 1
        for sha, r in seen.items():
            if not _is_content(freq[sha], npages):
                continue
            rows.append(r)
    # OCR labels from every kept figure on a page, joined as a footer — the
    # chunker then puts these words in the page's chunk so queries like
    # "感情线在手掌什么位置" reach the page whose diagram is labelled 感情线.
    # Watermarks were dropped from `rows` but their OCR would be "" anyway
    # (they are usually the publisher's logo, not readable text).
    for page, lines in ocr_by_page.items():
        joined = " ".join(sorted(set(lines)))
        if joined:
            pages_text[page - 1] = (pages_text[page - 1] or "") + f"\n【图示】{joined}"
    return rows


def _is_content(freq: int, npages: int) -> bool:
    """True = a real figure. The watermark threshold plan-b measured: its
    watermark appeared on every page, its figures never twice."""
    return freq < max(4, npages // 2)


# ---------- corpus ----------

def is_indexable_text(name: str) -> bool:
    if name.startswith(".") or "/_" in name or name.startswith("_"):
        return False
    for junk in ("target", "node_modules"):
        if f"/{junk}/" in f"/{name}":
            return False
    return Path(name).suffix.lower() in DOC_EXTS | {".doc"}


def rows_for_pdf(pdf: Path, rel: str) -> tuple[list[dict], dict, list[dict]]:
    """One PDF -> docs rows (per-page sections) + files row + figures."""
    pages = pdf_pages(pdf)
    npages = pdf_page_count(pdf)
    if len(pages) < npages:                      # trailing blank pages are dropped by pdftotext
        pages += [""] * (npages - len(pages))
    rows: list[dict] = []
    for pno, ptext in enumerate(pages, start=1):
        if pno > npages:
            break
        heading = f"第{pno}页"
        for c in chunk_page(ptext):
            rows.append({
                "id": f"{rel}#p{pno}", "file": rel,
                "name": heading, "headings": [heading],
                "start": pno, "end": pno, "parent": rel,
                "text": f"{rel} › {heading}\n\n{c}", "vector": None,
            })
    whole = "\n".join(f"── 第{i}页 ──\n{t}" for i, t in enumerate(pages, 1) if t)
    files = {"path": rel, "corpus": "docs", "name": pdf.name,
             "lines": len(rust_lines(whole)), "text": whole}
    figures = pdf_figures(pdf, pages)
    for r in figures:
        r["file"] = rel
    return rows, files, figures


def build(source: Path) -> tuple[list[dict], list[dict], list[dict], dict]:
    rows: list[dict] = []
    files: list[dict] = []
    figures: list[dict] = []
    stats: dict[str, int] = {"pdfs": 0, "docs": 0, "figures_raw": 0, "figures_kept": 0}
    for p in sorted(source.rglob("*")):
        if not p.is_file():
            continue
        rel = f"{source.name}/{p.relative_to(source).as_posix()}"
        try:
            if p.suffix.lower() == ".pdf":
                r, f, figs = rows_for_pdf(p, rel)
                rows.extend(r)
                files.append(f)
                figures.extend(figs)
                stats["pdfs"] += 1
                stats["figures_raw"] += len(figs)
                print(f"  {rel}: {len(r)} chunks, {len(figs)} figures", flush=True)
            elif is_indexable_text(p.name):
                text = doc_to_text(p) if p.suffix.lower() == ".doc" \
                    else p.read_bytes().decode("utf-8", errors="replace")
                r, f = chunk_plain_text(text, rel, p.name)
                if f:
                    rows.extend(r)
                    files.append(f)
                stats["docs"] += 1
                print(f"  {rel}: {len(r)} chunks", flush=True)
        except Exception as e:  # noqa: BLE001 — one bad file must not end the ingest
            print(f"  !! {rel}: {e}", flush=True)
    stats["figures_kept"] = len(figures)
    return rows, files, figures, stats


def write(db, rows: list[dict], files: list[dict], figures: list[dict],
          tokenizer: str = "ngram-1-2") -> None:
    tbl = pa.Table.from_pylist(rows, schema=SCHEMA)
    t = db.create_table("docs", tbl, mode="overwrite", on_bad_vectors="null")
    t.create_index("text", config=FTS(
        lower_case=True, stem=False, remove_stop_words=False, ascii_folding=False,
        **TOKENIZERS[tokenizer]), replace=True)
    replace_files(db, "docs", files)
    if figures:
        imgs = pa.Table.from_pylist(figures, schema=PAGE_IMAGES_SCHEMA)
        db.create_table("page_images", imgs, mode="overwrite")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path,
                    help="directory of PDFs and text documents (read locally)")
    ap.add_argument("--s3-endpoint", required=True, help="autumn-s3 gateway URL")
    ap.add_argument("--db-path", required=True, help="s3://bucket/prefix")
    ap.add_argument("--embed-url", required=True)
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--tokenizer", choices=TOKENIZERS, default="ngram-1-2")
    ap.add_argument("--marker", help="with --ensure: s3://bucket/key success marker")
    ap.add_argument("--ensure", action="store_true",
                    help="skip when --marker exists; write it last")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and count, write nothing")
    args = ap.parse_args()

    if args.ensure:
        if not args.marker or not args.marker.startswith("s3://"):
            ap.error("--ensure needs an s3://bucket/key --marker")
        g = Gateway(args.s3_endpoint)
        mbucket, _, mkey = args.marker[len("s3://"):].partition("/")
        if g.exists(mbucket, mkey):
            print(f"marker {args.marker} present — already ingested, nothing to do")
            return

    t0 = time.monotonic()
    print(f"scanning {args.source} ...", flush=True)
    rows, files, figures, stats = build(args.source)
    if not rows:
        raise SystemExit("no chunks produced: refusing to write an empty table")
    print(f"{stats['pdfs']} PDFs, {stats['docs']} text docs -> {len(rows)} chunks, "
          f"{stats['figures_raw']} raw figures kept {stats['figures_kept']} "
          f"({time.monotonic() - t0:.0f}s)", flush=True)
    if args.dry_run:
        return

    t1 = time.monotonic()
    emb = Embedder(args.embed_url, args.embed_model)
    texts = [r["text"] for r in rows]
    total = len(texts)
    done = 0
    for rng in batches(texts):
        vecs = emb._with_retry(texts[rng.start:rng.stop])  # noqa: SLF001
        for i, v in zip(rng, vecs, strict=True):
            rows[i]["vector"] = v
        done += len(rng)
        pct = done / total * 100
        rate = done / (time.monotonic() - t1)
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  embedded {done}/{total} chunks  {pct:.0f}%  {rate:.1f} q/s  ETA {eta:.0f}s", flush=True)
    print(f"embedded {total} chunks in {time.monotonic() - t1:.0f}s (dim={emb.dim})", flush=True)
    if emb.dim and emb.dim != DIM:
        raise SystemExit(f"embedder returned dim={emb.dim}, store expects {DIM}")

    db = connect(args.db_path, storage_options=s3_storage_options(args.s3_endpoint))
    write(db, rows, files, figures, args.tokenizer)
    n = db.open_table("docs").count_rows()
    if n != len(rows):
        raise SystemExit(f"wrote {len(rows)} rows but the table reports {n}")
    ni = db.open_table("page_images").count_rows() if figures else 0
    print(f"docs: {n} rows; page_images: {ni} rows "
          f"(total {time.monotonic() - t0:.0f}s)", flush=True)

    if args.ensure:
        g.put(mbucket, mkey, str(int(time.time())).encode())
        print(f"marker {args.marker} written")


if __name__ == "__main__":
    sys.exit(main())
