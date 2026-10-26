"""Compatibility for the installed Hermes 0.19 native Kanban SQLite guard.

POSIX close() of ANY descriptor for a database inode releases that process's
record locks, even if SQLite still owns a connection to the file. The upstream
post-commit guard opens/closes the main DB with Python open(), which can make
another process treat it as the last reader and remove live WAL/SHM files.
Use SQLite's own check through the existing connection instead. No scheduler,
transaction or database schema is replaced; remove when upstream fixes it.
"""
import inspect
import os
import sqlite3
from pathlib import Path
import sys


def install():
    from .native_clock import install as install_clock
    install_clock()
    from hermes_cli import kanban_db as kb
    guard = kb._check_file_length_invariant
    if os.name != 'posix' or getattr(guard, '_hermes_lock_safe', False):
        return
    if 'with open(path, "rb")' not in inspect.getsource(guard):
        return  # Do not override a newer, already fixed implementation.

    def check(conn):
        rows = conn.execute('PRAGMA quick_check(1)').fetchall()
        problems = [str(row[0]) for row in rows if str(row[0]).lower() != 'ok']
        if problems:
            raise sqlite3.DatabaseError('Kanban quick_check: ' + '; '.join(problems))

    check._hermes_lock_safe = True
    kb._check_file_length_invariant = check


def worker_launcher(home):
    """Native HERMES_BIN shim: apply the same fix before entering Hermes CLI."""
    launcher = Path(home) / 'bin' / 'hermes-research'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        '#!' + sys.executable + '\n'
        'import runpy, sys\n'
        'sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent.parent)) + ')\n'
        'from complex.native_compat import install\n'
        'install()\n'
        'from complex.worker_runtime import install as install_worker\n'
        'install_worker()\n'
        'runpy.run_module("hermes_cli.main", run_name="__main__")\n'
    )
    launcher.chmod(0o700)
    return str(launcher)
