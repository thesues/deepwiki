"""Which project each session belongs to — the webui's own record.

hermes' session store has no profile column: a session is a conversation, and
"which project's agent answered it" was, until profiles, a property of the
whole process. With several projects served from one process the mapping has
to live somewhere, and the honest place is a small file the webui owns —
written when a conversation starts (the one moment the profile is known for
certain: the card the reader clicked), read when the sidebar lists sessions,
cleared when the session is deleted.

JSON, not sqlite: the data is a handful of string→string pairs, written once
per conversation start, read on every sidebar poll. A second database would
buy nothing and cost a migration path this does not have yet. The file sits
in HERMES_HOME next to config.yaml — the same "operator-owned state" place
the rest of the deployment uses.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("deepwiki.session_profiles")


class SessionProfiles:
    """session_id → profile.key, persisted, process-lifetime cached."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
            path = home / "session_profiles.json"
        self._path = path
        self._lock = threading.Lock()
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        try:
            raw = json.loads(self._path.read_text())
            self._cache = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except FileNotFoundError:
            self._cache = {}
        except Exception:  # noqa: BLE001 — an unreadable file must not kill the sidebar
            log.exception("could not read %s; assuming empty", self._path)
            self._cache = {}
        return self._cache

    def _save(self, mapping: dict[str, str]) -> None:
        # Write-then-rename, the same discipline hermes_config._write uses:
        # the sidebar reads this file on every poll, and a half-written JSON
        # would be read as "no sessions have profiles".
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(mapping, f)
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def record(self, session_id: str, profile_key: str) -> None:
        """Pin a conversation to the project it was opened under."""
        if not session_id or not profile_key:
            return
        with self._lock:
            m = self._load()
            if m.get(session_id) == profile_key:
                return
            m[session_id] = profile_key
            try:
                self._save(m)
            except Exception:  # noqa: BLE001 — a failed write degrades to in-memory only
                log.exception("could not persist session profile for %s", session_id)

    def get(self, session_id: str) -> str | None:
        with self._lock:
            return self._load().get(session_id)

    def forget(self, session_id: str) -> None:
        """The conversation is gone; its row in the mapping should go too."""
        if not session_id:
            return
        with self._lock:
            m = self._load()
            if session_id not in m:
                return
            del m[session_id]
            try:
                self._save(m)
            except Exception:  # noqa: BLE001
                log.exception("could not persist removal of %s", session_id)
