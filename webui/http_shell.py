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
import hmac
import json
import logging
import mimetypes
import os
import secrets
import socketserver
import subprocess
import threading
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse

import brotli
from auth import AuthenticationError, AuthenticationUnavailable

log = logging.getLogger("deepwiki.http")

# The URL namespace the static assets live under. `index.html` and `app.js`
# reference `/static/...`, and the aiohttp server this module replaced mounted
# them with `add_static("/static/", STATIC)`.
STATIC_PREFIX = "/static/"
# Browsers still probe this conventional root URL when opening an old page or
# restoring a tab that predates the explicit <link rel="icon">. Keep one
# canonical SVG on disk and expose this as a compatibility alias, rather than
# shipping two icon files that can drift apart.
FAVICON_PATH = "/favicon.ico"
# Where a diagram the agent drew is served from. A second mount rather than a
# subdirectory of `static/`: these files are WRITTEN BY THE AGENT at runtime and
# live on the volume, while everything under `static/` is shipped in the image
# and read-only. Keeping the two apart is what lets the CSP below apply to one
# and not the other.
ARTIFACTS_PREFIX = "/artifacts/"

ARTIFACT_CSP = (
    "default-src 'none'; img-src 'self' data: blob:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "font-src data:; media-src blob: data:; "
    "connect-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)

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
# this path; buffering it for compression would destroy its first-token latency.
COMPRESSION_MIN_BYTES = 1024


def _parse_byte_range(value: str, size: int) -> tuple[int, int]:
    """Parse one RFC 9110 byte range into an inclusive ``(start, end)``.

    Browser media requests use only a single range. Multipart ranges add a
    different response body format and no benefit for this UI, so they are
    rejected as unsatisfiable rather than accidentally answered with the whole
    generated video.
    """
    unit, sep, raw = (value or "").partition("=")
    if not sep or unit.strip().lower() != "bytes" or "," in raw or size <= 0:
        raise ValueError("unsupported byte range")
    first, dash, last = raw.strip().partition("-")
    if not dash:
        raise ValueError("malformed byte range")
    try:
        if first:
            start = int(first)
            end = int(last) if last else size - 1
            if start < 0 or start >= size or end < start:
                raise ValueError("unsatisfiable byte range")
            return start, min(end, size - 1)
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("empty suffix range")
        return max(0, size - suffix), size - 1
    except (TypeError, ValueError) as exc:
        raise ValueError("malformed byte range") from exc


def _parse_http_timestamp(value: str | None) -> int | None:
    """Parse an HTTP date as UTC seconds, or ignore an invalid validator."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:  # obsolete RFC 850/asctime forms
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _if_none_match_matches(value: str | None, etag: str) -> bool:
    """Apply If-None-Match's weak comparison to one current strong ETag."""
    if value is None:
        return False
    for candidate in value.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].lstrip()
        if candidate == etag:
            return True
    return False


def _accepts_encoding(value: str, encoding: str) -> bool:
    """Whether an RFC-style Accept-Encoding value permits one coding."""
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
    # An explicit entry wins over `*`, including an explicit q=0 refusal.
    return accepted.get(encoding, accepted.get("*", 0.0)) > 0


def _maybe_compress(req: "Request", resp: "Response") -> "Response":
    """Apply Brotli content negotiation to a buffered response."""
    # A byte range is defined over the identity representation. Compressing a
    # 206 body after Content-Range was calculated would make its offsets false.
    if resp.status == 206:
        return resp
    if not resp.body or len(resp.body) < COMPRESSION_MIN_BYTES:
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

    if not _accepts_encoding(req.headers.get("Accept-Encoding", ""), "br"):
        return resp
    if any(k.lower() == "content-encoding" for k, _ in resp.headers):
        return resp
    # Quality 5 keeps dynamic-response CPU bounded while retaining most of
    # Brotli's density advantage. Quality 11 is inappropriate for live JSON.
    resp.body = brotli.compress(resp.body, quality=5)
    resp.headers.append(("Content-Encoding", "br"))
    return resp


