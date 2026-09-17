"""The AgentProfile rules: declaration, resolution, and the tool-scope cut.

profiles.py is pure config — no hermes import — so every rule here runs on its
own. Each test names the failure that motivates it: the ones that cost a
conversation were the silent fallbacks, where a project's agent quietly came
up wearing another project's brief or another project's tools.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import profiles as pr  # noqa: E402


# ── declaration ─────────────────────────────────────────────────────────────


def test_the_config_shape_is_a_mapping_keyed_by_profile():
    ps = pr.build_profiles({
        "buda": {"label": "佛典检索", "mcp_servers": ["memory"]},
        "code": {"label": "代码理解", "mcp_servers": ["code-index"]},
    })
    assert [p.key for p in ps] == ["buda", "code"]
    assert ps[0].mcp_servers == ["memory"]
    assert ps[1].label == "代码理解"


def test_the_env_shape_is_a_list_carrying_its_own_keys():
    ps = pr.build_profiles([
        {"key": "buda", "label": "佛典", "mcpServers": ["memory"]},
{"key": "video", "endpoints": ["vision"]},
    ])
    assert [p.key for p in ps] == ["buda", "video"]
    # camelCase (the DEEPWIKI_ENDPOINTS convention) and snake_case both parse.
    assert ps[0].mcp_servers == ["memory"]
    assert ps[1].endpoints == ["vision"]


def test_a_bad_entry_is_skipped_not_fatal_and_not_silent():
    """One malformed profile must not cost the others — and must not fall back
    to the default either: that would put the standing brief on a project it
    was never meant for, with nothing in the logs to explain it. Skipping, as
    here, keeps the surviving profiles honest."""
    ps = pr.build_profiles({"buda": "not a mapping", "code": {"label": "代码"}})
    assert [p.key for p in ps] == ["code"]


def test_duplicate_keys_first_wins():
    ps = pr.build_profiles([
        {"key": "buda", "label": "first"},
        {"key": "buda", "label": "second"},
    ])
    assert len(ps) == 1 and ps[0].label == "first"


def test_no_declaration_yields_the_builtin_default():
    """The pre-profile behaviour, byte for byte: one profile whose directive is
    None, so run_turn falls back to CHAT_DIRECTIVE. An un-migrated deploy must
    boot and answer exactly as before."""
    (ps,) = pr.build_profiles(None)
    assert ps.key == "default" and ps.directive is None
    assert ps.mcp_servers is None and ps.toolsets is None
    (ps,) = pr.build_profiles([])
    assert ps.key == "default"


def test_directive_none_inherits_an_empty_one_suppresses():
    ps = pr.build_profiles([
        {"key": "a"},
        {"key": "b", "directive": ""},
        {"key": "c", "directive": "定制 brief"},
    ])
    assert ps[0].directive is None      # inherit the standing brief
    assert ps[1].directive == ""        # explicit suppression
    assert ps[2].directive == "定制 brief"


# ── resolution ──────────────────────────────────────────────────────────────


def test_the_first_entry_is_the_default():
    ps = pr.build_profiles([{"key": "buda"}, {"key": "code"}])
    assert pr.resolve_profile(ps, None) is ps[0]
    assert pr.resolve_profile(ps, "code") is ps[1]


def test_a_stale_key_falls_back_to_the_default():
    """A profile renamed in config while an old tab still sends the old key:
    the send must land somewhere real, not 404 forever."""
    ps = pr.build_profiles([{"key": "buda"}, {"key": "code"}])
    assert pr.resolve_profile(ps, "gone").key == "buda"


def test_load_profiles_env_overrides_the_config_file():
    ps = pr.load_profiles(
        '[{"key":"from-env"}]',
        {"from-config": {"label": "from config"}},
    )
    assert [p.key for p in ps] == ["from-env"]


def test_an_unparseable_env_falls_back_to_the_config():
    """A broken ConfigMap JSON must not take the deploy down — the standing
    config is still a working profile set."""
    ps = pr.load_profiles("{not json", {"from-config": {}})
    assert [p.key for p in ps] == ["from-config"]


def test_load_profiles_with_neither_source_yields_the_default():
    (ps,) = pr.load_profiles("", None)
    assert ps.key == "default"


# ── the endpoint pin ────────────────────────────────────────────────────────


def test_a_profile_without_a_pin_passes_the_choice_through():
    p = pr.build_profiles([{"key": "a"}])[0]
    assert pr.allowed_endpoint(p, "vision", "dsv4") == "vision"


def test_a_pinned_profile_redirects_outside_choices():
    """The pin is a property of the project (the video profile needs the
    multimodal model), so a foreign endpoint choice redirects to the first
    allowed one rather than erroring — and the route echoes the redirect."""
    p = pr.build_profiles([{"key": "video", "endpoints": ["vision"]}])[0]
    assert pr.allowed_endpoint(p, "vision", "dsv4") == "vision"
    assert pr.allowed_endpoint(p, "dsv4", "dsv4") == "vision"
    assert pr.allowed_endpoint(p, "", "dsv4") == "vision"


# ── the tool-scope cut ──────────────────────────────────────────────────────


def _global():
    """What resolve_toolsets + the belt-and-braces loop produce today: the
    platform list with every enabled server's mcp-<name> appended."""
    return ["file", "terminal", "skills", "mcp-memory", "mcp-code-index"], ["memory", "code-index"]


