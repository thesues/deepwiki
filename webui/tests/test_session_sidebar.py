"""The session navigator can be collapsed without hiding the chat."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_session_sidebar_has_a_persistent_collapse_control():
    page = (ROOT / "static" / "index.html").read_text()
    script = (ROOT / "static" / "app.js").read_text()
    styles = (ROOT / "static" / "style.css").read_text()

    assert 'id="sessions-toggle"' in page
    assert 'aria-expanded="true"' in page
    assert 'LS_SESSIONS_COLLAPSED = "hermes.sessionsCollapsed"' in script
    assert "setSessionsCollapsed(sessionsCollapsed())" in script
    assert ".grid.sessions-collapsed .sessions { display:none; }" in styles
    assert ".sessions-toggle { width:2rem; height:2rem; align-self:center;" in styles


def test_media_skill_keeps_runtime_output_out_of_static():
    skill = (ROOT / "hermes" / "skills" / "media" / "comfyui-media" / "SKILL.md").read_text()
    assert "/opt/data/artifacts/" in skill
    assert "/artifacts/<本会话>/cover.png" in skill
    assert "`/static/` 只服务镜像中的 HTML/JS/CSS" in skill
