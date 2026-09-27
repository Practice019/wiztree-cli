"""Project management — a project is a saved scan snapshot.

The heavy data (the CSV WizTree wrote) stays where it is; the project file is a
small JSON sidecar that records which target was scanned, where the export
lives, aggregate stats, and named export profiles.  That keeps project files
diffable and cheap while the CSV remains the source of truth.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable

from .index import WizTreeEntry, load_entries, parse_export_csv
from .query import summary
from .session import _locked_save_json

SCHEMA_VERSION = 1

#: Export switches a profile may override, mirroring WizTree's own options.
DEFAULT_PROFILE: dict[str, Any] = {
    "filter": None,
    "filterexclude": None,
    "sortby": 0,
    "admin": False,
    "exportfolders": True,
    "exportfiles": True,
    "max_depth": 0,
    "all_dates": False,
    "split_filename": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Project:
    """A saved scan snapshot with named export profiles."""

    def __init__(self, path: str, data: dict[str, Any]):
        self.path = path
        self.data = data

    # ── construction ──────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        path: str,
        root: str,
        source_csv: str | None = None,
        entries: Iterable[WizTreeEntry] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "Project":
        """Create (and write) a new project for *root*.

        Args:
            path: Where the project JSON should live.
            root: The scan target, e.g. ``"C:"`` or ``"D:\\projects"``.
            source_csv: WizTree export this project indexes, if any.
            entries: Parsed entries, used to compute aggregate stats.
            meta: Parser metadata (banner, columns, encoding) for provenance.
        """
        stats = summary(entries) if entries is not None else {}
        data: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "name": os.path.splitext(os.path.basename(path))[0] or "wiztree",
            "root": root,
            "source_csv": source_csv or "",
            "created_at": _now(),
            "updated_at": _now(),
            "stats": stats,
            "meta": meta or {},
            "profiles": {"default": dict(DEFAULT_PROFILE)},
        }
        project = cls(path, data)
        project.save()
        return project

    @classmethod
    def open(cls, path: str) -> "Project":
        """Load a project file.

        The schema is validated **by type**, not merely by key presence:
        ``{"root": "C:", "profiles": "oops"}`` used to load "successfully" and
        then explode later inside the CLI as an ``AttributeError`` with empty
        JSON output on stdout.  A project that cannot be used is rejected here,
        where the error can still name the file.

        Raises:
            FileNotFoundError: the project does not exist.
            ValueError: the file exists but is not a usable project.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Project not found: {path}")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Project file is not valid JSON: {path} ({exc})") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Project file must contain a JSON object: {path}")
        if not isinstance(data.get("root", ""), str) or not data.get("root"):
            raise ValueError(f"Project is missing a string 'root' field: {path}")

        profiles = data.get("profiles")
        if profiles is None:
            profiles = {"default": dict(DEFAULT_PROFILE)}
        if not isinstance(profiles, dict):
            raise ValueError(
                f"Project 'profiles' must be an object, got {type(profiles).__name__}: {path}"
            )
        for name, spec in list(profiles.items()):
            if spec is None:
                profiles[name] = dict(DEFAULT_PROFILE)
            elif not isinstance(spec, dict):
                raise ValueError(f"Project profile {name!r} must be an object: {path}")
        data["profiles"] = profiles

        stats = data.get("stats")
        if stats is not None and not isinstance(stats, dict):
            raise ValueError(
                f"Project 'stats' must be an object, got {type(stats).__name__}: {path}"
            )
        data["stats"] = stats or {}

        source_csv = data.get("source_csv", "")
        if not isinstance(source_csv, str):
            raise ValueError(f"Project 'source_csv' must be a string: {path}")
        return cls(path, data)

    @classmethod
    def open_or_create(cls, path: str, root: str = "") -> "Project":
        """Open *path*, creating an empty project when it does not exist."""
        if os.path.exists(path):
            return cls.open(path)
        return cls.create(path, root=root or os.path.abspath(os.sep))

    # ── persistence ───────────────────────────────────────────────

    def save(self) -> dict[str, Any]:
        """Write the project file and return its data."""
        self.data["updated_at"] = _now()
        _locked_save_json(self.path, self.data, indent=2, ensure_ascii=False, sort_keys=True)
        return self.data

    # ── accessors ─────────────────────────────────────────────────

    def info(self) -> dict[str, Any]:
        """Project summary used by ``project info`` and ``--json`` output."""
        return {
            "name": self.data.get("name", ""),
            "path": self.path,
            "root": self.data.get("root", ""),
            "source_csv": self.data.get("source_csv", ""),
            "created_at": self.data.get("created_at", ""),
            "updated_at": self.data.get("updated_at", ""),
            "stats": self.data.get("stats", {}),
            "profiles": self.list_profiles(),
        }

    def entries(self) -> list[WizTreeEntry]:
        """Load this project's entries from its CSV export.

        Raises:
            FileNotFoundError: the project has no export, or it has vanished.
        """
        csv_path = self.data.get("source_csv") or ""
        if not csv_path:
            raise FileNotFoundError(
                f"Project {self.path} has no scan attached; run `scan run` with --project"
            )
        return load_entries(csv_path)

    def scan_meta(self) -> dict[str, Any]:
        """Parser metadata for this project's export (banner, columns, …)."""
        csv_path = self.data.get("source_csv") or ""
        if not csv_path or not os.path.exists(csv_path):
            return dict(self.data.get("meta", {}))
        return parse_export_csv(csv_path)[1]

    def attach_scan(
        self,
        source_csv: str,
        entries: Iterable[WizTreeEntry] | None = None,
        meta: dict[str, Any] | None = None,
        root: str | None = None,
    ) -> dict[str, Any]:
        """Point this project at a fresh export and refresh its stats."""
        self.data["source_csv"] = source_csv
        if root:
            self.data["root"] = root
        if entries is not None:
            self.data["stats"] = summary(entries)
        if meta is not None:
            self.data["meta"] = meta
        return self.save()

    # ── profiles ──────────────────────────────────────────────────

    def list_profiles(self) -> list[str]:
        """Names of the stored export profiles."""
        return sorted(self.data.get("profiles", {}).keys())

    def get_profile(self, name: str) -> dict[str, Any]:
        """One profile's settings.

        Raises:
            KeyError: no such profile.
        """
        profiles = self.data.get("profiles", {})
        if name not in profiles:
            raise KeyError(f"Profile not found: {name} (have: {', '.join(sorted(profiles))})")
        return profiles[name]

    def add_profile(self, name: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create or replace a named profile (merged over the defaults)."""
        merged = dict(DEFAULT_PROFILE)
        merged.update(spec or {})
        self.data.setdefault("profiles", {})[name] = merged
        self.save()
        return merged

    def remove_profile(self, name: str) -> None:
        """Delete a named profile.

        Raises:
            KeyError: no such profile (``default`` cannot be removed).
        """
        profiles = self.data.get("profiles", {})
        if name not in profiles:
            raise KeyError(f"Profile not found: {name}")
        if name == "default":
            raise KeyError("The 'default' profile cannot be removed")
        del profiles[name]
        self.save()
