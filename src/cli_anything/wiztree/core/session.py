"""Session management — command history, undo/redo and snapshots.

History is persisted so that a scan run in one invocation is still visible to
the next one.  Writes go through :func:`_locked_save_json`, which takes an
exclusive lock on the file so two concurrent CLI invocations cannot interleave
a read-modify-write and corrupt the history.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _acquire_lock(handle) -> str:
    """Take an exclusive advisory lock on *handle*; return the mechanism used.

    Falls back to ``"none"`` when the platform refuses (e.g. a filesystem
    without locking support) — a missing lock is better than a failed save.
    """
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return "msvcrt"
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return "fcntl"
    except (ImportError, OSError):
        return "none"


def _release_lock(handle, mechanism: str) -> None:
    """Release a lock previously taken by :func:`_acquire_lock`."""
    try:
        if mechanism == "msvcrt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif mechanism == "fcntl":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (ImportError, OSError):
        pass


def _locked_save_json(path: str, data: Any, **dump_kwargs) -> None:
    """Atomically write JSON with exclusive file locking."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    try:
        handle = open(path, "r+", encoding="utf-8")
    except FileNotFoundError:
        handle = open(path, "w", encoding="utf-8")
    with handle:
        mechanism = _acquire_lock(handle)
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle, **dump_kwargs)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            _release_lock(handle, mechanism)


@dataclass
class HistoryEntry:
    """One recorded CLI operation."""

    command: str
    args: dict
    timestamp: str = ""
    result: dict | None = None

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Serializable view."""
        return {
            "command": self.command,
            "args": self.args,
            "timestamp": self.timestamp,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        """Rebuild from :meth:`to_dict` output."""
        return cls(
            command=data["command"],
            args=data.get("args", {}),
            timestamp=data.get("timestamp", ""),
            result=data.get("result"),
        )


class Session:
    """Command history with undo/redo, optionally persisted to disk."""

    def __init__(self, session_file: str | None = None):
        self._history: list[HistoryEntry] = []
        self._redo_stack: list[HistoryEntry] = []
        self._session_file = session_file
        if session_file:
            self._load(session_file)

    def record(self, command: str, args: dict, result: dict | None = None) -> HistoryEntry:
        """Append an operation, invalidating any redo stack."""
        entry = HistoryEntry(command=command, args=args, result=result)
        self._history.append(entry)
        self._redo_stack.clear()
        self._auto_save()
        return entry

    def undo(self) -> HistoryEntry | None:
        """Move the last operation onto the redo stack."""
        if not self._history:
            return None
        entry = self._history.pop()
        self._redo_stack.append(entry)
        self._auto_save()
        return entry

    def redo(self) -> HistoryEntry | None:
        """Re-apply the most recently undone operation."""
        if not self._redo_stack:
            return None
        entry = self._redo_stack.pop()
        self._history.append(entry)
        self._auto_save()
        return entry

    def history(self, limit: int = 20) -> list[dict]:
        """Most recent *limit* operations (``0`` means all)."""
        entries = self._history[-limit:] if limit else self._history
        return [e.to_dict() for e in entries]

    def snapshot(self) -> dict:
        """Capture the full session state as a JSON-safe dict."""
        return {
            "history": [e.to_dict() for e in self._history],
            "redo_stack": [e.to_dict() for e in self._redo_stack],
        }

    def restore(self, snapshot: dict) -> None:
        """Replace session state with a :meth:`snapshot` result."""
        self._history = [HistoryEntry.from_dict(e) for e in snapshot.get("history", [])]
        self._redo_stack = [HistoryEntry.from_dict(e) for e in snapshot.get("redo_stack", [])]
        self._auto_save()

    @property
    def history_count(self) -> int:
        """Number of recorded operations."""
        return len(self._history)

    @property
    def can_undo(self) -> bool:
        """True when :meth:`undo` would do something."""
        return len(self._history) > 0

    @property
    def can_redo(self) -> bool:
        """True when :meth:`redo` would do something."""
        return len(self._redo_stack) > 0

    def status(self) -> dict:
        """Summary used by ``session status``."""
        return {
            "history_count": self.history_count,
            "can_undo": self.can_undo,
            "can_redo": self.can_redo,
            "redo_count": len(self._redo_stack),
            "session_file": self._session_file,
        }

    def _auto_save(self) -> None:
        if self._session_file:
            self.save(self._session_file)

    def save(self, path: str) -> None:
        """Persist session state to *path*."""
        _locked_save_json(path, self.snapshot(), indent=2, sort_keys=True, default=str)

    def _load(self, path: str) -> None:
        file_path = Path(path)
        if not file_path.exists():
            return
        try:
            with open(file_path, encoding="utf-8") as handle:
                data = json.load(handle)
            self._history = [HistoryEntry.from_dict(e) for e in data.get("history", [])]
            self._redo_stack = [HistoryEntry.from_dict(e) for e in data.get("redo_stack", [])]
        except (json.JSONDecodeError, IOError, KeyError, TypeError):
            # A corrupt history file must never make the CLI unusable.
            self._history = []
            self._redo_stack = []
