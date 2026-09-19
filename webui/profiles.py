"""AgentProfile — what makes one PROJECT's agent different from another's.

deepwiki serves several projects from one process, deepwiki.com style: the
homepage lists project cards, and opening one starts a conversation whose
agent carries THAT project's identity. The identity used to be global — one
CHAT_DIRECTIVE, one toolset list, every enabled MCP server, one skills dir —
which under several projects is contamination: the scripture agent would see
the code-index tools and vice versa. A profile is the parameterisation of
that identity, mirroring `Endpoint` (pure config, declared outside, resolved
at boot, keyed by a short string the client sends back).

A profile is NOT a model choice. Endpoints keep their own picker: a profile
says WHO the agent is, an endpoint says WHICH model answers. Their one point
of contact is `endpoints` — a profile may pin the subset its conversations
may use (a video profile nailed to the multimodal model).

Declarations come from, in order of precedence:

1. `DEEPWIKI_PROFILES` (JSON list, same shape as DEEPWIKI_ENDPOINTS) — the
   override entry, so k8s can inject profiles from a ConfigMap without
   editing a file inside the image.
2. hermes' config.yaml, a `profiles:` section keyed by profile key — the
   same file that owns mcp_servers and platform_toolsets, so editing a
   profile is a config edit + restart, never a code change. hermes ignores
   the key; we read it with the same loader it uses for its own keys.
3. neither → one built-in default profile (the CHAT_DIRECTIVE agent), which
   is exactly the pre-profile behaviour, so an un-migrated deploy still
   boots and still answers.

Per-profile skills carry a known gap, recorded in `skills`' docstring: hermes
reads skills from ONE process-wide directory and exposes no per-agent
injection point yet, so the field is declared, surfaced through the API, and
honestly inert until upstream grows the knob.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("deepwiki.profiles")


class AgentProfile:
    """One project's agent identity. See the module docstring for the split
    against Endpoint."""

    __slots__ = ("key", "label", "directive", "toolsets", "mcp_servers",
                 "workspace", "skills", "endpoints", "mcp_tools")

    def __init__(
        self,
        key: str,
        label: str = "",
        directive: str | None = None,
        toolsets: list[str] | None = None,
        mcp_servers: list[str] | None = None,
        workspace: str = "",
        skills: str = "",
        endpoints: list[str] | None = None,
        mcp_tools: list[str] | None = None,
    ) -> None:
        self.key = str(key)
        self.label = str(label or key)
        # None means "the deployment's standing brief" — run_turn falls back to
        # CHAT_DIRECTIVE, which is the pre-profile behaviour preserved. An
        # explicit "" suppresses the brief entirely, same rule as run_turn.
        self.directive = None if directive is None else str(directive)
        # None means "inherit the global toolset list" — per-toolset trimming
        # happens in scope_agent_tools, not here.
        self.toolsets = [str(t) for t in toolsets if str(t).strip()] if toolsets else None
        # None means "every enabled MCP server" (pre-profile behaviour); a list
        # is a SUBSET of the config's registered names, filtered at build time.
        self.mcp_servers = [str(s) for s in mcp_servers if str(s).strip()] if mcp_servers else None
        # The profile's folder on autumnfs: the agent's terminal cwd (registered
        # per session, the same injection point ACP's session/load uses).
        self.workspace = str(workspace or "").strip()
        # Declared but INERT today — see the module docstring. Surfaced so the
        # API and the logs say what was configured, and so the wiring is a
        # one-line change when hermes grows the injection point.
        self.skills = str(skills or "").strip()
        # None = any endpoint. A list restricts /api/chat/start to it.
        self.endpoints = [str(e) for e in endpoints if str(e).strip()] if endpoints else None
        # None means "every tool the granted servers expose" — the
        # pre-existing behaviour. A list names the BARE tool names to keep
        # (`search_docs`, not `mcp_memory_search_docs`), because an MCP
        # server is otherwise all-or-nothing: `mcp_servers` grants a server
        # and the server decides what that means. memory-mcp exposes 18
        # tools, of which the scripture profile has business with four; the
        # rest are a code index it has no corpus for and graph WRITES
        # (`graph_delete_node`, `ingest_documents`) nobody asked it to make.
        self.mcp_tools = [str(t).strip() for t in mcp_tools if str(t).strip()] if mcp_tools else None

    def as_json(self) -> dict:
        return {"key": self.key, "label": self.label}

    def __repr__(self) -> str:  # pragma: no cover - logging aid
        return f"AgentProfile({self.key!r}, mcp={self.mcp_servers!r}, workspace={self.workspace!r})"


def _norm_key(key: str) -> str:
    """What the client sends back must survive a config edit's whitespace."""
    return str(key or "").strip()


