"""Profile config controls both the prompt index and skill tools."""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import profile_skills as scoped
from profiles import build_profiles


def test_configured_skills_follow_the_active_profile(monkeypatch):
    catalog = [
        {"name": "lookup", "category": "research"},
        {"name": "draw", "category": "visual"},
        {"name": "generated-later", "category": "visual"},
    ]
    monkeypatch.setattr(scoped, "_catalog", lambda: catalog)
    prompt_builder = types.SimpleNamespace(get_disabled_skill_names=lambda *_a, **_kw: {"operator-disabled"})
    monkeypatch.setitem(sys.modules, "agent", types.SimpleNamespace(prompt_builder=prompt_builder))
    entries = {
        "skills_list": types.SimpleNamespace(handler=lambda args, **kw: json.dumps({
            "skills": catalog, "count": len(catalog), "categories": ["research", "visual"],
        })),
        "skill_view": types.SimpleNamespace(handler=lambda args, **kw: json.dumps({
            "success": True, "name": args["name"],
        })),
    }
    module = types.ModuleType("tools.registry")
    module.registry = types.SimpleNamespace(get_entry=entries.get)
    monkeypatch.setitem(sys.modules, "tools.registry", module)
    scoped.install()
    scoped.install()
    profiles = build_profiles([
        {"key": "arbitrary-one", "skills": ["lookup"]},
        {"key": "renamed-project", "skills": ["visual/*"]},
        {"key": "empty-project", "skills": []},
        {"key": "inherit-project"},
    ])
    # Re-enter the first profile to catch cache or context leaking on reuse.
    for profile, wanted in zip(profiles + profiles[:1], [
        ["lookup"], ["draw", "generated-later"], [],
        ["lookup", "draw", "generated-later"], ["lookup"],
    ]):
        token = scoped.enter(profile)
        try:
            listed = json.loads(entries["skills_list"].handler({}))
            assert [s["name"] for s in listed["skills"]] == wanted
            assert listed["count"] == len(wanted)
            disabled = prompt_builder.get_disabled_skill_names()
            assert "operator-disabled" in disabled
            for skill in catalog:
                name = skill["name"]
                visible = name in wanted
                assert (name not in disabled) == visible
                for requested in (name, skill["category"] + "/" + name):
                    assert json.loads(entries["skill_view"].handler({"name": requested}))["success"] == visible
            # A caller cannot forge the allowed category around a foreign skill.
            if profile.key == "renamed-project":
                assert not json.loads(entries["skill_view"].handler({"name": "visual/lookup"}))["success"]
        finally:
            scoped.leave(token)
    assert scoped._patterns.get() is None
