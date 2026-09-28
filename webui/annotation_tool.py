"""A profile-scoped tool that annotates user-owned images without a shell."""

from __future__ import annotations

import base64
import io
import math
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable


_SAFE_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_KINDS = {"box", "rect", "line", "circle", "chip"}
_LIB_DIR = Path(__file__).resolve().parent / "hermes" / "skills" / "mayi" / "annotate-screenshot" / "scripts"


def _annotator_class():
    if str(_LIB_DIR) not in sys.path:
        sys.path.insert(0, str(_LIB_DIR))
    from annotate_lib import Annotator

    return Annotator


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _point(value: Any, convert: Callable[[float, float], tuple[int, int]], name: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be [x, y]")
    return convert(_number(value[0], f"{name}.x"), _number(value[1], f"{name}.y"))


def _bounds(value: Any, convert: Callable[[float, float], tuple[int, int]], name: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{name} must be [x0, y0, x1, y1]")
    p0 = convert(_number(value[0], f"{name}.x0"), _number(value[1], f"{name}.y0"))
    p1 = convert(_number(value[2], f"{name}.x1"), _number(value[3], f"{name}.y1"))
    return [*p0, *p1]


def _preview(path: Path) -> tuple[str, int]:
    from PIL import Image

    with Image.open(path) as opened:
        image = opened.convert("RGB")
        image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
    body = buffer.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(body).decode("ascii"), len(body)


def create_annotation(
    args: dict,
    *,
    session_id: str,
    user_id: str,
    artifacts_root: Path,
) -> dict:
    """Validate one model request, render it, and return a multimodal preview."""
    from media_input import endpoint, get_object, owned_key

    key = str(args.get("object_key") or "").strip()
    if not owned_key(key, user_id):
        raise ValueError("invalid or unowned input image object_key")
    if not _SAFE_SESSION.fullmatch(session_id) or ".." in session_id:
        raise ValueError("invalid current session id")

    annotations = args.get("annotations")
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("annotations must be a non-empty list")
    if len(annotations) > 40:
        raise ValueError("at most 40 annotations are allowed")

    body, _ = get_object(endpoint(), key)
    Annotator = _annotator_class()
    scale = _number(args.get("scale", 1), "scale")
    if not 0.5 <= scale <= 4:
        raise ValueError("scale must be between 0.5 and 4")
    annotator = Annotator(io.BytesIO(body), scale=scale)
    width, height = annotator.image.size

    coordinate_mode = str(args.get("coordinate_mode") or "normalized_1000")
    if coordinate_mode not in {"normalized_1000", "pixels"}:
        raise ValueError("coordinate_mode must be normalized_1000 or pixels")

    def convert(x: float, y: float) -> tuple[int, int]:
        if coordinate_mode == "normalized_1000":
            if not (0 <= x <= 1000 and 0 <= y <= 1000):
                raise ValueError("normalized coordinates must be within 0..1000")
            x, y = x * width / 1000, y * height / 1000
        if not (-width <= x <= width * 2 and -height <= y <= height * 2):
            raise ValueError("pixel coordinates are implausibly far outside the image")
        return round(x), round(y)

    legend_rows: list[tuple[int | str | None, str]] = []
    for index, item in enumerate(annotations):
        if not isinstance(item, dict):
            raise ValueError(f"annotations[{index}] must be an object")
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            raise ValueError(f"annotations[{index}].kind must be one of {sorted(_KINDS)}")
        number = item.get("number")
        if number is not None:
            number = str(number).strip()
            if not number or len(number) > 3:
                raise ValueError(f"annotations[{index}].number must be 1-3 characters")
        label = str(item.get("label") or "").strip()
        if len(label) > 160:
            raise ValueError(f"annotations[{index}].label is too long")

        marker: tuple[int, int] | None = None
        if kind in {"box", "rect"}:
            bounds = _bounds(item.get("bounds"), convert, f"annotations[{index}].bounds")
            getattr(annotator, kind)(bounds)
            marker = (bounds[0] - round(8 * scale), bounds[1] - round(10 * scale))
        elif kind == "line":
            raw_points = item.get("points")
            if not isinstance(raw_points, list) or not 2 <= len(raw_points) <= 64:
                raise ValueError(f"annotations[{index}].points must contain 2-64 points")
            points = [_point(point, convert, f"annotations[{index}].points") for point in raw_points]
            annotator.line(points)
            marker = points[0]
        elif kind == "circle":
            marker = _point(item.get("position"), convert, f"annotations[{index}].position")
            if number is None:
                raise ValueError(f"annotations[{index}].number is required for circle")
        elif kind == "chip":
            position = _point(item.get("position"), convert, f"annotations[{index}].position")
            text = str(item.get("text") or label).strip()
            if not text or len(text) > 160:
                raise ValueError(f"annotations[{index}].text must be 1-160 characters")
            annotator.chip(position, text)

        if number is not None and marker is not None:
            annotator.circle(marker, number)
        if label:
            legend_rows.append((number, label))

    legend = args.get("legend")
    if legend_rows and legend is not False:
        if legend is None:
            legend = {}
        if not isinstance(legend, dict):
            raise ValueError("legend must be an object or false")
        position = _point(legend.get("position", [35, 700]), convert, "legend.position")
        title = str(legend.get("title") or "标注说明").strip()
        if not title or len(title) > 80:
            raise ValueError("legend.title must be 1-80 characters")
        annotator.legend(position, title, legend_rows)

    output_dir = artifacts_root / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"annotation-{uuid.uuid4().hex}.png"
    annotator.save(output_path)
    data_url, preview_bytes = _preview(output_path)
    public_url = f"/artifacts/{session_id}/{output_path.name}"
    summary = (
        f"Created annotated image {public_url} at {width}x{height}. "
        f"Embed it in the final answer as ![标注图]({public_url}). "
        "Review the preview and call annotate_image again if a marker misses the visible feature."
    )
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": summary},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "text_summary": summary,
        "meta": {
            "artifact_url": public_url,
            "object_key": key,
            "width": width,
            "height": height,
            "preview_bytes": preview_bytes,
        },
    }


def register_annotation_tool() -> None:
    """Register only into the dedicated toolset granted to the mayi profile."""
    from tools.registry import registry, tool_error

    if registry.get_entry("annotate_image") is not None:
        return

    async def _annotate(args: dict, **_: Any) -> Any:
        import asyncio
        from gateway.session_context import get_session_env

        session_id = (
            get_session_env("HERMES_SESSION_KEY", "")
            or get_session_env("HERMES_UI_SESSION_ID", "")
            or get_session_env("HERMES_SESSION_ID", "")
        )
        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        try:
            from hermes_agent import _artifacts_root

            return await asyncio.to_thread(
                create_annotation,
                args,
                session_id=session_id,
                user_id=user_id,
                artifacts_root=_artifacts_root(),
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"Could not annotate image: {exc}", success=False)

    registry.register(
        name="annotate_image",
        toolset="mayi-annotation",
        schema={
            "name": "annotate_image",
            "description": (
                "Annotate one user-owned browser image with the fixed red-box/number/legend style. "
                "Use normalized_1000 coordinates by default. For face or palm creases, send one "
                "numbered line per visible crease and a legend; review the returned preview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "object_key": {
                        "type": "string",
                        "description": "Exact input/webui/... object_key from conversation history.",
                    },
                    "coordinate_mode": {
                        "type": "string",
                        "enum": ["normalized_1000", "pixels"],
                        "default": "normalized_1000",
                    },
                    "scale": {
                        "type": "number",
                        "minimum": 0.5,
                        "maximum": 4,
                        "description": "Style scale. Use 2 for a Chrome 2x screenshot.",
                    },
                    "annotations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 40,
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["box", "rect", "line", "circle", "chip"]},
                                "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                                "points": {
                                    "type": "array",
                                    "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                                    "minItems": 2,
                                    "maxItems": 64,
                                },
                                "position": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                                "number": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 99,
                                    "description": "Short unique marker, normally 1, 2, 3...",
                                },
                                "label": {"type": "string", "description": "Legend text for this marker."},
                                "text": {"type": "string", "description": "Chip text when kind=chip."},
                            },
                            "required": ["kind"],
                        },
                    },
                    "legend": {
                        "type": "object",
                        "description": "Optional object with position [x,y] and title.",
                        "properties": {
                            "position": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                            "title": {"type": "string"},
                        },
                    },
                },
                "required": ["object_key", "annotations"],
            },
        },
        handler=_annotate,
        is_async=True,
        emoji="🖍️",
    )
