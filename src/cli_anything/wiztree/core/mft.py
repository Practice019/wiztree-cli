"""NTFS ``$MFT`` parser — the one-time seed for an FRN-keyed index.

Why this exists instead of the CSV export route
-----------------------------------------------
A full answer to "what is on this disk" needs every file.  Measured on a 331 GB
volume holding 3.3 M files:

===========================================  ==========
WizTree file-level CSV export, then parse    ~190 s
``/dumpmftfile`` + this parser                **~50 s**
===========================================  ==========

The dump is a flat array of fixed-size records, which buys two things the CSV
never could:

* **It is already keyed by MFT record number.**  The USN change journal —
  the mechanism that keeps the index fresh without rescanning — addresses
  everything by file reference number, so no join or path-matching is needed.
* **It shards perfectly.**  A byte range always begins on a record boundary, so
  the parse splits across cores with no coordination: 5.55 M records went from
  59.5 s to 27.2 s on a 4-core/8-thread machine.

Two format details that silently corrupt results if skipped:

* **Update sequence array (fixups).**  NTFS parks the last two bytes of every
  512-byte sector in the record header and writes a counter in their place.  A
  parser that ignores this reads wrong bytes at every sector tail.
* **``$FILE_NAME`` namespace.**  A record can carry several names (a Win32 long
  name plus a DOS 8.3 alias).  Only the highest-preference namespace is the real
  name; taking the first one hands back ``PROGRA~1``-style aliases.
"""

from __future__ import annotations

import mmap
import os
import struct
from concurrent.futures import ProcessPoolExecutor
from typing import Iterable, NamedTuple

#: An NTFS file reference is 64 bits: the low 48 are the MFT record number and
#: the high 16 are a sequence number bumped whenever the record is reused.
#: WizTree's ``MFTRECNO`` column carries only the record number, so anything
#: joining the two must mask.
FRN_MASK = 0xFFFFFFFFFFFF

#: The volume root directory always occupies MFT record 5 on NTFS.
ROOT_FRN = 5

ATTR_STANDARD_INFORMATION = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

#: ``$FILE_NAME`` namespace values, best first.  3 = Win32 + DOS, 1 = Win32,
#: 0 = POSIX, 2 = DOS (the 8.3 alias we must not mistake for the real name).
NAMESPACE_PREFERENCE = {3: 3, 1: 2, 0: 1, 2: 0}

#: Fixed part of a FILE record header, up to (not including) the fixup array.
_FILE_HEADER = struct.Struct("<4sHHQHHHHIIQH")

SECTOR_SIZE = 512


class MftNode(NamedTuple):
    """One MFT record, in the terms the USN journal speaks."""

    name: str
    parent: int          # parent MFT record number (low 48 bits)
    size: int            # logical size; 0 for directories
    allocated: int       # on-disk size; 0 for directories
    is_dir: bool


def detect_record_size(path: str) -> int:
    """Record size in bytes, from the first record's declared allocation.

    Volumes formatted with non-default cluster sizes can use 4096-byte records;
    reading ``$MFT`` with the wrong stride turns the whole parse into noise.
    """
    with open(path, "rb") as handle:
        head = handle.read(_FILE_HEADER.size)
    if head[:4] != b"FILE":
        raise ValueError(
            f"not an $MFT dump (first four bytes are {head[:4]!r}, expected b'FILE'). "
            "Produce one with `scan mft <drive> -o <path>`."
        )
    record_size = struct.unpack_from("<I", head, 28)[0] or 1024
    return record_size if record_size in (1024, 4096) else 1024


