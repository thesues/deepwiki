"""Portable per-command timeout executable, NOT a research scheduler.

The native terminal registry owns this subprocess. Its one payload/deadline
survives the spawning Worker's exit; no task state, process table or DB is added.
An exec gate captures the payload's native birth identity BEFORE command code
runs. Only that owned identity is passed to Hermes' native tree killer. Worker
PID/birth, explicitly supplied by the spawner (never inferred from PPID), is a
READ-ONLY liveness identity: owner exit/reuse promptly stops the owned payload.
The configured command deadline remains independent of task/run budgets.
Owner checks use the existing <=0.1s wait loop, plus native termination grace;
no task/DB polling or parent signals. External SIGKILL of this executable and
unobserved self-daemonization remain outside portable cleanup guarantees.
"""
import argparse
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

# Bounded SIGTERM grace before native SIGKILL escalation; independent of any
# task/run budget. Native grace=0 would otherwise allow TERM-ignoring payloads.
TERMINATION_GRACE_SECONDS = 0.25


def wrap_command(command, seconds, shell, shell_flags='-lic', *, owner_pid=None, owner_birth=None):
    """Return (command, receipt); Worker launches must supply owner PID/birth.

    No owner is supported only for standalone timeout use/tests. Worker hooks
    always pass their OWN immutable native process identity explicitly.
    """
    if isinstance(seconds, bool) or not math.isfinite(float(seconds)) or seconds <= 0:
        raise ValueError('command timeout must be positive finite seconds')
    if (owner_pid is None) != (owner_birth is None):
        raise ValueError('owner PID and birth identity must be supplied together')
    if owner_pid is not None and (isinstance(owner_pid, bool) or not isinstance(owner_pid, int)
                                  or owner_pid <= 0 or isinstance(owner_birth, bool)
                                  or not isinstance(owner_birth, int)):
        raise ValueError('owner must have a positive PID and native integer birth identity')
    token = uuid.uuid4().hex
    argv = [sys.executable, '-B', '-u', str(Path(__file__).resolve()),
            '--seconds=' + str(seconds), '--shell=' + shell,
            '--shell-flags=' + shell_flags, '--token=' + token,
            '--command=' + command]
    if owner_pid is not None:
        argv.extend(['--owner-pid=' + str(owner_pid), '--owner-birth=' + str(owner_birth)])
    # Exec keeps the native registry PID/birth identity on the timeout process.
    return 'exec ' + shlex.join(argv), token


def _native_termination():
    # Hermes 0.19's process_registry module constructs a singleton that replays
    # delegation DB records at import. This executable needs ONLY its stateless
    # identity/tree-kill methods; the Worker remains the sole ledger owner.
    # Suppress import-time replay in THIS fresh, single-threaded process. Never
    # spawn/register a native session here or read/create another state.db.
    from tools import async_delegation
    restore = async_delegation.restore_undelivered_completions
    async_delegation.restore_undelivered_completions = lambda queue: None
    try:
        from tools.process_registry import ProcessRegistry
    finally:
        async_delegation.restore_undelivered_completions = restore
    ProcessRegistry._daemon_term_grace_seconds = staticmethod(lambda: TERMINATION_GRACE_SECONDS)
    return ProcessRegistry


def _emit_timeout(token):
    # Worker stdout/PTY may disappear or stop being drained. A receipt must
    # NEVER block timeout enforcement/exit or make the payload depend on it.
    try:
        os.set_blocking(1, False)
        os.write(1, ('\n[research-command-timeout:' + token + ']\n').encode())
    except (OSError, ValueError):
        pass


def _payload(args):
    # If the timeout process dies before identity capture, EOF closes this gate
    # and no command executes. No model, registry or DB imports on this path.
    try:
        allowed = os.read(args.gate_fd, 1) == b'1'
    finally:
        os.close(args.gate_fd)
    if not allowed:
        return 125
    os.execv(args.shell, [args.shell, args.shell_flags, 'set +m; ' + args.command])


