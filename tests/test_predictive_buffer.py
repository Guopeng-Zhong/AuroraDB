"""Result correctness and isolation of the optional scan-aware buffer policy."""
from pathlib import Path
import random
import tempfile
import unittest

from auroradb.storage import BPlusTree, DiskPager, HeapTable, StorageError


class PredictiveBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.pager = DiskPager(self.directory, cache_size=8)

    def tearDown(self):
        self.pager.close()
        self.temp.cleanup()

    def seed_pages(self, count=80):
        self.pager.begin()
        for i in range(count):
            self.pager.allocate({"kind": "heap", "rows": [{"v": {"id": i}}]})
        self.pager.commit()
        self.pager.clear_cache()

    def assert_cache_invariants(self):
        self.assertLessEqual(len(self.pager._cache), self.pager.cache_size)
        self.assertEqual(set(self.pager._cache_slots), set(self.pager._cache))
        self.assertEqual(len(self.pager._cache_slots), len(self.pager._cache_positions))
        for position, page in enumerate(self.pager._cache_slots):
            self.assertEqual(self.pager._cache_positions[page], position)
        self.assertLessEqual(len(self.pager._cache_history), self.pager.cache_size*4)

    def test_default_and_unknown_future_fallback_remain_lru(self):
        self.seed_pages()
        self.assertEqual(self.pager.cache_policy_status()["policy"], "lru")
        self.pager.set_cache_policy("predictive", sample_size=3)
        oracle = []
        for page in (0, 1, 0, 2, 3, 4, 5, 6, 7, 8, 3, 9, 10, 0):
            self.assertEqual(self.pager.read_page(page)["rows"][0]["v"]["id"], page)
            if page in oracle:
                oracle.remove(page)
            oracle.append(page)
            oracle = oracle[-8:]
            self.assertEqual(list(self.pager._cache), oracle)
            self.assert_cache_invariants()
        self.assertGreater(self.pager.stats()["predictive_fallbacks"], 0)
        self.assertEqual(self.pager.stats()["predictive_evictions"], 0)

    def test_close_releases_bookkeeping_for_held_scan_context(self):
        """Closing a pager must not retain a phantom scan registration."""
        context = self.pager.scan_context("held-while-closing")
        context.__enter__()
        self.assertEqual(self.pager.stats()["active_scans"], 1)
        self.pager.close()
        self.assertEqual(self.pager.stats()["active_scans"], 0)
        context.__exit__(None, None, None)

    def test_switch_and_clear_preserve_staged_and_durable_pages(self):
        self.seed_pages()
        for page in (0, 1, 0):
            self.pager.read_page(page)
        before_order = list(self.pager._cache)
        durable = (self.directory/"pages.dat").read_bytes()
        self.pager.begin()
        self.pager.write_page(0, {"kind": "heap", "rows": [{"v": {"id": 999}}]})
        self.pager.set_cache_policy("predictive", sample_size=4)
        self.assertEqual(list(self.pager._cache), before_order)
        training_before = self.pager.stats()["scan_page_accesses"]
        with self.pager.scan_context("staged_traversal") as scan:
            self.assertEqual(self.pager.read_page(0, scan=scan, next_page=1)["rows"][0]["v"]["id"], 999)
        self.assertEqual(self.pager.stats()["scan_page_accesses"], training_before)
        self.pager.clear_cache()
        self.assertEqual(self.pager.stats()["staged_pages"], 1)
        self.assertEqual(self.pager.cache_policy_status()["history_pages"], 0)
        self.assertEqual((self.directory/"pages.dat").read_bytes(), durable)
        self.pager.set_cache_policy("lru")
        self.pager.rollback()
        self.assertEqual(self.pager.read_page(0)["rows"][0]["v"]["id"], 0)
        for policy, sample in (("unknown", 8), ("predictive", 0), ("predictive", True), ("lru", 4097)):
            with self.assertRaises(StorageError):
                self.pager.set_cache_policy(policy, sample)
        self.assertEqual(self.pager.cache_policy_status()["policy"], "lru")

    def test_real_next_page_hint_is_retained_without_prefetch(self):
        self.seed_pages()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=2)
        self.pager.set_cache_policy("predictive", sample_size=16)
        for _ in range(10):
            self.pager.read_page(0)
        self.pager.read_page(1)
        before = self.pager.stats()["disk_reads"]
        with self.pager.scan_context("test_actual_traversal") as scan:
            self.pager.read_page(2, scan=scan, next_page=1)
            self.assertIn(1, self.pager._cache)
            self.assertEqual(self.pager.stats()["disk_reads"]-before, 1)
            self.assertEqual(self.pager.cache_policy_status()["active_scans"], 1)
        self.assertEqual(self.pager.cache_policy_status()["active_scans"], 0)
        self.assertGreater(self.pager.stats()["predictive_evictions"], 0)
        self.assert_cache_invariants()

    def test_predictive_read_still_checks_crc_before_training_or_admission(self):
        self.seed_pages()
        self.pager.close()
        data = self.directory/"pages.dat"
        image = bytearray(data.read_bytes())
        image[22] ^= 1
        data.write_bytes(image)
        self.pager = DiskPager(self.directory, cache_size=8)
        self.pager.set_cache_policy("predictive")
        with self.assertRaisesRegex(StorageError, "CRC"):
            self.pager.read_page(0)
        self.assertEqual(self.pager.stats()["cache_used"], 0)
        self.assertEqual(self.pager.stats()["disk_reads"], 0)
        self.assertEqual(self.pager.cache_policy_status()["history_pages"], 0)

    def test_scan_admission_keeps_hot_resident_but_returns_isolated_data(self):
        self.seed_pages()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=2)
        self.pager.set_cache_policy("predictive", sample_size=8)
        for _ in range(10):
            self.pager.read_page(0)
        self.pager.read_page(1)
        with self.pager.scan_context("cold_scan") as scan:
            page = self.pager.read_page(2, scan=scan, next_page=3)
            self.assertEqual(list(self.pager._cache), [0, 1])
            self.assertEqual(page["rows"][0]["v"]["id"], 2)
            page["rows"][0]["v"]["id"] = -1
            again = self.pager.read_page(2, scan=scan, next_page=3)
            self.assertEqual(again["rows"][0]["v"]["id"], 2)
        self.assertEqual(self.pager.stats()["predictive_admission_bypasses"], 2)
        self.assert_cache_invariants()

    def test_scan_admission_respects_another_scans_known_successor(self):
        self.seed_pages()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=2)
        self.pager.set_cache_policy("predictive", sample_size=8)
        for _ in range(10):
            self.pager.read_page(0)
        self.pager.read_page(1)
        with self.pager.scan_context("waiting") as waiting:
            self.pager.advance_scan(waiting, 2)
            with self.pager.scan_context("reading") as reading:
                self.pager.read_page(2, scan=reading, next_page=3)
                self.assertIn(2, self.pager._cache)
        self.assertEqual(self.pager.stats()["predictive_admission_bypasses"], 0)
        self.assert_cache_invariants()

    def test_next_page_hint_expires_when_its_scan_ends(self):
        self.seed_pages()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=2)
        self.pager.set_cache_policy("predictive", sample_size=16)
        for _ in range(10):
            self.pager.read_page(0)
        self.pager.read_page(1)
        with self.pager.scan_context("known_successor") as scan:
            self.pager.read_page(2, scan=scan, next_page=1)
            self.assertEqual(set(self.pager._cache), {0, 1})
        fallbacks = self.pager.stats()["predictive_fallbacks"]
        self.pager.read_page(3)
        # The old hint must not protect page 1 after its scan has finished.
        # With no sampled scan evidence, eviction follows LRU despite page 0's
        # high historical weight.
        self.assertEqual(list(self.pager._cache), [1, 3])
        self.assertEqual(self.pager.stats()["predictive_fallbacks"], fallbacks+1)
        self.assert_cache_invariants()

    def test_overlapping_scan_hints_have_independent_lifetimes(self):
        self.seed_pages()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=3)
        self.pager.set_cache_policy("predictive", sample_size=16)
        for _ in range(10):
            self.pager.read_page(0)
        self.pager.read_page(1)
        self.pager.read_page(2)
        with self.pager.scan_context("first") as first:
            self.pager.advance_scan(first, 1)
            with self.pager.scan_context("second") as second:
                self.pager.advance_scan(second, 2)
                self.pager.read_page(3)
                self.assertEqual(set(self.pager._cache), {0, 1, 2})
            self.pager.read_page(4)
            self.assertEqual(set(self.pager._cache), {0, 1, 4})
        self.assertEqual(self.pager.stats()["active_scans"], 0)
        self.assert_cache_invariants()

    def test_bounded_history_and_sample_array_under_mixed_trace(self):
        self.seed_pages(160)
        self.pager.set_cache_policy("predictive", sample_size=5)
        randomizer = random.Random(123)
        for phase in range(6):
            hot = phase*10
            for _ in range(30):
                self.pager.read_page(hot+randomizer.randrange(4))
            with self.pager.scan_context("mixed_scan") as scan:
                for page in range(160):
                    self.assertEqual(self.pager.read_page(page, scan=scan,
                                     next_page=page+1 if page+1 < 160 else None)["rows"][0]["v"]["id"], page)
                    if page % 10 == 0:
                        self.pager.read_page(hot+randomizer.randrange(4))
                    self.assert_cache_invariants()
        self.assertGreater(self.pager.stats()["predictive_samples"], 0)
        self.assertGreater(self.pager.stats()["cache_policy_cpu_ns"], 0)
        self.pager.clear_cache()
        self.assert_cache_invariants()

    def test_heap_index_duplicates_overflow_restart_and_early_close(self):
        self.pager.set_cache_policy("predictive", sample_size=4)
        self.pager.begin()
        table_meta = {"pages": []}
        index_meta = {"root": None, "unique": False}
        self.pager.allocate({"kind": "catalog", "table": table_meta, "index": index_meta})
        heap = HeapTable(self.pager, table_meta)
        tree = BPlusTree(self.pager, index_meta)
        expected = []
        for i in range(600):
            value = {"id": i, "key": i % 20, "note": "x"*180}
            rid = heap.insert(value)
            expected.append((rid, value))
            tree.insert(value["key"], rid)
        grown = dict(expected[0][1], note="z"*3000)
        heap.update(expected[0][0], grown)
        expected[0] = (expected[0][0], grown)
        self.pager.write_page(0, {"kind": "catalog", "table": table_meta, "index": index_meta})
        self.pager.commit()
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=8)
        self.pager.set_cache_policy("predictive", sample_size=4)
        catalog = self.pager.read_page(0)
        heap = HeapTable(self.pager, catalog["table"])
        tree = BPlusTree(self.pager, catalog["index"])
        self.assertEqual(list(heap.rows()), expected)
        self.assertEqual(tree.search(7), [rid for rid, value in expected if value["key"] == 7])
        want = sorted([(v["key"], rid) for rid, v in expected if 4 <= v["key"] <= 8], key=lambda x: (x[0], *x[1]))
        self.assertEqual(tree.range(4, 8), [rid for _, rid in want])
        self.assertEqual(tree.validate()["entries"], 600)
        self.assertGreater(self.pager.stats()["scan_page_accesses"], 0)
        for iterator in (heap.rows(), tree.items()):
            next(iterator)
            self.assertEqual(self.pager.cache_policy_status()["active_scans"], 1)
            iterator.close()
            self.assertEqual(self.pager.cache_policy_status()["active_scans"], 0)
        self.assert_cache_invariants()

    def test_predictive_commit_failure_keeps_wal_recoverable(self):
        self.seed_pages()
        self.pager.set_cache_policy("predictive", sample_size=4)
        self.pager.begin()
        self.pager.write_page(0, {"kind": "heap", "rows": [{"v": {"id": 1000}}]})
        def fail(phase, _):
            if phase == "after_wal_fsync":
                raise OSError("predictive-policy recovery boundary")
        self.pager._fault_hook = fail
        with self.assertRaises(StorageError):
            self.pager.commit()
        self.assertTrue(self.pager.poisoned)
        with self.assertRaises(StorageError):
            self.pager.set_cache_policy("lru")
        self.assertGreater((self.directory/"redo.wal").stat().st_size, 0)
        self.pager.close()
        self.pager = DiskPager(self.directory, cache_size=8)
        self.assertEqual(self.pager.cache_policy_status()["policy"], "lru")
        self.assertEqual(self.pager.stats()["wal_recoveries"], 1)
        self.assertEqual(self.pager.read_page(0)["rows"][0]["v"]["id"], 1000)


if __name__ == "__main__":
    unittest.main()
