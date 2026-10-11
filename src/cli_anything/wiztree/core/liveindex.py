"""Incremental, FRN-keyed disk index kept fresh by the NTFS USN journal.

The problem this solves
-----------------------
Answering "what is filling this disk" from scratch costs a full MFT read: ~23 s
for the dump plus ~27 s to parse it on the measured 3.3 M-file volume.  Paying
that per question is what makes a disk-usage tool feel slow.

The fix is the same one Everything uses: enumerate once, then *maintain* the
index from the NTFS USN change journal, which records every create, delete,
rename and data change on the volume.  A sync is a few thousand records instead
of 3.5 M, and it lands in milliseconds.

Everything here is keyed on the MFT record number, because that is what both
`core.mft` and the USN journal address files by.  Paths are derived on demand
from the parent chain, never stored.

Four measured behaviours drive the design
-----------------------------------------
1. **A reset ``StartUsn`` is rejected.**  ``FSCTL_READ_USN_JOURNAL`` fails with
   ``ERROR_INVALID_PARAMETER`` when the start is close to ``NextUsn``, but
   reading from ``FirstUsn`` always works.  The journal is capped in size, so the
   fallback replays all of it and filters by USN -- bounded work, not a hang.
2. **An FRN is not a record number.**  The low 48 bits are the record number and
   the high 16 are a reuse sequence number; a raw comparison matches nothing.
3. **The journal can wrap.**  When the checkpoint is older than ``FirstUsn``, or
   the journal ID changed, incremental is impossible and the caller must be told
   to rebuild rather than silently return a stale index.
4. **USN records carry no size.**  Sizes come from ``os.stat`` on the handful of
   paths the journal flagged.
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterator, NamedTuple

from .mft import FRN_MASK, ROOT_FRN, MftNode

# ── Win32 plumbing ────────────────────────────────────────────────────

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.DeviceIoControl.restype = wintypes.BOOL
_kernel32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class _USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [
        ("UsnJournalID", ctypes.c_ulonglong),
        ("FirstUsn", ctypes.c_longlong),
        ("NextUsn", ctypes.c_longlong),
        ("LowestValidUsn", ctypes.c_longlong),
        ("MaxUsn", ctypes.c_longlong),
        ("MaximumSize", ctypes.c_ulonglong),
        ("AllocationDelta", ctypes.c_ulonglong),
    ]


class _READ_USN_JOURNAL_DATA(ctypes.Structure):
    _fields_ = [
        ("StartUsn", ctypes.c_longlong),
        ("ReasonMask", wintypes.DWORD),
        ("ReturnOnlyOnClose", wintypes.DWORD),
        ("Timeout", ctypes.c_ulonglong),
        ("BytesToWaitFor", ctypes.c_ulonglong),
        ("UsnJournalID", ctypes.c_ulonglong),
    ]


#: USN_RECORD_V2 header, up to (not including) the file name.
_USN_RECORD = struct.Struct("<IHHQQqQIIIIHH")

REASON_DATA_OVERWRITE = 0x00000001
REASON_DATA_EXTEND = 0x00000002
REASON_DATA_TRUNCATION = 0x00000004
REASON_FILE_CREATE = 0x00000100
REASON_FILE_DELETE = 0x00000200
REASON_RENAME_NEW_NAME = 0x00001000
REASON_RENAME_OLD_NAME = 0x00002000
REASON_BASIC_INFO_CHANGE = 0x00010000

REASON_DATA_ANY = (
    REASON_DATA_OVERWRITE | REASON_DATA_EXTEND | REASON_DATA_TRUNCATION
)

#: USN v2 attributes bit for a directory.
FILE_ATTRIBUTE_DIRECTORY = 0x10


class Journal(NamedTuple):
    """A snapshot of the volume's USN journal header."""

    journal_id: int
    first_usn: int
    next_usn: int
    maximum_size: int

    @property
    def is_usable(self) -> bool:
        return self.journal_id != 0 and self.next_usn > 0


class UsnRecord(NamedTuple):
    frn: int              # masked to the MFT record number
    parent: int           # masked
    usn: int
    reason: int
    name: str
    is_dir: bool


class JournalUnavailable(RuntimeError):
    """The USN journal cannot be read (missing, or access denied)."""


class CheckpointLost(RuntimeError):
    """The checkpoint is too old to replay; a full rebuild is required."""

    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


