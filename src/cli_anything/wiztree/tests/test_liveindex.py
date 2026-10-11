"""Tests for the incremental index: the $MFT parser and the USN-journal layer.

Synthetic data only — no WizTree, no elevation, no real volume.  The tests that
would need a live journal are written against injected fakes so they stay
runnable in CI, and the ones that matter for correctness (record parsing, the
FRN mask, tree aggregation) run entirely on bytes this file builds itself.
"""

from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from cli_anything.wiztree.core import liveindex, mft  # noqa: E402


# ── synthetic $MFT construction ───────────────────────────────────────
#
# Building real FILE records by hand is the only way to test the parser
# honestly: a fixture that skips the update-sequence array would pass a parser
# that ignores fixups, which is precisely the bug worth catching.

RECORD_SIZE = 1024


def _apply_fixups(record: bytearray, usa_offset: int = 48, usa_count: int = 3) -> None:
    """Write the update-sequence array the way NTFS does.

    The last two bytes of every 512-byte sector are moved into the array and
    replaced by a counter.  A parser that does not undo this reads garbage at
    each sector tail, so the fixture must do it for real.
    """
    struct.pack_into("<HH", record, 4, usa_offset, usa_count)
    struct.pack_into("<H", record, usa_offset, 0x0001)
    for sector in range(1, usa_count):
        tail = sector * 512 - 2
        saved = bytes(record[tail:tail + 2])
        record[usa_offset + sector * 2: usa_offset + sector * 2 + 2] = saved
        struct.pack_into("<H", record, tail, 0x0001)


