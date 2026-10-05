"""The local MCP breaker compatibility guard."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_resilience import install_semantic_error_guard, is_semantic_tool_error, recover_mcp_servers


def test_startup_discovery_recovers_without_reconnecting_healthy_servers():
    import threading

    connected = {"mayi"}
    attempts = []

    def register(servers):
        attempts.append(set(servers))
        if len(attempts) == 1:
            raise TimeoutError("retriever still starting")
        connected.update(servers)

    module = SimpleNamespace(
        get_mcp_status=lambda: [{"name": n, "connected": True} for n in connected],
        register_mcp_servers=register,
    )
    recover_mcp_servers(module, {
        "memory": {}, "code-index": {}, "mayi": {}, "disabled": {"enabled": False},
    }, threading.Event(), interval=0)
    assert attempts == [{"memory", "code-index"}, {"memory", "code-index"}]


def test_mcp_recovery_stops_before_retry_on_shutdown():
    import threading

    stop = threading.Event()
    stop.set()
    module = SimpleNamespace(get_mcp_status=lambda: (_ for _ in ()).throw(AssertionError("stopped")))
    recover_mcp_servers(module, {"memory": {}}, stop, interval=0)


def test_only_known_lance_tool_errors_are_semantic():
    assert is_semantic_tool_error(json.dumps({"error": "cannot read x: not a file this server indexed"}))
    assert not is_semantic_tool_error(json.dumps({"error": "MCP call failed: ConnectError"}))
    assert not is_semantic_tool_error("not-json")


def test_a_bad_indexed_path_does_not_open_the_server_breaker():
    state = {"errors": 0}

    def factory(_server, *_args, **_kwargs):
        def handler(*_call_args, **_call_kwargs):
            state["errors"] += 1  # Hermes' original isError accounting
            return json.dumps({"error": "cannot read x: not a file this server indexed"})
        return handler

    def reset(_server):
        state["errors"] = 0

    module = SimpleNamespace(_make_tool_handler=factory, _reset_server_error=reset)
    assert install_semantic_error_guard(module)
    assert not install_semantic_error_guard(module), "installation must be idempotent"
    result = module._make_tool_handler("code-index")( {})
    assert "not a file" in result
    assert state["errors"] == 0


def test_transport_errors_still_count_toward_the_breaker():
    state = {"errors": 0}

    def factory(_server, *_args, **_kwargs):
        def handler(*_call_args, **_call_kwargs):
            state["errors"] += 1
            return json.dumps({"error": "MCP call failed: ConnectError"})
        return handler

    module = SimpleNamespace(_make_tool_handler=factory, _reset_server_error=lambda _server: state.update(errors=0))
    assert install_semantic_error_guard(module)
    module._make_tool_handler("code-index")({})
    assert state["errors"] == 1
