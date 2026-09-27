"""Locale-agnostic reader for WizTree command-line CSV exports.

WizTree writes an export as::

    生成由 WizTree 4.27 2026/9/27 14:05:24 (您可以通过捐赠隐藏此信息)
    文件名称,大小,分配,修改时间,属性,文件,文件夹,DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE
    "D:\\sample\\",20702725,20844544,2026/09/27 11:53:56,0,65,57,364242792448,...

Two properties of that format drive the whole design of this module:

1. **The header row is localized** to whatever language WizTree's UI is in
   (the sample above is a Chinese system).  Column *names* are therefore
   useless as keys; every field is read by **position**.
2. **The column count is not stable.**  Unless the advanced export switches are
   pinned (see :func:`cli_anything.wiztree.utils.wiztree_backend.build_export_args`),
   WizTree appends ``DRIVECAPACITY,FREESPACE,USEDSPACE,RESERVEDSPACE`` to the
   header and to the first record only, so the file is *ragged* and
   :class:`csv.DictReader` cannot be used at all.

The first seven columns are always the same, regardless of language and of the
optional extras, which is what :data:`POSITIONAL_COLUMNS` encodes.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Column order shared by every WizTree export variant (after the banner).
POSITIONAL_COLUMNS: tuple[str, ...] = (
    "name",
    "size",
    "allocated",
    "modified",
    "attributes",
    "files",
    "folders",
)

#: Column order of the ``/exportfiletypes`` report.
FILETYPE_COLUMNS: tuple[str, ...] = (
    "ext",
    "type",
    "percent",
    "size",
    "allocated",
    "files",
)

#: Tried in order; WizTree writes UTF-8 with a BOM on a UTF-8 system and the
#: system code page on others (cp936 seen on the Chinese test machine).  The
#: UTF-16 codecs are checked before ``latin-1`` because ``latin-1`` never fails
#: and would otherwise turn a UTF-16 file into silent mojibake.
ENCODINGS: tuple[str, ...] = (
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "cp936",
    "gbk",
    "big5",
    "latin-1",
)

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")


def _is_int(value: str) -> bool:
    """True when *value* looks like a plain integer field."""
    return bool(_INT_RE.match(str(value or "").strip()))


def _is_float(value: str) -> bool:
    """True when *value* looks like a plain float field."""
    return bool(_FLOAT_RE.match(str(value or "").strip()))


def _to_int(value: Any, default: int = 0) -> int:
    """Coerce a CSV field (or an already-numeric value) to int.

    Accepts ints/floats directly because entries are also built from JSON
    round-trips, not only from CSV text.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip().strip('"')
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return default


def _decode(raw: bytes) -> tuple[str, str, bool]:
    """Decode export bytes, returning ``(text, encoding_used, is_fallback)``.

    ``latin-1`` accepts *any* byte sequence, so a mojibake decode can never fail.
    It is therefore tried last and flagged, letting callers surface "this file
    decoded by fallback" instead of silently returning zero rows.
    """
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1", True


@dataclass
class WizTreeEntry:
    """One row of a WizTree export — a file or a folder.

    Folders are marked by a trailing separator in :attr:`name` (``C:\\Users\\``
    / ``/system/``); for folders ``size`` and ``allocated`` are the totals of
    everything beneath them, and ``files``/``folders`` are child counts.
    """

    name: str
    size: int = 0
    allocated: int = 0
    modified: str = ""
    attributes: int = 0
    files: int = 0
    folders: int = 0
    extra: dict[str, str] = field(default_factory=dict)
    #: Verbatim allocated-size field, kept because a leading ``0`` marks a
    #: hard link and int coercion would destroy that signal.
    allocated_raw: str = ""

    def __post_init__(self) -> None:
        self.name = str(self.name or "")
        self.size = _to_int(self.size)
        self.allocated = _to_int(self.allocated)
        self.attributes = _to_int(self.attributes)
        self.files = _to_int(self.files)
        self.folders = _to_int(self.folders)
        self.modified = str(self.modified or "")
        self.allocated_raw = str(self.allocated_raw or "")

    @property
    def is_folder(self) -> bool:
        """True when the row describes a folder (trailing separator)."""
        return self.name.endswith("\\") or self.name.endswith("/")

    @property
    def basename(self) -> str:
        """The last path component, without a trailing separator."""
        return self.name.rstrip("\\/").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        """Lower-case extension without the dot; empty for folders/no extension."""
        if self.is_folder:
            return ""
        base = self.basename
        if "." not in base:
            return ""
        return base.rsplit(".", 1)[-1].lower()

    @property
    def is_hardlink(self) -> bool:
        """WizTree marks hard links with a leading ``0`` on the allocated size."""
        return self.allocated_raw.startswith("0") and self.allocated_raw != "0"

    def to_dict(self) -> dict[str, Any]:
        """Serializable view used by ``--json`` output."""
        return {
            "name": self.name,
            "size": self.size,
            "allocated": self.allocated,
            "modified": self.modified,
            "attributes": self.attributes,
            "files": self.files,
            "folders": self.folders,
            "is_folder": self.is_folder,
            "ext": self.ext,
        }


