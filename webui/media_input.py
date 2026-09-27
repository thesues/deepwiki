"""Autumn S3 storage and model-facing hydration for browser image inputs."""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import urllib.parse
import urllib.request


MAX_IMAGE_BYTES = 8 << 20
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
IMAGE_KEY_PATTERN = re.compile(
    r"input/webui/(?:u_[A-Za-z0-9_-]+/)?[A-Za-z0-9_.-]+/[0-9a-f]{32}\.(?:png|jpe?g|webp|heic|heif)",
    re.IGNORECASE,
)
IMAGE_MARKER_PATTERN = re.compile(
    r"\[输入图片 object_key:\s*(input/webui/(?:u_[A-Za-z0-9_-]+/)?[A-Za-z0-9_.-]+/[0-9a-f]{32}\.(?:png|jpe?g|webp|heic|heif))\]",
    re.IGNORECASE,
)


def endpoint() -> str:
    return os.environ.get("AUTUMN_S3_ENDPOINT", "http://autumn-s3.autumn.svc:9100")


def valid_key(key: str) -> bool:
    return IMAGE_KEY_PATTERN.fullmatch(key) is not None


def owned_key(key: str, user_id: str) -> bool:
    """Authenticated keys must live below their deterministic user prefix."""
    return valid_key(key) and (not user_id or key.startswith(f"input/webui/{user_id}/"))


def put_object(endpoint_url: str, key: str, body: bytes, content_type: str) -> None:
    """Write one immutable browser image to Autumn S3."""
    url = endpoint_url.rstrip("/") + "/" + urllib.parse.quote(key, safe="/")
    upstream = urllib.request.Request(
        url, data=body, method="PUT", headers={"Content-Type": content_type}
    )
    with urllib.request.urlopen(upstream, timeout=120) as response:
        if response.status // 100 != 2:
            raise RuntimeError(f"unexpected S3 status {response.status}")


def get_object(endpoint_url: str, key: str) -> tuple[bytes, str]:
    """Read one immutable browser image from Autumn S3."""
    if not valid_key(key):
        raise ValueError("invalid image key")
    url = endpoint_url.rstrip("/") + "/" + urllib.parse.quote(key, safe="/")
    upstream = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(upstream, timeout=120) as response:
        if response.status // 100 != 2:
            raise RuntimeError(f"unexpected S3 status {response.status}")
        body = response.read(MAX_IMAGE_BYTES + 1)
        if len(body) > MAX_IMAGE_BYTES:
            raise RuntimeError("stored image exceeds 8 MiB")
        content_type = response.headers.get_content_type()
        if content_type not in IMAGE_TYPES:
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return body, content_type


def marker_keys(text: str) -> list[str]:
    return [match.group(1) for match in IMAGE_MARKER_PATTERN.finditer(text or "")]


def _data_url(key: str) -> tuple[str, int]:
    body, content_type = get_object(endpoint(), key)
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{content_type};base64,{encoded}", len(body)


def model_user_content(text: str) -> str | list[dict]:
    """Attach only images explicitly uploaded with this user turn.

    Historical references remain lightweight text. The model can resolve a
    reference such as "上一张图" from that ordered history and call the
    ``input_image_open`` tool for exactly the object it needs.
    """
    keys = marker_keys(text)
    if not keys:
        return text

    # Preserve order but do not send the same immutable image twice.
    keys = list(dict.fromkeys(keys))
    parts: list[dict] = [{"type": "text", "text": text}]
    for key in keys:
        data_url, _ = _data_url(key)
        parts.append({"type": "text", "text": f"Current image attachment ({key}):"})
        parts.append({
            "type": "image_url",
            "image_url": {"url": data_url},
        })
    return parts


def tool_result(key: str, question: str) -> dict:
    """Load one historical S3 image as a transient multimodal tool result."""
    data_url, size = _data_url(key)
    note = f"Loaded historical image {key} from Autumn S3."
    if question.strip():
        note += f"\nQuestion: {question.strip()}"
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": note},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "text_summary": f"Loaded historical image {key} ({size} bytes) from Autumn S3.",
        "meta": {"object_key": key, "size_bytes": size},
    }
