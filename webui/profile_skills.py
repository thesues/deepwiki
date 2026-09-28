"""Keep the skills shipped for one project out of the other projects' tools."""

from __future__ import annotations

import contextvars
import json
import threading


# These are the project skills shipped in /app/hermes/skills. Hermes scans one
# process-wide directory, so its skills_list/skill_view tools need the same
# per-conversation boundary that the MCP and annotation tools already have.
SKILL_OWNER = {
    "buddhist-canon-retrieval": "buda",
    "archify": "code-autumn-rs",
    "annotate-screenshot": "mayi",
}

_profile: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "deepwiki_skill_profile", default=None,
)
_install_lock = threading.Lock()


def enter(profile_key: str | None):
    return _profile.set(profile_key)


def leave(token) -> None:
    _profile.reset(token)


def _visible(name: str) -> bool:
    bare = str(name or "").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    owner = SKILL_OWNER.get(bare)
    current = _profile.get()
    return owner is None or owner == current


def install() -> None:
    """Wrap Hermes' registered handlers once; contextvars isolate live turns."""
    from tools.registry import registry

    with _install_lock:
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
                            if _visible(skill.get("name", ""))
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
