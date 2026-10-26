"""Pause native timeout accounting across system sleep, not the dispatcher.

Keep native wall timestamps intact. Only run elapsed time changes; anchors live
in the existing native run metadata, surviving WebUI restarts on the same boot.
No new scheduler, task table, or global time monkeypatch.
"""
import inspect
import json
import os
from pathlib import Path
import sys
import time
from functools import lru_cache

KEY = '_research_clock'


def enabled():
    return os.environ.get('COMPLEX_PAUSE_SLEEP', '1' if sys.platform == 'darwin' else '0') == '1'


@lru_cache(maxsize=1)
def boot_id():
    if sys.platform == 'darwin':
        # CLOCK_MONOTONIC includes system sleep on macOS, whereas Python's
        # monotonic() uses sleep-excluding mach_absolute_time(). Their boot
        # epoch needs no privileged sysctl or power-management subprocess.
        return 'darwin-boot-' + str(round((time.time()-time.clock_gettime(time.CLOCK_MONOTONIC))/60))
    path = Path('/proc/sys/kernel/random/boot_id')
    return path.read_text().strip() if path.exists() else 'process-' + str(os.getpid())


def _metadata(run):
    value = run.metadata if run else None
    if isinstance(value, dict):return value
    try:
        parsed=json.loads(value or '{}')
        return parsed if isinstance(parsed,dict) else {}
    except (ValueError, TypeError):return {}


def elapsed(run, *, now=None, awake=None):
    if run is None or run.started_at is None:return 0
    now = time.time() if now is None else now
    clock = _metadata(run).get(KEY) or {}
    if run.ended_at is not None and 'elapsed' in clock:return clock['elapsed']
    wall = max(0, (run.ended_at if run.ended_at is not None else now) - run.started_at)
    if enabled() and run.ended_at is None and clock.get('boot') == boot_id():
        return max(0, (time.monotonic() if awake is None else awake) - clock['awake_started'])
    return wall


def timing(run):
    if run is None:return None
    active = elapsed(run)
    wall = max(0, (run.ended_at if run.ended_at is not None else time.time()) - (run.started_at or time.time()))
    return {'elapsed_seconds':int(active), 'paused_seconds':int(max(0, wall-active)),
            'sleep_aware':KEY in _metadata(run)}


def _anchor(conn, run):
    if run is None or run.ended_at is not None:return
    metadata = _metadata(run)
    if KEY in metadata:return
    # Legacy live runs retain their already-accounted duration; only future
    # sleep is excluded. Closed/exhausted runs are never rewritten.
    metadata[KEY] = {'boot':boot_id(), 'awake_started':time.monotonic()-max(0, time.time()-run.started_at)}
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET metadata=? WHERE id=? AND ended_at IS NULL',
                     (json.dumps(metadata), run.id))


def _awake_elapsed(conn, task_id, started_at, now):
    from hermes_cli import kanban_db as kb
    run = kb.latest_run(conn, task_id)
    _anchor(conn, run)
    return int(elapsed(kb.latest_run(conn, task_id), now=now)) if run else now-int(started_at)


class ActiveTime:
    """Module-local time facade for Hermes' ephemeral model-stream timers."""
    def __init__(self):
        self.wall = time.time();self.awake = time.monotonic()
    def time(self):return self.wall + time.monotonic() - self.awake
    def __getattr__(self, name):return getattr(time, name)


def install():
    if not enabled():return
    from hermes_cli import kanban_db as kb
    if getattr(kb.enforce_max_runtime, '_research_sleep_aware', False):return
    source = inspect.getsource(kb.enforce_max_runtime)
    old = 'elapsed = now - int(row["active_started_at"])'
    if old not in source:
        raise RuntimeError('Native Kanban timeout changed; review the sleep-clock compatibility adapter.')
    # Preserve native host checks, signals, transitions, retries and events;
    # change ONLY its elapsed expression, never its wall-clock timestamps.
    kb.__dict__['_research_awake_elapsed'] = _awake_elapsed
    source = source.replace(old, 'elapsed = _research_awake_elapsed(conn, row["id"], row["active_started_at"], now)')
    exec(compile(source, '<native-kanban-sleep-clock>', 'exec'), kb.__dict__)
    kb.enforce_max_runtime._research_sleep_aware = True
    original_claim = kb.claim_task;original_end = kb._end_run
    def claim(conn, task_id, **kwargs):
        task = original_claim(conn, task_id, **kwargs)
        if task is not None:_anchor(conn, kb.latest_run(conn, task_id))
        return task
    def end(conn, task_id, **kwargs):
        run = kb.latest_run(conn, task_id)
        if run and run.ended_at is None:
            old_metadata = _metadata(run);clock = old_metadata.get(KEY)
            metadata = {**old_metadata, **(kwargs.get('metadata') or {})}
            if clock:
                metadata[KEY] = {**clock, 'elapsed':elapsed(run)}
            metadata['_worker_pid'] = run.worker_pid
            kwargs['metadata'] = metadata
        return original_end(conn, task_id, **kwargs)
    kb.claim_task = claim;kb._end_run = end
    from agent import chat_completion_helpers, conversation_loop
    active_time=ActiveTime()
    chat_completion_helpers.time = active_time
    # This module's time.time() uses are API durations and retry backoff only;
    # session/message wall timestamps live in other, untouched modules.
    conversation_loop.time = active_time
