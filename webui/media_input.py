"""Autumn S3 storage and model-facing hydration for browser image inputs."""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable


MAX_IMAGE_BYTES = 8 << 20
MAX_ARTIFACT_IMAGE_BYTES = 64 << 20
ARTIFACT_PREVIEW_SIZE = (2048, 2048)
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
ARTIFACT_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ARTIFACT_SESSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
MEDIA_CACHE_PATTERN = re.compile(
    r"(?:MEDIA:)?(?:[A-Za-z]:)?(?:(?:/[^/\s]+)*)?/cache/images/(img_[0-9a-f]{12}\.(?:png|jpe?g|webp|gif))$|"
    r"(?:MEDIA:)?(img_[0-9a-f]{12}\.(?:png|jpe?g|webp|gif))$",
    re.IGNORECASE,
)
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


def _artifact_path(reference: str, artifacts_root: Path) -> tuple[Path, str, str]:
    """Resolve one public or filesystem artifact reference below its root."""
    reference = reference.strip()
    if not reference:
        raise ValueError("artifact_path is required")

    parsed = urllib.parse.urlsplit(reference)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise ValueError("artifact_path must be an artifact URL or local artifact path")
    raw_path = urllib.parse.unquote(parsed.path if parsed.scheme else reference.split("?", 1)[0].split("#", 1)[0])
    root = artifacts_root.resolve()
    prefix = "/artifacts/"
    if raw_path.startswith(prefix):
        relative_text = raw_path[len(prefix):]
    else:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise ValueError("artifact_path must start with /artifacts/ or the artifact filesystem root")
        try:
            relative_text = candidate.resolve().relative_to(root).as_posix()
        except (OSError, ValueError) as exc:
            raise ValueError("artifact_path is outside the artifact root") from exc

    relative = PurePosixPath(relative_text)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid artifact_path")
    session_id = relative.parts[0]
    if not ARTIFACT_SESSION_PATTERN.fullmatch(session_id):
        raise ValueError("invalid artifact session")

    candidate = root.joinpath(*relative.parts).resolve()
    try:
        resolved_relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact_path is outside the artifact root") from exc
    if not resolved_relative.parts:
        raise ValueError("invalid artifact_path")
    # Authorize the real target's session.  A symlink inside session A must
    # not make an image belonging to session B readable as an A artifact.
    session_id = resolved_relative.parts[0]
    if not ARTIFACT_SESSION_PATTERN.fullmatch(session_id):
        raise ValueError("invalid artifact session")
    if candidate.suffix.lower() not in ARTIFACT_IMAGE_SUFFIXES:
        raise ValueError("artifact is not a supported raster image")
    return candidate, session_id, prefix + "/".join(
        urllib.parse.quote(part, safe="") for part in relative.parts
    )


def _artifact_preview(path: Path) -> tuple[str, int, int, int]:
    source_bytes = path.stat().st_size
    if source_bytes > MAX_ARTIFACT_IMAGE_BYTES:
        raise ValueError("artifact image exceeds 64 MiB")

    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        opened.verify()
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        width, height = image.size
        image.thumbnail(ARTIFACT_PREVIEW_SIZE, Image.Resampling.LANCZOS)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=90, optimize=True)
    preview = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{preview}", source_bytes, width, height


def _media_cache_path(reference: str, image_cache_dir: Path) -> tuple[Path, str]:
    """Resolve one Hermes Gateway cached MCP image by its bearer-style name."""
    reference = reference.strip()
    if not reference:
        raise ValueError("media_path is required")
    if reference.upper().startswith("MEDIA:"):
        reference = reference[len("MEDIA:"):].strip()

    parsed = urllib.parse.urlsplit(reference)
    if parsed.scheme:
        raise ValueError("media_path must be a MEDIA tag, cache path, or image cache filename")
    raw_path = urllib.parse.unquote(reference.split("?", 1)[0].split("#", 1)[0])
    match = MEDIA_CACHE_PATTERN.fullmatch(raw_path)
    if not match:
        raise ValueError("media_path is not a supported Hermes cached image name")
    filename = match.group(1) or match.group(2)

    root = image_cache_dir.resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("media_path is outside the image cache") from exc
    return candidate, filename


def media_cache_tool_result(reference: str, question: str, image_cache_dir: Path) -> dict:
    """Load one MCP/Hermes cached image as a transient multimodal result."""
    path, filename = _media_cache_path(reference, image_cache_dir)
    if not path.is_file():
        raise ValueError("cached image does not exist")
    data_url, source_bytes, width, height = _artifact_preview(path)
    note = f"Loaded cached MCP image {filename} at {width}x{height}."
    if question.strip():
        note += f"\nQuestion: {question.strip()}"
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": note},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "text_summary": f"Loaded cached MCP image {filename} ({source_bytes} bytes, {width}x{height}).",
        "meta": {
            "media_path": filename,
            "size_bytes": source_bytes,
            "width": width,
            "height": height,
        },
    }


def artifact_tool_result(
    reference: str,
    question: str,
    artifacts_root: Path,
    owns_session: Callable[[str], bool],
) -> dict:
    """Load one authorized artifact image as a transient multimodal result."""
    path, session_id, public_url = _artifact_path(reference, artifacts_root)
    if not owns_session(session_id):
        raise ValueError("artifact image is not owned by the current user and session")
    if not path.is_file():
        raise ValueError("artifact image does not exist")
    data_url, source_bytes, width, height = _artifact_preview(path)
    note = f"Loaded generated artifact image {public_url} at {width}x{height}."
    if question.strip():
        note += f"\nQuestion: {question.strip()}"
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": note},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "text_summary": f"Loaded artifact image {public_url} ({source_bytes} bytes, {width}x{height}).",
        "meta": {
            "artifact_url": public_url,
            "size_bytes": source_bytes,
            "width": width,
            "height": height,
        },
    }
