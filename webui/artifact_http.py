"""Authenticated-origin streaming for generated media stored in Autumn S3."""
from __future__ import annotations

import logging
from pathlib import Path
import urllib.error

import artifact_store
from http_shell import (
    App, ARTIFACT_CSP, Response, Streaming, json_response,
    _if_none_match_matches, _parse_byte_range, _parse_http_timestamp,
)

log = logging.getLogger("deepwiki.artifacts")


def serve_media(req, relative: str):
    """Called only after the artifact's session has passed authorization."""
    try:
        metadata = artifact_store.head(relative)
        if metadata is None:
            return None  # historical or not-yet-published local file
        size = int(metadata["Content-Length"])
        etag = metadata.get("ETag", "")
        headers = [
            ("Content-Type", App._content_type(Path(relative))),
            ("Accept-Ranges", "bytes"),
            ("Cache-Control", "private, max-age=31536000, immutable" if req.user_id else "public, max-age=31536000, immutable"),
            ("Content-Security-Policy", ARTIFACT_CSP),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if req.user_id:
            headers.append(("Vary", "Cookie"))
        if etag:
            headers.append(("ETag", etag))
        modified = metadata.get("Last-Modified")
        if modified:
            headers.append(("Last-Modified", modified))
        if _if_none_match_matches(req.headers.get("If-None-Match"), etag):
            return Response(304, headers)
        raw_range = req.headers.get("Range")
        if_range = req.headers.get("If-Range")
        if raw_range and if_range:
            # Autumn implements Range but not If-Range; resolve it here, with
            # strong comparison, before asking S3 for any bytes.
            matches = bool(etag) and not if_range.startswith("W/") and if_range == etag
            if not if_range.startswith(('"', 'W/')):
                matches = (_parse_http_timestamp(if_range) is not None
                           and _parse_http_timestamp(if_range) == _parse_http_timestamp(modified))
            if not matches:
                raw_range = None
        upstream_headers = {"If-Match": etag} if etag else {}
        status, length = 200, size
        if raw_range:
            try:
                start, end = _parse_byte_range(raw_range, size)
            except ValueError:
                return Response(416, headers + [("Content-Range", f"bytes */{size}")])
            upstream_headers["Range"] = f"bytes={start}-{end}"
            status, length = 206, end - start + 1
            headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        upstream = artifact_store.open_media(relative, headers=upstream_headers)
        if upstream.status != status or int(upstream.headers.get("Content-Length", -1)) != length:
            upstream.close()
            raise RuntimeError("unexpected S3 media response")

        def pump(write):
            remaining = length
            while remaining:
                block = upstream.read(min(artifact_store.CHUNK, remaining))
                if not block:
                    raise OSError("truncated S3 media response")
                write(block)
                remaining -= len(block)

        response = Streaming(headers + [("Content-Length", str(length))], pump, close=upstream.close)
        response.status = status
        return response
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        log.warning("S3 media read failed for %s: %s", relative, exc)
        response = json_response({"error": "media storage unavailable"}, status=503)
        response.headers.append(("Cache-Control", "no-store"))
        return response
