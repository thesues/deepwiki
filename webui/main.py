"""Entry point — assembles the six pieces and serves.

Run with HERMES' interpreter, not ours. Importing `run_agent` is the whole
design, and it only resolves inside that venv. Everything this server adds is
stdlib, so nothing of ours competes with hermes' dependency tree:

    /opt/hermes/.venv/bin/python main.py

`DEEPWIKI_ENDPOINTS` is a JSON list, first entry the default:

    [{"key":"dsv4","label":"DSV4","model":"dsv4-flash",
      "base_url":"http://freetoken-l3:1919/v1","maxConcurrent":4}, ...]

Unset, the single endpoint the LLM_* variables already describe is used, which
is the previous behaviour exactly.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app_routes import build_app  # noqa: E402
from hermes_agent import AgentPool, load_endpoints, load_endpoints_env  # noqa: E402
from http_shell import serve  # noqa: E402
from mcp_resilience import install_semantic_error_guard  # noqa: E402
from profiles import load_profiles  # noqa: E402
from turns import TurnManager  # noqa: E402

log = logging.getLogger("deepwiki")


def mcp_servers_from_env() -> list[tuple[str, str]]:
    """The MCP servers this deployment declares, in order, as (name, url).

    `MCP_SERVERS` is the one variable: comma-separated `NAME=URL`, and the
    FIRST entry is the one the UI names — the same "first wins" rule
    `load_endpoints` and `build_profiles` follow, so the three ends agree
    without talking.

    `MEMORY_MCP_URL` / `MEMORY_MCP_NAME` / `MEMORY_MCP_EXTRA` are the old
    spelling and still read, because a manifest and a running PVC are not
    updated in the same instant and a rollout that silently dropped every
    server would take retrieval with it. They are a fallback, not a merge:
    a deployment states its servers in one place or the other, and reading
    both would make "remove a server" mean nothing.

    Order is preserved and duplicates collapse onto the first spelling.
    """
    def parse(spec: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in spec.split(","):
            item = item.strip()
            if not item or "=" not in item:
                continue
            name, url = (part.strip() for part in item.split("=", 1))
            if not name or not url or name in seen:
                continue
            seen.add(name)
            out.append((name, url))
        return out

    declared = parse(os.environ.get("MCP_SERVERS", ""))
    if declared:
        return declared

    legacy: list[tuple[str, str]] = []
    primary = os.environ.get("MEMORY_MCP_URL", "").strip()
    if primary:
        legacy.append((os.environ.get("MEMORY_MCP_NAME", "memory").strip() or "memory", primary))
    legacy.extend(parse(os.environ.get("MEMORY_MCP_EXTRA", "")))
    if legacy:
        log.warning(
            "MEMORY_MCP_URL/EXTRA are the old spelling; set MCP_SERVERS=%s",
            ",".join(f"{n}={u}" for n, u in legacy),
        )
    return legacy


def _sessions_module():
    """hermes' own session store, in process.

    The subprocess bridge this replaces existed because the two venvs disagreed
    on some packages — a reason that disappears once this server runs inside
    hermes' interpreter. Import failure is not fatal: the chat works without a
    sidebar, and refusing to start over it would be the wrong trade.
    """
    try:
        import hermes_session_api

        return hermes_session_api
    except Exception:  # noqa: BLE001
        log.warning("session store unavailable — the sidebar will be empty", exc_info=True)
        return None


def _reaper(manager: TurnManager, stop: threading.Event) -> None:
    """Drop finished streams so a long-lived server does not accumulate them.

    A finished stream is kept for a while so a reader who was away can still
    collect its tail; it is not kept forever.
    """
    while not stop.wait(60):
        try:
            manager.forget_finished(keep=200)
        except Exception:  # noqa: BLE001
            log.exception("stream reaper failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    endpoints = load_endpoints(
        load_endpoints_env(),
        default_home_model=os.environ.get("LLM_MODEL", ""),
    )
    log.info(
        "endpoints: %s",
        ", ".join(f"{e.key}={e.model}@{e.base_url} (<={e.max_concurrent})" for e in endpoints),
    )

    # The project cards. hermes' config.yaml is the standing source — a
    # `profiles:` section in the SAME file that owns mcp_servers and
    # platform_toolsets, so editing a profile is a config edit + restart,
    # never a code change. DEEPWIKI_PROFILES (JSON list) is the override entry
    # for a k8s ConfigMap. Neither present: one built-in default profile, the
    # pre-profile behaviour exactly.
    try:
        from hermes_cli.config import load_config

        cfg_profiles = (load_config() or {}).get("profiles")
    except Exception:  # noqa: BLE001 -- no hermes config, the env still works
        log.debug("could not read config.yaml for profiles", exc_info=True)
        cfg_profiles = None
    profiles = load_profiles(
        os.environ.get("DEEPWIKI_PROFILES", ""),
        cfg_profiles,
        default_directive=None,   # the built-in default falls back to CHAT_DIRECTIVE
    )
    log.info(
        "profiles: %s",
        " | ".join(
            f"{p.key} (mcp={','.join(p.mcp_servers or ['*'])}"
            f", workspace={p.workspace or '-'})"
            for p in profiles
        ),
    )

    # Before any agent is built: a tool result goes into the model's context
    # whole, and hermes caps nothing. One corpus search can spend a fifth of
    # the window on a single symbol's body — see tool_budget for the measured
    # numbers and why trimming each hit beats lowering `k`.
    from tool_budget import install as install_tool_ceiling

    install_tool_ceiling()

    # Point hermes at the retrieval server before any agent is built: the MCP
    # list is read when an agent is constructed, so writing it afterwards would
    # leave the first conversation of every restart without retrieval. Loud but
    # not fatal — chat without retrieval beats no chat, and `/api/status`
    # reports what was configured either way.
    hermes_cfg = Path(
        os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    ) / "config.yaml"
    # One memory-mcp instance serves ONE corpus, so a second project's index
    # is a second instance on its own port. All of them are declared in ONE
    # variable as NAME=URL pairs, in order — the first is the one the UI
    # names, the same "first entry wins" rule `load_endpoints` and
    # `build_profiles` use. A profile then narrows the agent to the server
    # its corpus lives on (profiles.py scope_agent_tools rule 1).
    # NOT `servers`: that name is taken further down for the config's own
    # mcp_servers mapping, and the rebind is invisible here — the UI's server
    # line then indexed a dict by 0 and the process died on startup.
    declared_servers = mcp_servers_from_env()
    for name, url in declared_servers:
        try:
            from hermes_config import ensure_mcp_server

            ensure_mcp_server(hermes_cfg, name, url)
        except Exception as e:  # noqa: BLE001
            log.error("could not point hermes at %s (%s): %s", name, url, e)
    # And remove what is no longer deployed. `ensure_mcp_server` only ever
    # adds, so a server dropped from the manifest lived on in the config file
    # hermes reads — handing the agent tools whose server is gone. See
    # `prune_mcp_servers`.
    try:
        from hermes_config import prune_mcp_servers

        prune_mcp_servers(hermes_cfg, [n for n, _ in declared_servers])
    except Exception as e:  # noqa: BLE001
        log.error("could not prune retired mcp servers: %s", e)

    # The toolsets live in the SAME config file, under `platform_toolsets.cli`
    # — the key hermes' own CLI reads. Seed it only if the file does not have
    # the key yet: HERMES_ACP_TOOLSETS (what the manifest used to drive
    # directly) becomes a FIRST-BOOT seed instead of a per-build lookup, and
    # from then on the file is authoritative — edit it, or run `hermes tools`.
    # The built-in default includes terminal, so an empty env and an empty file
    # still yield an agent that can run things.
    try:
        from hermes_config import ensure_platform_toolsets

        seed = [
            t.strip()
            for t in os.environ.get("HERMES_ACP_TOOLSETS", "").split(",")
            if t.strip()
        ]
        ensure_platform_toolsets(hermes_cfg, seed or None)
    except Exception as e:  # noqa: BLE001
        log.error("could not seed platform_toolsets: %s", e)

    # Context compression summarises an overflowing conversation through an
    # auxiliary model, which defaults to the ACTIVE endpoint's model — and
    # hermes refuses the session outright when that model's window is under
    # its 32K floor (the MiniMax endpoint's real, VRAM-derived ceiling).
    # Point the slot at the first endpoint that declares enough window;
    # seed-only, because a compression: mapping in the file is the operator's
    # choice, exactly like platform_toolsets.
    try:
        from hermes_config import ensure_compression_model

        ensure_compression_model(hermes_cfg, load_endpoints(load_endpoints_env()))
        # Independently of the above, which stops as soon as the file names a
        # compression model — as this deployment's does. A failed compression
        # with no deadline cost twelve minutes of a conversation showing
        # 回复中… and produced nothing; see the function's note.
    except Exception as e:  # noqa: BLE001
        log.error("could not seed auxiliary.compression: %s", e)

    # Say what actually resolved — the file won this boot or the seed did, and
    # the log is where the difference is visible without opening a shell.
    try:
        from hermes_cli.config import load_config
        from hermes_config import resolve_toolsets

        log.info("toolsets: %s", ", ".join(resolve_toolsets(load_config() or {})))
    except Exception:  # noqa: BLE001
        log.debug("could not log the resolved toolsets", exc_info=True)

    # Writing the server into config.yaml is not connecting to it. The ACP
    # adapter called `register_mcp_servers` itself; nothing in the library path
    # did, so `mcp_servers` sat in the config, `mcp-memory` sat in
    # enabled_toolsets, and the agent was handed eleven tools none of which
    # could reach the corpus. Asked about a sutra it ran `search_files` over a
    # filesystem that has no corpus on it and answered that the corpus "is not
    # available in this environment" — which was true, and entirely our doing.
    #
    # Once per process, before any agent is built: the registry is global and
    # `refresh_agent_mcp_tools` picks the tools up per turn from there.
    try:
        from hermes_cli.config import load_config
        from tools import mcp_tool
        from tools.mcp_tool import register_mcp_servers

        servers = (load_config() or {}).get("mcp_servers") or {}
        if servers:
            if install_semantic_error_guard(mcp_tool):
                log.info("MCP breaker: semantic tool errors do not mark a server unreachable")
            added = register_mcp_servers(servers)
            log.info("registered %d MCP tool(s) from %s: %s",
                     len(added), list(servers), ", ".join(added[:4]) + ("…" if len(added) > 4 else ""))
        else:
            log.warning("no mcp_servers in hermes config; corpus search will be unavailable")
    except Exception:  # noqa: BLE001 -- a chat box without retrieval still starts
        log.exception("could not register MCP servers; corpus search will be unavailable")

    artifacts = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "artifacts"
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001 -- a chat box without diagrams still starts
        log.error("could not create %s: diagrams will 404: %s", artifacts, e)

    # Σ max_concurrent is the exact number of turns admission ever lets run,
    # so it is the exact worker count the turn pool needs.
    manager = TurnManager(AgentPool(), workers=sum(e.max_concurrent for e in endpoints))
    # The frontend is immutable image content. It must never share a path with
    # the PVC: an old app.js on a volume can otherwise shadow a newly deployed
    # page and leave visible controls without their handlers. Runtime output
    # goes exclusively to ``artifacts`` below.
    shipped = HERE / "static"
    app = build_app(
        manager=manager,
        endpoints=endpoints,
        profiles=profiles,
        static_dir=shipped,
        # Where a drawn diagram lands. On the VOLUME, not in the image: the
        # agent writes here at runtime (skills/diagram/archify), and an
        # artifact has to outlive the pod that drew it — a link in a
        # transcript that 404s after the next rollout is worse than no link.
        artifacts_dir=artifacts,
        index_html=shipped / "index.html",
        auth_user=os.environ.get("AUTH_USER", ""),
        auth_pass=os.environ.get("AUTH_PASS", ""),
        sessions=_sessions_module(),
        mcp=(
            {"name": declared_servers[0][0], "url": declared_servers[0][1]}
            if declared_servers else None
        ),
    )

    srv = serve(app, args.host, args.port)
    log.info("deepwiki webui on http://%s:%d", args.host, args.port)

    stop = threading.Event()
    threading.Thread(target=_reaper, args=(manager, stop), name="reaper", daemon=True).start()

    # A container gets SIGTERM on rollout. Stop accepting, then exit — a turn in
    # flight is lost either way, and hanging on to the port makes the next pod
    # fail to bind.
    def _bye(*_a):
        log.info("shutting down")
        stop.set()
        srv.shutdown()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    stop.wait()


if __name__ == "__main__":
    main()
