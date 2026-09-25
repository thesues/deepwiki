"""lance-mcp — memory-mcp's retrieval tools over a LanceDB on autumn.

    python server.py --db-path /mnt/autumn/lancedb/buda --embed-url http://llama-embed:8080
    python server.py --db-path s3://lancedb/buda --s3-endpoint http://autumn-s3:9100 ...
    # MCP (JSON-RPC 2.0) at POST http://0.0.0.0:5102/mcp, health at GET /healthz

A drop-in for the tools a hermes profile names: same tool names, arguments,
result shapes and defaults (k=8, auto → hybrid when an embedder is set), so a
profile can switch from `memory` to `lance` by changing its server and nothing
else. The graph_* tools are not carried over; nothing in buda calls them.

What is different is where things come from. Searches are Lance queries
(full-text and vector legs, fused by the same reciprocal-rank rule and tie
break as autumn-memory), call and outline edges are equality filters, and
read_file reads the `files` table. Community LanceDB opens an ordinary local
path or an `s3://` URI against the autumn-s3 gateway (--s3-endpoint; the same
objects the old FUSE mount exposed, so no re-ingest); the corpus the
ingest_documents tool reads comes from the same place (--fs-root locally, the
gateway in S3 mode).

The transport is memory-mcp's too, and as small: POST /mcp takes one message
or a batch, a notification gets 202 with no body, an unknown method -32601.
It runs on the standard library's threading HTTP server; there is no SDK
because the protocol surface is four methods.
"""
import argparse
import base64
import json
import logging
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from embed import Embedder
from s3 import Gateway, S3Error
from store import connect, is_s3_uri, lit, rust_lines, s3_storage_options, table_names

log = logging.getLogger("lance-mcp")

RRF_K = 60.0
MAX_READ_LINES = 400
MAX_PAGE_CHARS = 24000
HIT_COLS = ["id", "name", "file", "start", "end"]


class Content(list):
    """A tool result that is already a list of MCP content parts (text plus
    image blocks) — call_tool sends it verbatim instead of JSON-wrapping it."""


class ToolError(Exception):
    """A caller mistake, answered as a tool result the agent can read and act
    on. A JSON-RPC error reads to a client as "the tool broke"."""


