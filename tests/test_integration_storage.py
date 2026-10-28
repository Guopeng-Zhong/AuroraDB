"""Engine/pager integration: catalog scalability, index atomicity and recovery."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from auroradb.engine import Database, DatabaseError
from auroradb.storage import BPlusTree, DiskPager, PAGE_SIZE, StorageError


class StorageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.db = Database(self.directory, cache_size=3)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = Database(self.directory, cache_size=3)

    def rows(self, sql):
        return self.db.execute(sql)[-1]["rows"]

    def seed(self):
        self.db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, tag TEXT UNIQUE, note TEXT);")
        values = ",".join(f"({i},'s{i}','{'x' * 80}')" for i in range(1, 121))
        self.db.execute("INSERT INTO students VALUES " + values)

    def test_catalog_scales_to_75_tables_and_survives_rollback_restart(self):
        self.db.execute("BEGIN")
        for i in range(75):
            self.db.execute(f"CREATE TABLE course_{i:03d} (id INTEGER PRIMARY KEY, title TEXT DEFAULT '数据库');")
        self.db.execute("INSERT INTO course_074 VALUES (1,'终端持久化')")
        self.db.execute("COMMIT")
        self.assertEqual(len(self.db.schema()), 75)
        manifest = self.db.pager.read_page(0)
        self.assertEqual(manifest["kind"], "catalog_root")
        self.assertGreater(len(manifest["pages"]), 3)
        self.reopen()
        self.assertEqual(len(self.db.schema()), 75)
        self.assertEqual(self.rows("SELECT * FROM course_074"), [[1, "终端持久化"]])
        committed_pages = self.db.pager.page_count
        self.db.execute("BEGIN; CREATE TABLE temporary_table (id INTEGER PRIMARY KEY); DROP TABLE course_000;")
        self.db.execute("ROLLBACK")
        self.assertEqual(len(self.db.schema()), 75)
        self.assertEqual(self.db.pager.page_count, committed_pages)
        self.reopen()
        self.assertEqual(len(self.db.schema()), 75)
        self.assertIn("course_000", [m["name"] for m in self.db.schema()])

    def test_index_rids_follow_growing_heap_update_after_restart(self):
        self.seed()
        before_meta = copy.deepcopy(self.db.catalog["tables"]["students"])
        primary = next(ix for ix in before_meta["indexes"] if ix["column"] == "id")
        before_rid = BPlusTree(self.db.pager, primary).search(1)[0]
        long_note = "z" * 3000
        self.db.execute(f"UPDATE students SET id=121, tag='renamed', note='{long_note}' WHERE id=1")
        self.assertEqual(self.rows("SELECT tag,note FROM students WHERE id=121"), [["renamed", long_note]])
        meta = self.db.catalog["tables"]["students"]
        primary = next(ix for ix in meta["indexes"] if ix["column"] == "id")
        tree = BPlusTree(self.db.pager, primary)
        self.assertEqual(tree.search(1), [])
        self.assertEqual(tree.search(121), [before_rid])
        self.assertIn("overflow", self.db.pager.read_page(before_rid[0])["rows"][before_rid[1]])
        for ix in meta["indexes"]:
            BPlusTree(self.db.pager, ix).validate()
        self.reopen()
        self.assertEqual(self.rows("SELECT id FROM students WHERE tag='renamed'"), [[121]])
        self.assertEqual(self.rows("SELECT id FROM students WHERE tag='s1'"), [])
        self.db.execute("DELETE FROM students WHERE id=121; INSERT INTO students VALUES (1,'s1','replacement');")
        self.assertEqual(self.rows("SELECT id,note FROM students WHERE tag='s1'"), [[1, "replacement"]])
        self.reopen()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM students"), [[120]])

    def test_failure_after_index_removal_rolls_back_every_explicit_write(self):
        self.seed()
        pages_before = self.db.pager.page_count
        self.db.execute("BEGIN; UPDATE students SET tag='first_changed' WHERE id=2")
        oversized_key = "x" * 1400
        with self.assertRaisesRegex(DatabaseError, "limit"):
            self.db.execute(f"UPDATE students SET id=200,tag='{oversized_key}' WHERE id=1")
        self.assertFalse(self.db.active)
        self.assertEqual(self.db.pager.page_count, pages_before)
        self.assertEqual(self.rows("SELECT id,tag FROM students WHERE id<=2 ORDER BY id"), [[1, "s1"], [2, "s2"]])
        for ix in self.db.catalog["tables"]["students"]["indexes"]:
            BPlusTree(self.db.pager, ix).validate()
        self.reopen()
        self.assertEqual(self.rows("SELECT id,tag FROM students WHERE id<=2 ORDER BY id"), [[1, "s1"], [2, "s2"]])

    def test_failed_heap_growth_reverts_primary_and_unique_indexes(self):
        self.seed()
        note = "x" * 6000
        with self.assertRaises(DatabaseError):
            self.db.execute(f"UPDATE students SET id=200,tag='changed',note='{note}' WHERE id=1")
        self.assertEqual(self.rows("SELECT id FROM students WHERE tag='s1'"), [[1]])
        self.assertEqual(self.rows("SELECT id FROM students WHERE id=200"), [])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM students"), [[120]])

    def test_commit_io_error_blocks_stale_catalog_reads_until_reopen(self):
        self.seed()
        self.db.execute("BEGIN; CREATE TABLE durable_new (id INTEGER PRIMARY KEY); INSERT INTO durable_new VALUES (99)")
        def fail(phase, _):
            if phase == "after_page_write":
                raise OSError("injected commit failure")
        self.db.pager._fault_hook = fail
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.execute("COMMIT")
        self.assertTrue(self.db.stats()["poisoned"])
        for sql in ("SHOW TABLES", "DESCRIBE students", "SELECT COUNT(*) FROM students", "BEGIN"):
            with self.assertRaisesRegex(DatabaseError, "recovery"):
                self.db.execute(sql)
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.schema()
        self.reopen()
        self.assertEqual(self.rows("SELECT * FROM durable_new"), [[99]])
        self.assertEqual(self.db.stats()["wal_recoveries"], 1)

    def test_durable_commit_memory_error_does_not_allow_stale_catalog_use(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        def fail_publish(page_id, value):
            raise MemoryError("injected cache deepcopy failure after durable commit")
        self.db.pager._cache_put = fail_publish
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.execute("INSERT INTO t VALUES (1)")
        self.assertTrue(self.db.stats()["poisoned"])
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.execute("SELECT COUNT(*) FROM t")
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.schema()
        self.assertGreater((self.directory / "redo.wal").stat().st_size, 0)
        self.reopen()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM t"), [[1]])
        self.assertEqual(self.db.catalog["tables"]["t"]["row_count"], 1)
        self.assertEqual(self.db.stats()["wal_recoveries"], 1)

    def test_post_publication_wal_cleanup_failure_requires_reopen(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        def fail_cleanup():
            raise MemoryError("injected non-I/O failure in post-commit WAL cleanup")
        # Unlike cache publication failure, the pager has already cleared its
        # staged set and transaction flag before this final helper is invoked.
        self.db.pager._clear_wal = fail_cleanup
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.execute("INSERT INTO t VALUES (1)")
        self.assertFalse(self.db.pager.in_transaction)
        self.assertTrue(self.db.pager.poisoned)
        self.assertGreater((self.directory / "redo.wal").stat().st_size, 0)
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.execute("SELECT COUNT(*) FROM t")
        with self.assertRaisesRegex(DatabaseError, "recovery"):
            self.db.schema()
        self.reopen()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM t"), [[1]])
        self.assertEqual(self.db.catalog["tables"]["t"]["row_count"], 1)
        self.assertEqual(self.db.stats()["wal_recoveries"], 1)

    def test_database_startup_crc_failure_releases_writer_lock(self):
        self.seed()
        chunk_id = self.db.pager.read_page(0)["pages"][0]
        self.db.close()
        path = self.directory / "pages.dat"
        original = path.read_bytes()
        damaged = bytearray(original)
        damaged[chunk_id * PAGE_SIZE + 30] ^= 1
        path.write_bytes(damaged)
        with self.assertRaises(StorageError):
            Database(self.directory)
        path.write_bytes(original)
        self.db = Database(self.directory)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM students"), [[120]])

    def test_legacy_catalog_migrates_atomically(self):
        self.db.close()
        # Use a separate empty directory: the v1 direct catalog was the earlier
        # interoperable format, and the first write should install chunked metadata.
        legacy = self.directory / "legacy"
        pager = DiskPager(legacy)
        pager.begin()
        pager.allocate({"kind": "catalog", "version": 1, "tables": {}})
        pager.commit()
        pager.close()
        self.db = Database(legacy)
        self.db.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        self.assertEqual(self.db.pager.read_page(0)["kind"], "catalog_root")
        self.db.close()
        self.db = Database(legacy)
        self.assertEqual([m["name"] for m in self.db.schema()], ["sample"])

    def test_process_crash_atomically_recovers_catalog_heap_and_index(self):
        self.seed()
        self.db.close()
        original = (self.directory / "pages.dat").read_bytes()
        project = Path(__file__).resolve().parents[1]
        script = """
