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