class Retriever:
    def __init__(self, db, emb: Embedder | None, fs_root: Path | None = None,
                 s3: Gateway | None = None, docs_table: str = "docs"):
        self.db = db
        self.emb = emb
        self.fs_root = fs_root
        self.s3 = s3
        self.docs_table = docs_table
        self._lock = threading.Lock()  # ingest_documents: one writer at a time

    # -- tables ---------------------------------------------------------------

    def _open(self, name: str):
        try:
            return self.db.open_table(name)
        except (ValueError, FileNotFoundError) as e:
            raise ToolError(f"this instance has no `{name}` table; ingest it first") from e

    def _has_vectors(self, name: str) -> bool:
        try:
            return self.db.open_table(name).count_rows("vector IS NOT NULL") > 0
        except Exception:  # noqa: BLE001 — absent table: no vectors to speak of
            return False

    def modes(self, corpus: str) -> list[str]:
        if self.emb is not None and self._has_vectors(corpus):
            return ["lexical", "vector", "hybrid"]
        return ["lexical"]

    # -- search ---------------------------------------------------------------

    def search(self, corpus: str, q: str, mode: str = "auto", k: int = 6) -> list[dict]:
        table = self.docs_table if corpus == "docs" else "code"
        available = self.modes(table)
        if mode == "auto":
            mode = "hybrid" if "hybrid" in available else "lexical"
        if mode not in ("lexical", "vector", "hybrid"):
            raise ToolError(f"unknown mode `{mode}`; use lexical, vector, hybrid or auto")
        if mode not in available:
            why = "this instance has no embedder" if self.emb is None else \
                f"the `{table}` table was ingested without vectors"
            raise ToolError(f"mode `{mode}` needs vectors and {why}; retry with mode=lexical")
        if not q.strip() or k <= 0:
            return []
        t = self._open(table)
        cols = HIT_COLS + ["text"] + (["headings"] if corpus == "docs" else ["kind"])
        if mode == "lexical":
            return [self._hit(corpus, r, r["_score"]) for r in self._lexical(t, q, k, cols)]
        qv = self.emb.embed(q)
        if mode == "vector":
            return [self._hit(corpus, r, 1.0 - r["_distance"]) for r in self._vector(t, qv, k, cols)]
        # autumn-memory's search_hybrid: each leg to depth max(k,10)*2, fused by
        # 1/(60 + rank) summed over legs, ties broken by id so a result does not
        # move between two runs of the same query.
        depth = max(k, 10) * 2
        legs = [self._lexical(t, q, depth, cols), self._vector(t, qv, depth, cols)]
        score: dict[str, float] = {}
        row: dict[str, dict] = {}
        for leg in legs:
            for rank, r in enumerate(leg):
                score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
                row.setdefault(r["id"], r)
        best = sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]
        return [self._hit(corpus, row[i], s) for i, s in best]

    @staticmethod
    def _lexical(t, q: str, n: int, cols: list[str]) -> list[dict]:
        return t.search(q, query_type="fts").select(cols).limit(n).to_list()

    @staticmethod
    def _vector(t, qv, n: int, cols: list[str]) -> list[dict]:
        return (t.search(qv, vector_column_name="vector").distance_type("cosine")
                .where("vector IS NOT NULL", prefilter=True).select(cols).limit(n).to_list())

    @staticmethod
    def _hit(corpus: str, r: dict, score: float | None, source: str | None = None) -> dict:
        """A search hit is a location, not a delivery. Search results carry an
        80-char preview so the caller judges whether to fetch; explicit callers
        (get_symbol) that pass the whole body get it back as `source`."""
        h = {"id": r["id"], "name": r["name"],
             "kind": "Section" if corpus == "docs" else r["kind"],
             "file": r["file"], "start": r["start"], "end": r["end"]}
        if corpus == "docs":
            h["headings"] = list(r["headings"] or [])
        if source is not None:
            h["source"] = source
        elif "text" in r:
            body = r["text"].split("\n\n", 1)[1] if "\n\n" in r["text"] else r["text"]
            h["preview"] = (body[:80] + "…") if len(body) > 80 else body
        if score is not None:
            h["score"] = float(score)
        return h

    # -- symbols and files ----------------------------------------------------

    def get_symbol(self, sid: str) -> dict | None:
        for corpus, table in (("code", "code"), ("docs", self.docs_table)):
            if table not in table_names(self.db):
                continue
            cols = HIT_COLS + ["text"] + (["headings"] if corpus == "docs" else ["kind"])
            rows = self.db.open_table(table).search().where(f"id = {lit(sid)}") \
                .select(cols).limit(1).to_list()
            if rows:
                return self._hit(corpus, rows[0], None, source=rows[0]["text"])
        return None

    def read_file(self, path: str, start: int | None, end: int | None) -> dict:
        p = path.lstrip("/")
        rows = self._open("files").search().where(f"path = {lit(p)}") \
            .select(["text"]).limit(1).to_list()
        if not rows:
            raise ToolError(f"cannot read {path}: not a file this server indexed")
        lines = rust_lines(rows[0]["text"])
        frm = max(start or 1, 1)
        to = min(end if end is not None else len(lines), len(lines))
        if frm > len(lines):
            raise ToolError(f"{path} has {len(lines)} lines; start={frm} is past the end")
        if frm > to:
            raise ToolError(f"{path}: start={frm} is after end={to}; "
                            "the range is 1-based and inclusive")
        capped = min(to, frm + MAX_READ_LINES - 1)
        return {"file": path, "start": frm, "end": capped, "total_lines": len(lines),
                "truncated": capped < to, "text": "\n".join(lines[frm - 1:capped])}

    # -- pages and pictures (a PDF corpus: pages are the citation coordinate) -

    def read_page(self, path: str, page_start: int | None, page_end: int | None) -> dict:
        """Whole pages of a PDF ingest: the per-page chunks of the `docs` table
        grouped back into page order. `page` numbers are the ingest's start/end
        values, so this is the page analogue of read_file's line range."""
        p = path.lstrip("/")
        a = max(page_start or 1, 1)
        b = page_end if page_end is not None else a
        if b < a:
            raise ToolError(f"page range {a}-{b}: end is before start")
        rows = self._open("docs").search() \
            .where(f"file = {lit(p)} AND start >= {a} AND end <= {b}") \
            .select(["id", "start", "text"]).limit(None).to_list()
        if not rows:
            raise ToolError(f"no indexed pages {a}-{b} of {path}: the file a search hit "
                            "reports is the value to pass here")
        by_page: dict[int, list[str]] = {}
        for r in rows:
            # the row text is "<file> › 第N页\n\n<body>" — the page header is
            # re-emitted by the separator, so drop the per-chunk breadcrumb
            body = r["text"].split("\n\n", 1)[1] if "\n\n" in r["text"] else r["text"]
            by_page.setdefault(r["start"], []).append(body)
        all_pages = [(pg, f"── {p} › 第{pg}页 ──\n" + "\n".join(by_page[pg]))
                     for pg in sorted(by_page)]
        kept, size, truncated = [], 0, False
        for pg, text in all_pages:
            if size + len(text) > MAX_PAGE_CHARS and kept:
                truncated = True
                break
            kept.append(text)
            size += len(text)
        return {"file": p, "pages": [all_pages[0][0], kept and all_pages[len(kept) - 1][0]],
                "truncated": truncated, "text": "\n\n".join(kept)}

    def page_images(self, path: str, page_start: int | None, page_end: int | None) -> "Content":
        """The figures of a page range, as MCP image content the host can show
        a vision model. The PNG bytes live IN the page_images table."""
        p = path.lstrip("/")
        a = max(page_start or 1, 1)
        b = page_end if page_end is not None else a
        rows = self._open("page_images").search() \
            .where(f"file = {lit(p)} AND page >= {a} AND page <= {b}") \
            .select(["page", "width", "height", "image"]).limit(None).to_list()
        if not rows:
            raise ToolError(f"no figures indexed for {path} pages {a}-{b}")
        rows.sort(key=lambda r: (r["page"], r["image"][:8]))
        content = Content([{"type": "text", "text": json.dumps(
            {"file": p, "pages": [a, b], "count": len(rows),
             "images": [{"page": r["page"], "width": r["width"], "height": r["height"]}
                        for r in rows]}, ensure_ascii=False)}])
        for r in rows:
            content.append({"type": "image", "mimeType": "image/png",
                            "data": base64.b64encode(r["image"]).decode()})
        return content

    # -- graph ----------------------------------------------------------------

    def _briefs(self, ids: list[str]) -> dict[str, dict]:
        """Brief node info for each id that has one. Code symbols, then doc
        sections, then documents — the three kinds of node memory-mcp stores."""
        out: dict[str, dict] = {}
        if not ids:
            return out
        names = table_names(self.db)
        in_list = ", ".join(lit(i) for i in ids)
        if "code" in names:
            for r in self.db.open_table("code").search().where(f"id IN ({in_list})") \
                    .select(["id", "kind", "name", "file", "start"]).limit(None).to_list():
                out[r["id"]] = {k: r[k] for k in ("id", "kind", "name", "file", "start")}
        if self.docs_table in names:
            for r in self.db.open_table(self.docs_table).search().where(f"id IN ({in_list})") \
                    .select(["id", "name", "file", "start", "headings"]).limit(None).to_list():
                out[r["id"]] = {"id": r["id"], "kind": "Section", "name": r["name"],
                                "file": r["file"], "start": r["start"],
                                "headings": list(r["headings"] or [])}
        if "files" in names:
            for r in self.db.open_table("files").search() \
                    .where(f"corpus = 'docs' AND path IN ({in_list})") \
                    .select(["path", "name"]).limit(None).to_list():
                out.setdefault(r["path"], {"id": r["path"], "kind": "Document", "name": r["name"],
                                           "file": r["path"], "start": 1})
        return out

    def _neighbors(self, frontier: list[str], dir_: str, etype: str) -> dict[str, list[str]]:
        """One query for a whole BFS level. Neighbours come back sorted by id,
        which is the order autumn-memory's edge-key scan yields them in."""
        if not frontier:
            return {}
        in_list = ", ".join(lit(i) for i in frontier)
        nb: dict[str, list[str]] = {}
        if etype == "CONTAINS-DOC":
            # A document outline's CONTAINS edges are the docs table's parent column.
            for r in self._open(self.docs_table).search().where(f"parent IN ({in_list})") \
                    .select(["id", "parent"]).limit(None).to_list():
                nb.setdefault(r["parent"], []).append(r["id"])
        else:
            near, far = ("src", "dst") if dir_ == "out" else ("dst", "src")
            for r in self._open("edges").search() \
                    .where(f"type = {lit(etype)} AND {near} IN ({in_list})") \
                    .select([near, far]).limit(None).to_list():
                nb.setdefault(r[near], []).append(r[far])
        return {k: sorted(v) for k, v in nb.items()}

    def far_briefs(self, sid: str, dir_: str, etype: str = "CALLS") -> list[dict]:
        """find_callers / find_callees: an equality filter on one end of `edges`."""
        near, far = ("src", "dst") if dir_ == "out" else ("dst", "src")
        ids = sorted({r[far] for r in self._open("edges").search()
                      .where(f"type = {lit(etype)} AND {near} = {lit(sid)}")
                      .select([far]).limit(None).to_list()})
        b = self._briefs(ids)
        # A far end with no node is a dangling edge; memory-mcp drops those too.
        return [b[i] for i in ids if i in b]

    def traverse(self, start: str, dir_: str, etype: str, max_depth: int, max_nodes: int) -> list[dict]:
        """autumn-memory's bfs, a level at a time: the same visit order and
        cut-offs, with one query per level instead of one per node."""
        max_depth, max_nodes = min(max_depth, 16), min(max_nodes, 2000)
        visited, order = {start}, [(start, 0)]
        frontier, depth = [start], 0
        while frontier and depth < max_depth and len(order) < max_nodes:
            nb = self._neighbors(frontier, dir_, etype)
            nxt = []
            for node in frontier:
                for n in nb.get(node, []):
                    if n not in visited:
                        visited.add(n)
                        nxt.append(n)
            order.extend((n, depth + 1) for n in nxt)
            frontier, depth = nxt, depth + 1
        order = order[:max_nodes]
        b = self._briefs([n for n, _ in order])
        return [{**b.get(n, {"id": n}), "depth": d} for n, d in order]

    def documents(self) -> list[dict]:
        rows = self._open("files").search().where("corpus = 'docs'") \
            .select(["path", "name"]).limit(None).to_list()
        rows.sort(key=lambda r: r["path"])
        return [{"id": r["path"], "kind": "Document", "name": r["name"], "file": r["path"],
                 "start": 1} for r in rows[:500]]

    def ingest_documents(self, path: str) -> dict:
        import ingest_docs  # heavy and only needed here

        rel = path.strip("/")
        if self.s3 is not None:
            # Same shape as --index: <bucket>/<prefix> under the fs/ tree.
            try:
                rows, files = ingest_docs.build_s3(self.s3, rel)
            except S3Error as e:
                raise ToolError(f"gateway read failed for {path}: {e}") from None
            if not files:
                raise ToolError(f"no .md/.txt files under {path} via the gateway")
        else:
            if self.fs_root is None:
                raise ToolError("this instance was started without --fs-root or --s3-endpoint "
                                "and reads no files; ingest with ingest_docs.py instead")
            try:
                rows, files = ingest_docs.build(self.fs_root, rel)
            except FileNotFoundError:
                raise ToolError(f"path not found under --fs-root: {path}") from None
        if rows and self.emb is not None:
            ingest_docs.embed_rows(rows, self.emb)
        with self._lock:
            ingest_docs.write(self.db, rows, files, table=self.docs_table, under=rel)
        return {"files": len(files), "chunks": len(rows), "edges": len(rows)}


