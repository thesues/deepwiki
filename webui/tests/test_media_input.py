"""Current-turn S3 image hydration and historical image tool results."""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media_input  # noqa: E402


KEY = "input/webui/session/0123456789abcdef0123456789abcdef.png"


def test_current_marker_becomes_temporary_multimodal_content(monkeypatch):
    monkeypatch.setattr(
        media_input,
        "get_object",
        lambda endpoint, key: (b"pixels", "image/png"),
    )
    text = f"描述它\n[输入图片 object_key: {KEY}]"

    content = media_input.model_user_content(text)

    assert content[0] == {"type": "text", "text": text}
    assert content[-1]["type"] == "image_url"
    encoded = content[-1]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(encoded) == b"pixels"


def test_history_without_a_marker_is_not_speculatively_hydrated(monkeypatch):
    calls = []
    monkeypatch.setattr(media_input, "get_object", lambda *args: calls.append(args))

    assert media_input.model_user_content("请看上一张图") == "请看上一张图"
    assert calls == []


def test_tool_result_is_transient_multimodal_with_a_small_summary(monkeypatch):
    monkeypatch.setattr(
        media_input,
        "get_object",
        lambda endpoint, key: (b"pixels", "image/png"),
    )

    result = media_input.tool_result(KEY, "图里有什么？")

    assert result["_multimodal"] is True
    assert result["content"][-1]["type"] == "image_url"
    assert "base64" not in result["text_summary"]
    assert result["meta"]["object_key"] == KEY


def _png(path: Path, size=(2400, 1200)) -> None:
    image = Image.new("RGBA", size, (10, 20, 30, 128))
    image.save(path)


def test_artifact_image_becomes_an_authorized_bounded_visual_preview(tmp_path):
    root = tmp_path / "artifacts"
    path = root / "session-1" / "media-run" / "frame.png"
    path.parent.mkdir(parents=True)
    _png(path)

    result = media_input.artifact_tool_result(
        "/artifacts/session-1/media-run/frame.png",
        "检查人物是否一致",
        root,
        lambda session_id: session_id == "session-1",
    )

    assert result["_multimodal"] is True
    assert result["meta"]["artifact_url"] == "/artifacts/session-1/media-run/frame.png"
    assert result["meta"]["width"] == 2400
    assert result["meta"]["height"] == 1200
    encoded = result["content"][-1]["image_url"]["url"].split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as preview:
        assert preview.format == "JPEG"
        assert max(preview.size) == 2048


def test_artifact_image_accepts_its_filesystem_path(tmp_path):
    root = tmp_path / "artifacts"
    path = root / "session-1" / "frame.webp"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (32, 24), "red").save(path)

    result = media_input.artifact_tool_result(
        str(path), "what is visible", root, lambda _: True,
    )

    assert result["meta"]["artifact_url"] == "/artifacts/session-1/frame.webp"


@pytest.mark.parametrize("reference", [
    "img_0123abcd4567.png",
    "MEDIA:img_0123abcd4567.png",
    "MEDIA:/opt/data/cache/images/img_0123abcd4567.png",
])
def test_mcp_media_cache_image_becomes_a_visual_preview(tmp_path, reference):
    cache = tmp_path / "cache" / "images"
    cache.mkdir(parents=True)
    _png(cache / "img_0123abcd4567.png", (96, 48))

    result = media_input.media_cache_tool_result(reference, "看这张图", cache)

    assert result["_multimodal"] is True
    assert result["meta"]["media_path"] == "img_0123abcd4567.png"
    assert result["meta"]["width"] == 96
    assert result["meta"]["height"] == 48
    encoded = result["content"][-1]["image_url"]["url"].split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as preview:
        assert preview.format == "JPEG"


@pytest.mark.parametrize("reference", [
    "/tmp/img_0123abcd4567.png",
    "img_nothex.png",
    "../cache/images/img_0123abcd4567.png",
    "http://example.com/cache/images/img_0123abcd4567.png",
])
def test_mcp_media_cache_image_rejects_non_cache_references(tmp_path, reference):
    cache = tmp_path / "cache" / "images"
    cache.mkdir(parents=True)
    _png(cache / "img_0123abcd4567.png", (32, 24))

    with pytest.raises(ValueError):
        media_input.media_cache_tool_result(reference, "inspect", cache)


def test_artifact_image_rejects_another_session(tmp_path):
    root = tmp_path / "artifacts"
    path = root / "session-b" / "frame.png"
    path.parent.mkdir(parents=True)
    _png(path, (32, 24))

    with pytest.raises(ValueError, match="not owned"):
        media_input.artifact_tool_result(
            "/artifacts/session-b/frame.png", "inspect", root,
            lambda session_id: session_id == "session-a",
        )


def test_artifact_image_authorizes_the_resolved_symlink_session(tmp_path):
    root = tmp_path / "artifacts"
    source = root / "session-b" / "frame.png"
    link = root / "session-a" / "frame.png"
    source.parent.mkdir(parents=True)
    link.parent.mkdir(parents=True)
    _png(source, (32, 24))
    link.symlink_to(source)

    with pytest.raises(ValueError, match="not owned"):
        media_input.artifact_tool_result(
            "/artifacts/session-a/frame.png", "inspect", root,
            lambda session_id: session_id == "session-a",
        )


@pytest.mark.parametrize("reference", [
    "/artifacts/session-a/../session-b/frame.png",
    "/etc/passwd",
    "session-a/frame.png",
])
def test_artifact_image_rejects_paths_outside_the_artifact_contract(tmp_path, reference):
    root = tmp_path / "artifacts"
    (root / "session-b").mkdir(parents=True)
    _png(root / "session-b" / "frame.png", (32, 24))

    with pytest.raises(ValueError):
        media_input.artifact_tool_result(reference, "inspect", root, lambda _: True)


def test_artifact_image_rejects_non_raster_files(tmp_path):
    root = tmp_path / "artifacts"
    path = root / "session-a" / "notes.txt"
    path.parent.mkdir(parents=True)
    path.write_text("not an image")

    with pytest.raises(ValueError, match="supported raster"):
        media_input.artifact_tool_result(
            "/artifacts/session-a/notes.txt", "inspect", root, lambda _: True,
        )


def test_artifact_image_rejects_sources_over_the_size_limit(tmp_path):
    root = tmp_path / "artifacts"
    path = root / "session-a" / "huge.png"
    path.parent.mkdir(parents=True)
    with path.open("wb") as output:
        output.truncate(media_input.MAX_ARTIFACT_IMAGE_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds 64 MiB"):
        media_input.artifact_tool_result(
            "/artifacts/session-a/huge.png", "inspect", root, lambda _: True,
        )
