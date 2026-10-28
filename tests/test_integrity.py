"""Read-only whole-database diagnostics, including CRC-valid logical damage."""
import copy
from pathlib import Path
import tempfile
import unittest

from auroradb.engine import Database
from auroradb.integrity import check_database
from auroradb.storage import BPlusTree, PAGE_SIZE, StorageError


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.db = Database(self.directory, cache_size=3)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def seed(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, tag TEXT UNIQUE, note TEXT, enabled BOOLEAN DEFAULT 0)")
        self.db.execute("INSERT INTO t (id,tag,note) VALUES (1,'one','a'),(2,NULL,'b'),(3,NULL,'c')")

    def codes(self, result):
        return {issue["code"] for issue in result["issues"]}

    def index(self):
        return self.db.catalog["tables"]["t"]["indexes"][0]

    def change_leaf(self, transform):
        self.db.execute("BEGIN")
        root = self.index()["root"]
        page = self.db.pager.read_page(root)
        transform(page["entries"])
        self.db.pager.write_page(root, page)
        self.db.execute("COMMIT")

    def test_empty_and_normal_database_have_complete_matching_counts(self):
        empty = check_database(self.db)
        self.assertTrue(empty["ok"], empty)
        self.assertEqual(empty["summary"]["rows"], 0)
        self.seed()
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["view"], "committed")
        self.assertEqual(result["summary"]["rows"], 3)
        self.assertEqual(result["summary"]["indexes"], 2)
        self.assertEqual(result["summary"]["checked_pages"], self.db.pager.page_count)
        self.assertEqual(result["summary"]["crc_checked_pages"], self.db.pager.page_count)
        self.assertEqual(result["tables"][0]["indexes"][1]["entries"], 1)

    def test_check_does_not_change_files_cache_catalog_or_transaction(self):
        self.seed()
        self.db.execute("BEGIN; INSERT INTO t (id,tag,note) VALUES (4,'four','staged')")
        before = (copy.deepcopy(self.db.catalog), copy.deepcopy(self.db.pager._staged),
                  copy.deepcopy(self.db.pager._cache), self.db.stats(),
                  (self.directory / "pages.dat").read_bytes(), (self.directory / "redo.wal").read_bytes())
        result = check_database(self.db)
        after = (copy.deepcopy(self.db.catalog), copy.deepcopy(self.db.pager._staged),
                 copy.deepcopy(self.db.pager._cache), self.db.stats(),
                 (self.directory / "pages.dat").read_bytes(), (self.directory / "redo.wal").read_bytes())
        self.assertEqual(before, after)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["view"], "current transaction")
        self.assertEqual(result["summary"]["rows"], 4)
        self.assertLess(result["summary"]["crc_checked_pages"], result["summary"]["checked_pages"])
        self.db.execute("ROLLBACK")
        self.assertTrue(check_database(self.db)["ok"])
        self.assertEqual(check_database(self.db)["summary"]["rows"], 3)

    def test_crc_damage_is_detected_even_when_page_is_warm_in_cache(self):
        self.seed()
        page_id = self.db.catalog["tables"]["t"]["pages"][0]
        self.db.pager.read_page(page_id)
        original = (self.directory / "pages.dat").read_bytes()
        damaged = bytearray(original)
        damaged[page_id * PAGE_SIZE + 50] ^= 1
        (self.directory / "pages.dat").write_bytes(damaged)
        try:
            result = check_database(self.db)
            self.assertFalse(result["ok"])
            self.assertIn("PAGE_CRC_OR_READ_ERROR", self.codes(result))
            self.assertEqual(next(i for i in result["issues"] if i["code"] == "PAGE_CRC_OR_READ_ERROR")["page"], page_id)
        finally:
            (self.directory / "pages.dat").write_bytes(original)

    def test_missing_index_entry_with_valid_page_crc_is_reported(self):
        self.seed()
        self.change_leaf(lambda entries: entries.pop(0))
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INDEX_MISSING_ENTRIES", self.codes(result))
        self.assertNotIn("PAGE_CRC_OR_READ_ERROR", self.codes(result))

    def test_extra_dangling_index_entry_is_reported(self):
        self.seed()
        page_id = self.db.catalog["tables"]["t"]["pages"][0]
        self.change_leaf(lambda entries: entries.append([4, [page_id, 99]]))
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INDEX_DANGLING_RID", self.codes(result))
        self.assertIn("INDEX_EXTRA_ENTRIES", self.codes(result))

    def test_wrong_index_key_reports_both_missing_and_extra(self):
        self.seed()
        self.change_leaf(lambda entries: entries[0].__setitem__(0, 0))
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INDEX_WRONG_KEY", self.codes(result))
        self.assertIn("INDEX_MISSING_ENTRIES", self.codes(result))
        self.assertIn("INDEX_EXTRA_ENTRIES", self.codes(result))

    def test_catalog_row_count_mismatch_and_missing_constraint_index(self):
        self.seed()
        self.db.execute("BEGIN")
        meta = self.db.catalog["tables"]["t"]
        meta["row_count"] += 8
        meta["indexes"].pop(0)
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("ROW_COUNT_MISMATCH", self.codes(result))
        self.assertIn("MISSING_CONSTRAINT_INDEX", self.codes(result))
        self.db.execute("ROLLBACK")
        self.assertTrue(check_database(self.db)["ok"])

    def test_drop_retained_pages_are_informational(self):
        self.seed()
        self.db.execute("DROP TABLE t")
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        self.assertGreater(result["summary"]["retained_pages"], 0)
        self.assertIn("RETAINED_ALLOCATED_PAGES", self.codes(result))
        self.assertEqual(result["error_count"], 0)

    def test_forwarding_large_update_and_old_overflow_are_valid(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, note TEXT)")
        self.db.execute("INSERT INTO t VALUES " + ",".join(f"({i},'{'x' * 80}')" for i in range(120)))
        self.db.execute("UPDATE t SET note='" + "z" * 3000 + "' WHERE id=0")
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tables"][0]["overflow_pages"], 1)
        self.db.execute("UPDATE t SET note='small' WHERE id=0")
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        self.assertGreaterEqual(result["summary"]["retained_pages"], 1)

    def test_duplicate_keys_across_leaves_and_lazy_deletion_are_valid(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, tag TEXT)")
        key = "same" * 20
        self.db.execute("INSERT INTO t VALUES " + ",".join(f"({i},'{key}')" for i in range(250)))
        self.db.execute("CREATE INDEX ix_tag ON t(tag)")
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        ix = next(i for i in result["tables"][0]["indexes"] if i["name"] == "ix_tag")
        self.assertGreater(ix["leaves"], 1)
        self.assertEqual(ix["entries"], 250)
        self.db.execute("DELETE FROM t WHERE id<240")
        result = check_database(self.db)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["rows"], 10)

    def test_heap_field_type_null_and_unique_violations_are_detected(self):
        self.seed()
        self.db.execute("BEGIN")
        page_id = self.db.catalog["tables"]["t"]["pages"][0]
        page = self.db.pager.read_page(page_id)
        page["rows"][0]["v"]["id"] = None
        page["rows"][1]["v"]["enabled"] = "bad"
        page["rows"][1]["v"]["tag"] = "one"
        page["rows"][2]["v"]["extra"] = 1
        self.db.pager.write_page(page_id, page)
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("ROW_TYPE_OR_NULL_CONSTRAINT", self.codes(result))
        self.assertIn("ROW_SCHEMA_MISMATCH", self.codes(result))
        self.assertIn("UNIQUE_HEAP_DUPLICATE", self.codes(result))

    def test_missing_root_does_not_allocate_and_cycle_does_not_hang(self):
        self.seed()
        self.db.execute("BEGIN")
        pages_before = self.db.pager.page_count
        self.index()["root"] = None
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_INDEX_METADATA", self.codes(result))
        self.assertEqual(self.db.pager.page_count, pages_before)
        self.db.execute("ROLLBACK; BEGIN")
        root = self.index()["root"]
        self.db.pager.write_page(root, {"kind": "bplus_internal", "keys": [], "children": [root]})
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INDEX_NODE_REUSED", self.codes(result))

    def test_duplicate_heap_reference_and_forwarding_to_wrong_page(self):
        self.seed()
        self.db.execute("BEGIN")
        meta = self.db.catalog["tables"]["t"]
        page_id = meta["pages"][0]
        meta["pages"].append(page_id)
        page = self.db.pager.read_page(page_id)
        page["rows"][0] = {"overflow": self.index()["root"]}
        self.db.pager.write_page(page_id, page)
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("DUPLICATE_HEAP_PAGE", self.codes(result))
        self.assertIn("INVALID_FORWARDING", self.codes(result))
        self.assertIn("PAGE_MULTIPLE_OWNERS", self.codes(result))

    def test_issue_cap_never_hides_failure_or_changes_real_count(self):
        self.seed()
        self.db.execute("BEGIN")
        meta = self.db.catalog["tables"]["t"]
        meta["row_count"] = -1
        meta["pages"].extend([-1, -2, -3, -4])
        full = check_database(self.db)
        limited = check_database(self.db, max_issues=1)
        self.assertFalse(limited["ok"])
        self.assertTrue(limited["issues_truncated"])
        self.assertEqual(len(limited["issues"]), 1)
        self.assertEqual(limited["issue_count"], full["issue_count"])
        self.assertEqual(limited["error_count"], full["error_count"])

    def test_malformed_reference_values_are_reported_without_crashing(self):
        self.seed()
        self.db.execute("BEGIN")
        meta = self.db.catalog["tables"]["t"]
        meta["pages"].append([])
        meta["insert_hint"] = []
        meta["reusable_pages"] = [[]]
        root = self.index()["root"]
        self.db.pager.write_page(root, {"kind": "bplus_internal", "keys": [], "children": [[]]})
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_PAGE_REFERENCE", self.codes(result))
        self.assertIn("INVALID_INDEX_REFERENCE", self.codes(result))

    def test_catalog_chunk_damage_and_memory_failure_are_not_silenced(self):
        self.seed()
        self.db.execute("BEGIN")
        chunk_id = self.db.pager.read_page(0)["pages"][0]
        chunk = self.db.pager.read_page(chunk_id)
        chunk["ordinal"] = 9
        self.db.pager.write_page(chunk_id, chunk)
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_CATALOG_CHUNK", self.codes(result))
        self.db.execute("ROLLBACK")
        from unittest.mock import patch
        with patch("auroradb.integrity._decode_page", side_effect=MemoryError("allocation failure")):
            with self.assertRaises(MemoryError):
                check_database(self.db)

    def test_closed_or_poisoned_database_requires_reopen(self):
        self.db.pager._poisoned = True
        with self.assertRaisesRegex(StorageError, "recovery"):
            check_database(self.db)
        self.db.pager._poisoned = False
        self.db.close()
        with self.assertRaisesRegex(StorageError, "closed"):
            check_database(self.db)

    def test_appended_file_tail_and_retained_malformed_page_are_detected(self):
        self.seed()
        original = (self.directory / "pages.dat").read_bytes()
        (self.directory / "pages.dat").write_bytes(original + b"tail")
        try:
            result = check_database(self.db)
            self.assertFalse(result["ok"])
            self.assertIn("DATA_FILE_SIZE_MISMATCH", self.codes(result))
        finally:
            (self.directory / "pages.dat").write_bytes(original)
        self.db.execute("BEGIN")
        self.db.pager.allocate({"kind": "heap", "rows": [123]})
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_PAGE_STRUCTURE", self.codes(result))

    def test_unique_numeric_equality_and_shared_overflow_are_detected(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v REAL UNIQUE, note TEXT)")
        self.db.execute("INSERT INTO t VALUES (1,1.0,'a'),(2,2.0,'b')")
        self.db.execute("BEGIN")
        page_id = self.db.catalog["tables"]["t"]["pages"][0]
        page = self.db.pager.read_page(page_id)
        page["rows"][1]["v"]["v"] = 1  # Equal to 1.0 under numeric equality.
        self.db.pager.write_page(page_id, page)
        result = check_database(self.db)
        self.assertIn("UNIQUE_HEAP_DUPLICATE", self.codes(result))
        self.db.execute("ROLLBACK; BEGIN")
        page = self.db.pager.read_page(page_id)
        overflow = self.db.pager.allocate({"kind": "overflow", "values": page["rows"][0]["v"]})
        page["rows"][0] = {"overflow": overflow}
        page["rows"][1] = {"overflow": overflow}
        self.db.pager.write_page(page_id, page)
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("PAGE_MULTIPLE_OWNERS", self.codes(result))

    def test_invalid_schema_metadata_is_reported_and_argument_cap_is_validated(self):
        self.seed()
        self.db.execute("BEGIN")
        self.db.catalog["tables"]["t"]["columns"][0]["type"] = []
        result = check_database(self.db)
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_SCHEMA", self.codes(result))
        for limit in (0, True, 10001, 1.5):
            with self.assertRaises(ValueError):
                check_database(self.db, max_issues=limit)


if __name__ == "__main__":
    unittest.main()