# -- MCP ------------------------------------------------------------------------

_ID = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
_QUERY = {"type": "object", "properties": {"query": {"type": "string"}, "mode": {"type": "string"},
                                           "k": {"type": "integer"}}, "required": ["query"]}
DOC_TOOLS = [
    {"name": "search_docs", "inputSchema": _QUERY, "description":
        "Search ingested documents (mode: lexical|vector|hybrid|auto). Returns chunks' source file, "
        "heading path, line range and score — enough to cite 'file › headings, lines a-b'; read the "
        "passage itself with read_file or get_symbol."},
    {"name": "list_documents", "inputSchema": {"type": "object", "properties": {}},
     "description": "List ingested document files."},
    {"name": "document_outline", "inputSchema": _ID, "description":
        "Heading outline of an ingested document (`id` = its file path, from list_documents or a "
        "chunk's `file`), depth-tagged."},
]

CODE_TOOLS = [
    {"name": "search_code", "inputSchema": _QUERY, "description":
        "Search the indexed codebase (mode: lexical|vector|hybrid|auto). Returns WHERE each match "
        "is — id, name, kind, file, start/end lines, score — and no source. Read what you want with "
        "read_file (a line range) or get_symbol (one whole symbol). Code only; use search_docs for prose."},
    {"name": "find_callers", "inputSchema": _ID, "description": "Symbols that call `id`."},
    {"name": "find_callees", "inputSchema": _ID, "description": "Symbols that `id` calls."},
    {"name": "trace_call_path", "description":
        "Bounded call-path from `id` (direction out=callees, in=callers).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "direction": {"type": "string"}},
                     "required": ["id"]}},
]

