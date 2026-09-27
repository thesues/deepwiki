"""Current-turn S3 image hydration and historical image tool results."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

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
