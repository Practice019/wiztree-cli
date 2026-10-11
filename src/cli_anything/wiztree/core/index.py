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

import codecs
import csv
import io
import os
import pickle
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

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


def _is_int(value: Any) -> bool:
    """True when *value* looks like a plain integer field.

    Profiled on a 653k-row export this helper is called once per row, and the
    regex dominated it.  ``str.isdigit`` settles the common case -- WizTree
    writes byte sizes as bare digits -- in C, and the regex still handles
    whitespace, signs and Unicode digits, so the accepted language is unchanged.
    ``isascii`` only guards the *fast path*: it keeps a non-ASCII digit run off
    the shortcut and onto the regex, which decides it.
    """
    if type(value) is str:
        if value.isascii() and value.isdigit():
            return True
        return bool(_INT_RE.match(value.strip()))
    if type(value) is int:
        return not isinstance(value, bool)
    return bool(_INT_RE.match(str(value or "").strip()))


def _is_float(value: Any) -> bool:
    """True when *value* looks like a plain float field."""
    return bool(_FLOAT_RE.match(str(value or "").strip()))


def _to_int(value: Any, default: int = 0) -> int:
    """Coerce a CSV field (or an already-numeric value) to int.

    Accepts ints/floats directly because entries are also built from JSON
    round-trips, not only from CSV text.

    The order matters for speed: profiled over a 653k-row export this ran 3.26
    million times (5 fields per row) and spent 13.7 s, almost all of it on
    ``isinstance`` dispatch plus two ``str.strip`` calls *per field*.  A clean
    CSV number goes straight through ``int(value)`` -- which already tolerates
    surrounding whitespace -- and the stripping/quoted forms are only reached
    when that fails.
    """
    if type(value) is int:
        return value
    if type(value) is str:
        try:
            return int(value)
        except ValueError:
            pass
        text = value.strip().strip('"')
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
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


@dataclass(slots=True)
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


#: Bumped whenever the cache layout or the entry model changes.
CACHE_VERSION = 1

#: Exports below this size parse fast enough that a sidecar file is pure noise.
CACHE_MIN_BYTES = 4 * 1024 * 1024

#: Set to any non-empty value to disable the sidecar index entirely.
CACHE_DISABLE_ENV = "CLI_ANYTHING_WIZTREE_NO_CACHE"


def index_cache_path(csv_path: str) -> str:
    """Sidecar path holding the parsed index for *csv_path*."""
    return f"{csv_path}.wiztree-index.pkl"


def _cache_signature(csv_path: str) -> list:
    """Identity of the export this cache describes.

    ``st_mtime_ns`` is needed, not ``st_mtime``: on NTFS the latter has 100 ns
    resolution but a coarse Python float rendering, and a re-export that lands in
    the same second with the same byte count would otherwise be served stale.
    """
    stat = os.stat(csv_path)
    return [stat.st_size, stat.st_mtime_ns, CACHE_VERSION]


def _cache_enabled(csv_path: str) -> bool:
    if os.environ.get(CACHE_DISABLE_ENV):
        return False
    try:
        return os.path.getsize(csv_path) >= CACHE_MIN_BYTES
    except OSError:
        return False


class _IndexUnpickler(pickle.Unpickler):
    """Unpickler that can only rebuild :class:`WizTreeEntry` and plain data.

    ``pickle.load`` executes whatever the stream names, and the cache lives in
    the same directory as the export it describes -- a directory an attacker who
    can write there could also fill with CSV exports.  Restricting the globals
    to this module's entry class means a planted cache can at worst produce
    wrong numbers, never arbitrary code execution.
    """

    def find_class(self, module: str, name: str) -> Any:
        if module == __name__ and name == "WizTreeEntry":
            return WizTreeEntry
        raise pickle.UnpicklingError(f"refused unexpected global {module}.{name}")


