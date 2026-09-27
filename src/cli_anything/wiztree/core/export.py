"""Rendering and artifact verification.

Two jobs live here:

* :func:`render` turns parsed entries into a report (CSV / JSON / table /
  Markdown / HTML) that a human reads or an agent parses.
* :func:`verify_file` proves an artifact WizTree claims to have written really
  is what it should be.  This matters because **WizTree's process exit code is
  0 even when it produces nothing** — verified on the test machine with a
  non-existent target — so no caller may trust the exit status alone.
"""

from __future__ import annotations

import csv
import io
import json
import os
from typing import Any, Iterable, Sequence

from .index import WizTreeEntry
from .query import human_size

#: Default report columns, in output order.
DEFAULT_COLUMNS: tuple[str, ...] = ("name", "size", "allocated", "modified", "is_folder")

#: Named export profiles.  The ``wiztree`` sub-dict maps straight onto
#: :func:`cli_anything.wiztree.utils.wiztree_backend.build_export_args`.
EXPORT_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "description": "Everything, sorted by name — the raw WizTree view.",
        "columns": list(DEFAULT_COLUMNS),
        "wiztree": {"filter": None, "sortby": 0, "exportfolders": True, "exportfiles": True, "admin": False},
    },
    "large-files": {
        "description": "Files only, biggest first — for space reclamation triage.",
        "columns": ["name", "size", "allocated", "modified"],
        "wiztree": {"filter": None, "sortby": 1, "exportfolders": False, "exportfiles": True, "admin": False},
    },
    "media": {
        "description": "Audio/video/image files only.",
        "columns": ["name", "size", "allocated", "modified"],
        "wiztree": {
            "filter": "*.mp3|*.wav|*.flac|*.mp4|*.mkv|*.mov|*.jpg|*.jpeg|*.png|*.raw",
            "sortby": 1,
            "exportfolders": False,
            "exportfiles": True,
            "admin": False,
        },
    },
    "cleanup": {
        "description": "Deep index for duplicate hunting: split names, all dates, sizes.",
        "columns": ["name", "size", "allocated", "modified", "is_folder"],
        "wiztree": {
            "filter": None,
            "sortby": 1,
            "exportfolders": True,
            "exportfiles": True,
            "admin": False,
            "split_filename": True,
            "all_dates": True,
            "all_sizes": True,
        },
    },
    "folders-only": {
        "description": "Directory sizes only — fast structural overview.",
        "columns": ["name", "size", "allocated", "files", "folders"],
        "wiztree": {"filter": None, "sortby": 2, "exportfolders": True, "exportfiles": False, "admin": False},
    },
}

_ALIGN_RIGHT = {"size", "allocated", "attributes", "files", "folders"}


def _select(entries: Iterable[WizTreeEntry] | None, top: int | None) -> list[WizTreeEntry]:
    items = [e for e in (entries or []) if e is not None]
    if top is not None:
        items = items[: max(0, top)]
    return items


def _cell(entry: WizTreeEntry, column: str) -> Any:
    """Read one output column from an entry."""
    if column == "size_human":
        return human_size(entry.size)
    if column == "basename":
        return entry.basename
    value = getattr(entry, column, "")
    if isinstance(value, bool):
        return "folder" if value else "file"
    return value