def parse_range(job: tuple[str, int, int, int]) -> tuple[int, list[tuple]]:
    """Parse records ``[start, end)`` of *path*; the ProcessPool worker.

    Deliberately returns plain tuples rather than :class:`MftNode` objects —
    3.5 M namedtuples cost noticeably more to pickle back to the parent.
    """
    path, start, end, record_size = job
    rows: list[tuple] = []

    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
            view = memoryview(mapping)
            for index in range(start, end):
                base = index * record_size

                # Cheap fixed-offset gates before touching anything expensive:
                # read "FI" and the flags straight from the mapping, so the ~29%
                # of records that are free never get copied or fixed up.
                if view[base] != 0x46 or view[base + 1] != 0x49:
                    continue
                flags = view[base + 22] | (view[base + 23] << 8)
                if not (flags & 0x01):
                    continue

                record = bytearray(view[base:base + record_size])
                usa_offset, usa_count = struct.unpack_from("<HH", record, 4)
                if usa_count >= 2:
                    for sector in range(1, usa_count):
                        tail = sector * SECTOR_SIZE - 2
                        record[tail:tail + 2] = record[
                            usa_offset + sector * 2: usa_offset + sector * 2 + 2
                        ]

                is_dir = bool(flags & 0x02)
                parent = 0
                name = ""
                best_namespace = -1
                name_size = 0
                data_size = 0
                data_allocated = 0

                offset = struct.unpack_from("<H", record, 20)[0]
                while offset + 8 <= record_size:
                    attr_type, attr_len = struct.unpack_from("<II", record, offset)
                    if attr_type == ATTR_END or attr_len == 0 or offset + attr_len > record_size:
                        break
                    non_resident = record[offset + 8]

                    if attr_type == ATTR_FILE_NAME and not non_resident:
                        content = offset + struct.unpack_from("<H", record, offset + 20)[0]
                        if content:
                            namespace = record[content + 65]
                            if NAMESPACE_PREFERENCE.get(namespace, -1) > best_namespace:
                                best_namespace = NAMESPACE_PREFERENCE.get(namespace, -1)
                                parent = struct.unpack_from("<Q", record, content)[0]
                                name_size = struct.unpack_from("<Q", record, content + 48)[0]
                                name_len = record[content + 64]
                                # NTFS forbids NUL in a name, so anything that
                                # decodes to one is a malformed/over-long field.
                                # Left in place it later reaches os.stat as an
                                # embedded-null path, which raises ValueError
                                # somewhere far less obvious than here.
                                name = record[
                                    content + 66: content + 66 + name_len * 2
                                ].decode("utf-16-le", "replace").replace("\x00", "")
                                # Only a *long* name lets us stop: a record can
                                # carry a DOS 8.3 alias as well, and taking the
                                # first $FILE_NAME handed back "PROGRA~1" instead
                                # of "Program Files".  Directories have no
                                # $DATA to wait for, so a long name ends it.
                                if best_namespace >= NAMESPACE_PREFERENCE[1] and (is_dir or data_size):
                                    break
                    elif attr_type == ATTR_DATA and record[offset + 9] == 0:
                        # Unnamed $DATA only: a directory's $DATA streams are
                        # named ($I30 etc.) and must not be read as content.
                        if non_resident and attr_len >= 64:
                            data_size = struct.unpack_from("<Q", record, offset + 48)[0]
                            data_allocated = struct.unpack_from("<Q", record, offset + 40)[0]
                            if name and best_namespace >= NAMESPACE_PREFERENCE[1]:
                                break
                        elif not non_resident and attr_len >= 24:
                            # Resident data lives inside the MFT record itself,
                            # so it consumes no clusters of its own.  Its content
                            # length is a 32-bit field, not the 64-bit one used
                            # by non-resident attributes.
                            if not data_size:
                                data_size = struct.unpack_from("<I", record, offset + 16)[0]
                            if name and best_namespace >= NAMESPACE_PREFERENCE[1]:
                                break

                    offset += attr_len

                if not name:
                    continue                     # no $FILE_NAME: not reachable by path

                rows.append((
                    index,
                    name,
                    parent & FRN_MASK,
                    name_size if is_dir else (data_size or name_size),
                    data_allocated,
                    is_dir,
                ))
            del view

    return start, rows


