"""Keep MCP availability separate from a tool's domain-level answer.

Hermes' current MCP adapter increments its per-server circuit breaker for every
``CallToolResult.isError``.  That is right for a dead transport, but wrong for
a healthy retriever saying that a requested file does not exist.  One mistaken
path then makes every tool on that server appear unavailable for a minute.

The adapter creates every registered handler through ``_make_tool_handler``.
Wrapping that factory lets the adapter retain its connection/timeout breaker
while clearing only the count it just added for known, structured tool errors.
This is deliberately narrow: uncertain errors still exercise Hermes' original
protection rather than being guessed to be safe.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any


log = logging.getLogger("deepwiki.mcp_resilience")


def recover_mcp_servers(mcp_tool: Any, servers: dict, stop: Any, interval: float = 30) -> None:
    """Retry failed startup discovery; Hermes handles registered connections.

    Startup can race the retrievers. A failed discovery registers no tools,
    so neither an agent's tool refresh nor the transport breaker can retry it.
    Run off the request threads and stop once all configured servers connect.
    """
    pending = {name: cfg for name, cfg in servers.items() if cfg.get("enabled", True) is not False}
    while pending and not stop.is_set():
        try:
            connected = {s["name"] for s in mcp_tool.get_mcp_status() if s.get("connected")}
            pending = {name: cfg for name, cfg in pending.items() if name not in connected}
            if not pending or stop.wait(interval):
                return
            log.info("retrying MCP discovery for %s", ", ".join(pending))
            mcp_tool.register_mcp_servers(pending)
        except Exception:  # keep transient failures off the HTTP server
            log.exception("MCP discovery retry failed")
            if stop.wait(interval):
                return


# These are messages raised by lance-mcp after it has successfully received,
# parsed and answered an MCP call.  They are not evidence that its HTTP/MCP
# session is unhealthy.
_SEMANTIC_ERROR_MARKERS = (
    "not a file this server indexed",
    "unknown tool:",
    "range start is after end",
    "range start is past the end",
)


def is_semantic_tool_error(result: str) -> bool:
    """Whether a serialized MCP response is a known, non-transport error."""
    try:
        error = json.loads(result).get("error", "")
    except (TypeError, ValueError, AttributeError):
        return False
    return isinstance(error, str) and any(marker in error.lower() for marker in _SEMANTIC_ERROR_MARKERS)


def install_semantic_error_guard(mcp_tool: Any) -> bool:
    """Patch one Hermes MCP module before its tools are registered.

    Returns ``True`` when the guard was installed.  An already-patched or
    incompatible Hermes version is left untouched; availability must never
    depend on this compatibility layer.
    """
    factory = getattr(mcp_tool, "_make_tool_handler", None)
    reset = getattr(mcp_tool, "_reset_server_error", None)
    if not callable(factory) or not callable(reset):
        return False
    if getattr(factory, "_deepwiki_semantic_error_guard", False):
        return False

    @functools.wraps(factory)
    def guarded_factory(server_name: str, *args: Any, **kwargs: Any):
        handler = factory(server_name, *args, **kwargs)

        @functools.wraps(handler)
        def guarded_handler(*call_args: Any, **call_kwargs: Any):
            result = handler(*call_args, **call_kwargs)
            if is_semantic_tool_error(result):
                # The original handler already incremented the count for this
                # response.  Undo only that false availability signal.
                reset(server_name)
            return result

        return guarded_handler

    guarded_factory._deepwiki_semantic_error_guard = True
    mcp_tool._make_tool_handler = guarded_factory
    return True