# Shared by doc and code corpora: reading a line range is the natural follow-up
# to any search hit, regardless of corpus kind. get_symbol returns one whole
# chunk (doc chunk OR code symbol) by id — useful when a hit's preview points
# at exactly the symbol you want.
READ_TOOLS = [
    {"name": "read_file", "description":
        "Read a line range of an indexed file: `path` is the `file` a search hit reports, and "
        "`start`/`end` are 1-based inclusive (omit for the whole file). The natural follow-up to a "
        "search hit's file+start+end. Capped at 400 lines per call; `truncated` says when the range was cut.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "start": {"type": "integer"},
                                                      "end": {"type": "integer"}}, "required": ["path"]}},
    {"name": "get_symbol", "inputSchema": _ID, "description":
        "Full text + metadata for an id — a code symbol (e.g. 'src/lib.rs::MemoryStore::add_edge') or "
        "a document chunk (e.g. 'docs/ops.md#L10-L42'). The natural follow-up when a search hit's id "
        "is exactly the unit you want."},
]

# ingest_documents is intentionally NOT advertised to any served corpus: all
# our corpora are built offline (ingest_docs.py / ingest_pdfs.py on a Mac) and
# pods are read-only. Keeping the tool hidden prevents the agent from trying
# to write into s3://lancedb — both against policy and because it lacks the
# credentials (autumn-s3 gateway forbids POST to /lancedb outside ingest).