def render(
    entries: Iterable[WizTreeEntry] | None,
    fmt: str = "csv",
    columns: Sequence[str] | None = None,
    top: int | None = None,
) -> str:
    """Render entries as a report.

    Args:
        entries: Parsed WizTree entries.
        fmt: ``csv``, ``json``, ``table``, ``markdown`` or ``html``.
        columns: Column names to include (defaults to :data:`DEFAULT_COLUMNS`).
        top: Keep only the first *top* entries.

    Raises:
        ValueError: unknown format.
    """
    items = _select(entries, top)
    cols = list(columns or DEFAULT_COLUMNS)
    fmt = (fmt or "csv").lower()

    if fmt == "json":
        return json.dumps(
            [{c: _cell(e, c) for c in cols} for e in items], indent=2, ensure_ascii=False, default=str
        )

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(cols)
        for entry in items:
            writer.writerow([_cell(entry, c) for c in cols])
        return buffer.getvalue()

    if fmt == "markdown":
        lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
        for entry in items:
            lines.append("| " + " | ".join(str(_cell(entry, c)) for c in cols) + " |")
        return "\n".join(lines) + "\n"

    if fmt == "html":
        head = "".join(f"<th>{c}</th>" for c in cols)
        body = "".join(
            "<tr>" + "".join(f"<td>{_cell(e, c)}</td>" for c in cols) + "</tr>" for e in items
        )
        return f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>{body}</tbody>\n</table>\n"

    if fmt == "table":
        rows = [[str(_cell(entry, c)) for c in cols] for entry in items]
        widths = [
            max([len(cols[i].upper())] + [len(r[i]) for r in rows]) if rows else len(cols[i].upper())
            for i in range(len(cols))
        ]

        def line(cells: Sequence[str]) -> str:
            parts = []
            for i, cell in enumerate(cells):
                parts.append(cell.rjust(widths[i]) if cols[i] in _ALIGN_RIGHT else cell.ljust(widths[i]))
            return "  ".join(parts).rstrip()

        out = [line([c.upper() for c in cols]), "  ".join("-" * w for w in widths)]
        out.extend(line(r) for r in rows)
        return "\n".join(out) + "\n"

    raise ValueError(f"Unknown render format: {fmt!r} (use csv, json, table, markdown or html)")


def write_export(
    entries: Iterable[WizTreeEntry] | None,
    path: str,
    fmt: str | None = None,
    columns: Sequence[str] | None = None,
    top: int | None = None,
) -> dict[str, Any]:
    """Render entries and write them to *path*.

    The format defaults to the file extension.  Returns a result dict with
    ``path``, ``size``, ``format`` and ``rows``.
    """
    resolved = (fmt or os.path.splitext(path)[1].lstrip(".") or "csv").lower()
    if resolved == "md":
        resolved = "markdown"
    text = render(entries, fmt=resolved, columns=columns, top=top)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return {
        "path": path,
        "size": os.path.getsize(path),
        "format": resolved,
        "rows": len(_select(entries, top)),
    }


def verify_file(path: str) -> dict[str, Any]:
    """Check that an artifact exists, is non-empty and matches its extension.

    Returns ``{"valid", "format", "file_size", "details"}``.
    """
    if not os.path.exists(path):
        return {"valid": False, "format": "missing", "file_size": 0, "details": "File not found"}
    size = os.path.getsize(path)
    if size == 0:
        return {"valid": False, "format": "empty", "file_size": 0, "details": "Zero-byte file"}
    with open(path, "rb") as handle:
        head = handle.read(4096)
    suffix = os.path.splitext(path)[1].lower()

    if suffix == ".png":
        ok = head.startswith(b"\x89PNG\r\n\x1a\n")
        return {
            "valid": ok,
            "format": "PNG",
            "file_size": size,
            "details": "Valid PNG" if ok else "Bad PNG signature",
        }

    if suffix == ".mft":
        ok = head.startswith(b"FILE")
        return {
            "valid": ok,
            "format": "MFT",
            "file_size": size,
            "details": "NTFS MFT record stream" if ok else "Missing FILE record signature",
        }

    if suffix == ".csv":
        if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
            return {
                "valid": False,
                "format": "binary",
                "file_size": size,
                "details": "Contains NUL bytes — not a CSV text export",
            }
        text = ""
        for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp936", "latin-1"):
            try:
                text = head.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        non_blank = [ln for ln in text.splitlines() if ln.strip()]
        # A real export is banner + header + at least one data row.  Certifying a
        # banner+header-only file used to make a WizTree silent no-op (e.g. a
        # filter that matched nothing) indistinguishable from a real scan.
        data_rows = 0
        for line in non_blank[1:]:
            cells = next(csv.reader([line]), [])
            if len(cells) >= 2 and cells[1].strip().strip('"').lstrip("-").isdigit():
                data_rows += 1
        ok = data_rows >= 1
        return {
            "valid": ok,
            "format": "CSV",
            "file_size": size,
            "data_rows": data_rows,
            "details": (
                f"banner + header + {data_rows} data row(s)"
                if ok
                else f"no data rows ({len(non_blank)} non-blank line(s): banner/header only)"
            ),
        }

    return {
        "valid": True,
        "format": suffix.lstrip(".") or "unknown",
        "file_size": size,
        "details": f"File exists ({size:,} bytes); format not verified",
    }
