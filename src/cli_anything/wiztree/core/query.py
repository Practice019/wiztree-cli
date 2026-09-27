"""Query layer over a parsed WizTree export.

Everything here is pure: it takes a list of
:class:`~cli_anything.wiztree.core.index.WizTreeEntry` and returns plain dicts
and lists, so the whole query surface is unit-testable with synthetic data.
"""

from __future__ import annotations

import fnmatch
import math
from typing import Any, Iterable, Sequence

from .index import WizTreeEntry

_SIZE_UNITS = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}

#: 16 EiB — beyond this nothing is a real filesystem, so it is a typo, not a size.
_MAX_BYTES = 2 ** 64


def parse_size(text: str | int | None) -> int | None:
    """Parse ``"10"``, ``"500k"``, ``"1.5g"`` into bytes.

    Returns ``None`` for empty input; raises ``ValueError`` for junk.  Non-finite
    input (``inf``, ``nan``, ``1e999999``) is rejected as ``ValueError`` rather
    than escaping as ``OverflowError``: a bad size string is user input, not a
    crash.  Integer input without a fraction keeps full precision.
    """
    if text is None or text == "":
        return None
    if isinstance(text, bool):
        raise ValueError(f"Cannot parse size {text!r} (use 10, 500k, 1.5g …)")
    if isinstance(text, int):
        return text
    raw = str(text).strip().lower().replace("_", "").replace(",", "")
    if not raw:
        return None
    multiplier = 1
    if raw[-1] in _SIZE_UNITS:
        multiplier = _SIZE_UNITS[raw[-1]]
        raw = raw[:-1]
    if multiplier == 1 and raw.isdigit():
        return int(raw)  # exact, no float round-trip
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"Cannot parse size {text!r} (use 10, 500k, 1.5g …)") from exc
    if not math.isfinite(number):
        raise ValueError(f"Cannot parse size {text!r}: not a finite number")
    value = number * multiplier
    if value > _MAX_BYTES:
        raise ValueError(f"Cannot parse size {text!r}: larger than any real filesystem")
    return int(value)


