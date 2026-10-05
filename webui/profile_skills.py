"""Keep the skills shipped for one project out of the other projects' tools."""

from __future__ import annotations

import contextvars
from fnmatch import fnmatchcase
import json
import threading


_patterns: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "deepwiki_skill_patterns", default=None,
)
_install_lock = threading.Lock()


def enter(profile):
    skills = profile.skills if profile is not None else None
    return _patterns.set(tuple(skills) if skills is not None else None)


def leave(token) -> None:
    _patterns.reset(token)


def _catalog():
    from tools.skills_tool import _find_all_skills

    return _find_all_skills(skip_disabled=True)


def _visible(name: str, category: str | None = None) -> bool:
    patterns = _patterns.get()
    if patterns is None:
        return True
    bare = str(name or "").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if category is None:
        # Resolve category from the catalog, not from an untrusted caller's
        # prefix. skill_view accepts both bare and qualified names.
        category = next((s.get("category", "") for s in _catalog() if s["name"] == bare), "")
    qualified = f"{category}/{bare}" if category else bare
    return any(fnmatchcase(bare, p) or fnmatchcase(qualified, p) for p in patterns)


def install() -> None:
    """Wrap Hermes' registered handlers once; contextvars isolate live turns."""
    from tools.registry import registry

    with _install_lock:
        # The prompt index is built separately from skills_list. Use Hermes'
        # disabled-name input so its prompt cache key includes the project
        # exclusions too; filtering only tool responses leaks the first index.
        from agent import prompt_builder

        disabled_names = prompt_builder.get_disabled_skill_names
        if not getattr(disabled_names, "_deepwiki_skill_scope", False):
            def scoped_disabled(*args, _original=disabled_names, **kwargs):
                return set(_original(*args, **kwargs)) | {
                    s["name"] for s in _catalog()
                    if not _visible(s["name"], s.get("category", ""))
                }

            scoped_disabled._deepwiki_skill_scope = True
            prompt_builder.get_disabled_skill_names = scoped_disabled

        for tool_name in ("skills_list", "skill_view"):
            entry = registry.get_entry(tool_name)
            if entry is None or getattr(entry.handler, "_deepwiki_skill_scope", False):
                continue
            original = entry.handler

            if tool_name == "skill_view":
                def scoped_view(args, _original=original, **kwargs):
                    if not _visible(args.get("name", "")):
                        return json.dumps({"success": False, "error": "skill not available in this project"})
                    return _original(args, **kwargs)

                wrapped = scoped_view
            else:
                def scoped_list(args, _original=original, **kwargs):
                    result = _original(args, **kwargs)
                    try:
                        payload = json.loads(result)
                        if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
                            return result
                        payload["skills"] = [
                            skill for skill in payload["skills"]
                            if _visible(skill.get("name", ""), skill.get("category", ""))
                        ]
                        payload["count"] = len(payload["skills"])
                        payload["categories"] = sorted({
                            skill.get("category") for skill in payload["skills"]
                            if skill.get("category")
                        })
                        return json.dumps(payload, ensure_ascii=False)
                    except (TypeError, ValueError):
                        return result

                wrapped = scoped_list
            wrapped._deepwiki_skill_scope = True
            entry.handler = wrapped