class Request:
    """What a handler is given. Deliberately small."""

    __slots__ = (
        "method", "path", "query", "headers", "body", "client_id",
        "principal", "user_id", "_h",
    )

    def __init__(self, h: BaseHTTPRequestHandler, client_id: str, principal=None) -> None:
        parsed = urlparse(h.path)
        self.method = h.command
        self.path = parsed.path
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.headers = h.headers
        self.client_id = client_id
        self.principal = principal
        self.user_id = principal.user_id if principal is not None else ""
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
        authenticator=None,
    ) -> None:
        self.routes: dict[tuple[str, str], Callable[[Request], Response]] = {}
        self.static_dir = static_dir
        self.artifacts_dir = artifacts_dir
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        if authenticator is not None and (auth_user or auth_pass):
            raise ValueError("JWT and Basic Auth cannot be combined")
        self.authenticator = authenticator
        self.artifact_authorizer = None
        # Poster creation is rare and ffmpeg is expensive. One process-wide
        # lock is simpler than a lock table that itself needs eviction; unique
        # artifact paths mean unrelated posters are not on a hot request path.
        self._poster_lock = threading.Lock()

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
        """Dispatch filesystem requests like nginx ``location`` prefixes."""
        if req.path == FAVICON_PATH:
            target = self._resolve_location(
                f"{STATIC_PREFIX}favicon.svg", STATIC_PREFIX, self.static_dir
            )
            if target is None:
                return None
            # `/favicon.ico` is a stable, unversioned compatibility URL, so it
            # must revalidate. The page itself uses the versioned SVG below.
            return self._serve_revalidating_file(req, target, "image/svg+xml")

        if req.path.startswith(STATIC_PREFIX):
            target = self._resolve_location(req.path, STATIC_PREFIX, self.static_dir)
            if target is None:
                return None
            ctype = self._content_type(target)
            # Fonts are immutable by filename convention. Other shipped assets
            # are immutable only through the version emitted by `_versioned_page`.
            if ctype.startswith("font/") or bool(req.query.get("v")):
                return self._serve_immutable_file(req, target, ctype)
            return self._serve_revalidating_file(req, target, ctype)

        if req.path.startswith(ARTIFACTS_PREFIX):
            if req.user_id:
                candidate = self._resolve_candidate(req.path, ARTIFACTS_PREFIX, self.artifacts_dir)
                if candidate is None or self.artifact_authorizer is None:
                    return json_response({"error": "not found"}, status=404)
                relative = candidate.relative_to(self.artifacts_dir.resolve())
                if not relative.parts or not self.artifact_authorizer(req, relative.parts[0]):
                    return json_response({"error": "not found"}, status=404)
            target = self._resolve_location(
                req.path, ARTIFACTS_PREFIX, self.artifacts_dir
            )
            if target is None:
                target = self._generate_artifact_poster(req.path)
            if target is None:
                return None
            return self._serve_immutable_artifact(
                req,
                target,
                self._content_type(target),
                extra_headers=[
                    ("Content-Security-Policy", ARTIFACT_CSP),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )

        return None

    def _generate_artifact_poster(self, path: str) -> Path | None:
        """Lazily create ``video.jpg`` beside ``video.mp4`` on first request.

        The browser requests this only when the video approaches the viewport.
        Publishing via a hard link keeps immutable artifact URLs honest even
        if two requests arrive together or another process wins the race.
        """
        poster = self._resolve_candidate(path, ARTIFACTS_PREFIX, self.artifacts_dir)
        if poster is None or poster.suffix.lower() != ".jpg":
            return None
        with self._poster_lock:
            if poster.is_file():
                return poster
            video = next(
                (poster.with_suffix(ext) for ext in (".mp4", ".webm", ".mov", ".m4v")
                 if poster.with_suffix(ext).is_file()),
                None,
            )
            if video is None:
                return None
            candidate = poster.with_name(
                f".{poster.stem}.{secrets.token_hex(8)}.tmp.jpg"
            )
            try:
                done = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-ss", "0.1", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=640:-2:force_original_aspect_ratio=decrease",
                        "-q:v", "5", "-f", "image2", str(candidate),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=30,
                    check=False,
                )
                if done.returncode != 0 or not candidate.is_file() or candidate.stat().st_size == 0:
                    log.warning("poster generation failed for %s: %s", video, done.stderr[-1000:])
                    return None
                try:
                    os.link(candidate, poster)
                except FileExistsError:
                    pass
                return poster if poster.is_file() else None
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("poster generation failed for %s: %s", video, exc)
                return None
            finally:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _resolve_location(path: str, prefix: str, root: Path | None) -> Path | None:
        """Resolve one location without allowing traversal or escaping symlinks."""
        target = App._resolve_candidate(path, prefix, root)
        return target if target is not None and target.is_file() else None

    @staticmethod
    def _resolve_candidate(path: str, prefix: str, root: Path | None) -> Path | None:
        """Resolve a possibly-not-yet-created file under one location root."""
        if root is None:
            return None
        rel = path[len(prefix):]
        if not rel:
            return None
        try:
            resolved_root = root.resolve()
            target = (resolved_root / rel).resolve()
            if target.is_relative_to(resolved_root):
                return target
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _content_type(target: Path) -> str:
        ctype = (CONTENT_TYPES.get(target.suffix.lower())
                 or mimetypes.guess_type(str(target))[0]
                 or "application/octet-stream")
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return ctype

    @staticmethod
    def _serve_immutable_file(
        req: Request,
        target: Path,
        ctype: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Response | None:
        """Serve a content-versioned file without a redundant validator."""
        return App._send_file(
            req,
            target,
            ctype,
            "public, max-age=31536000, immutable",
            use_last_modified=False,
            use_metadata_etag=False,
            extra_headers=extra_headers,
        )

    @staticmethod
    def _serve_immutable_artifact(
        req: Request,
        target: Path,
        ctype: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Response | None:
        """Serve a unique artifact with a cheap strong validator for ranges."""
        return App._send_file(
            req,
            target,
            ctype,
            "private, no-cache" if req.user_id else "public, max-age=31536000, immutable",
            use_last_modified=False,
            use_metadata_etag=True,
            extra_headers=extra_headers,
        )

    @staticmethod
    def _serve_revalidating_file(
        req: Request,
        target: Path,
        ctype: str,
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Response | None:
        """Serve a mutable file that must be revalidated before cache reuse."""
        return App._send_file(
            req,
            target,
            ctype,
            "no-cache",
            use_last_modified=True,
            use_metadata_etag=False,
            extra_headers=extra_headers,
        )

    @staticmethod
    def _send_file(
        req: Request,
        target: Path,
        ctype: str,
        cache: str,
        *,
        use_last_modified: bool,
        use_metadata_etag: bool,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> Response | None:
        """Common response path: validator first, then Range, then full body."""
        try:
            file_stat = target.stat()
        except OSError:
            return None

        headers = [
            ("Content-Type", ctype),
            ("Cache-Control", cache),
            ("Accept-Ranges", "bytes"),
        ]
        if extra_headers:
            headers.extend(extra_headers)

        mtime_secs = int(file_stat.st_mtime)
        last_modified = None
        if use_last_modified:
            # Mutable filesystem-backed resources use the filesystem's own
            # validator. This is O(1) even for generated videos and avoids
            # reading an unversioned static asset merely to hash it. HTTP dates
            # have one-second resolution; our writers do not replace the same
            # pathname more than once within a second.
            last_modified = formatdate(mtime_secs, usegmt=True)
            headers.append(("Last-Modified", last_modified))

        etag = None
        if use_metadata_etag:
            # Artifact paths are UUID/content-unique and never overwritten, so
            # this O(1) identity is strong without reading and hashing a video.
            etag = f'"{file_stat.st_mtime_ns:x}-{file_stat.st_size:x}"'
            headers.append(("ETag", etag))

        if etag is not None and _if_none_match_matches(
            req.headers.get("If-None-Match"), etag
        ):
            return Response(304, [
                ("ETag", etag),
                ("Cache-Control", cache),
                ("Accept-Ranges", "bytes"),
            ] + (extra_headers or []))

        if last_modified is not None:
            modified_since = _parse_http_timestamp(req.headers.get("If-Modified-Since"))
            if modified_since is not None and mtime_secs <= modified_since:
                return Response(304, [
                    ("Last-Modified", last_modified),
                    ("Cache-Control", cache),
                    ("Accept-Ranges", "bytes"),
                ])

        range_header = req.headers.get("Range")
        if_range = req.headers.get("If-Range")
        # If-Range uses strong ETag comparison. Date comparison is available
        # only for resources that expose Last-Modified. Anything stale or
        # unverifiable falls back to a complete 200 response.
        range_allowed = not if_range
        if if_range and etag is not None:
            range_allowed = not if_range.startswith("W/") and if_range == etag
        elif if_range and last_modified is not None:
            if_range_time = _parse_http_timestamp(if_range)
            range_allowed = if_range_time is not None and mtime_secs <= if_range_time
        if range_header and range_allowed:
            size = file_stat.st_size
            try:
                start, end = _parse_byte_range(range_header, size)
            except ValueError:
                return Response(
                    416, headers + [("Content-Range", f"bytes */{size}")]
                )
            try:
                with target.open("rb") as f:
                    f.seek(start)
                    body = f.read(end - start + 1)
            except OSError:
                return None
            range_headers = headers + [("Content-Range", f"bytes {start}-{end}/{size}")]
            return Response(206, range_headers, body)
        try:
            body = target.read_bytes()
        except OSError:
            return None
        return Response(200, headers, body)


class _Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _serve(self) -> None:
        # The health endpoint stays open so a k8s probe needs no credential, and
        # the static assets are useless without the API behind them.
        path = self.path.split("?", 1)[0]
        principal = None
        if path != "/healthz" and self.app.authenticator is not None:
            try:
                principal = self.app.authenticator.authenticate(self.headers.get("Cookie"))
            except (AuthenticationError, AuthenticationUnavailable) as exc:
                # Reject before reading request bodies; close so unread bytes
                # cannot be interpreted as the next HTTP request.
                self.close_connection = True
                if isinstance(exc, AuthenticationUnavailable):
                    body = b'{"error":"authentication service unavailable"}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.command == "GET" and not path.startswith("/api/"):
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        "/auth/login?return=" + quote(self.path, safe=""),
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"error":"unauthorized","login":"/auth/login"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Auth-Login", "/auth/login")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if path != "/healthz" and self.app.authenticator is None and not self.app.authorised(self.headers):
            body = b"unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="deepwiki"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        cid, fresh = self.app.client_id_for(self.headers.get("Cookie"))
        if principal is not None and self.command not in ("GET", "HEAD", "OPTIONS"):
            origin = self.headers.get("Origin")
            foreign_origin = origin and (
                urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()
                or urlparse(origin).scheme not in ("http", "https")
            )
            if foreign_origin or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.close_connection = True
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        try:
            req = Request(self, cid, principal)
            resp = self.app.handle(req)
        except Exception:  # noqa: BLE001
            log.exception("handler raised for %s", self.path)
            resp = json_response({"error": "internal error"}, status=500)

        if principal is not None and not path.startswith(STATIC_PREFIX):
            resp.headers = [(k, v) for k, v in resp.headers if k.lower() != "cache-control"]
            resp.headers.append(("Cache-Control", "private, no-store"))

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

        resp = _maybe_compress(req, resp)
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