def _read_rows(path: str) -> tuple[list[list[str]], str, bool]:
    """Read *path* into CSV rows plus the encoding used and whether it was a fallback."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Export file not found: {path}")
    with open(path, "rb") as handle:
        raw = handle.read()
    text, encoding, is_fallback = _decode(raw)
    # newline="" keeps embedded newlines inside quoted fields intact
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    return rows, encoding, is_fallback


def _split_banner(rows: list[list[str]]) -> tuple[str, list[list[str]]]:
    """Strip the banner line WizTree writes first.

    Normally the banner is a single unquoted cell (``生成由 WizTree 4.27 …``), but
    a localised banner or an exporter change could contain commas.  Adopting
    such a row as the header would produce garbage column names, so a row is
    also treated as the banner when it is short and names WizTree.
    """
    if not rows:
        return "", rows
    first = rows[0]
    looks_like_banner = len(first) < 2 or (
        len(first) < 4 and "wiztree" in " ".join(first).lower()
        and not _is_int(first[1] if len(first) > 1 else "")
    )
    if looks_like_banner and not _is_int(first[1] if len(first) > 1 else ""):
        return first[0].strip(), rows[1:]
    return "", rows


def parse_export_csv(path: str) -> tuple[list[WizTreeEntry], dict[str, Any]]:
    """Parse a WizTree CSV export into entries plus metadata.

    Header names are ignored for lookup — they are only reported in
    ``meta["columns"]`` for diagnostics.  Fields are read positionally so that
    a Chinese, English or any other localized export parses identically.

    Args:
        path: Path to a CSV produced by ``WizTree64.exe <target> /export=...``.

    Returns:
        ``(entries, meta)``.  ``meta`` carries ``banner``, ``columns``,
        ``encoding``, ``encoding_fallback``, ``header_found``, ``row_count``,
        ``skipped_rows`` and ``path``.  ``skipped_rows`` matters: a row whose
        byte size is empty or non-numeric is dropped, and silently dropping rows
        would understate every total, so callers can warn about it.
    """
    rows, encoding, encoding_fallback = _read_rows(path)
    banner, rows = _split_banner(rows)

    header_index: int | None = None
    columns: list[str] = list(POSITIONAL_COLUMNS)
    for i, row in enumerate(rows):
        # A header row has several fields and a non-numeric second field
        # (data rows always carry a numeric byte size in column 2).
        if len(row) >= 5 and not _is_int(row[1]):
            header_index = i
            columns = [c.strip().strip('"') for c in row]
            break

    body = rows[(header_index + 1):] if header_index is not None else rows

    entries: list[WizTreeEntry] = []
    skipped_rows: list[int] = []
    for offset, row in enumerate(body):
        if len(row) < 2 or not _is_int(row[1]):
            skipped_rows.append(offset + 1)  # 1-based, relative to the body
            continue
        padded = list(row) + [""] * (len(POSITIONAL_COLUMNS) - len(row))
        extra: dict[str, str] = {}
        for i in range(len(POSITIONAL_COLUMNS), len(row)):
            key = columns[i] if i < len(columns) and columns[i] else f"col{i}"
            extra[key] = row[i]
        entry = WizTreeEntry(
            name=padded[0].strip().strip('"'),
            size=padded[1],
            allocated=padded[2],
            modified=padded[3].strip().strip('"'),
            attributes=padded[4],
            files=padded[5],
            folders=padded[6],
            extra=extra,
            allocated_raw=row[2] if len(row) > 2 else "",
        )
        entries.append(entry)

    meta: dict[str, Any] = {
        "path": path,
        "banner": banner,
        "columns": columns,
        "encoding": encoding,
        "encoding_fallback": encoding_fallback,
        "header_found": header_index is not None,
        "row_count": len(entries),
        "skipped_rows": skipped_rows,
        "body_rows": len(body),
        "extra_columns": columns[len(POSITIONAL_COLUMNS):],
    }
    return entries, meta


def parse_filetypes_csv(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse a ``/exportfiletypes`` report.

    Schema (also localized): ``ext, type, percent, size, allocated, files``.
    Percent is a float; the rest are integers except the two text columns.
    """
    rows, encoding, encoding_fallback = _read_rows(path)
    banner, rows = _split_banner(rows)

    header_index = next(
        (i for i, row in enumerate(rows) if len(row) >= 5 and not _is_int(row[1])),
        None,
    )
    body = rows[(header_index + 1):] if header_index is not None else rows

    out: list[dict[str, Any]] = []
    for row in body:
        if len(row) < 4 or not _is_int(row[3]):
            continue
        padded = list(row) + [""] * (len(FILETYPE_COLUMNS) - len(row))
        try:
            percent = float(padded[2].strip().strip('"').replace(",", "."))
        except ValueError:
            percent = 0.0
        out.append({
            "ext": padded[0].strip().strip('"'),
            "type": padded[1].strip().strip('"'),
            "percent": percent,
            "size": _to_int(padded[3]),
            "allocated": _to_int(padded[4]),
            "files": _to_int(padded[5]),
        })

    meta: dict[str, Any] = {
        "path": path,
        "banner": banner,
        "encoding": encoding,
        "header_found": header_index is not None,
        "row_count": len(out),
    }
    return out, meta


def load_entries(path: str) -> list[WizTreeEntry]:
    """Convenience wrapper returning only the entries."""
    return parse_export_csv(path)[0]


def entries_from_dicts(rows: Iterable[dict[str, Any]]) -> list[WizTreeEntry]:
    """Rebuild entries from their :meth:`WizTreeEntry.to_dict` form."""
    fields = set(POSITIONAL_COLUMNS) | {"extra", "allocated_raw"}
    return [
        WizTreeEntry(**{k: v for k, v in row.items() if k in fields})
        for row in rows
    ]
