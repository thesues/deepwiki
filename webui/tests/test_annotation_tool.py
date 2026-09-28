"""The mayi-only annotation tool and its reusable Pillow library."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import annotation_tool as at  # noqa: E402
import media_input  # noqa: E402


KEY = "input/webui/u_owner/draft/0123456789abcdef0123456789abcdef.png"


def _png(size=(800, 600), color=(242, 242, 242)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def test_annotation_renders_numbered_lines_and_session_owned_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(media_input, "get_object", lambda endpoint, key: (_png(), "image/png"))
    result = at.create_annotation(
        {
            "object_key": KEY,
            "annotations": [
                {"kind": "line", "points": [[150, 200], [350, 400], [650, 500]], "number": 1, "label": "生命线"},
                {"kind": "box", "bounds": [500, 100, 800, 260], "number": 2, "label": "掌丘"},
            ],
            "legend": {"position": [30, 720], "title": "掌纹标注"},
        },
        session_id="session-1",
        user_id="u_owner",
        artifacts_root=tmp_path,
    )

    artifact = tmp_path / "session-1" / Path(result["meta"]["artifact_url"]).name
    assert artifact.is_file()
    assert result["meta"]["width"] == 800
    assert result["meta"]["height"] == 600
    assert result["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with Image.open(artifact) as annotated:
        marker_pixels = [
            annotated.getpixel((x, y))[:3]
            for x in range(108, 133)
            for y in range(108, 133)
        ]
        assert (229, 57, 70) in marker_pixels


def test_annotation_rejects_another_users_object(tmp_path):
    with pytest.raises(ValueError, match="unowned"):
        at.create_annotation(
            {"object_key": KEY, "annotations": [{"kind": "circle", "position": [1, 1], "number": 1}]},
            session_id="session-1",
            user_id="u_someone_else",
            artifacts_root=tmp_path,
        )


def test_four_2x_tiles_stitch_in_reading_order(tmp_path):
    sys.path.insert(0, str(at._LIB_DIR))
    from annotate_lib import stitch_2x_tiles

    tiles = [Image.new("RGB", (20, 10), color) for color in ("red", "green", "blue", "yellow")]
    output = tmp_path / "stitched.png"
    image = stitch_2x_tiles(tiles, output)

    assert image.size == (40, 20)
    assert image.getpixel((5, 5))[:3] == (255, 0, 0)
    assert image.getpixel((25, 5))[:3] == (0, 128, 0)
    assert image.getpixel((5, 15))[:3] == (0, 0, 255)
    assert image.getpixel((25, 15))[:3] == (255, 255, 0)
    assert output.is_file()


def test_mayi_directive_requires_the_annotation_skill():
    manifest = (Path(__file__).resolve().parents[2] / "k8s" / "webui.yaml").read_text()
    assert '"toolsets":["skills","mayi-annotation"]' in manifest
    assert "skill_view('pdf-figure-reading')" in manifest
    assert "skill_view('annotate-screenshot')" in manifest
    assert "掌纹/面纹逐条用独立编号折线和图例区分" in manifest
