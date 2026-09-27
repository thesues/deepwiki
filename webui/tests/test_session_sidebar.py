"""The session navigator can be collapsed without hiding the chat."""

from __future__ import annotations

import json
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
    assert ".grid.sessions-collapsed .chat { width:min(100%, 1024px); justify-self:center; }" in styles
    assert ".sessions-toggle { width:2rem; height:2rem; align-self:center;" in styles
    assert "the server result is authoritative even when the cache was non-empty" in script
    assert "S.tools.clear(); S.seg = null; S.activity = null; S.turnTop = null; S.actIndex = 0;" in script


def test_media_skill_keeps_runtime_output_out_of_static():
    skill = (ROOT / "hermes" / "skills" / "media" / "comfyui-media" / "SKILL.md").read_text()
    assert "/opt/data/artifacts/" in skill
    assert "/artifacts/media-<uuid>/cover.png" in skill
    assert "`/static/` 只服务镜像中的 HTML/JS/CSS" in skill
    assert "Artifact 路径永不重用" in skill


def test_vllm_media_skill_and_client_forbid_artifact_overwrite():
    root = ROOT / "hermes" / "skills" / "media" / "vllm-omni-h3"
    skill = (root / "SKILL.md").read_text()
    client = (root / "scripts" / "h3_client.py").read_text()
    assert "/opt/data/artifacts/media-<run-uuid>/" in skill
    assert "禁止覆盖" in skill
    assert "refusing to overwrite existing artifact" in client
    assert "os.link(partial, args.output)" in client


def test_h3_workflows_match_current_basic_scheduler_contract():
    assets = ROOT / "hermes" / "skills" / "media" / "comfyui-media" / "assets"
    for path in assets.glob("h3_*.json"):
        workflow = json.loads(path.read_text())
        schedulers = [node for node in workflow.values()
                      if node.get("class_type") == "BasicScheduler"]
        assert schedulers, f"{path.name} has no BasicScheduler"
        for node in schedulers:
            assert node["inputs"]["scheduler"] == "simple"
            assert node["inputs"]["denoise"] == 1.0
