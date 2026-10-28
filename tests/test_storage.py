"""Storage invariants, crash recovery, and randomized B+ tree verification."""
import copy
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest

from auroradb.storage import BPlusTree, DiskPager, HeapTable, PAGE_SIZE, StorageError


class PagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.pager = DiskPager(self.directory, cache_size=2)

    def tearDown(self):
        self.pager.close()
        self.temp.cleanup()

    def reopen(self):
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=2)

    def seed(self):
        self.pager.begin()
        self.pager.allocate({"version": 1})
        self.pager.allocate({"value": "old"})
        self.pager.commit()

    def test_no_steal_copy_isolation_and_rollback(self):
        self.seed()
        before = (self.directory / "pages.dat").read_bytes()
        self.pager.begin()
        value = {"value": "uncommitted"}
        self.pager.write_page(1, value)
        value["value"] = "caller mutation"
        self.assertEqual(self.pager.read_page(1), {"value": "uncommitted"})
        self.pager.allocate({"new": True})
        read = self.pager.read_page(1)
        read["value"] = "read mutation"
        self.assertEqual(self.pager.read_page(1), {"value": "uncommitted"})
        self.assertEqual((self.directory / "pages.dat").read_bytes(), before)
        self.pager.rollback()
        self.assertEqual(self.pager.page_count, 2)
        self.assertIsNone(self.pager.read_page(2))
        self.assertEqual(self.pager.read_page(1), {"value": "old"})
        self.reopen()
        self.assertEqual(self.pager.read_page(0), {"version": 1})

    def test_transaction_and_capacity_guards(self):
        with self.assertRaises(StorageError):
            self.pager.allocate({})
        self.pager.begin()
        with self.assertRaises(StorageError):
            self.pager.begin()
        with self.assertRaises(StorageError):
            self.pager.allocate({"text": "x" * PAGE_SIZE})
        self.assertEqual(self.pager.page_count, 0)
        self.pager.allocate({"ok": True})
        with self.assertRaises(StorageError):
            self.pager.write_page(0, {"text": "x" * PAGE_SIZE})
        with self.assertRaises(StorageError):
            self.pager.write_page(4, {})
        self.assertEqual(self.pager.read_page(0), {"ok": True})
        self.pager.commit()

    def test_decoded_cache_page_keeps_nested_read_results_isolated(self):
        expected = {"rows": [{"name": "original", "tags": ["a", "b"]}]}
        self.pager.begin()
        self.pager.allocate(expected)
        self.pager.commit()
        self.reopen()
        first = self.pager.read_page(0)  # File miss, decoder owns the cache page.
        first["rows"][0]["name"] = "changed"
        first["rows"][0]["tags"].append("caller")
        second = self.pager.read_page(0)  # Cache hit must remain isolated too.
        self.assertEqual(second, expected)
        second["rows"].clear()
        self.assertEqual(self.pager.read_page(0), expected)
        self.reopen()
        self.assertEqual(self.pager.read_page(0), expected)

    def test_bounded_lru_cache(self):
        self.pager.begin()
        for i in range(8):
            self.pager.allocate({"i": i})
        self.pager.commit()
        self.reopen()
        for i in (0, 1, 0, 2):
            self.pager.read_page(i)
        stats = self.pager.stats()
        self.assertEqual(stats["cache_used"], 2)
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["disk_reads"], 3)
        self.assertEqual(stats["cache_evictions"], 1)
        self.assertEqual(list(self.pager._cache), [0, 2])

    def test_writer_lock(self):
        with self.assertRaisesRegex(StorageError, "already open"):
            DiskPager(self.directory)
        self.seed()  # A failed second open cannot release the first lock.
        with self.assertRaises(StorageError):
            DiskPager(self.directory)
        self.reopen()

    def test_crc_detects_payload_header_and_padding_damage(self):
        self.seed()
        self.pager.close()
        original = (self.directory / "pages.dat").read_bytes()
        for offset in (8, 12, 22, PAGE_SIZE - 1):
            with self.subTest(offset=offset):
                damaged = bytearray(original)
                damaged[offset] ^= 1
                (self.directory / "pages.dat").write_bytes(damaged)
                self.pager = DiskPager(self.directory)
                with self.assertRaises(StorageError):
                    self.pager.read_page(0)
                self.pager.close()
        (self.directory / "pages.dat").write_bytes(original)
        self.pager = DiskPager(self.directory)

    def test_commit_failure_poison_and_durable_recovery(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.write_page(1, {"value": "new"})
        self.pager.allocate({"appended": 3})
        def fail(phase, _):
            if phase == "after_page_write":
                raise OSError("simulated interruption")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.rollback()
        self.assertTrue(self.pager.stats()["poisoned"])
        with self.assertRaisesRegex(StorageError, "recovery"):
            self.pager.read_page(0)
        with self.assertRaises(StorageError):
            self.pager.begin()
        self.reopen()
        self.assertEqual(self.pager.stats()["wal_recoveries"], 1)
        self.assertEqual(self.pager.read_page(0), {"version": 2})
        self.assertEqual(self.pager.read_page(1), {"value": "new"})
        self.assertEqual(self.pager.read_page(2), {"appended": 3})

    def test_durable_commit_cache_publish_failure_is_poisoned_and_recoverable(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.allocate({"appended": True})
        def fail_publish(page_id, value):
            raise MemoryError("injected allocation failure in cache publication")
        self.pager._cache_put = fail_publish
        with self.assertRaisesRegex(StorageError, "recovery"):
            self.pager.commit()
        self.assertTrue(self.pager.poisoned)
        self.assertGreater((self.directory / "redo.wal").stat().st_size, 0)
        self.pager.rollback()
        with self.assertRaisesRegex(StorageError, "recovery"):
            self.pager.read_page(0)
        self.reopen()
        self.assertEqual(self.pager.read_page(0), {"version": 2})
        self.assertEqual(self.pager.read_page(2), {"appended": True})
        self.assertEqual(self.pager.stats()["wal_recoveries"], 1)

    def test_commit_interrupt_is_poisoned_without_swallowing_interrupt(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        def interrupted(phase, _):
            if phase == "after_data_fsync":
                raise KeyboardInterrupt("injected cancellation")
        self.pager._fault_hook = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.pager.commit()
        self.assertTrue(self.pager.poisoned)
        with self.assertRaisesRegex(StorageError, "recovery"):
            self.pager.read_page(0)
        self.reopen()
        self.assertEqual(self.pager.read_page(0), {"version": 2})

    def test_committed_wal_with_any_trailing_frame_preserves_every_file(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        def fail(phase, _):
            if phase == "after_wal_fsync":
                raise OSError("stop after durable commit before data writes")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        committed = wal_path.read_bytes()
        unchanged_data = (self.directory / "pages.dat").read_bytes()
        image = unchanged_data[:PAGE_SIZE]
        tails = {
            "one byte": b"x",
            "partial frame header": b"\x10\x00\x00",
            "partial frame body": struct.pack("<II", 100, 0) + b"abc",
            "CRC damaged frame": struct.pack("<II", 1, 0) + b"X",
            "unknown complete frame": DiskPager._frame(b"X"),
            "extra complete page frame": DiskPager._frame(b"P" + struct.pack("<Q", 0) + image),
            "extra complete begin frame": DiskPager._frame(b"B" + struct.pack("<QQI", 2, 2, 1)),
            "extra complete commit frame": committed[-53:],
        }
        for label, tail in tails.items():
            with self.subTest(tail=label):
                damaged = committed + tail
                wal_path.write_bytes(damaged)
                with self.assertRaisesRegex(StorageError, "trailing bytes"):
                    DiskPager(self.directory)
                self.assertEqual(wal_path.read_bytes(), damaged)
                self.assertEqual((self.directory / "pages.dat").read_bytes(), unchanged_data)
        wal_path.write_bytes(committed)
        self.pager = DiskPager(self.directory)
        self.assertEqual(self.pager.read_page(0), {"version": 2})
        self.assertEqual(self.pager.stats()["wal_recoveries"], 1)

    def test_commit_evidence_survives_prefix_damage_plus_trailing_bytes(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        def fail(phase, _):
            if phase == "after_wal_fsync":
                raise OSError("stop after durable WAL before data")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        committed = wal_path.read_bytes()
        unchanged = (self.directory / "pages.dat").read_bytes()
        variants = {}
        broken_magic = bytearray(committed)
        broken_magic[0] ^= 1
        variants["magic"] = bytes(broken_magic)
        broken_begin_length = bytearray(committed)
        broken_begin_length[8:12] = struct.pack("<I", 0xFFFFFFFF)
        variants["begin frame length"] = bytes(broken_begin_length)
        broken_page_length = bytearray(committed)
        # Magic (8) plus framed B (8 + 21) puts the first P frame at byte 37.
        broken_page_length[37:41] = struct.pack("<I", 0xFFFFFFFF)
        variants["page frame length"] = bytes(broken_page_length)
        for label, prefix in variants.items():
            with self.subTest(prefix=label):
                damaged = prefix + b"x"
                wal_path.write_bytes(damaged)
                with self.assertRaisesRegex(StorageError, "Committed WAL is corrupt"):
                    DiskPager(self.directory)
                self.assertEqual(wal_path.read_bytes(), damaged)
                self.assertEqual((self.directory / "pages.dat").read_bytes(), unchanged)
        # Finding C alone is never enough to replay. The damaged versions above
        # preserve the old data, and only restoring the fully valid WAL redoes it.
        wal_path.write_bytes(committed)
        self.pager = DiskPager(self.directory)
        self.assertEqual(self.pager.read_page(0), {"version": 2})

    def test_commit_evidence_survives_damaged_commit_length_after_partial_data(self):
        """A torn C-frame header must not discard a WAL after partial writes."""
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.write_page(1, {"value": "new"})

        def fail(phase, page_id):
            if phase == "after_page_write" and page_id == 0:
                raise OSError("stop after first data page")

        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        wal = bytearray(wal_path.read_bytes())
        offset = len(b"AURWAL1\0")
        while offset + 8 <= len(wal):
            length, _ = struct.unpack_from("<II", wal, offset)
            body = offset + 8
            if wal[body:body + 1] == b"C":
                # Preserve the body/checksum but damage only the C-frame
                # length. A recovery scanner must retain the evidence.
                wal[offset:offset + 4] = struct.pack("<I", 0)
                break
            offset = body + length
        else:
            self.fail("complete commit frame not found")
        wal_path.write_bytes(wal)
        data_before = (self.directory / "pages.dat").read_bytes()
        with self.assertRaisesRegex(StorageError, "Committed WAL is corrupt"):
            DiskPager(self.directory)
        self.assertEqual(wal_path.read_bytes(), wal)
        self.assertEqual((self.directory / "pages.dat").read_bytes(), data_before)

    def test_commit_evidence_survives_damaged_length_and_tag_after_partial_data(self):
        """A damaged C tag plus length still preserves a complete page prefix."""
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.write_page(1, {"value": "new"})

        def fail(phase, page_id):
            if phase == "after_page_write" and page_id == 0:
                raise OSError("stop after first data page")

        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        wal = bytearray(wal_path.read_bytes())
        offset = len(b"AURWAL1\0")
        while offset + 8 <= len(wal):
            length, _ = struct.unpack_from("<II", wal, offset)
            body = offset + 8
            if wal[body:body + 1] == b"C":
                wal[offset:offset + 4] = struct.pack("<I", 0)
                wal[body] = ord("X")
                break
            offset = body + length
        else:
            self.fail("complete commit frame not found")
        wal_path.write_bytes(wal)
        data_before = (self.directory / "pages.dat").read_bytes()
        with self.assertRaisesRegex(StorageError, "Committed WAL"):
            DiskPager(self.directory)
        self.assertEqual(wal_path.read_bytes(), wal)
        self.assertEqual((self.directory / "pages.dat").read_bytes(), data_before)

    def test_corrupt_committed_wal_preserves_evidence_and_data(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        def fail(phase, _):
            if phase == "after_wal_fsync":
                raise OSError("crash before data")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        wal = bytearray(wal_path.read_bytes())
        wal[80] ^= 1
        wal_path.write_bytes(wal)
        data_before = (self.directory / "pages.dat").read_bytes()
        with self.assertRaisesRegex(StorageError, "CRC"):
            DiskPager(self.directory)
        self.assertEqual(wal_path.read_bytes(), wal)
        self.assertEqual((self.directory / "pages.dat").read_bytes(), data_before)

    def test_complete_unknown_commit_frame_and_header_only_tail_are_preserved(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.write_page(1, {"value": "new"})
        def fail(phase, page_id):
            if phase == "after_page_write" and page_id == 0:
                raise OSError("stop after first published page")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError): self.pager.commit()
        self.pager.close()
        wal_path = self.directory / "redo.wal"
        committed = wal_path.read_bytes()
        offset = len(b"AURWAL1\0")
        while offset + 8 <= len(committed):
            length, _ = struct.unpack_from("<II", committed, offset)
            body = committed[offset + 8:offset + 8 + length]
            if body[:1] == b"C": break
            offset += 8 + length
        else: self.fail("complete commit frame not found")
        unchanged = (self.directory / "pages.dat").read_bytes()
        for tail in (DiskPager._frame(b"X" + body[1:]), struct.pack("<II", 0, 0)):
            damaged = committed[:offset] + tail
            wal_path.write_bytes(damaged)
            with self.subTest(tail_bytes=len(tail)):
                with self.assertRaisesRegex(StorageError, "damaged commit evidence"):
                    DiskPager(self.directory)
                self.assertEqual(wal_path.read_bytes(), damaged)
                self.assertEqual((self.directory / "pages.dat").read_bytes(), unchanged)
        wal_path.write_bytes(committed)
        self.pager = DiskPager(self.directory)
        self.assertEqual(self.pager.read_page(0), {"version": 2})
        self.assertEqual(self.pager.read_page(1), {"value": "new"})

    def test_actual_process_crash_boundaries(self):
        self.seed()
        self.pager.close()
        original = (self.directory / "pages.dat").read_bytes()
        root = Path(__file__).resolve().parents[1]
        script = """
import os, sys
from auroradb.storage import DiskPager
p = DiskPager(sys.argv[1])
p.begin()
p.write_page(0, {'version': 2})
p.write_page(1, {'value': 'new'})
p.allocate({'appended': 3})
def hook(phase, page):
    if phase == sys.argv[2]:
        os._exit(71)
p._fault_hook = hook
p.commit()
"""
        for phase, committed in (("after_wal_begin", False), ("after_wal_page", False),
                                 ("after_wal_fsync", True), ("after_page_write", True),
                                 ("after_data_fsync", True)):
            with self.subTest(phase=phase):
                (self.directory / "pages.dat").write_bytes(original)
                (self.directory / "redo.wal").write_bytes(b"")
                result = subprocess.run([sys.executable, "-c", script, str(self.directory), phase],
                                        cwd=root, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 71, result.stderr.decode(errors="replace"))
                self.pager = DiskPager(self.directory)
                self.assertEqual(self.pager.read_page(0), {"version": 2 if committed else 1})
                self.assertEqual(self.pager.read_page(1), {"value": "new" if committed else "old"})
                self.assertEqual(self.pager.page_count, 3 if committed else 2)
                if committed:
                    self.assertEqual(self.pager.read_page(2), {"appended": 3})
                self.pager.close()

    def test_torn_append_repaired_by_complete_wal(self):
        self.seed()
        self.pager.begin()
        self.pager.write_page(0, {"version": 2})
        self.pager.allocate({"appended": True})
        def fail(phase, _):
            if phase == "after_wal_fsync":
                raise OSError("crash before data")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.pager.close()
        with open(self.directory / "pages.dat", "ab") as data:
            data.write(b"torn append")
        self.pager = DiskPager(self.directory)
        self.assertEqual(self.pager.page_count, 3)
        self.assertEqual(self.pager.read_page(2), {"appended": True})


class HeapAndIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.pager = DiskPager(self.temp.name, cache_size=3)
        self.pager.begin()
        self.pager.allocate({"tables": {}})

    def tearDown(self):
        self.pager.close()
        self.temp.cleanup()

    def table(self):
        return HeapTable(self.pager, {"columns": [], "pages": [], "next_rid": 1, "row_count": 0, "indexes": []})

    def test_heap_paging_growth_forwarding_delete_and_reopen(self):
        table = self.table()
        expected = {}
        for i in range(160):
            values = {"id": i, "name": "学生" + str(i), "note": "x" * 70}
            rid = table.insert(values)
            expected[tuple(rid)] = values
        self.assertGreater(len(table.meta["pages"]), 2)
        rid = list(next(iter(expected)))
        larger = {"id": 0, "name": "改后", "note": "z" * 3000}
        table.update(rid, larger)
        expected[tuple(rid)] = larger
        self.assertIn("overflow", self.pager.read_page(rid[0])["rows"][rid[1]])
        self.assertEqual(table.get(rid), larger)
        table.update(rid, {"id": 0, "name": "短", "note": "z" * 3200})
        expected[tuple(rid)] = {"id": 0, "name": "短", "note": "z" * 3200}
        deleted = list(expected)[40]
        table.delete(list(deleted))
        del expected[deleted]
        self.assertIsNone(table.get(deleted))
        self.assertEqual(table.meta["row_count"], 159)
        self.pager.write_page(0, {"table": table.meta})
        self.pager.commit()
        self.pager.close()
        self.pager = DiskPager(self.temp.name, cache_size=3)
        table = HeapTable(self.pager, self.pager.read_page(0)["table"])
        self.assertEqual({tuple(r): v for r, v in table.rows()}, expected)
        self.assertEqual(table.get(rid), expected[tuple(rid)])

    def test_heap_oversize_update_is_atomic_within_transaction(self):
        table = self.table()
        rid = table.insert({"id": 1, "name": "original"})
        pages = self.pager.page_count
        with self.assertRaises(StorageError):
            table.update(rid, {"name": "x" * 10000})
        self.assertEqual(table.get(rid), {"id": 1, "name": "original"})
        self.assertEqual(pages, self.pager.page_count)
        with self.assertRaises(StorageError):
            table.insert({"name": "x" * 10000})
        self.assertEqual(table.meta["row_count"], 1)

    def test_heap_append_reads_scale_linearly_and_deleted_slots_are_reused(self):
        table = self.table()
        start = self.pager.stats()["staged_hits"]
        rids = [table.insert({"id": i, "note": "x" * 50}) for i in range(1000)]
        reads = self.pager.stats()["staged_hits"] - start
        # Scanning every preceding full page grows quadratically. The insertion
        # hint inspects one append page and, occasionally, allocates its successor.
        self.assertLess(reads, 1200)
        self.assertGreater(len(table.meta["pages"]), 10)
        old_rid = rids[10]
        table.delete(old_rid)
        next_rid = table.insert({"id": 1001, "note": "small"})
        self.assertEqual(next_rid, old_rid)
        self.assertEqual(table.meta["row_count"], 1000)

    def test_heap_variable_width_holes_remain_available_for_small_rows(self):
        table = self.table()
        rids = [table.insert({"id": i, "note": "x" * 50}) for i in range(160)]
        old_rid = rids[5]
        table.delete(old_rid)
        large_rid = table.insert({"id": 200, "note": "z" * 2500})
        self.assertNotEqual(large_rid, old_rid)
        self.assertIn(old_rid[0], table.meta["reusable_pages"])
        small_rid = table.insert({"id": 201, "note": "short"})
        self.assertEqual(small_rid, old_rid)
        self.assertEqual(table.get(large_rid)["id"], 200)

    def test_bplus_random_duplicates_ranges_delete_and_restart(self):
        meta = {"name": "idx", "column": "key", "root": None, "unique": False}
        tree = BPlusTree(self.pager, meta)
        rng = random.Random(504)
        entries = [(rng.randrange(-70, 71), [i // 17 + 1, i % 17]) for i in range(2200)]
        rng.shuffle(entries)
        for key, rid in entries:
            tree.insert(key, rid)
        # Same composite insert is idempotent; SQL duplicate keys remain indexed.
        tree.insert(*entries[0])
        expected = sorted(entries, key=lambda item: (item[0], *item[1]))
        self.assertEqual(list(tree.items()), expected)
        self.assertEqual(tree.validate()["entries"], len(entries))
        self.assertGreater(tree.validate()["leaves"], 5)
        for key in range(-80, 81, 7):
            self.assertEqual(tree.search(key), [rid for k, rid in expected if k == key])
        for low, high, inc_low, inc_high in ((None, None, True, True), (-30, 30, True, True),
                                           (-30, 30, False, False), (0, 0, True, True),
                                           (0, 0, False, True), (80, 70, True, True),
                                           (None, -20, True, False), (20, None, False, True)):
            result = [rid for key, rid in expected if (low is None or key > low or (inc_low and key == low)) and
                      (high is None or key < high or (inc_high and key == high))]
            self.assertEqual(tree.range(low, high, inc_low, inc_high), result)
        removed = entries[:1600]
        for key, rid in removed:
            tree.remove(key, rid)
        expected = sorted(entries[1600:], key=lambda item: (item[0], *item[1]))
        self.assertEqual(list(tree.items()), expected)
        tree.validate()
        self.pager.write_page(0, {"index": meta})
        self.pager.commit()
        self.pager.close()
        self.pager = DiskPager(self.temp.name, cache_size=2)
        tree = BPlusTree(self.pager, self.pager.read_page(0)["index"])
        self.assertEqual(list(tree.items()), expected)
        tree.validate()
        self.pager.begin()
        for key, rid in removed:
            tree.insert(key, rid)
        self.assertEqual(list(tree.items()), sorted(entries, key=lambda item: (item[0], *item[1])))
        tree.validate()

    def test_bplus_multilevel_variable_text_keys(self):
        meta = {"name": "idx", "root": None, "unique": False}
        tree = BPlusTree(self.pager, meta)
        entries = [(f"{i:05d}-" + "大" * (100 + i % 50), [i + 1, 0]) for i in range(300)]
        shuffled = entries[:]
        random.Random(901).shuffle(shuffled)
        for key, rid in shuffled:
            tree.insert(key, rid)
        self.assertEqual(list(tree.items()), entries)
        status = tree.validate()
        self.assertGreaterEqual(status["height"], 3)
        for key, rid in entries[::11]:
            self.assertEqual(tree.search(key), [rid])
        for key, rid in entries[:200]:
            tree.remove(key, rid)
        self.assertEqual(list(tree.items()), entries[200:])
        tree.validate()

    def test_unique_null_and_oversize_keys(self):
        meta = {"name": "unique_name", "root": None, "unique": True}
        tree = BPlusTree(self.pager, meta)
        tree.insert(None, [1, 0])
        self.assertEqual(tree.search(None), [])
        tree.insert("alice", [1, 0])
        tree.insert("alice", [1, 0])
        before = copy.deepcopy(self.pager._staged)
        with self.assertRaisesRegex(StorageError, "Duplicate key"):
            tree.insert("alice", [2, 0])
        with self.assertRaisesRegex(StorageError, "limit"):
            tree.insert("x" * 2000, [2, 0])
        with self.assertRaises(StorageError):
            tree.insert(float("nan"), [2, 0])
        self.assertEqual(self.pager._staged, before)
        self.assertEqual(list(tree.items()), [("alice", [1, 0])])

    def test_all_equal_keys_span_linked_leaves(self):
        tree = BPlusTree(self.pager, {"root": None, "unique": False})
        rids = [[i + 1, 0] for i in range(1000)]
        for rid in reversed(rids):
            tree.insert(42, rid)
        self.assertEqual(tree.search(42), rids)
        self.assertEqual(tree.range(42, 42), rids)
        tree.validate()
        for rid in rids:
            tree.remove(42, rid)
        self.assertEqual(tree.search(42), [])
        tree.validate()
        for rid in rids[::3]:
            tree.insert(42, rid)
        self.assertEqual(tree.search(42), rids[::3])
        tree.validate()


if __name__ == "__main__":
    unittest.main()