def _profile_from(entry: dict, key: str, default_directive: str | None) -> AgentProfile | None:
    """One mapping → one profile. Returns None (logged) for one that cannot
    work: a bad entry must not cost the others, and it must not fall back
    silently to the default either — that would put a scripture brief on a
    code project with nothing in the logs to explain it."""
    if not isinstance(entry, dict):
        log.error("profile %s: entry is not a mapping; skipped", key)
        return None
    p = AgentProfile(
        key=key,
        label=str(entry.get("label") or key),
        directive=entry.get("directive") if entry.get("directive") is not None else default_directive,
        toolsets=entry.get("toolsets") or entry.get("platform_toolsets"),
        mcp_servers=entry.get("mcp_servers") or entry.get("mcpServers"),
        workspace=entry.get("workspace") or "",
        skills=entry.get("skills") or "",
        endpoints=entry.get("endpoints"),
        mcp_tools=entry.get("mcp_tools") or entry.get("mcpTools"),
    )
    if not p.key:
        log.error("profile with an empty key: skipped (label=%r)", p.label)
        return None
    return p


def build_profiles(
    items: list | dict | None,
    default_directive: str | None = None,
    default_key: str = "default",
    default_label: str = "默认",
) -> list[AgentProfile]:
    """Normalize either declaration shape into an ordered list.

    Accepts the config.yaml shape (a mapping keyed by profile key) and the
    env shape (a JSON list of dicts, each carrying its own `key`). Order is
    preserved — the FIRST entry is the default profile, the same rule
    `load_endpoints` uses, so the two ends agree without talking.

    Never raises: a malformed collection yields the built-in default. A chat
    that starts on the standing brief beats a correct refusal to start.
    """
    profiles: list[AgentProfile] = []
    if isinstance(items, dict):
        for key, entry in items.items():
            k = _norm_key(key)
            p = _profile_from(entry or {}, k, default_directive)
            if p is not None:
                profiles.append(p)
    elif isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                log.error("profile entry is not an object; skipped: %r", entry)
                continue
            k = _norm_key(entry.get("key"))
            p = _profile_from(entry, k, default_directive)
            if p is not None:
                profiles.append(p)
    elif items is not None:
        log.error("profiles declaration is neither a mapping nor a list; ignored")

    seen: set[str] = set()
    unique: list[AgentProfile] = []
    for p in profiles:
        if p.key in seen:
            log.error("duplicate profile key %s; first wins", p.key)
            continue
        seen.add(p.key)
        unique.append(p)
    if not unique:
        # The built-in default: one project, the standing brief. directive
        # stays None so run_turn's CHAT_DIRECTIVE fallback applies — which is
        # what makes this byte-for-byte the pre-profile behaviour.
        unique = [AgentProfile(key=default_key, label=default_label,
                               directive=default_directive)]
        log.info("profiles: none declared; one built-in default profile")
    return unique


def load_profiles(
    env_raw: str | None,
    cfg_profiles: dict | list | None,
    default_directive: str | None = None,
) -> list[AgentProfile]:
    """Resolve the declarations, env override first, config file second.

    The env is the OVERRIDE entry (k8s ConfigMap injection), the config file
    the standing source. An env that parses wins outright; an env that does
    not parse falls through to the config rather than taking the deploy down.
    """
    raw = (env_raw or "").strip()
    if raw:
        try:
            return build_profiles(json.loads(raw), default_directive=default_directive)
        except Exception as e:  # noqa: BLE001
            log.error("DEEPWIKI_PROFILES ignored (%s); falling back to config.yaml", e)
    return build_profiles(cfg_profiles, default_directive=default_directive)


def resolve_profile(profiles: list[AgentProfile], key: str | None) -> AgentProfile:
    """The profile a request names, with the stale-key fallback.

    Same rule as the endpoint picker: a key the server no longer advertises
    (a profile renamed in config, a pod behind) resolves to the DEFAULT —
    the first entry — rather than failing every send until someone notices.
    """
    if profiles:
        for p in profiles:
            if p.key == (key or "").strip():
                return p
        return profiles[0]
    raise ValueError("no profiles configured")  # callers always pass a built list


def allowed_endpoint(profile: AgentProfile | None, endpoint_key: str, default_key: str) -> str:
    """The endpoint this turn may use, honouring the profile's pin.

    A profile that names endpoints refuses the ones outside the list — not
    with an error, but by falling back to the FIRST allowed one: the pin is
    a property of the project (the video profile needs the multimodal model),
    so the honest answer to "can I use dsv4 here" is "use vision instead",
    echoed back so the client's picker stays truthful.
    """
    if profile is None or not profile.endpoints:
        return endpoint_key or default_key
    if endpoint_key in profile.endpoints:
        return endpoint_key
    return profile.endpoints[0]