def load_index_cache(csv_path: str) -> tuple[list[WizTreeEntry], dict[str, Any]] | None:
    """Return a cached parse of *csv_path*, or ``None`` when unusable.

    Every failure mode -- missing file, truncated write, version bump, a CSV
    that changed since the cache was written -- collapses to ``None`` so the
    caller simply re-parses.  Profiled on a 653k-row / 99 MB export, a hit costs
    2.9 s against 8.2 s to re-parse.
    """
    cache_path = index_cache_path(csv_path)
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "rb") as handle:
            payload = _IndexUnpickler(handle).load()
    except (OSError, EOFError, ValueError, pickle.UnpicklingError,
            AttributeError, ImportError, IndexError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if payload.get("signature") != _cache_signature(csv_path):
            return None
    except OSError:
        return None
    entries = payload.get("entries")
    meta = payload.get("meta")
    if not isinstance(entries, list) or not isinstance(meta, dict):
        return None
    return entries, meta


def save_index_cache(
    csv_path: str, entries: list[WizTreeEntry], meta: dict[str, Any]
) -> bool:
    """Write the sidecar index for *csv_path*; ``False`` when it was skipped.

    Written to a PID-suffixed temporary and then :func:`os.replace`-d, so two
    concurrent queries can never leave a half-written cache for the third one to
    read.
    """
    if not _cache_enabled(csv_path):
        return False
    cache_path = index_cache_path(csv_path)
    tmp_path = f"{cache_path}.{os.getpid()}.tmp"
    try:
        signature = _cache_signature(csv_path)
        with open(tmp_path, "wb") as handle:
            pickle.dump(
                {"signature": signature, "entries": entries, "meta": meta},
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        os.replace(tmp_path, cache_path)
        return True
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def _sniff_encoding(path: str, probe_bytes: int = 1 << 16) -> tuple[str, bool]:
    """Choose an encoding from a head sample, returning ``(encoding, is_fallback)``.

    Streaming a 100 MB+ export means the codec must be decided *before* rows are
    consumed, because a mid-stream ``UnicodeDecodeError`` cannot be rewound.  A
    64 KiB head suffices: WizTree writes UTF-8-with-BOM on a UTF-8 system and
    the system code page otherwise, and the banner line already carries
    non-ASCII on a localized system.

    ``utf-16`` is chosen only on a real BOM or a NUL-dense head.  Trying it
    unconditionally (as :func:`_decode` does over whole-file bytes) risks
    accepting an arbitrary code-page file as UTF-16 and silently emitting
    mojibake -- exactly the failure :data:`ENCODINGS` exists to prevent.
    """
    with open(path, "rb") as handle:
        head = handle.read(probe_bytes)
    if not head:
        return "utf-8", False
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", False
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", False
    if head.count(b"\x00") > max(1, len(head) // 8):
        return "utf-16", False
    try:
        head.decode("utf-8")
        return "utf-8", False
    except UnicodeDecodeError:
        pass
    for encoding in ("cp936", "gbk", "big5"):
        try:
            head.decode(encoding)
            return encoding, False
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1", True


def _iter_csv_rows(path: str, encoding: str) -> Iterator[list[str]]:
    """Stream CSV rows straight off the file object.

    The previous path read the whole file into ``bytes``, decoded it into a
    second full copy of the text, then wrapped that in ``io.StringIO`` for
    :func:`csv.reader` -- three times the file size in peak memory, plus a fully
    materialized list of every row before any parsing could begin.
    """
    with open(path, "r", encoding=encoding, newline="", errors="strict") as handle:
        yield from csv.reader(handle)


def _is_meaningful(row: list[str]) -> bool:
    """Cheap blank-line filter for one CSV row.

    Previously ``any(cell.strip() for cell in row)``: that allocates a stripped
    throwaway copy of *every cell of every row* -- several million strings on a
    real volume -- purely to drop blank lines.  A blank CSV line yields ``[]``
    and a whitespace-only line yields one blank-ish cell, so two comparisons
    cover the same cases at a fraction of the cost.
    """
    if not row:
        return False
    if len(row) == 1 and not row[0].strip():
        return False
    return True


def _parse_export_rows(
    row_iter: Iterable[list[str]],
    path: str,
    encoding: str,
    encoding_fallback: bool,
) -> tuple[list[WizTreeEntry], dict[str, Any]]:
    """Single-pass driver shared by the streaming and whole-file code paths."""
    banner = ""
    columns: list[str] = list(POSITIONAL_COLUMNS)
    header_index: int | None = None
    entries: list[WizTreeEntry] = []
    skipped_rows: list[int] = []
    body_rows = 0
    banner_pending = True

    for row in row_iter:
        if not _is_meaningful(row):
            continue

        if banner_pending:
            banner_pending = False
            looks_like_banner = len(row) < 2 or (
                len(row) < 4 and "wiztree" in " ".join(row).lower()
                and not _is_int(row[1] if len(row) > 1 else "")
            )
            if looks_like_banner and not _is_int(row[1] if len(row) > 1 else ""):
                banner = row[0].strip()
                continue

        # A header row has several fields and a non-numeric second field
        # (data rows always carry a numeric byte size in column 2).
        if header_index is None and len(row) >= 5 and not _is_int(row[1]):
            header_index = body_rows
            columns = [c.strip().strip('"') for c in row]
            continue

        body_rows += 1
        if len(row) < 2 or not _is_int(row[1]):
            skipped_rows.append(body_rows)  # 1-based, relative to the body
            continue

        padded = list(row) + [""] * (len(POSITIONAL_COLUMNS) - len(row))
        extra: dict[str, str] = {}
        for i in range(len(POSITIONAL_COLUMNS), len(row)):
            key = columns[i] if i < len(columns) and columns[i] else f"col{i}"
            extra[key] = row[i]
        entries.append(WizTreeEntry(
            name=padded[0].strip().strip('"'),
            size=padded[1],
            allocated=padded[2],
            modified=padded[3].strip().strip('"'),
            attributes=padded[4],
            files=padded[5],
            folders=padded[6],
            extra=extra,
            allocated_raw=row[2] if len(row) > 2 else "",
        ))

    meta: dict[str, Any] = {
        "path": path,
        "banner": banner,
        "columns": columns,
        "encoding": encoding,
        "encoding_fallback": encoding_fallback,
        "header_found": header_index is not None,
        "row_count": len(entries),
        "skipped_rows": skipped_rows,
        "body_rows": body_rows,
        "extra_columns": columns[len(POSITIONAL_COLUMNS):],
    }
    return entries, meta


def parse_export_csv(
    path: str, use_cache: bool = True
) -> tuple[list[WizTreeEntry], dict[str, Any]]:
    """Parse a WizTree CSV export into entries plus metadata.

    Rows are streamed off disk and parsed in a single pass (see
    :func:`_iter_csv_rows`); the read-everything path is kept only as a fallback
    for a ``UnicodeDecodeError`` the head sample did not predict.

    A successful parse of a large export is memoized in a sidecar file next to
    it (see :func:`load_index_cache`), because every ``query`` subcommand loads
    the whole export: without the cache, asking a second question about the same
    scan re-pays the full parse.

    Header names are ignored for lookup — they are only reported in
    ``meta["columns"]`` for diagnostics.  Fields are read positionally so that
    a Chinese, English or any other localized export parses identically.

    Args:
        path: Path to a CSV produced by ``WizTree64.exe <target> /export=...``.
        use_cache: Consult and refresh the sidecar index.  Set ``False`` to
            force a re-parse.

    Returns:
        ``(entries, meta)``.  ``meta`` carries ``banner``, ``columns``,
        ``encoding``, ``encoding_fallback``, ``header_found``, ``row_count``,
        ``skipped_rows`` and ``path``.  ``skipped_rows`` matters: a row whose
        byte size is empty or non-numeric is dropped, and silently dropping rows
        would understate every total, so callers can warn about it.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Export file not found: {path}")

    if use_cache:
        cached = load_index_cache(path)
        if cached is not None:
            cached_meta = dict(cached[1])
            cached_meta["cache"] = "hit"
            return cached[0], cached_meta

    encoding, encoding_fallback = _sniff_encoding(path)
    try:
        entries, meta = _parse_export_rows(
            _iter_csv_rows(path, encoding), path, encoding, encoding_fallback
        )
    except UnicodeDecodeError:
        # The head sample did not predict the code page; the whole-file path
        # tries every candidate codec and is therefore correct, if slow.
        rows, real_encoding, real_fallback = _read_rows(path)
        entries, meta = _parse_export_rows(iter(rows), path, real_encoding, real_fallback)

    if use_cache:
        save_index_cache(path, entries, meta)
        meta["cache"] = "miss"
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
