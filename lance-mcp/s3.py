"""A tiny S3 client for the autumn gateway — stdlib only, read-mostly.

The gateway is an unauthenticated S3 endpoint over the fs/ tree: bucket =
first-level directory, key = everything below it (`s3://docs/buda/x.md` is
`fs/docs/buda/x.md`). Only the operations lance-mcp actually needs are
implemented — HEAD, GET, PUT (the startup marker), and paginated
ListObjectsV2 — because lance-mcp talks to LanceDB through lancedb's own S3
support and only reads corpus files and the marker by hand.

Every URL is path-style: `<endpoint>/<bucket>/<key>`, which is what the
gateway serves and what object_store's `virtual_hosted_style_request: false`
produces on the LanceDB side.
"""
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


class S3Error(Exception):
    """A gateway response that is neither the expected one nor a plain miss."""


class Gateway:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")

    # -- one request ----------------------------------------------------------

    def _request(self, method: str, bucket: str, key: str = "", query: str = "",
                 data: bytes = b"") -> tuple[int, bytes]:
        url = f"{self.endpoint}/{quote(bucket, safe='')}"
        if key:
            url += "/" + quote(key, safe="/")
        if query:
            url += f"?{query}"
        req = Request(url, data=data if method == "PUT" else None, method=method)
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except HTTPError as e:
            return e.code, e.read()

    # -- object operations ----------------------------------------------------

    def exists(self, bucket: str, key: str) -> bool:
        status, _ = self._request("HEAD", bucket, key)
        if status == 200:
            return True
        if status == 404:
            return False
        raise S3Error(f"HEAD {bucket}/{key}: HTTP {status}")

    def get(self, bucket: str, key: str) -> bytes:
        status, body = self._request("GET", bucket, key)
        if status != 200:
            raise S3Error(f"GET {bucket}/{key}: HTTP {status}")
        return body

    def put(self, bucket: str, key: str, data: bytes = b"") -> None:
        status, body = self._request("PUT", bucket, key, data=data)
        if status not in (200, 204):
            raise S3Error(f"PUT {bucket}/{key}: HTTP {status}: {body[:200]!r}")

    # -- listing --------------------------------------------------------------

    def list(self, bucket: str, prefix: str, limit: int | None = None) -> list[str]:
        """Object keys under `prefix`, following ListObjectsV2's isTruncated
        pagination. `limit` stops after that many keys (polling uses this to
        keep a readiness probe cheap)."""
        out: list[str] = []
        token = None
        while True:
            query = "list-type=2"
            if prefix:
                query += f"&prefix={quote(prefix, safe='')}"
            if token:
                query += f"&continuation-token={quote(token, safe='')}"
            if limit is not None:
                query += f"&max-keys={min(1000, limit - len(out))}"
            status, body = self._request("GET", bucket, query=query)
            if status != 200:
                raise S3Error(f"LIST {bucket}/{prefix}: HTTP {status}: {body[:200]!r}")
            root = ET.fromstring(body)
            # The response carries an S3 xmlns; {*} matches any namespace.
            out.extend(c.text or "" for c in root.findall("{*}Contents/{*}Key"))
            if limit is not None and len(out) >= limit:
                return out[:limit]
            trunc = root.find("{*}IsTruncated")
            next_tok = root.find("{*}NextContinuationToken")
            if trunc is None or (trunc.text or "").lower() != "true" or next_tok is None:
                return out
            token = next_tok.text