def scope_agent_tools(
    profile: AgentProfile | None,
    global_toolsets: list[str],
    enabled_servers: list[str],
) -> tuple[list[str], list[str]]:
    """Trim (toolsets, mcp_server_names) to what ONE profile may see.

    `global_toolsets` is the process-wide resolution — hermes' own resolver
    output with `mcp-<name>` appended for every enabled server, exactly what
    build_agent computes today. Three rules on top:

    * a profile that names `mcp_servers` gets ONLY those servers, and the
      `mcp-<name>` toolsets of the others come OFF the list — leaving them on
      would hand the agent tools whose server is not connected to it, which
      reads as a working tool that always fails;
    * a profile that names `toolsets` replaces the platform list outright
      (the MCP reconciliation still runs on top of the replacement);
    * a profile that keeps `terminal` gets `clarify` too, whether it asked
      for it or not — see the comment on rule 3 below.
    """
    toolsets = list(global_toolsets)
    servers = list(enabled_servers)
    # Rule 1 — the MCP subset. Every enabled server's `mcp-<name>` toolset is
    # in global_toolsets (the resolver appends it); narrowing the servers must
    # also strip the toolsets of the ones excluded, or the agent carries tools
    # whose server was never handed to it — a working-looking tool that always
    # fails. No additions happen here: an `mcp-*` name for a server that is
    # not enabled must never appear, and the enabled check is what keeps that
    # honest.
    if profile is not None and profile.mcp_servers is not None:
        wanted = set(profile.mcp_servers)
        servers = [s for s in servers if s in wanted]
        toolsets = [t for t in toolsets if not t.startswith("mcp-") or t[4:] in wanted]
    # Rule 2 — the toolset replacement. Independent of rule 1 so a profile can
    # narrow servers AND replace the platform list; the (possibly trimmed)
    # servers' toolsets are reconciled on top, the same belt-and-braces shape
    # build_agent applies to the global list.
    if profile is not None and profile.toolsets is not None:
        toolsets = list(profile.toolsets)
        for name in servers:
            t = f"mcp-{name}"
            if t not in toolsets:
                toolsets.append(t)
    # Rule 3 — terminal implies clarify. Applied AFTER the replacement so it
    # holds for the profile's own list too, and only when the profile actually
    # carries terminal: adding clarify to a profile that cannot run anything
    # would just be a question box on a turn that needs none.
    if profile is not None and "terminal" in toolsets and "clarify" not in toolsets:
        toolsets.append("clarify")
        log.info("profile %s: terminal without clarify; clarify added", profile.key)
    return toolsets, servers


def allowed_mcp_names(allow: list[str] | None, servers: list[str]) -> set[str] | None:
    """The exact tool names a profile's `mcp_tools` allowlist admits.

    Built by CONSTRUCTION, not by matching: hermes names an MCP tool
    `mcp_{server}_{tool}` with both halves sanitized (anything outside
    `[A-Za-z0-9_]` becomes `_`, so `code-index` becomes `code_index`), and it
    says in its own source that the form is ambiguous — `mcp_a_b_tool` is
    either server `a` + tool `b_tool` or server `a_b` + tool `tool`. Matching
    a suffix inherits that ambiguity and adds one of its own: allowing
    `read_file` would also admit some server's `unsafe_read_file`. Spelling
    out every (server, tool) pair we mean is exact, and the servers are known
    here — they are the ones the profile was granted.

    `None` (no allowlist) means every tool of the granted servers, which is
    the behaviour profiles had before this existed.
    """
    if allow is None:
        return None
    san = lambda v: re.sub(r"[^A-Za-z0-9_]", "_", str(v or ""))  # noqa: E731
    return {f"mcp_{san(s)}_{san(t)}" for s in servers for t in allow}


def tool_allowed(name: str, allowed: set[str] | None) -> bool:
    """Is `name` a tool this profile may carry?

    Only MCP tools are judged. Everything else was already decided by the
    toolset list, and re-deciding it here would mean a profile that names
    `mcp_tools` silently loses its file or skills tools.
    """
    if allowed is None or not name.startswith("mcp_"):
        return True
    return name in allowed


def register_workspace_cwd(session_id: str, workspace: str) -> bool:
    """Point this session's terminal at the profile's workspace folder.

    Uses hermes' own per-task override — the same injection point its ACP
    adapter calls on `session/load` — so directory isolation is a mechanism,
    not a prompt request. Only registered when the folder EXISTS: a mount
    that has not come up yet (FUSE INIT) must not poison the terminal env
    with a cwd that cannot be entered. Returns True when registered.

    Runs inside hermes' interpreter only; guarded like every hermes import.
    """
    if not workspace:
        return False
    if not Path(workspace).is_dir():
        log.warning("profile workspace %s does not exist (yet); terminal stays on the default cwd", workspace)
        return False
    try:
        from tools.terminal_tool import register_task_env_overrides

        register_task_env_overrides(session_id, {"cwd": workspace})
        return True
    except Exception:  # noqa: BLE001 -- a missing terminal module must not fail the build
        log.debug("could not register workspace cwd for %s", session_id, exc_info=True)
        return False