def _run(args):
    if os.name != 'posix':
        raise RuntimeError('bounded native commands require macOS/Linux')
    native = _native_termination()
    import psutil
    if native._safe_host_start_time(os.getpid()) is None:
        raise RuntimeError('host birth-time probing unavailable; command was not run')
    psutil.Process(os.getpid()).children(recursive=True)
    owner_pid = getattr(args, 'owner_pid', None)
    owner_birth = getattr(args, 'owner_birth', None)
    if (owner_pid is None) != (owner_birth is None):
        raise RuntimeError('owner requires both PID and native birth identity; command was not run')
    def owner_alive():
        # Native predicate excludes zombies and verifies kernel birth identity.
        # This is a liveness read ONLY; owner is never a termination target.
        return owner_pid is None or native._host_pid_is_ours(owner_pid, owner_birth)
    if not owner_alive():return 125  # owner ended during imports: no payload launch
    stopped = []
    def stop(signum, frame):
        if not stopped:
            stopped.append(signum)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    read_fd, write_fd = os.pipe()
    proc = None
    birth = None
    released = False
    try:
        argv = [sys.executable, '-B', '-u', str(Path(__file__).resolve()), '--payload',
                '--gate-fd=' + str(read_fd), '--shell=' + args.shell,
                '--shell-flags=' + args.shell_flags, '--command=' + args.command]
        # Inherit stdio, so native streaming/PTY interaction remains intact.
        # The payload gets its own group but stays a descendant of the native
        # registry-owned wrapper; native tree kill can still reach both.
        proc = subprocess.Popen(argv, pass_fds=(read_fd,), start_new_session=True)
        os.close(read_fd)
        read_fd = None
        birth = native._safe_host_start_time(proc.pid)
        if birth is None or not native._host_pid_is_ours(proc.pid, birth) or stopped or not owner_alive():
            # Closing the still-blocked gate causes a clean exit, without
            # ever signalling an unknown PID or executing the command.
            return 125
        deadline = time.monotonic() + args.seconds
        os.write(write_fd, b'1')
        released = True
        os.close(write_fd)
        write_fd = None
        owner_ended = False
        while not stopped:
            if not owner_alive():
                owner_ended = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                code = proc.wait(timeout=min(0.1, remaining))
                return code if code >= 0 else 128 - code
            except subprocess.TimeoutExpired:
                pass
        # The registry's kernel birth-time check is repeated INSIDE the native
        # tree killer; neither a bare PID nor an existing session is adopted.
        native._terminate_host_pid(proc.pid, expected_start=birth)
        proc.wait(timeout=2)
        if stopped:
            return 128 + stopped[0]
        if owner_ended:
            return 128 + signal.SIGTERM
        _emit_timeout(args.token)
        return 124
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if proc is not None:
            if released and proc.poll() is None and birth is not None:
                native._terminate_host_pid(proc.pid, expected_start=birth)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Never signal a PID whose spawn identity cannot be proven.
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--shell', required=True)
    parser.add_argument('--shell-flags', default='-lic')
    parser.add_argument('--command', required=True)
    parser.add_argument('--token', default='')
    parser.add_argument('--owner-pid', type=int)
    parser.add_argument('--owner-birth', type=int)
    parser.add_argument('--payload', action='store_true')
    parser.add_argument('--gate-fd', type=int)
    args = parser.parse_args()
    if args.payload:
        if args.gate_fd is None:
            parser.error('payload requires exec gate')
        return _payload(args)
    if args.seconds is None or not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('seconds must be positive finite')
    if (args.owner_pid is None) != (args.owner_birth is None) or (args.owner_pid is not None and args.owner_pid <= 0):
        parser.error('owner requires a positive PID and its native birth identity')
    return _run(args)


if __name__ == '__main__':
    sys.exit(main())
