#!/usr/bin/env python3
"""Offline first-rollout maintenance; run with Hermes' interpreter on its PVC.

Stop the WebUI writer before --apply. Without --apply this only reports counts.
A unique backup of the database and transcript directory is made before deletion.
"""
import argparse
import os
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = args.home.resolve()
    path = home / "state.db"
    if not path.is_file():
        parser.error(f"database does not exist: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        ids = [row[0] for row in conn.execute("SELECT id FROM sessions WHERE COALESCE(user_id, '') = ''")]
        print(f"Anonymous sessions: {len(ids)}")
        if not args.apply or not ids:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = home / "auth-migration-backups" / stamp
        backup.mkdir(parents=True, exist_ok=False)
        destination = sqlite3.connect(backup / "state.db")
        try:
            conn.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup integrity check failed")
        finally:
            destination.close()
        if (home / "sessions").exists():
            shutil.copytree(home / "sessions", backup / "sessions", symlinks=True)
        print(f"Backup complete: {backup}")
    finally:
        conn.close()
    os.environ["HERMES_HOME"] = str(home)
    from hermes_state import SessionDB
    db = SessionDB(db_path=path)
    try:
        db.delete_sessions(ids, sessions_dir=home / "sessions")
        remaining = db._conn.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(user_id, '') = ''").fetchone()[0]
        if remaining:
            raise RuntimeError(f"{remaining} anonymous sessions remain")
        print(f"Removed {len(ids)} anonymous sessions; authenticated rows were retained")
    finally:
        db.close()


if __name__ == "__main__":
    main()