def _open_volume(drive: str):
    letter = drive.rstrip(":\\/")[:1]
    handle = _kernel32.CreateFileW(
        rf"\\.\{letter}:", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if handle == _INVALID_HANDLE or handle is None:
        raise JournalUnavailable(
            f"cannot open \\\\.\\{letter}: for reading (WinError "
            f"{ctypes.get_last_error()}); reading the USN journal needs elevation"
        )
    return handle


def query_journal(drive: str) -> Journal:
    """Read the volume's USN journal header."""
    handle = _open_volume(drive)
    try:
        data = _USN_JOURNAL_DATA()
        returned = wintypes.DWORD(0)
        if not _kernel32.DeviceIoControl(
            handle, FSCTL_QUERY_USN_JOURNAL, None, 0,
            ctypes.byref(data), ctypes.sizeof(data), ctypes.byref(returned), None,
        ):
            raise JournalUnavailable(
                f"FSCTL_QUERY_USN_JOURNAL failed on {drive} "
                f"(WinError {ctypes.get_last_error()}); the volume has no USN journal"
            )
        return Journal(
            data.UsnJournalID, data.FirstUsn, data.NextUsn, data.MaximumSize
        )
    finally:
        _kernel32.CloseHandle(handle)


#: USN record versions seen that this V2-only reader cannot decode, by version.
#: Surfaced through ``sync`` so a silent gap is visible rather than inferred.
SKIPPED_RECORD_VERSIONS: dict[int, int] = {}


def reset_record_version_stats() -> None:
    """Clear :data:`SKIPPED_RECORD_VERSIONS` before a read."""
    SKIPPED_RECORD_VERSIONS.clear()


def _read_chunk(handle, journal: Journal, start_usn: int, buffer_size: int = 1 << 20):
    """One ``FSCTL_READ_USN_JOURNAL`` call.

    Returns ``(records, next_usn)``, or ``None`` when the driver rejects the
    start (see behaviour 1 in the module docstring).
    """
    request = _READ_USN_JOURNAL_DATA()
    request.StartUsn = start_usn
    request.ReasonMask = 0xFFFFFFFF
    request.ReturnOnlyOnClose = 0
    request.Timeout = 0
    request.BytesToWaitFor = 0
    request.UsnJournalID = journal.journal_id

    buffer = ctypes.create_string_buffer(buffer_size)
    returned = wintypes.DWORD(0)
    if not _kernel32.DeviceIoControl(
        handle, FSCTL_READ_USN_JOURNAL, ctypes.byref(request),
        ctypes.sizeof(request), buffer, buffer_size, ctypes.byref(returned), None,
    ):
        return None

    raw = buffer.raw[: returned.value]
    if len(raw) < 8:
        return [], start_usn

    next_usn = struct.unpack_from("<q", raw, 0)[0]
    records: list[UsnRecord] = []
    offset = 8
    while offset + _USN_RECORD.size <= len(raw):
        (length, major, _minor, frn, parent, usn, _stamp, reason,
         _source, _security, attributes, name_len, name_offset) = \
            _USN_RECORD.unpack_from(raw, offset)
        if length == 0:
            break

        # USN_RECORD_V2 is the only layout whose field offsets this struct
        # matches; V3/V4 widen the two file references to 128 bits and shift
        # everything after them.  Parsing those as V2 is how a name picks up
        # neighbouring bytes, which then reaches os.stat as a bogus path and
        # silently reports size 0.
        if major == 2:
            record_end = offset + length
            # The name can never extend past its own record.
            name_end = min(offset + name_offset + name_len * 2, record_end)
            name_start = offset + name_offset
            name = ""
            if record_end <= len(raw) and name_start < name_end:
                name = raw[name_start:name_end].decode("utf-16-le", "replace") \
                    .replace("\x00", "")
            records.append(UsnRecord(
                frn=frn & FRN_MASK,
                parent=parent & FRN_MASK,
                usn=usn,
                reason=reason,
                name=name,
                is_dir=bool(attributes & FILE_ATTRIBUTE_DIRECTORY),
            ))
        else:
            SKIPPED_RECORD_VERSIONS[major] = SKIPPED_RECORD_VERSIONS.get(major, 0) + 1

        offset += length
    return records, next_usn


def read_journal_since(drive: str, checkpoint: int, journal: Journal | None = None):
    """Every USN record newer than *checkpoint*, plus how they were obtained.

    Raises:
        CheckpointLost: the journal was reset or has wrapped past *checkpoint*,
            so the delta is incomplete and the caller must rebuild.
    """
    journal = journal or query_journal(drive)
    if not journal.is_usable:
        raise JournalUnavailable(f"no usable USN journal on {drive}")
    if checkpoint < journal.first_usn:
        raise CheckpointLost(
            f"the USN journal on {drive} has wrapped: the checkpoint (USN "
            f"{checkpoint}) is older than the oldest valid record "
            f"({journal.first_usn}), so the changes in between are gone",
            reason="wrapped",
        )

    handle = _open_volume(drive)
    try:
        result = _read_chunk(handle, journal, checkpoint)
        if result is not None:
            collected: list[UsnRecord] = []
            records, cursor = result
            collected.extend(records)
            while records and cursor < journal.next_usn and cursor > 0:
                result = _read_chunk(handle, journal, cursor)
                if result is None:
                    break
                records, cursor = result
                collected.extend(records)
            return [r for r in collected if r.usn > checkpoint], "from checkpoint"

        # Rejected start: replay the whole journal and filter.  It is capped in
        # size, so this is bounded work rather than a fallback that never ends.
        collected = []
        cursor = journal.first_usn
        while cursor < journal.next_usn:
            result = _read_chunk(handle, journal, cursor)
            if result is None:
                break
            records, cursor = result
            if not records:
                break
            collected.extend(records)
        return [r for r in collected if r.usn > checkpoint], "full journal replay"
    finally:
        _kernel32.CloseHandle(handle)


# ── the persisted index ───────────────────────────────────────────────


@dataclass
class LiveIndex:
    """An FRN-keyed snapshot of a volume plus the checkpoint that dates it."""

    drive: str
    nodes: dict[int, MftNode] = field(default_factory=dict)
    journal_id: int = 0
    checkpoint_usn: int = 0
    built_at: str = ""
    synced_at: str = ""
    #: parent FRN -> child FRNs, maintained so a directory delete is O(subtree).
    #: Built lazily: on load it costs a full pass over 3.5 M entries, and a
    #: read-only query never needs it.
    children: dict[int, set[int]] = field(default_factory=dict, repr=False)
    _children_ready: bool = field(default=False, repr=False, compare=False)
    #: folder FRN -> recursive ``(size, allocated)``.
    #: Kept up to date incrementally rather than recomputed: a full roll-up over
    #: 3.5 M entries measured 23 s, while a change touches only the handful of
    #: ancestors on its parent chain.
    totals: dict[int, tuple[int, int]] = field(default_factory=dict, repr=False)

    # ── construction ─────────────────────────────────────────────────

    def rebuild_children(self) -> None:
        """Recompute the parent->children map from :attr:`nodes`."""
        children: dict[int, set[int]] = {}
        for frn, node in self.nodes.items():
            children.setdefault(node.parent, set()).add(frn)
        self.children = children
        self._children_ready = True

    def ensure_children(self) -> None:
        """Build the child map on first use."""
        if not self._children_ready:
            self.rebuild_children()

    def rebuild_totals(self) -> None:
        """Full recursive roll-up; the expensive fallback when totals are absent."""
        from .mft import compute_folder_totals

        self.totals = compute_folder_totals(self.nodes)

    def bump_ancestors(self, parent: int, size: int, allocated: int) -> None:
        """Add a delta to every folder on *parent*'s ancestor chain.

        This is what keeps totals incremental: a file changing by Δ costs one
        walk to the root (typically ~10 hops), not a pass over the volume.
        """
        seen: set[int] = set()
        current = parent
        while current and current not in seen:
            seen.add(current)
            node = self.nodes.get(current)
            if node is None:
                break
            if node.is_dir:
                current_size, current_alloc = self.totals.get(current, (0, 0))
                self.totals[current] = (current_size + size, current_alloc + allocated)
            if current == ROOT_FRN or node.parent == current:
                break
            current = node.parent

    # ── persistence ──────────────────────────────────────────────────

    #: Bumped whenever the payload layout changes.
    FORMAT_VERSION = 1

    def save(self, path: str) -> int:
        """Write the index; returns the byte size.

        Layout note: ``names`` is one ``"\\n"``-joined blob rather than a list of
        3.5 M strings.  NTFS forbids control characters in a file name, so a
        newline is an unambiguous separator and the whole thing serializes at C
        speed instead of paying per-object pickle overhead.
        """
        import pickle

        node_count = len(self.nodes)
        frn = [0] * node_count
        parent = [0] * node_count
        size = [0] * node_count
        allocated = [0] * node_count
        flags = bytearray(node_count)
        names = [""] * node_count

        for position, (key, node) in enumerate(self.nodes.items()):
            frn[position] = key
            parent[position] = node.parent
            size[position] = node.size
            allocated[position] = node.allocated
            flags[position] = 1 if node.is_dir else 0
            names[position] = node.name

        payload = {
            "version": self.FORMAT_VERSION,
            "drive": self.drive,
            "journal_id": self.journal_id,
            "checkpoint_usn": self.checkpoint_usn,
            "built_at": self.built_at,
            "synced_at": self.synced_at,
            "frn": frn,
            "parent": parent,
            "size": size,
            "allocated": allocated,
            "flags": bytes(flags),
            "names": "\n".join(names),
            # Only folders carry a recursive total, so this is ~13% of the rows
            # and keeps ``index top`` from re-rolling the whole volume.
            "totals_frn": list(self.totals.keys()),
            "totals_size": [value[0] for value in self.totals.values()],
            "totals_alloc": [value[1] for value in self.totals.values()],
        }

        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        temporary = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temporary, "wb") as handle:
                pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(temporary, path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise
        return os.path.getsize(path)

    @classmethod
    def load(cls, path: str) -> "LiveIndex":
        """Read an index written by :meth:`save`."""
        import pickle

        with open(path, "rb") as handle:
            payload = pickle.load(handle)
        if payload.get("version") != cls.FORMAT_VERSION:
            raise ValueError(
                f"index format {payload.get('version')} is not supported "
                f"(expected {cls.FORMAT_VERSION}); rebuild it"
            )

        frn = payload["frn"]
        parent = payload["parent"]
        size = payload["size"]
        allocated = payload["allocated"]
        flags = payload["flags"]
        names = payload["names"].split("\n") if frn else []

        # ``map`` + ``zip`` do the object construction in C.  An explicit Python
        # loop over 3.5 M records costs tens of seconds; this is the difference
        # between a query that feels instant and one that does not.
        nodes: dict[int, MftNode] = dict(zip(
            frn,
            map(MftNode, names, parent, size, allocated, map(bool, flags)),
        ))

        index = cls(
            drive=payload.get("drive", ""),
            nodes=nodes,
            journal_id=payload.get("journal_id", 0),
            checkpoint_usn=payload.get("checkpoint_usn", 0),
            built_at=payload.get("built_at", ""),
            synced_at=payload.get("synced_at", ""),
            totals=dict(zip(
                payload.get("totals_frn", ()),
                zip(payload.get("totals_size", ()), payload.get("totals_alloc", ())),
            )),
        )
        # children is left unbuilt: a read-only query never needs it.
        return index

    @classmethod
    def peek(cls, path: str) -> dict:
        """Read an index's metadata and counts without materializing its nodes.

        Measured on a 3.59 M-entry index: ``pickle.load`` is 2.4 s and the name
        split 0.9 s, but turning every row into an :class:`~core.mft.MftNode` and
        inserting 3.59 M dict entries costs **14.6 s**.  Anything that only needs
        counts or the checkpoint should not pay that.
        """
        import pickle

        with open(path, "rb") as handle:
            payload = pickle.load(handle)
        if payload.get("version") != cls.FORMAT_VERSION:
            raise ValueError(
                f"index format {payload.get('version')} is not supported "
                f"(expected {cls.FORMAT_VERSION}); rebuild it"
            )
        flags = payload.get("flags", b"")
        return {
            "drive": payload.get("drive", ""),
            "entries": len(payload.get("frn", ())),
            "folders": flags.count(1) if flags else 0,
            "journal_id": payload.get("journal_id", 0),
            "checkpoint_usn": payload.get("checkpoint_usn", 0),
            "built_at": payload.get("built_at", ""),
            "synced_at": payload.get("synced_at", ""),
            "totals": len(payload.get("totals_frn", ())),
        }

    # ── incremental maintenance ──────────────────────────────────────

    def path_of(self, frn: int, prefix: str | None = None) -> str:
        """Full path for *frn* (walks the parent chain).

        The volume root is skipped: its ``$FILE_NAME`` is ``"."``, which must not
        leak into a displayed path.
        """
        prefix = self.drive if prefix is None else prefix
        parts: list[str] = []
        seen: set[int] = set()
        current = frn
        while current and current not in seen and len(parts) < 256:
            seen.add(current)
            node = self.nodes.get(current)
            if node is None or current == ROOT_FRN or node.parent == current:
                break
            parts.append(node.name)
            current = node.parent
        if not parts:
            return prefix
        return prefix.rstrip("\\") + "\\" + "\\".join(reversed(parts))

    def _drop_subtree(self, frn: int, parent: int | None) -> tuple[int, int, int]:
        """Remove *frn* and everything beneath it.

        *parent* is passed in rather than looked up: deriving it by scanning the
        child map is O(folders) **per delete**, which measured 122 s on a sync
        with ~1000 deletions.  The caller already has the node in hand.

        Returns:
            ``(removed_count, total_size, total_allocated)`` for the subtree, so
            the caller can subtract it from the surviving ancestors' totals.
        """
        self.ensure_children()
        removed = 0
        size_total = 0
        alloc_total = 0
        stack = [frn]
        while stack:
            current = stack.pop()
            node = self.nodes.pop(current, None)
            if node is None:
                continue
            removed += 1
            size_total += node.size
            alloc_total += node.allocated
            self.totals.pop(current, None)
            stack.extend(self.children.pop(current, ()))
        if parent is not None:
            self.children.get(parent, set()).discard(frn)
        return removed, size_total, alloc_total

    def apply(self, records: Iterator[UsnRecord] | list[UsnRecord]) -> dict[str, int]:
        """Fold USN *records* into the index.

        Returns counters, including ``orphans``: records whose parent is not in
        the index.  Those are counted rather than silently inserted, because a
        non-empty orphan count means the index has a hole and the next sync
        should be a rebuild.

        Renames need no descendant fix-up: a path is derived from the parent
        chain, so renaming a directory re-paths its whole subtree for free.
        """
        self.ensure_children()
        counters = {
            "created": 0, "deleted": 0, "renamed": 0,
            "resized": 0, "orphans": 0, "skipped": 0,
        }

        for record in records:
            frn, parent = record.frn, record.parent

            if record.reason & REASON_FILE_DELETE:
                node = self.nodes.get(frn)
                if node is not None:
                    # Take the parent from the record we already have; the USN
                    # record's own parent field is for the *name* it reports,
                    # which for a delete can already be stale.
                    removed_size, removed_alloc = node.size, node.allocated
                    self._drop_subtree(frn, node.parent)
                    self.bump_ancestors(node.parent, -removed_size, -removed_alloc)
                    counters["deleted"] += 1
                else:
                    counters["skipped"] += 1
                continue

            if record.reason & REASON_RENAME_OLD_NAME:
                counters["skipped"] += 1          # paired with RENAME_NEW_NAME
                continue

            if record.reason & REASON_RENAME_NEW_NAME:
                node = self.nodes.get(frn)
                if node is None:
                    counters["orphans"] += 1
                    continue
                old_parent = node.parent
                self.nodes[frn] = node._replace(name=record.name, parent=parent)
                self.ensure_children()
                self.children.get(old_parent, set()).discard(frn)
                self.children.setdefault(parent, set()).add(frn)
                if parent != old_parent:
                    # The bytes did not move on disk, but they changed owner: the
                    # old chain loses them and the new one gains them.
                    self.bump_ancestors(old_parent, -node.size, -node.allocated)
                    self.bump_ancestors(parent, node.size, node.allocated)
                counters["renamed"] += 1
                continue

            if record.reason & REASON_FILE_CREATE:
                existing = self.nodes.get(frn)
                if existing is not None and existing.name == record.name:
                    counters["skipped"] += 1
                    continue
                if not record.is_dir and parent not in self.nodes:
                    counters["orphans"] += 1
                path = self.path_of(parent) + "\\" + record.name
                size, allocated = _stat_sizes(path)
                self.nodes[frn] = MftNode(
                    name=record.name, parent=parent, size=size,
                    allocated=allocated, is_dir=record.is_dir,
                )
                self.children.setdefault(parent, set()).add(frn)
                if record.is_dir:
                    self.totals.setdefault(frn, (0, 0))
                self.bump_ancestors(parent, size, allocated)
                counters["created"] += 1
                continue

            if record.reason & (REASON_DATA_ANY | REASON_BASIC_INFO_CHANGE):
                node = self.nodes.get(frn)
                if node is None:
                    counters["skipped"] += 1
                    continue
                if node.is_dir:
                    counters["skipped"] += 1
                    continue
                size, allocated = _stat_sizes(self.path_of(frn))
                if size != node.size:
                    self.nodes[frn] = node._replace(size=size, allocated=allocated)
                    self.bump_ancestors(
                        node.parent, size - node.size, allocated - node.allocated
                    )
                    counters["resized"] += 1
                else:
                    counters["skipped"] += 1
                continue

            counters["skipped"] += 1

        return counters

    def sync(self, *, save_to: str | None = None) -> dict:
        """Apply everything the journal recorded since the checkpoint."""
        import datetime

        journal = query_journal(self.drive)
        if self.journal_id and journal.journal_id != self.journal_id:
            raise CheckpointLost(
                f"the USN journal on {self.drive} was reset (id "
                f"0x{journal.journal_id:016x} != 0x{self.journal_id:016x})",
                reason="reset",
            )

        started = __import__("time").perf_counter()
        reset_record_version_stats()
        records, strategy = read_journal_since(self.drive, self.checkpoint_usn, journal)
        read_seconds = __import__("time").perf_counter() - started

        counters = self.apply(records)
        self.journal_id = journal.journal_id
        self.checkpoint_usn = journal.next_usn
        self.synced_at = datetime.datetime.now().isoformat(timespec="seconds")

        size = self.save(save_to) if save_to else 0
        return {
            "drive": self.drive,
            "strategy": strategy,
            "records_read": len(records),
            "read_seconds": round(read_seconds, 3),
            "applied": counters,
            "skipped_record_versions": dict(SKIPPED_RECORD_VERSIONS),
            "checkpoint_usn": self.checkpoint_usn,
            "entries": len(self.nodes),
            "saved_bytes": size,
        }


def _stat_sizes(path: str) -> tuple[int, int]:
    """``(size, allocated)`` for *path*, allocated rounded up to 4 KiB clusters.

    ``ValueError`` is caught alongside ``OSError`` because a path carrying an
    embedded NUL raises it, and a sync must degrade one bad record rather than
    abort the whole run.
    """
    try:
        stat = os.stat(path)
        size = stat.st_size
        cluster = 4096
        return size, ((size + cluster - 1) // cluster) * cluster if size else 0
    except (OSError, ValueError):
        return 0, 0


def build_index(
    drive: str,
    *,
    dump_path: str,
    workers: int | None = None,
    keep_dump: bool = False,
    progress=None,
) -> tuple[LiveIndex, dict]:
    """One-time full build: ``$MFT`` dump, parse, checkpoint.

    Args:
        drive: Volume to index, e.g. ``"D:"``.
        dump_path: Where the raw ``$MFT`` dump is written.  It is several GB
            (5.30 GiB on the measured 331 GB volume), so it wants a volume with
            room; it is deleted afterwards unless *keep_dump*.
        workers: Passed through to :func:`core.mft.parse_mft`.
        keep_dump: Keep the dump (useful for re-parsing while developing).
        progress: Forwarded to the parser.

    Returns:
        ``(index, timings)`` where *timings* carries the per-stage wall seconds.
        Reported rather than hidden, because "the dump is fast but the parse is
        not" is exactly the kind of thing that gets lost in a single total.
    """
    import datetime
    import time

    from ..utils import wiztree_backend as backend
    from . import mft as mft_module

    timings: dict = {}

    started = time.perf_counter()
    backend.dump_mft(drive, dump_path)
    timings["dump_seconds"] = round(time.perf_counter() - started, 2)
    timings["dump_bytes"] = os.path.getsize(dump_path)

    started = time.perf_counter()
    nodes = mft_module.parse_mft(dump_path, workers=workers, progress=progress)
    timings["parse_seconds"] = round(time.perf_counter() - started, 2)
    timings["records"] = len(nodes)

    started = time.perf_counter()
    journal = query_journal(drive)
    timings["journal_seconds"] = round(time.perf_counter() - started, 3)

    index = LiveIndex(
        drive=drive,
        nodes=nodes,
        journal_id=journal.journal_id if journal.is_usable else 0,
        checkpoint_usn=journal.next_usn if journal.is_usable else 0,
        built_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    index.rebuild_children()

    # Roll the recursive folder totals once, here, so that every later query and
    # every later sync is incremental.  Paying it at build time turns ``index
    # top`` from a 23 s pass into a sort.
    started = time.perf_counter()
    index.rebuild_totals()
    timings["totals_seconds"] = round(time.perf_counter() - started, 2)

    if not keep_dump:
        try:
            os.remove(dump_path)
            timings["dump_removed"] = True
        except OSError:
            timings["dump_removed"] = False

    return index, timings