PAGE_TOOLS = [
    {"name": "read_page", "description":
        "Whole pages of an ingested PDF, in page order: `file` is the `file` a search hit "
        "reports, `page_start`/`page_end` are 1-based inclusive page numbers. Use this instead "
        "of read_file for PDFs — a PDF's citation coordinate is the PAGE, not the line.",
     "inputSchema": {"type": "object", "properties": {"file": {"type": "string"},
                                                      "page_start": {"type": "integer"},
                                                      "page_end": {"type": "integer"}}, "required": ["file"]}},
    {"name": "page_images", "description":
        "The figures of a PDF page range (face-reading charts, diagrams). Returns each image as "
        "a picture you can look at. Call it after search hits on pages the question is about — "
        "a physiognomy answer that ignores the figure is half an answer.",
     "inputSchema": {"type": "object", "properties": {"file": {"type": "string"},
                                                      "page_start": {"type": "integer"},
                                                      "page_end": {"type": "integer"}}, "required": ["file"]}},
]


def tools_for(r: Retriever) -> list[dict]:
    """Tools advertised to this connection. The set depends on which tables
    the opened DB actually carries:
      - `docs`        → DOC_TOOLS (search_docs/list_documents/document_outline)
      - `symbols`     → CODE_TOOLS (search_code/get_symbol/find_callers/…)
      - `page_images` → PAGE_TOOLS (read_page/page_images, PDF-only)
    READ_TOOLS (read_file) is universal because hit coordinates are always
    file + line range (or file + page, read_page is the PDF variant).
    This conditional advertising is what lets ONE image serve the buda
    prose corpus, the mayi PDF+image corpus and the code-index corpus
    without any profile seeing tools that would error on its corpus — and
    without needing profile-awareness in the server at all.
    """
    names = table_names(r.db)
    tools: list[dict] = [*READ_TOOLS]
    if "docs" in names:
        tools.extend(DOC_TOOLS)
    if "code" in names:
        tools.extend(CODE_TOOLS)
    if "page_images" in names:
        tools.extend(PAGE_TOOLS)
    return tools