def parse_mft(path: str, workers: int | None = None, progress=None) -> dict[int, MftNode]:
    """Parse an ``$MFT`` dump into ``{record_number: MftNode}``.

    Args:
        path: Dump produced by ``WizTree64.exe <drive> /dumpmftfile=<path>``.
        workers: Process count; defaults to ``min(8, cpu_count)``.  Pass ``1``
            to parse in-process (useful for debugging and for tests).
        progress: Optional ``callable(done, total)``.

    Returns:
        Record number to node.  Records without a ``$FILE_NAME`` attribute are
        skipped — they have no path and so cannot appear in a path-based index.

    Raises:
        ValueError: *path* is not an ``$MFT`` dump.
    """
    record_size = detect_record_size(path)
    total_records = os.path.getsize(path) // record_size

    if workers == 1:
        _, rows = parse_range((path, 0, total_records, record_size))
        if progress:
            progress(total_records, total_records)
        return {row[0]: MftNode(*row[1:]) for row in rows}

    worker_count = workers or min(8, os.cpu_count() or 4)
    chunk = (total_records + worker_count - 1) // worker_count
    jobs = [
        (path, start, min(start + chunk, total_records), record_size)
        for start in range(0, total_records, chunk)
    ]

    nodes: dict[int, MftNode] = {}
    done = 0
    with ProcessPoolExecutor(max_workers=worker_count) as pool:
        for _start, rows in pool.map(parse_range, jobs):
            for row in rows:
                nodes[row[0]] = MftNode(*row[1:])
            done += 1
            if progress:
                progress(done * chunk, total_records)
    return nodes


# ── tree helpers ──────────────────────────────────────────────────────


def resolve_path(nodes: dict[int, MftNode], frn: int, prefix: str = "") -> str:
    """Full path for *frn*, built by walking the parent chain.

    Names are components, not full paths, so this is the only place a path is
    materialized — cheap when only the rows a query actually returns need one.
    A cycle guard keeps a corrupt record from hanging the walk.

    The volume root is *not* appended: its ``$FILE_NAME`` is ``"."``, so
    including it would produce ``D:\\.\\Users`` instead of ``D:\\Users``.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current = frn
    while current and current not in seen and len(parts) < 256:
        seen.add(current)
        node = nodes.get(current)
        if node is None or current == ROOT_FRN or node.parent == current:
            break
        parts.append(node.name)
        current = node.parent
    if not parts:
        return prefix
    return prefix + "\\" + "\\".join(reversed(parts))


def compute_depths(nodes: dict[int, MftNode]) -> dict[int, int]:
    """Depth of every record, 0 for the volume root.

    Iterative with memoization so a deep tree cannot blow the recursion limit,
    and a parent cycle cannot loop forever.
    """
    depths: dict[int, int] = {}

    for frn in nodes:
        if frn in depths:
            continue
        chain: list[int] = []
        current = frn
        while current not in depths:
            node = nodes.get(current)
            if node is None:
                depths[current] = 0
                break
            chain.append(current)
            if current == ROOT_FRN or node.parent == current or node.parent not in nodes:
                depths[current] = 0
                break
            current = node.parent
        base = depths.get(current, 0)
        for offset, item in enumerate(reversed(chain)):
            depths[item] = base + offset + 1 if item != current else base
    return depths


def compute_folder_totals(nodes: dict[int, MftNode]) -> dict[int, tuple[int, int]]:
    """Recursive ``(size, allocated)`` for every directory, including files.

    One pass in decreasing depth: each record's own size is folded into its
    parent's running total, so every directory ends up holding its whole
    subtree without any repeated walk.

    Only directories appear in the result; callers that need a file's size read
    it from its own node.
    """
    depths = compute_depths(nodes)

    totals: dict[int, list[int]] = {
        frn: [0 if node.is_dir else node.size, 0 if node.is_dir else node.allocated]
        for frn, node in nodes.items()
    }

    buckets: dict[int, list[int]] = {}
    for frn, depth in depths.items():
        buckets.setdefault(depth, []).append(frn)

    for depth in sorted(buckets, reverse=True):
        for frn in buckets[depth]:
            node = nodes[frn]
            parent = node.parent
            if parent == frn or parent not in nodes or parent not in totals:
                continue
            child = totals[frn]
            target = totals[parent]
            target[0] += child[0]
            target[1] += child[1]

    return {
        frn: (totals[frn][0], totals[frn][1])
        for frn, node in nodes.items()
        if node.is_dir
    }


def iter_descendants(nodes: dict[int, MftNode], root_frn: int) -> Iterable[int]:
    """Every record whose parent chain passes through *root_frn*.

    Used by the incremental layer to re-path or drop a renamed/deleted subtree
    without touching the rest of the index.
    """
    children: dict[int, list[int]] = {}
    for frn, node in nodes.items():
        children.setdefault(node.parent, []).append(frn)

    stack = [root_frn]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        yield current
        stack.extend(children.get(current, ()))
