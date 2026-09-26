"""The HTTP layer: `ThreadingHTTPServer`, stdlib only.

## Why not aiohttp any more

Two reasons, and the second is the one that decides it.

The agent runs in this process now and `run_conversation` BLOCKS, so on an event
loop every turn would have to be handed to an executor and every callback
handed back across `call_soon_threadsafe` — a thread boundary in the middle of
the hottest path, for a server whose concurrency is a handful of turns. A
thread-per-request server has the agent, its callbacks and the SSE writer all on
one thread, and the boundary disappears.

The deciding reason: importing `run_agent` means running inside HERMES' venv,
and hermes' own session bridge exists precisely because "the two venvs share
most of their packages and disagree on some". aiohttp was this server's ONLY
third-party import. Without it there is nothing left to disagree about.

## What this module is not

No routing framework, no middleware stack — a dict of handlers and two checks
that run before them. The handlers themselves live where their logic does; this
file is the transport.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import mimetypes
import secrets
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("deepwiki.http")

# The URL namespace the static assets live under. `index.html` and `app.js`
# reference `/static/...`, and the aiohttp server this module replaced mounted
# them with `add_static("/static/", STATIC)`.
STATIC_PREFIX = "/static/"
# Where a diagram the agent drew is served from. A second mount rather than a
# subdirectory of `static/`: these files are WRITTEN BY THE AGENT at runtime and
# live on the volume, while everything under `static/` is shipped in the image
# and read-only. Keeping the two apart is what lets the CSP below apply to one
# and not the other.
ARTIFACTS_PREFIX = "/artifacts/"

# The media types this server actually serves, spelled out rather than asked
# for. `mimetypes.guess_type` reads the PLATFORM's database — /etc/mime.types
# and friends — and the slim image this runs in ships none of them
# (`mimetypes.knownfiles` resolves to nothing there). So every .woff2 came back
# `application/octet-stream` in the pod while the same call on a developer's
# macOS returned `font/woff2` off /etc/apache2/mime.types: a bug that cannot
# reproduce where it is written.
#
# The visible cost was not the type, it was the CACHE. The immutable branch in
# `serve_static` keys on `font/`, so the fonts fell back to no-cache and went
# back to costing a revalidation round trip each, per navigation — the exact
# round trips self-hosting them was meant to remove. An asset server should not
# take the contract it serves from a file it does not install.
CONTENT_TYPES = {
    ".html": "text/html", ".css": "text/css", ".js": "application/javascript",
    ".mjs": "application/javascript", ".json": "application/json",
    ".md": "text/markdown", ".txt": "text/plain", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
    # Media the agent generates lands here as files too — an <img>/<video> the
    # transcript renders inline is only as good as the type the response
    # carries: a video served as octet-stream downloads instead of playing.
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm",
    ".mov": "video/quicktime",
}

# Tells one BROWSER from another. Not authentication — everyone here shares one
# credential; this only separates two people's cursors and double-click
# detection. A cookie rather than a header because `EventSource` cannot set
# headers, and a cookie rides every request including the SSE one.
CLIENT_COOKIE = "deepwiki_cid"

# Compress responses large enough for the saved transfer time to outweigh the
# envelope and CPU cost. SSE is represented by `Streaming` and never reaches
# this path; buffering it for gzip would destroy its first-token latency.
GZIP_MIN_BYTES = 1024


def _accepts_gzip(value: str) -> bool:
    """Whether an RFC-style Accept-Encoding value permits gzip."""
    accepted: dict[str, float] = {}
    for raw in (value or "").split(","):
        parts = [part.strip() for part in raw.split(";")]
        coding = parts[0].lower()
        if not coding:
            continue
        quality = 1.0
        for param in parts[1:]:
            name, sep, val = param.partition("=")
            if sep and name.strip().lower() == "q":
                try:
                    quality = float(val.strip())
                except ValueError:
                    quality = 0.0
        accepted[coding] = quality
    # An explicit gzip entry wins over `*`, including `gzip;q=0`.
    return accepted.get("gzip", accepted.get("*", 0.0)) > 0


def _maybe_gzip(req: "Request", resp: "Response") -> "Response":
    """Apply ordinary HTTP gzip content negotiation to a buffered response."""
    if not resp.body or len(resp.body) < GZIP_MIN_BYTES:
        return resp
    content_type = next(
        (v.lower() for k, v in resp.headers if k.lower() == "content-type"), ""
    )
    compressible = (
        content_type.startswith("text/")
        or content_type.startswith("application/json")
        or content_type.startswith("application/javascript")
        or content_type.startswith("image/svg+xml")
    )
    if not compressible:
        return resp

    # Shared caches must keep the compressed and identity representations apart.
    vary_index = next(
        (i for i, (k, _) in enumerate(resp.headers) if k.lower() == "vary"), None
    )
    if vary_index is None:
        resp.headers.append(("Vary", "Accept-Encoding"))
    else:
        key, value = resp.headers[vary_index]
        if "accept-encoding" not in {v.strip().lower() for v in value.split(",")}:
            resp.headers[vary_index] = (key, value + ", Accept-Encoding")

    if not _accepts_gzip(req.headers.get("Accept-Encoding", "")):
        return resp
    if any(k.lower() == "content-encoding" for k, _ in resp.headers):
        return resp
    resp.body = gzip.compress(resp.body, compresslevel=6)
    resp.headers.append(("Content-Encoding", "gzip"))
    return resp


class Request:
    """What a handler is given. Deliberately small."""

    __slots__ = ("method", "path", "query", "headers", "body", "client_id", "_h")

    def __init__(self, h: BaseHTTPRequestHandler, client_id: str) -> None:
        parsed = urlparse(h.path)
        self.method = h.command
        self.path = parsed.path
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = h.headers
        self.client_id = client_id
        self.body = b""
        self._h = h
        if h.command in ("POST", "PUT", "PATCH"):
            try:
                n = int(h.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            # Cap it: this endpoint takes prompts, not uploads, and an unbounded
            # read is a way to make the server hold memory on request.
            self.body = h.rfile.read(min(n, 8 << 20)) if n > 0 else b""

    def json(self) -> dict:
        try:
            return json.loads(self.body or b"{}")
        except Exception:  # noqa: BLE001
            return {}


class Response:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int = 200, headers: list | None = None, body: bytes = b"") -> None:
        self.status = status
        self.headers = headers or []
        self.body = body


def json_response(obj: Any, status: int = 200) -> Response:
    body = json.dumps(obj, ensure_ascii=False).encode()
    return Response(status, [("Content-Type", "application/json; charset=utf-8")], body)


class Streaming(Response):
    """A response the handler writes itself.

    `pump(write)` is called with a function that puts bytes on the wire
    immediately — the point of SSE is that the reader sees the first token
    before the last one exists, so nothing here may buffer to the end.
    """

    __slots__ = ("pump",)

    def __init__(self, headers: list, pump: Callable[[Callable[[bytes], None]], None]) -> None:
        super().__init__(200, headers, b"")
        self.pump = pump


class App:
    def __init__(
        self,
        static_dir: Path | None = None,
        artifacts_dir: Path | None = None,
        auth_user: str = "",
        auth_pass: str = "",
    ) -> None:
        self.routes: dict[tuple[str, str], Callable[[Request], Response]] = {}
        self.static_dir = static_dir
        self.artifacts_dir = artifacts_dir
        self.auth_user = auth_user
        self.auth_pass = auth_pass

    def route(self, method: str, path: str):
        def deco(fn):
            self.routes[(method, path)] = fn
            return fn

        return deco

    # ── the two checks that run before any handler ─────────────────────────

    def authorised(self, headers) -> bool:
        """Basic auth, both halves compared in constant time.

        A plain `==` on the USER leaks its length through timing just as surely
        as one on the password.
        """
        if not self.auth_user:
            return True
        hdr = headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, passwd = base64.b64decode(hdr[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(user, self.auth_user) and hmac.compare_digest(
            passwd, self.auth_pass
        )

    @staticmethod
    def client_id_for(cookie_header: str | None) -> tuple[str, bool]:
        """`(id, is_fresh)` for this caller.

        Minted on WHATEVER the caller asked for first, not only on `/`: anything
        reaching an API endpoint without having loaded the page — a second tab
        restored from history, a cleared cookie, curl — would otherwise share
        the same empty id, and two of them read as one person, which is the bug
        this mechanism exists to prevent.

        The id is decided BEFORE the handler runs, so a caller whose very first
        request is an API call is attributed to the id it is about to receive.
        """
        if cookie_header:
            for part in cookie_header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == CLIENT_COOKIE and value:
                    return value, False
        return secrets.token_hex(8), True

    # ── dispatch ───────────────────────────────────────────────────────────

    def handle(self, req: Request) -> Response:
        fn = self.routes.get((req.method, req.path))
        if fn is not None:
            return fn(req)
        if req.method == "GET":
            served = self.serve_static(req)
            if served is not None:
                return served
        return json_response({"error": "not found"}, status=404)

    def serve_static(self, req: Request) -> Response | None:
        """Files under `static_dir` (`/static/`) or `artifacts_dir` (`/artifacts/`).

        The prefix is part of the contract, not decoration. `index.html` asks
        for `/static/style.css`, `/static/app.js` and the two vendor scripts,
        and the aiohttp server this replaced mounted them with
        `add_static("/static/", STATIC)`. Serving the same bytes at the URL
        root instead answers every one of those with 404 while `/` itself
        still returns 200 — so the page loads, blank and unstyled, with a
        working API behind it and nothing but console errors to say why.

        The traversal guard is `resolve()` + `is_relative_to`, not a scan for
        "..": a symlink inside the directory reaches outside it without the
        string ever containing one.

        `/static/` is served only from the image. Runtime media and documents
        belong in the PVC-backed `/artifacts/` mount; keeping the two namespaces
        disjoint makes a frontend deploy atomic from the browser's perspective.
        """
        pairs: list[tuple[str, Path]] = []
        if self.static_dir is not None:
            pairs.append((STATIC_PREFIX, self.static_dir))
        if self.artifacts_dir is not None:
            pairs.append((ARTIFACTS_PREFIX, self.artifacts_dir))
        rel = None
        for prefix, root in pairs:
            if not req.path.startswith(prefix):
                continue
            r = req.path[len(prefix):]
            if not r:
                continue
            try:
                target = (root / r).resolve()
                if not target.is_relative_to(root.resolve()) or not target.is_file():
                    continue
                body = target.read_bytes()
                rel, matched_prefix = r, prefix
                break
            except (OSError, ValueError):
                continue
        if rel is None:
            return None
        ctype = (CONTENT_TYPES.get(target.suffix.lower())
                 or mimetypes.guess_type(str(target))[0]
                 or "application/octet-stream")
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        # Fonts are immutable by filename convention. Other shipped assets are
        # immutable only when addressed through the versioned URL emitted by
        # `_versioned_page`; artifacts remain mutable even with a query string.
        versioned = matched_prefix == STATIC_PREFIX and bool(req.query.get("v"))
        immutable = ctype.startswith("font/") or versioned
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        headers = [("Content-Type", ctype), ("Cache-Control", cache)]
        # A content-addressed URL is its own validator. Only mutable URLs need
        # an ETag and therefore pay the hashing cost when they are requested.
        etag = None if immutable else f'"{hashlib.sha256(body).hexdigest()[:16]}"'
        if etag is not None:
            headers.append(("ETag", etag))
        if matched_prefix == ARTIFACTS_PREFIX:
            # An artifact is a page the MODEL wrote, served from this app's
            # own origin, and it carries its own inline script — that is what
            # makes an Archify diagram explorable. Same origin means that
            # script can reach `/api/*` as the reader, so it is boxed in:
            # everything it needs is inline or a data: URI already, and this
            # policy permits exactly that and no fetch, no frame, no origin
            # but itself. The one thing it takes away from a self-contained
            # artifact is the ability to call home.
            headers.append(("Content-Security-Policy",
                            "default-src 'none'; img-src 'self' data: blob:; "
                            "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                            "font-src data:; media-src blob: data:; "
                            "connect-src 'none'; frame-ancestors 'none'; base-uri 'none'"))
            headers.append(("X-Content-Type-Options", "nosniff"))
        if etag is not None and req.headers.get("If-None-Match") == etag:
            return Response(304, [("ETag", etag), ("Cache-Control", cache)])
        return Response(200, headers, body)


class _Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _serve(self) -> None:
        # The health endpoint stays open so a k8s probe needs no credential, and
        # the static assets are useless without the API behind them.
        if self.path.split("?")[0] != "/healthz" and not self.app.authorised(self.headers):
            body = b"unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="deepwiki"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        cid, fresh = self.app.client_id_for(self.headers.get("Cookie"))
        try:
            req = Request(self, cid)
            resp = self.app.handle(req)
        except Exception:  # noqa: BLE001
            log.exception("handler raised for %s", self.path)
            resp = json_response({"error": "internal error"}, status=500)

        cookie = (
            [(
                "Set-Cookie",
                f"{CLIENT_COOKIE}={cid}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax",
            )]
            if fresh
            else []
        )

        if isinstance(resp, Streaming):
            self.send_response(resp.status)
            for k, v in list(resp.headers) + cookie:
                self.send_header(k, v)
            # Chunked, because the length is unknowable and HTTP/1.1 keep-alive
            # would otherwise wait for a Content-Length that never comes.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(b: bytes) -> None:
                if not b:
                    return
                self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
                self.wfile.flush()

            try:
                resp.pump(write)
            except (BrokenPipeError, ConnectionResetError):
                # The reader went away. Normal; the turn is unaffected.
                return
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        resp = _maybe_gzip(req, resp)
        self.send_response(resp.status)
        for k, v in list(resp.headers) + cookie:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Restarting after a deploy must not fail on a socket still in TIME_WAIT.
    allow_reuse_address = True


def serve(app: App, host: str, port: int) -> Server:
    handler = type("BoundHandler", (_Handler,), {"app": app})
    srv = Server((host, port), handler)
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    return srv