def call_tool(r: Retriever, name: str, args: dict) -> dict:
    s = lambda k: str(args.get(k) or "")  # noqa: E731
    i = lambda k: int(args[k]) if args.get(k) is not None else None  # noqa: E731
    try:
        if name in ("search_code", "search_docs"):
            data = r.search("code" if name == "search_code" else "docs", s("query"),
                            s("mode") or "auto", i("k") or 8)
        elif name == "get_symbol":
            data = r.get_symbol(s("id"))
        elif name == "read_file":
            data = r.read_file(s("path"), i("start"), i("end"))
        elif name == "find_callers":
            data = r.far_briefs(s("id"), "in")
        elif name == "find_callees":
            data = r.far_briefs(s("id"), "out")
        elif name == "trace_call_path":
            data = r.traverse(s("id"), s("direction") or "out", "CALLS", 6, 200)
        elif name == "ingest_documents":
            data = r.ingest_documents(s("path"))
        elif name == "read_page":
            data = r.read_page(s("file"), i("page_start"), i("page_end"))
        elif name == "page_images":
            data = r.page_images(s("file"), i("page_start"), i("page_end"))
        elif name == "list_documents":
            data = r.documents()
        elif name == "document_outline":
            data = r.traverse(s("id"), "out", "CONTAINS-DOC", 8, 500)
        else:
            raise ToolError(f"unknown tool {name}")
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    if isinstance(data, Content):
        return {"content": data}
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


def dispatch(r: Retriever, method: str, params: dict):
    if method == "initialize":
        return {"protocolVersion": "2024-11-05",
                "serverInfo": {"name": "lance-mcp", "version": "0.1.0"},
                "capabilities": {"tools": {}}}
    if method == "tools/list":
        return {"tools": tools_for(r)}
    if method == "tools/call":
        return call_tool(r, params.get("name", ""), params.get("arguments") or {})
    if method == "ping":
        return {}
    return None


def handle_one(r: Retriever, msg: dict) -> dict | None:
    mid = msg.get("id")
    try:
        result = dispatch(r, msg.get("method", ""), msg.get("params") or {})
        if mid is None:
            return None  # a notification: nobody to answer, even on failure
        if result is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # noqa: BLE001 — one bad call must not end the server
        log.exception("mcp %s failed", msg.get("method"))
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def make_handler(r: Retriever, info: dict):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._send(200, json.dumps(info).encode())
            elif self.path == "/config":
                self._send(200, json.dumps({**info, "modes": {
                    "docs": r.modes(r.docs_table), "code": r.modes("code")}}).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                return self._send(404, b'{"error":"not found"}')
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                msg = json.loads(raw)
            except ValueError as e:
                return self._send(400, json.dumps({"error": f"mcp: request is not JSON: {e}"}).encode())
            if isinstance(msg, list):
                replies = [x for x in (handle_one(r, m) for m in msg) if x is not None]
                payload = replies or None
            else:
                payload = handle_one(r, msg)
            if payload is None:
                return self._send(202)
            self._send(200, json.dumps(payload, ensure_ascii=False).encode())

        def log_message(self, fmt, *a):
            log.debug("%s " + fmt, self.address_string(), *a)

    return H


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-path", required=True,
                    help="absolute LanceDB directory, or s3://bucket/prefix with --s3-endpoint")
    ap.add_argument("--s3-endpoint", help="autumn-s3 gateway URL; db-path and the corpus the "
                    "ingest_documents tool reads go through it")
    ap.add_argument("--embed-url", help="OpenAI-style embeddings server; unset = lexical only")
    ap.add_argument("--embed-model", default="bge-m3")
    ap.add_argument("--fs-root", type=Path, help="enables ingest_documents over this local tree")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5102)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    s3 = Gateway(args.s3_endpoint) if args.s3_endpoint else None
    if s3 is None and is_s3_uri(args.db_path):
        ap.error("--db-path is s3:// but --s3-endpoint is not set")
    # Tables are re-checked for new versions at most every few seconds, so an
    # ingest by another process shows up without a restart.
    db = connect(args.db_path, read_consistency_interval=timedelta(seconds=5),
                 storage_options=s3_storage_options(args.s3_endpoint) if s3 else None)
    emb = Embedder(args.embed_url, args.embed_model) if args.embed_url else None
    if emb is not None:
        # One vector before serving: a wrong URL fails here, at the mistake,
        # not on somebody's first search.
        emb.embed("lance-mcp startup probe")
        log.info("embedder ready: %s, %d dims", emb.url, emb.dim)
    r = Retriever(db, emb, args.fs_root.expanduser().resolve() if args.fs_root else None,
                  s3=s3)
    tables = sorted(table_names(db))
    info = {"server": "lance-mcp", "db": str(args.db_path),
            "tables": tables, "embedder": args.embed_model if emb else "none"}
    log.info("tables %s; docs modes %s%s", tables, r.modes("docs"),
             f", code modes {r.modes('code')}" if "code" in tables else "")
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(r, info))
    log.info("lance-mcp → http://%s:%d/mcp", args.host, args.port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