def _file_name_attr(name: str, parent: int, size: int, allocated: int, namespace: int = 1) -> bytes:
    """A resident ``$FILE_NAME`` attribute.

    Layout, and the reason the offsets below are not free-form:

    ==========  ====================================
    0 / 4       attribute type / total length
    8 / 9       non-resident flag / name length
    12 / 14     flags / attribute id
    **16 / 20** content size / content offset  <- resident header ends at 22
    ==========  ====================================

    ``$FILE_NAME`` content: parent, ctime, mtime, mft-mtime, atime, allocated,
    real size, flags, reparse, name length, namespace, then UTF-16LE name.
    """
    encoded = name.encode("utf-16-le")
    content = struct.pack(
        "<QQQQQQQIIBB",
        parent, 0, 0, 0, 0, allocated, size, 0, 0, len(name), namespace,
    ) + encoded
    content = content.ljust((len(content) + 7) // 8 * 8, b"\x00")

    header = struct.pack("<IIBBHHH", mft.ATTR_FILE_NAME, 22 + len(content), 0, 0, 0, 0, 0)
    header += struct.pack("<IH", len(content), 22)
    return header + content


def _data_attr(size: int, allocated: int) -> bytes:
    """A non-resident, unnamed ``$DATA`` attribute.

    The non-resident header is 64 bytes and the parser reads the sizes at fixed
    offsets 40 (allocated) and 48 (real), so the padding before them is load
    bearing, not cosmetic.
    """
    header = struct.pack("<IIBBHHH", mft.ATTR_DATA, 64, 1, 0, 0, 0, 0)
    header += struct.pack("<QQHH", 0, 0, 64, 0)          # VCNs, runs offset, unit
    header += b"\x00" * 4                                # padding -> offset 40
    header += struct.pack("<QQQ", allocated, size, size)  # 40 / 48 / 56
    assert len(header) == 64, len(header)
    return header


def build_record(index: int, *, name: str, parent: int, size: int, allocated: int,
                 is_dir: bool, namespace: int = 1) -> bytes:
    record = bytearray(RECORD_SIZE)
    record[0:4] = b"FILE"
    struct.pack_into("<H", record, 16, 1)             # sequence number
    struct.pack_into("<H", record, 18, 1)             # hard link count
    struct.pack_into("<H", record, 22, 0x01 | (0x02 if is_dir else 0))
    struct.pack_into("<I", record, 28, RECORD_SIZE)   # allocated size of record
    struct.pack_into("<I", record, 44, index)         # this record's number

    body = _file_name_attr(name, parent, size, allocated, namespace)
    if not is_dir:
        body += _data_attr(size, allocated)
    body += struct.pack("<II", mft.ATTR_END, 0)

    attr_offset = 56
    struct.pack_into("<H", record, 20, attr_offset)
    record[attr_offset:attr_offset + len(body)] = body
    struct.pack_into("<I", record, 24, attr_offset + len(body))

    _apply_fixups(record)
    return bytes(record)


def free_record(index: int) -> bytes:
    """A record that is allocated but not in use.

    Real NTFS keeps the ``FILE`` signature on free records and merely clears the
    in-use flag, so a fixture of zeroed bytes would let a parser that never
    checks the flag pass.  This mirrors the real layout.
    """
    record = bytearray(RECORD_SIZE)
    record[0:4] = b"FILE"
    struct.pack_into("<H", record, 22, 0)             # flag bit 0 clear
    struct.pack_into("<I", record, 28, RECORD_SIZE)
    struct.pack_into("<I", record, 44, index)
    return bytes(record)


@pytest.fixture
def mft_dump(tmp_path):
    """A tiny volume: root(5) -> docs(10), plus two files under docs."""
    records = [free_record(i) for i in range(12)]     # 0..11, mostly free
    records[5] = build_record(5, name=".", parent=5, size=0, allocated=0, is_dir=True)
    records[10] = build_record(10, name="docs", parent=5, size=0, allocated=0, is_dir=True)
    records[11] = build_record(11, name="readme.txt", parent=10, size=1000, allocated=4096,
                               is_dir=False)
    path = tmp_path / "vol.mft"
    path.write_bytes(b"".join(records))
    return str(path)


class TestMftParser:

    def test_record_size_is_detected(self, mft_dump):
        assert mft.detect_record_size(mft_dump) == RECORD_SIZE

    def test_a_non_mft_file_is_rejected(self, tmp_path):
        path = tmp_path / "not.mft"
        path.write_bytes(b"NOPE" + b"\x00" * 200)
        with pytest.raises(ValueError, match="not an"):
            mft.detect_record_size(str(path))

    def test_parses_names_parents_and_sizes(self, mft_dump):
        nodes = mft.parse_mft(mft_dump, workers=1)
        assert set(nodes) == {5, 10, 11}
        assert nodes[10].name == "docs"
        assert nodes[10].parent == 5
        assert nodes[10].is_dir is True
        assert nodes[11].name == "readme.txt"
        assert nodes[11].parent == 10
        assert nodes[11].size == 1000
        assert nodes[11].allocated == 4096
        assert nodes[11].is_dir is False

    def test_free_records_are_skipped(self, mft_dump):
        """Records that are not in use must not enter the index."""
        nodes = mft.parse_mft(mft_dump, workers=1)
        assert 0 not in nodes and 1 not in nodes and 9 not in nodes

    def test_parallel_and_serial_agree(self, mft_dump):
        """Sharding must not change the result: byte ranges are record-aligned."""
        serial = mft.parse_mft(mft_dump, workers=1)
        parallel = mft.parse_mft(mft_dump, workers=4)
        assert serial == parallel

    def test_fixups_are_applied(self, tmp_path):
        """A parser that ignores the update-sequence array reads wrong bytes.

        The fixture deliberately parks the real bytes at a sector tail, so a
        record whose name straddles offset 510 only decodes correctly once the
        array has been undone.
        """
        name = "x" * 240                       # pushes content well past 512
        records = [free_record(i) for i in range(12)]
        records[5] = build_record(5, name=".", parent=5, size=0, allocated=0, is_dir=True)
        records[10] = build_record(10, name=name, parent=5, size=7, allocated=4096, is_dir=False)
        path = tmp_path / "fixup.mft"
        path.write_bytes(b"".join(records))
        nodes = mft.parse_mft(str(path), workers=1)
        assert nodes[10].name == name

    def test_prefers_the_long_name_over_the_dos_alias(self, tmp_path):
        """A record can carry both a Win32 name and an 8.3 alias."""
        records = [free_record(i) for i in range(12)]
        records[5] = build_record(5, name=".", parent=5, size=0, allocated=0, is_dir=True)

        # DOS alias first, then the Win32 name -- the order NTFS really writes
        combined = bytearray(b"\x00" * RECORD_SIZE)
        combined[0:4] = b"FILE"
        struct.pack_into("<H", combined, 16, 1)
        struct.pack_into("<H", combined, 22, 0x03)
        struct.pack_into("<I", combined, 28, RECORD_SIZE)
        struct.pack_into("<I", combined, 44, 10)
        body = (_file_name_attr("PROGRA~1", 5, 0, 0, namespace=2)
                + _file_name_attr("Program Files", 5, 0, 0, namespace=1)
                + struct.pack("<II", mft.ATTR_END, 0))
        struct.pack_into("<H", combined, 20, 56)
        combined[56:56 + len(body)] = body
        struct.pack_into("<I", combined, 24, 56 + len(body))
        _apply_fixups(combined)
        records[10] = bytes(combined)

        path = tmp_path / "ns.mft"
        path.write_bytes(b"".join(records))
        nodes = mft.parse_mft(str(path), workers=1)
        assert nodes[10].name == "Program Files", "the 8.3 alias must not win"


class TestTreeHelpers:

    @pytest.fixture
    def nodes(self):
        return {
            5: mft.MftNode(".", 5, 0, 0, True),
            10: mft.MftNode("docs", 5, 0, 0, True),
            11: mft.MftNode("a.bin", 10, 1000, 4096, False),
            12: mft.MftNode("b.bin", 10, 500, 4096, False),
            13: mft.MftNode("sub", 10, 0, 0, True),
            14: mft.MftNode("c.bin", 13, 250, 4096, False),
        }

    def test_resolve_path_walks_the_parent_chain(self, nodes):
        assert mft.resolve_path(nodes, 14, "D:") == "D:\\docs\\sub\\c.bin"

    def test_resolve_path_survives_a_cycle(self):
        """A corrupt parent link must not hang the walk."""
        cyclic = {1: mft.MftNode("a", 2, 0, 0, True), 2: mft.MftNode("b", 1, 0, 0, True)}
        assert mft.resolve_path(cyclic, 1, "D:")   # returns something, does not loop

    def test_folder_totals_roll_up_the_whole_subtree(self, nodes):
        totals = mft.compute_folder_totals(nodes)
        assert totals[13] == (250, 4096)                    # sub/
        assert totals[10] == (1000 + 500 + 250, 4096 * 3)   # docs/ inclusive
        assert totals[5] == (1750, 4096 * 3)                # root inclusive
        assert 11 not in totals, "files are not folders"

    def test_iter_descendants_is_inclusive(self, nodes):
        assert set(mft.iter_descendants(nodes, 10)) == {10, 11, 12, 13, 14}
        assert set(mft.iter_descendants(nodes, 11)) == {11}


class TestFrnMasking:

    def test_mask_strips_the_sequence_number(self):
        """USN packs a reuse counter into the high 16 bits; WizTree does not."""
        assert (0x000100000037EE8D & mft.FRN_MASK) == 3665549
        assert (0x001B000000002E3F & mft.FRN_MASK) == 11839

    def test_a_real_frn_does_not_equal_its_record_number(self):
        assert 0x000100000037EE8D != 3665549


class TestLiveIndex:

    @pytest.fixture
    def index(self, tmp_path):
        nodes = {
            5: mft.MftNode(".", 5, 0, 0, True),
            10: mft.MftNode("docs", 5, 0, 0, True),
            11: mft.MftNode("a.bin", 10, 1000, 4096, False),
        }
        live = liveindex.LiveIndex(drive="D:", nodes=nodes, journal_id=0xABCD, checkpoint_usn=500)
        live.rebuild_children()
        return live

    def test_round_trips_through_disk(self, index, tmp_path):
        path = str(tmp_path / "idx.live")
        size = index.save(path)
        assert size > 0
        reloaded = liveindex.LiveIndex.load(path)
        assert reloaded.nodes == index.nodes
        assert reloaded.journal_id == index.journal_id
        assert reloaded.checkpoint_usn == index.checkpoint_usn
        assert reloaded.totals == index.totals
        # The child map is deliberately NOT built on load: it costs a full pass
        # over every entry, and a read-only query never needs it.
        assert reloaded.children == {}
        reloaded.ensure_children()
        assert reloaded.children == index.children

    def test_totals_round_trip_and_are_incremental(self, index, tmp_path):
        index.rebuild_totals()
        assert index.totals[10] == (1000, 4096)      # docs/ contains a.bin
        assert index.totals[5] == (1000, 4096)

        path = str(tmp_path / "idx.live")
        index.save(path)
        assert liveindex.LiveIndex.load(path).totals == index.totals

    def test_a_resize_updates_every_ancestor(self, index, monkeypatch):
        """Totals must move with the file, not just at the leaf."""
        index.rebuild_totals()
        monkeypatch.setattr(liveindex, "_stat_sizes", lambda _p: (5000, 8192))
        index.apply([
            liveindex.UsnRecord(frn=11, parent=10, usn=900,
                                reason=liveindex.REASON_DATA_EXTEND,
                                name="a.bin", is_dir=False)
        ])
        assert index.totals[10] == (5000, 8192)
        assert index.totals[5] == (5000, 8192), "the root must see it too"

    def test_a_delete_subtracts_from_ancestors(self, index):
        index.rebuild_totals()
        index.apply([
            liveindex.UsnRecord(frn=11, parent=10, usn=901,
                                reason=liveindex.REASON_FILE_DELETE,
                                name="a.bin", is_dir=False)
        ])
        assert index.totals[10] == (0, 0)
        assert index.totals[5] == (0, 0)

    def test_a_foreign_format_version_is_refused(self, index, tmp_path):
        import pickle

        path = tmp_path / "old.live"
        with open(path, "wb") as handle:
            pickle.dump({"version": 999}, handle)
        with pytest.raises(ValueError, match="rebuild"):
            liveindex.LiveIndex.load(str(path))

    def test_names_survive_a_newline_in_the_join(self, index, tmp_path):
        """The blob uses '\\n' as a separator, which NTFS forbids in names."""
        index.nodes[12] = mft.MftNode("a b c", 10, 1, 4096, False)
        index.rebuild_children()
        path = str(tmp_path / "idx.live")
        index.save(path)
        assert liveindex.LiveIndex.load(path).nodes[12].name == "a b c"

    def test_create_is_applied(self, index, monkeypatch):
        monkeypatch.setattr(liveindex, "_stat_sizes", lambda _path: (777, 4096))
        counters = index.apply([
            liveindex.UsnRecord(frn=12, parent=10, usn=501,
                                reason=liveindex.REASON_FILE_CREATE,
                                name="new.bin", is_dir=False)
        ])
        assert counters["created"] == 1
        assert index.nodes[12].size == 777
        assert 12 in index.children[10]

    def test_delete_removes_the_whole_subtree(self, index):
        liveindex  # keep the import used
        index.nodes[12] = mft.MftNode("sub", 10, 0, 0, True)
        index.nodes[13] = mft.MftNode("deep.bin", 12, 5, 4096, False)
        index.rebuild_children()

        counters = index.apply([
            liveindex.UsnRecord(frn=12, parent=10, usn=502,
                                reason=liveindex.REASON_FILE_DELETE,
                                name="sub", is_dir=True)
        ])
        assert counters["deleted"] == 1
        assert 12 not in index.nodes and 13 not in index.nodes, "children must go too"
        assert 12 not in index.children.get(10, set())

    def test_rename_updates_name_and_parent(self, index):
        counters = index.apply([
            liveindex.UsnRecord(frn=11, parent=5, usn=503,
                                reason=liveindex.REASON_RENAME_NEW_NAME,
                                name="moved.bin", is_dir=False)
        ])
        assert counters["renamed"] == 1
        assert index.nodes[11].name == "moved.bin"
        assert index.nodes[11].parent == 5
        assert index.path_of(11) == "D:\\moved.bin"

    def test_rename_old_name_is_not_a_separate_event(self, index):
        counters = index.apply([
            liveindex.UsnRecord(frn=11, parent=10, usn=504,
                                reason=liveindex.REASON_RENAME_OLD_NAME,
                                name="a.bin", is_dir=False)
        ])
        assert counters["renamed"] == 0 and counters["skipped"] == 1

    def test_a_create_whose_parent_is_unknown_is_counted(self, index, monkeypatch):
        """A hole in the index must be visible, not silently papered over."""
        monkeypatch.setattr(liveindex, "_stat_sizes", lambda _path: (1, 4096))
        counters = index.apply([
            liveindex.UsnRecord(frn=99, parent=987654, usn=505,
                                reason=liveindex.REASON_FILE_CREATE,
                                name="orphan.bin", is_dir=False)
        ])
        assert counters["orphans"] == 1

    def test_resize_is_picked_up(self, index, monkeypatch):
        monkeypatch.setattr(liveindex, "_stat_sizes", lambda _path: (4242, 8192))
        counters = index.apply([
            liveindex.UsnRecord(frn=11, parent=10, usn=506,
                                reason=liveindex.REASON_DATA_EXTEND,
                                name="a.bin", is_dir=False)
        ])
        assert counters["resized"] == 1
        assert index.nodes[11].size == 4242


class TestJournalGuards:
    """The cases where incremental sync must refuse rather than lie."""

    def test_a_wrapped_journal_raises_checkpoint_lost(self, monkeypatch):
        journal = liveindex.Journal(journal_id=1, first_usn=1000, next_usn=2000, maximum_size=1)
        monkeypatch.setattr(liveindex, "query_journal", lambda _d: journal)
        monkeypatch.setattr(
            liveindex, "_open_volume",
            lambda _d: pytest.fail("must not open the volume once the checkpoint is stale"),
        )
        with pytest.raises(liveindex.CheckpointLost) as exc:
            liveindex.read_journal_since("D:", checkpoint=500)
        assert exc.value.reason == "wrapped"

    def test_a_reset_journal_raises_on_sync(self, monkeypatch, tmp_path):
        index = liveindex.LiveIndex(drive="D:", nodes={5: mft.MftNode(".", 5, 0, 0, True)},
                                    journal_id=0x1234, checkpoint_usn=10)
        monkeypatch.setattr(liveindex, "query_journal",
                            lambda _d: liveindex.Journal(0x9999, 0, 100, 1))
        with pytest.raises(liveindex.CheckpointLost) as exc:
            index.sync()
        assert exc.value.reason == "reset"
