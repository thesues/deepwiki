"""Tests for the S3 paths: the gateway HTTP layer, the corpus reader, and the
marker/ensure logic that decides "already ingested".

    python -m pytest test_s3.py -q      (or: python test_s3.py)

No cluster and no lancedb S3 store: the HTTP layer runs against a canned
in-process server, the corpus reader against a fake gateway object, and the
corpus it reads is the same files a local-path ingest reads — so build_s3 and
build must produce byte-identical rows for the same tree, which is what makes
"no re-digest" true rather than hoped-for.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import s3 as s3mod
from ingest_docs import build, build_s3, is_indexable, rows_for_text
from s3 import Gateway, S3Error

CORPUS = Path(__file__).parent / "testdata_s3"


# -- the HTTP layer, against a canned server ----------------------------------

XMLNS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


class FakeS3Handler(BaseHTTPRequestHandler):
    objects: dict[str, bytes] = {}
    routes: dict[str, tuple[int, str]] = {}  # "GET /b/k" -> (status, body)

    def log_message(self, *a):  # noqa: N802 — quiet
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n)

    def _reply(self, status: int, body: bytes = b""):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str):
        route = self.routes.get(f"{method} {self.path}")
        if route:
            self._reply(*route)
            return
        from urllib.parse import unquote
        key = unquote(self.path.lstrip("/").split("?")[0])
        if method == "PUT":
            FakeS3Handler.objects[key] = self._body()
            self._reply(200)
        if method == "GET":
            if "list-type=2" in self.path:
                from urllib.parse import unquote
                bucket = self.path.lstrip("/").split("?")[0]
                prefix = unquote(self.path.split("prefix=")[-1].split("&")[0])
                keys = [k for k in sorted(FakeS3Handler.objects)
                        if k.startswith(f"{bucket}/{prefix}")]
                listing = (f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult {XMLNS}>'
                           # real S3 lists keys relative to the bucket
                           + "".join(f"<Contents><Key>{k[len(bucket) + 1:]}</Key></Contents>" for k in keys)
                           + "<IsTruncated>false</IsTruncated></ListBucketResult>").encode()
                self._reply(200, listing)
            elif key in FakeS3Handler.objects:
                self._reply(200, FakeS3Handler.objects[key])
            else:
                self._reply(404, b"no such key")
        elif method == "HEAD":
            self._reply(200 if key in FakeS3Handler.objects else 404)
        else:
            self._reply(405)

    def do_GET(self):    self._handle("GET")   # noqa: N802
    def do_PUT(self):    self._handle("PUT")   # noqa: N802
    def do_HEAD(self):   self._handle("HEAD")  # noqa: N802


def _serve() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_gateway_roundtrip_and_errors():
    srv, url = _serve()
    try:
        FakeS3Handler.objects.clear()
        FakeS3Handler.routes.clear()
        g = Gateway(url)
        g.put("bkt", "dir/a.md", b"hello")
        assert FakeS3Handler.objects["bkt/dir/a.md"] == b"hello"
        assert g.exists("bkt", "dir/a.md") and not g.exists("bkt", "dir/missing")
        assert g.get("bkt", "dir/a.md") == b"hello"
        # keys with characters that must survive quoting
        g.put("bkt", "dir/a b#c?.md", b"x")
        assert g.get("bkt", "dir/a b#c?.md") == b"x"
        assert g.list("bkt", "dir/") == ["dir/a b#c?.md", "dir/a.md"]
        assert g.list("bkt", "dir/", limit=1) == ["dir/a b#c?.md"]
        try:
            g.get("bkt", "missing")
            raise AssertionError("expected S3Error")
        except S3Error:
            pass
        # a non-404 unexpected status is raised, never mistaken for a miss
        FakeS3Handler.routes["HEAD /bkt/broken"] = (500, b"boom")
        try:
            g.exists("bkt", "broken")
            raise AssertionError("expected S3Error on 500")
        except S3Error:
            pass
    finally:
        srv.shutdown()


def test_gateway_list_pagination():
    """A listing that comes back in two truncated pages yields every key once."""
    pages = iter([
        (200, (f'<?xml version="1.0"?><ListBucketResult {XMLNS}>'
               '<Contents><Key>k1</Key></Contents>'
               "<IsTruncated>true</IsTruncated>"
               "<NextContinuationToken>t2</NextContinuationToken>"
               "</ListBucketResult>").encode()),
        (200, (f'<?xml version="1.0"?><ListBucketResult {XMLNS}>'
               '<Contents><Key>k2</Key></Contents>'
               "<IsTruncated>false</IsTruncated>"
               "</ListBucketResult>").encode()),
    ])
    seen = []

    class PagedGateway(Gateway):
        def _request(self, method, bucket, key="", query=""):
            seen.append(query)
            status, body = next(pages)
            return status, body

    out = PagedGateway("http://unused").list("bkt", "pre/")
    assert out == ["k1", "k2"], out
    assert "continuation-token=t2" in seen[1], seen


# -- the corpus reader: S3 build == local build -------------------------------

def _fake_gateway_from_dir(root: Path, bucket: str) -> object:
    """A gateway duck-type over a directory laid out as the fs/ tree: the
    bucket is root's first-level directory, keys are the rest."""
    files = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*") if p.is_file()}   # keys: "docs/buda/a.md"

    class FakeGateway:
        def list(self, bucket, prefix, limit=None):
            keys = sorted(k[len(bucket) + 1:] for k in files
                          if k.startswith(f"{bucket}/{prefix}"))
            return keys[:limit] if limit is not None else keys

        def get(self, bucket, key):
            return files[f"{bucket}/{key}"]

    return FakeGateway()


def test_build_s3_matches_local_build():
    """Same tree, both paths in: identical ids, file paths and text — so a
    table written through one access path never needs re-ingesting. The local
    call uses the production shape: --fs-root at the fs/ tree, --index
    docs/buda under it."""
    gw = _fake_gateway_from_dir(CORPUS, "docs")
    s3_rows, s3_files = build_s3(gw, "docs/buda")
    fs_rows, fs_files = build(CORPUS, "docs/buda")
    assert [r["id"] for r in s3_rows] == [r["id"] for r in fs_rows]
    assert s3_rows == fs_rows
    assert s3_files == fs_files


def test_is_indexable_collect_rules():
    assert is_indexable("docs/buda/a.md")
    assert is_indexable("docs/buda/sub/b.txt")
    assert not is_indexable("docs/buda/._a.md")          # AppleDouble sidecar
    assert not is_indexable("docs/buda/a.md.bak")        # not a doc extension
    assert not is_indexable("docs/buda/.x/a.md")         # dot directory
    assert not is_indexable("docs/buda/target/a.md")     # excluded directory
    assert not is_indexable("docs/buda/sub/c.py")        # wrong extension


def test_rows_for_text_identifies_file_and_parent():
    text = "# H1\n\nbody one\n\n## H2\n\nbody two\n"
    rows, file_row = rows_for_text(text, "docs/buda/x.md", "x.md")
    assert file_row["path"] == "docs/buda/x.md"
    assert file_row["name"] == "x.md"
    assert rows[0]["id"] == "docs/buda/x.md#L1-L3"
    assert rows[0]["text"].startswith("docs/buda/x.md › H1")
    # the second chunk's parent is the first chunk id, its breadcrumb the full path
    assert rows[1]["parent"] == rows[0]["id"]
    assert rows[1]["text"].startswith("docs/buda/x.md › H1 › H2")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} — ok")
    print("\nall s3 tests passed")