def test_no_profile_leaves_the_global_surface_untouched():
    toolsets, servers = pr.scope_agent_tools(None, *_global())
    assert toolsets == ["file", "terminal", "skills", "mcp-memory", "mcp-code-index"]
    assert servers == ["memory", "code-index"]


def test_an_mcp_subset_trims_both_servers_and_their_toolsets():
    """Narrowing the servers must also strip the excluded servers' toolsets,
    or the agent carries tools whose server was never handed to it — a
    working-looking tool that always fails."""
    p = pr.build_profiles([{"key": "buda", "mcp_servers": ["memory"]}])[0]
    toolsets, servers = pr.scope_agent_tools(p, *_global())
    assert servers == ["memory"]
    assert "mcp-memory" in toolsets
    assert "mcp-code-index" not in toolsets


def test_a_server_not_enabled_is_never_added():
    """A profile naming a server the config does not register must not gain an
    mcp-* toolset for it — a dead toolset, not a feature."""
    p = pr.build_profiles([{"key": "buda", "mcp_servers": ["memory", "ghost"]}])[0]
    toolsets, servers = pr.scope_agent_tools(p, *_global())
    assert servers == ["memory"]
    assert "mcp-ghost" not in toolsets


def test_a_toolset_list_replaces_the_platform_list():
    p = pr.build_profiles([{"key": "buda", "toolsets": ["file", "skills"]}])[0]
    toolsets, servers = pr.scope_agent_tools(p, *_global())
    assert toolsets == ["file", "skills", "mcp-memory", "mcp-code-index"]
    assert servers == ["memory", "code-index"]


def test_a_toolset_list_and_an_mcp_subset_compose():
    p = pr.build_profiles([
        {"key": "buda", "toolsets": ["file", "skills"], "mcp_servers": ["memory"]},
    ])[0]
    toolsets, servers = pr.scope_agent_tools(p, *_global())
    assert toolsets == ["file", "skills", "mcp-memory"]
    assert servers == ["memory"]


# ── the workspace registration ──────────────────────────────────────────────


def test_a_missing_workspace_is_reported_not_registered(tmp_path, caplog):
    """A FUSE mount that has not come up yet must not poison the terminal env
    with a cwd that cannot be entered — the honest answer is 'not registered',
    logged, with the turn proceeding on the default cwd."""
    assert pr.register_workspace_cwd("s1", str(tmp_path / "absent")) is False
    assert pr.register_workspace_cwd("s1", "") is False


def test_an_existing_workspace_outside_hermes_reports_failure(tmp_path):
    """Inside hermes' interpreter this registers the per-task cwd override;
    here the import fails, and the guard — not the turn — absorbs it."""
    assert pr.register_workspace_cwd("s1", str(tmp_path)) is False