import os, sys
from auroradb.engine import Database
db = Database(sys.argv[1], cache_size=2)
db.execute("BEGIN; CREATE TABLE crash_new (id INTEGER PRIMARY KEY, note TEXT UNIQUE); INSERT INTO crash_new VALUES (10,'new')")
db.execute(\"UPDATE students SET tag='durable_changed' WHERE id=1\")
def hook(phase, page):
    if phase == sys.argv[2]: os._exit(72)
db.pager._fault_hook = hook
db.execute('COMMIT')
"""
        for phase, committed in (("after_wal_page", False), ("after_wal_fsync", True), ("after_page_write", True)):
            with self.subTest(phase=phase):
                (self.directory / "pages.dat").write_bytes(original)
                (self.directory / "redo.wal").write_bytes(b"")
                process = subprocess.run([sys.executable, "-c", script, str(self.directory), phase],
                                         cwd=project, capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 72, process.stderr.decode(errors="replace"))
                self.db = Database(self.directory)
                names = [m["name"] for m in self.db.schema()]
                self.assertEqual("crash_new" in names, committed)
                tag = "durable_changed" if committed else "s1"
                self.assertEqual(self.rows(f"SELECT id FROM students WHERE tag='{tag}'"), [[1]])
                if committed:
                    self.assertEqual(self.rows("SELECT id FROM crash_new WHERE note='new'"), [[10]])
                for meta in self.db.catalog["tables"].values():
                    for ix in meta["indexes"]:
                        BPlusTree(self.db.pager, ix).validate()
                self.db.close()


if __name__ == "__main__":
    unittest.main()
