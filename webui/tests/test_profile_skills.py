"""Project skills stay inside the conversation that owns them."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profile_skills as scoped  # noqa: E402


def test_skill_list_and_view_follow_the_active_profile(monkeypatch):
    entries = {
        "skills_list": types.SimpleNamespace(handler=lambda args, **kw: json.dumps({
            "success": True,
            "skills": [
                {"name": "buddhist-canon-retrieval", "category": "research"},
                {"name": "archify", "category": "diagram"},
                {"name": "annotate-screenshot", "category": "mayi"},
            ],
            "count": 3, "categories": ["diagram", "mayi", "research"],
        })),
        "skill_view": types.SimpleNamespace(handler=lambda args, **kw: json.dumps({
            "success": True, "name": args["name"],
        })),
    }
    registry = types.SimpleNamespace(get_entry=entries.get)
    module = types.ModuleType("tools.registry")
    module.registry = registry
    monkeypatch.setitem(sys.modules, "tools.registry", module)

    scoped.install()
    scoped.install()  # reinstalling on another agent must not stack wrappers
    for profile, wanted in (
        ("buda", "buddhist-canon-retrieval"),
        ("code-autumn-rs", "archify"),
        ("mayi", "annotate-screenshot"),
    ):
        token = scoped.enter(profile)
        try:
            listed = json.loads(entries["skills_list"].handler({}))
            assert [s["name"] for s in listed["skills"]] == [wanted]
            assert listed["count"] == 1
            assert json.loads(entries["skill_view"].handler({"name": wanted}))["success"]
            other = next(name for name in scoped.SKILL_OWNER if name != wanted)
            assert not json.loads(entries["skill_view"].handler({"name": other}))["success"]
        finally:
            scoped.leave(token)

    token = scoped.enter("general")
    try:
        assert json.loads(entries["skills_list"].handler({}))["skills"] == []
        assert not json.loads(entries["skill_view"].handler({"name": "mayi/annotate-screenshot"}))["success"]
    finally:
        scoped.leave(token)
