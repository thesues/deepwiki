"""Request deadlines, independent of a native Kanban task's run budget.

``settings(endpoint_capabilities)`` returns ``tool_timeout_seconds`` (int),
``api_timeout_seconds`` (float), and ``api_stale_timeout_seconds`` (float).
Only API settings have endpoint overrides: either the returned API key names or
Hermes' native ``timeout_seconds`` / ``stale_timeout_seconds`` aliases. Other
capability keys are ignored. Explicit endpoint values beat COMPLEX_* environment
values; malformed/nonpositive/nonfinite values fail closed with ValueError.

Profile writers should put the API values in
``providers[provider]['models'][model]`` under ``timeout_seconds`` and
``stale_timeout_seconds`` respectively. Native provider/model resolution then
wins over the Worker's environment fallbacks, including for streaming calls.
The terminal bound is Worker-wide, configured by COMPLEX_TOOL_TIMEOUT_SECONDS.
Local commands additionally stop when their originating Worker PID/birth ends;
this is lifecycle cleanup, NOT a timeout value derived from the run budget.
No value here is derived from wall_seconds, max_runtime_seconds or an extension.
"""
import math
import os
from collections.abc import Mapping

# Hermes' terminal default, request fallback, and streaming stale default.
DEFAULT_TOOL_TIMEOUT_SECONDS = 180
DEFAULT_API_TIMEOUT_SECONDS = 1800.0
DEFAULT_API_STALE_TIMEOUT_SECONDS = 180.0


def _seconds(value, name, *, whole=False):
    if isinstance(value, bool):
        raise ValueError(name + ' must be positive finite seconds')
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(name + ' must be positive finite seconds') from None
    if not math.isfinite(seconds) or seconds <= 0 or (whole and not seconds.is_integer()):
        raise ValueError(name + (' must be a positive integer' if whole else ' must be positive finite seconds'))
    return int(seconds) if whole else seconds


def settings(overrides=None):
    """Return validated request settings; safe to call before importing Hermes tools."""
    if overrides is not None and not isinstance(overrides, Mapping):
        raise ValueError('request timeout overrides must be a mapping')
    overrides = overrides or {}
    result = {}
    for key, env, default, aliases in (
        ('tool_timeout_seconds', 'COMPLEX_TOOL_TIMEOUT_SECONDS', DEFAULT_TOOL_TIMEOUT_SECONDS, ()),
        ('api_timeout_seconds', 'COMPLEX_API_TIMEOUT_SECONDS', DEFAULT_API_TIMEOUT_SECONDS,
         ('api_timeout_seconds', 'timeout_seconds')),
        ('api_stale_timeout_seconds', 'COMPLEX_API_STALE_TIMEOUT_SECONDS', DEFAULT_API_STALE_TIMEOUT_SECONDS,
         ('api_stale_timeout_seconds', 'stale_timeout_seconds')),
    ):
        source = next((alias for alias in aliases if alias in overrides), None)
        value = overrides[source] if source is not None else os.environ.get(env, default)
        result[key] = _seconds(value, source or env, whole=key == 'tool_timeout_seconds')
    return result


def configure_worker_environment():
    """Undo native _default_spawn's run-derived terminal env BEFORE tool imports.

    Return the same settings mapping. Generated native per-model API settings
    remain authoritative; these environment values are only API fallbacks.
    """
    values = settings()
    os.environ['TERMINAL_TIMEOUT'] = str(values['tool_timeout_seconds'])
    os.environ['TERMINAL_MAX_FOREGROUND_TIMEOUT'] = str(values['tool_timeout_seconds'])
    os.environ['HERMES_API_TIMEOUT'] = str(values['api_timeout_seconds'])
    stale = str(values['api_stale_timeout_seconds'])
    os.environ['HERMES_API_CALL_STALE_TIMEOUT'] = stale
    os.environ['HERMES_STREAM_STALE_TIMEOUT'] = stale
    return values