def human_size(num_bytes: int | float) -> str:
    """Format a byte count the way WizTree's GUI does."""
    value = float(num_bytes or 0)
    for unit in ("Bytes", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            if unit == "Bytes":
                return f"{int(value)} Bytes"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _clean(entries: Iterable[WizTreeEntry] | None) -> list[WizTreeEntry]:
    """Drop ``None`` members and tolerate ``None`` input."""
    if not entries:
        return []
    return [e for e in entries if e is not None]


def summary(entries: Iterable[WizTreeEntry] | None) -> dict[str, Any]:
    """Aggregate statistics over an export.

    ``total_file_size`` counts files only — folder rows already contain their
    subtree totals, so including them would double count.
    """
    items = _clean(entries)
    files = [e for e in items if not e.is_folder]
    folders = [e for e in items if e.is_folder]
    total_size = sum(e.size for e in files)
    total_allocated = sum(e.allocated for e in files)

    top_ext = ""
    if files:
        breakdown = ext_breakdown(files, n=1)
        if breakdown:
            top_ext = breakdown[0]["ext"]

    largest = max(files, key=lambda e: e.size, default=None)
    roots = [e.to_dict() for e in items if e.name.count("\\") + e.name.count("/") <= 2][:10]

    return {
        "entry_count": len(items),
        "file_count": len(files),
        "folder_count": len(folders),
        "total_file_size": total_size,
        "total_file_size_human": human_size(total_size),
        "total_allocated": total_allocated,
        "top_extension": top_ext,
        "largest_file": largest.to_dict() if largest else None,
        "roots": roots,
    }


def top_entries(
    entries: Iterable[WizTreeEntry] | None,
    n: int = 20,
    *,
    by: str = "size",
    files_only: bool = False,
    folders_only: bool = False,
) -> list[WizTreeEntry]:
    """Return the *n* largest entries, biggest first.

    Args:
        by: ``"size"`` (logical) or ``"allocated"`` (on-disk).
        files_only: keep files only.
        folders_only: keep folders only.
    """
    if by not in ("size", "allocated"):
        raise ValueError("by must be 'size' or 'allocated'")
    items = _clean(entries)
    if files_only:
        items = [e for e in items if not e.is_folder]
    if folders_only:
        items = [e for e in items if e.is_folder]
    key = (lambda e: getattr(e, by))
    items.sort(key=lambda e: (-key(e), e.name))
    return items[: max(0, n)]


def find_entries(
    entries: Iterable[WizTreeEntry] | None,
    pattern: str = "*",
    *,
    ext: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    files_only: bool | None = None,
    folders_only: bool | None = None,
    limit: int = 100,
) -> list[WizTreeEntry]:
    """Filter entries by glob, extension and size range.

    ``pattern`` matches the full path, so ``"*.mp3"`` catches every audio file
    at any depth, while ``"C:\\\\Users*"`` restricts to a subtree.
    """
    glob = pattern or "*"
    wanted_ext = (ext or "").lstrip(".").lower()
    hits: list[WizTreeEntry] = []
    for entry in _clean(entries):
        if not fnmatch.fnmatch(entry.name, glob):
            continue
        if wanted_ext and entry.ext != wanted_ext:
            continue
        if min_size is not None and entry.size < min_size:
            continue
        if max_size is not None and entry.size > max_size:
            continue
        if files_only and entry.is_folder:
            continue
        if folders_only and not entry.is_folder:
            continue
        hits.append(entry)
        if len(hits) >= limit:
            break
    return hits


def duplicates(
    entries: Iterable[WizTreeEntry] | None,
    *,
    min_size: int = 1,
    by: str = "size",
) -> list[list[WizTreeEntry]]:
    """Group files sharing a base name **and** a size.

    This mirrors WizTree's own duplicate detector: it compares name plus size,
    never file contents, so results are candidates for review, not proof.
    """
    if by not in ("size", "allocated"):
        raise ValueError("by must be 'size' or 'allocated'")
    groups: dict[tuple[str, int], list[WizTreeEntry]] = {}
    for entry in _clean(entries):
        if entry.is_folder:
            continue
        key_value = getattr(entry, by)
        if key_value < min_size:
            continue
        groups.setdefault((entry.basename.lower(), key_value), []).append(entry)
    result = [group for group in groups.values() if len(group) > 1]
    result.sort(key=lambda group: (-getattr(group[0], by), group[0].basename.lower()))
    return result


def duplicates_wasted(groups: Sequence[Sequence[WizTreeEntry]], by: str = "size") -> int:
    """Bytes recoverable if every copy but the first were deleted."""
    total = 0
    for group in groups:
        if len(group) < 2:
            continue
        total += getattr(group[0], by) * (len(group) - 1)
    return total


def ext_breakdown(
    entries: Iterable[WizTreeEntry] | None,
    n: int = 20,
) -> list[dict[str, Any]]:
    """Files grouped by extension, biggest total size first."""
    buckets: dict[str, dict[str, Any]] = {}
    for entry in _clean(entries):
        if entry.is_folder:
            continue
        key = entry.ext or "(none)"
        bucket = buckets.setdefault(key, {"ext": key, "count": 0, "size": 0, "allocated": 0})
        bucket["count"] += 1
        bucket["size"] += entry.size
        bucket["allocated"] += entry.allocated
    rows = sorted(buckets.values(), key=lambda r: (-r["size"], r["ext"]))
    for row in rows:
        row["size_human"] = human_size(row["size"])
    return rows[: max(0, n)]


def depth_of(entry: WizTreeEntry) -> int:
    """Approximate path depth of an entry (used for tree-style output)."""
    return entry.name.rstrip("\\/").count("\\") + entry.name.rstrip("\\/").count("/")
