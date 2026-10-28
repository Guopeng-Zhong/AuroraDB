"""Read-only integrity checks across pages, catalog, heap and B+ trees.

Checks read committed pages through a separate binary file handle so a warm
buffer-cache entry cannot hide an on-disk CRC error. During a transaction, staged
pages and the in-memory catalog define the current view. No repair, allocation,
checkpoint, WAL cleanup or SQL execution is performed.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import nullcontext
import copy
import json
import math
import os
import time

from .storage import BPlusTree, MAX_INDEX_KEY_BYTES, PAGE_SIZE, StorageError, _decode_page, _payload


_DAMAGE = (StorageError, OSError, ValueError, TypeError, KeyError, IndexError,
           AttributeError, RecursionError, OverflowError, UnicodeError)
_KINDS = {"catalog", "catalog_root", "catalog_chunk", "heap", "overflow",
          "bplus_leaf", "bplus_internal"}
_TYPES = {"INTEGER", "REAL", "TEXT", "BOOLEAN"}


def _page_id(value, count):
    return type(value) is int and 0 <= value < count


def _rid(value, count):
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and _page_id(value[0], count) and type(value[1]) is int and value[1] >= 0)


def _index_entry(value, count):
    if not isinstance(value, list) or len(value) != 2 or not _rid(value[1], count): return False
    key = value[0]
    if type(key) not in (bool, int, float, str) or isinstance(key, float) and not math.isfinite(key): return False
    return len(json.dumps(key, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= MAX_INDEX_KEY_BYTES


def _token(key, rid):
    # JSON retains the distinction between boolean, integer, real and text keys.
    return json.dumps([key, list(rid)], ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def _unique_key(value):
    # SQL/storage numeric equality treats 1 and 1.0 as the same key.
    if type(value) in (bool, int, float):
        if isinstance(value, float) and not math.isfinite(value):
            raise StorageError("Non-finite unique key.")
        return (0, value)
    if isinstance(value, str): return (1, value)
    raise StorageError("Non-scalar unique key.")


class _Reader:
    """A current-view reader with no writes and no dependency on the LRU cache."""
    def __init__(self, pager, handle):
        self.pager, self.handle = pager, handle
        self.page_count = pager.page_count
        self.invalid = set()

    def read_page(self, page_id, *, scan=None, next_page=None):
        # The B+ traversal's optional buffering hints never affect this direct
        # binary reader. Integrity scans must not train or populate the cache.
        if not _page_id(page_id, self.page_count):
            raise StorageError(f"Page reference {page_id!r} is outside the allocated range.")
        if page_id in self.invalid:
            raise StorageError(f"Page {page_id} could not be verified.")
        if page_id in self.pager._staged:
            value = copy.deepcopy(self.pager._staged[page_id])
            _payload(value)
            return value
        self.handle.seek(page_id * PAGE_SIZE)
        return _decode_page(page_id, self.handle.read(PAGE_SIZE))

    def scan_context(self, kind="scan"):
        return nullcontext(None)

    def advance_scan(self, scan, next_page):
        pass


def check_database(db, max_issues=100):
    """Return a bounded JSON report; ``ok`` depends on all errors, not its cap.

    The caller may already hold ``db.lock``. This function also acquires it so
    direct callers get a stable database view. Unexpected resource failures such
    as MemoryError propagate instead of being reported as data corruption.
    """
    if type(max_issues) is not int or not 1 <= max_issues <= 10000:
        raise ValueError("max_issues must be an integer from 1 to 10000")
    started = time.perf_counter()
    with db.lock:
        pager = db.pager
        if db.closed or pager._closed:
            raise StorageError("Database is closed.")
        if pager.poisoned:
            raise StorageError("Commit outcome requires recovery; close and reopen the database.")
        active = bool(db.active)
        report = {
            "ok": True, "view": "current transaction" if active else "committed",
            "summary": {"pages": pager.page_count, "committed_pages": pager._page_count,
                        "staged_pages": len(pager._staged), "checked_pages": 0,
                        "crc_checked_pages": 0, "tables": 0, "indexes": 0, "rows": 0,
                        "reachable_pages": 0, "retained_pages": 0},
            "checks": [], "tables": [], "issues": [], "issue_count": 0,
            "error_count": 0, "issues_truncated": False,
        }
        failures = Counter()
        reachable, owners = set(), {}

        def issue(code, message, *, category="structure", severity="error", **context):
            report["issue_count"] += 1
            if severity == "error":
                report["error_count"] += 1
                failures[category] += 1
            if len(report["issues"]) < max_issues:
                report["issues"].append(dict(severity=severity, code=code,
                                             message=str(message), **context))
            else:
                report["issues_truncated"] = True

        def claim(page_id, owner, **context):
            if not _page_id(page_id, pager.page_count):
                issue("INVALID_PAGE_REFERENCE", f"Invalid allocated-page reference: {page_id!r}",
                      **context)
                return False
            reachable.add(page_id)
            if page_id in owners and owners[page_id] != owner:
                issue("PAGE_MULTIPLE_OWNERS", f"Page is shared by {owners[page_id]} and {owner}.",
                      page=page_id, **context)
            else:
                owners[page_id] = owner
            return True

        def read(reader, page_id, **context):
            try:
                return reader.read_page(page_id)
            except _DAMAGE as exc:
                issue("PAGE_UNREADABLE", exc, page=page_id, **context)
                return None

        def check_shape(page_id, page):
            kind = page.get("kind")
            valid = kind in _KINDS if isinstance(kind, str) else False
            if valid and kind == "heap":
                valid = isinstance(page.get("rows"), list)
                if valid:
                    valid = all(entry is None or isinstance(entry, dict) and (
                        set(entry) == {"v"} and isinstance(entry["v"], dict)
                        or set(entry) == {"overflow"} and _page_id(entry["overflow"], pager.page_count))
                        for entry in page["rows"])
            if valid and kind == "overflow": valid = isinstance(page.get("values"), dict)
            if valid and kind == "bplus_leaf":
                valid = (isinstance(page.get("entries"), list) and "next" in page
                         and (page["next"] is None or _page_id(page["next"], pager.page_count))
                         and all(_index_entry(e, pager.page_count) for e in page["entries"]))
            if valid and kind == "bplus_internal":
                valid = (isinstance(page.get("keys"), list) and isinstance(page.get("children"), list)
                         and len(page["children"]) == len(page["keys"]) + 1
                         and all(_page_id(p, pager.page_count) for p in page["children"])
                         and all(_index_entry(e, pager.page_count) for e in page["keys"]))
            if valid and kind == "catalog_root":
                valid = type(page.get("version")) is int and page["version"] == 1 and isinstance(page.get("pages"), list)
                if valid: valid = bool(page["pages"]) and all(_page_id(p, pager.page_count) and p != 0 for p in page["pages"]) and len(set(page["pages"])) == len(page["pages"])
            if valid and kind == "catalog_chunk":
                valid = type(page.get("ordinal")) is int and page["ordinal"] >= 0 and isinstance(page.get("data"), str)
                if valid:
                    try: base64.b64decode(page["data"], validate=True)
                    except _DAMAGE: valid = False
            if valid and kind == "catalog":
                valid = type(page.get("version")) is int and page["version"] == 1 and isinstance(page.get("tables"), dict)
            if not valid:
                issue("INVALID_PAGE_STRUCTURE", f"Malformed or unknown page kind {kind!r}.",
                      category="pages", page=page_id)

        def check_value(value, column, *, default=False):
            typ = column.get("type")
            if value is None:
                return not (column.get("not_null") or column.get("primary"))
            valid = (typ == "INTEGER" and type(value) is int
                     or typ == "REAL" and type(value) in (int, float)
                     or typ == "TEXT" and isinstance(value, str)
                     or typ == "BOOLEAN" and (type(value) is bool or default and type(value) is int and value in (0, 1)))
            if valid and isinstance(value, float): valid = math.isfinite(value)
            if valid and typ == "TEXT" and column.get("max_length") is not None:
                limit = column["max_length"]
                valid = type(limit) is int and limit > 0 and len(value) <= limit
            return valid

        def run(reader):
            # Verify every allocated current-view page, including retained pages.
            for page_id in range(pager.page_count):
                try:
                    page = reader.read_page(page_id)
                    report["summary"]["checked_pages"] += 1
                    if page_id not in pager._staged:
                        report["summary"]["crc_checked_pages"] += 1
                    check_shape(page_id, page)
                except _DAMAGE as exc:
                    reader.invalid.add(page_id)
                    issue("PAGE_CRC_OR_READ_ERROR", exc, category="pages", page=page_id)

            claim(0, "catalog root")
            root = read(reader, 0, category="catalog")
            stored_catalog = None
            if root and root.get("kind") == "catalog_root":
                ids = root.get("pages")
                if not isinstance(ids, list) or not ids:
                    issue("INVALID_CATALOG_MANIFEST", "Catalog manifest must contain chunk pages.", category="catalog")
                else:
                    chunks, seen = [], set()
                    for ordinal, page_id in enumerate(ids):
                        if not _page_id(page_id, pager.page_count) or page_id == 0 or page_id in seen:
                            issue("INVALID_CATALOG_MANIFEST", f"Invalid or duplicate catalog chunk {page_id!r}.", category="catalog")
                            continue
                        seen.add(page_id)
                        claim(page_id, "catalog chunks")
                        chunk = read(reader, page_id, category="catalog")
                        if not chunk: continue
                        if chunk.get("kind") != "catalog_chunk" or chunk.get("ordinal") != ordinal:
                            issue("INVALID_CATALOG_CHUNK", "Catalog chunk kind or ordinal is invalid.", category="catalog", page=page_id)
                            continue
                        try: chunks.append(base64.b64decode(chunk["data"], validate=True))
                        except _DAMAGE as exc:
                            issue("INVALID_CATALOG_CHUNK", exc, category="catalog", page=page_id)
                    if len(chunks) == len(ids):
                        try: stored_catalog = json.loads(b"".join(chunks).decode("utf-8"))
                        except _DAMAGE as exc: issue("INVALID_CATALOG_JSON", exc, category="catalog")
            elif root and root.get("kind") == "catalog":
                stored_catalog = root
            elif root:
                issue("INVALID_CATALOG_ROOT", "Page 0 is not a supported catalog.", category="catalog", page=0)
            if stored_catalog is not None:
                if not isinstance(stored_catalog, dict) or stored_catalog.get("kind") != "catalog" or stored_catalog.get("version") != 1 or not isinstance(stored_catalog.get("tables"), dict):
                    issue("INVALID_CATALOG", "Decoded catalog structure is invalid.", category="catalog")
                elif not active and stored_catalog != db.catalog:
                    issue("CATALOG_VIEW_MISMATCH", "Committed catalog differs from the open database catalog.", category="catalog")

            catalog = db.catalog
            if not isinstance(catalog, dict) or not isinstance(catalog.get("tables"), dict):
                issue("INVALID_CATALOG", "Current catalog has no table dictionary.", category="catalog")
                return
            index_names = set()
            for table_name, meta in catalog["tables"].items():
                context = {"table": table_name}
                table_report = {"name": table_name, "ok": True, "rows": 0, "heap_pages": 0,
                                "overflow_pages": 0, "indexes": []}
                report["tables"].append(table_report)
                report["summary"]["tables"] += 1
                errors_before = report["error_count"]
                if not isinstance(table_name, str) or not table_name or not isinstance(meta, dict):
                    issue("INVALID_TABLE_METADATA", "Table name or metadata is invalid.", category="schema", **context)
                    table_report["ok"] = False
                    continue
                columns, heap_pages, indexes = meta.get("columns"), meta.get("pages"), meta.get("indexes")
                if not isinstance(columns, list) or not columns or not all(isinstance(c, dict) for c in columns):
                    issue("INVALID_SCHEMA", "Table must have a nonempty column list.", category="schema", **context)
                    table_report["ok"] = False
                    continue
                names = [c.get("name") for c in columns]
                valid_schema = all(isinstance(n, str) and n for n in names) and len(set(names)) == len(names)
                valid_schema = valid_schema and all(isinstance(c.get("type"), str) and c["type"] in _TYPES for c in columns)
                if not valid_schema:
                    issue("INVALID_SCHEMA", "Column names or types are invalid or duplicated.", category="schema", **context)
                    table_report["ok"] = False
                    continue
                if sum(bool(c.get("primary")) for c in columns) > 1:
                    issue("INVALID_SCHEMA", "Multiple primary-key columns are unsupported.", category="schema", **context)
                for c in columns:
                    if any(field in c and type(c[field]) is not bool for field in ("primary", "unique", "not_null")):
                        issue("INVALID_CONSTRAINT_METADATA", "Constraint flags must be boolean.", category="schema", column=c["name"], **context)
                    if c.get("primary") and not (c.get("unique") and c.get("not_null")):
                        issue("INVALID_PRIMARY_METADATA", "Primary key must be UNIQUE and NOT NULL.", category="schema", column=c["name"], **context)
                    if c.get("default") is not None and not check_value(c["default"], dict(c, not_null=False, primary=False), default=True):
                        issue("INVALID_DEFAULT", "Column default violates its type or length.", category="schema", column=c["name"], **context)
                if not isinstance(heap_pages, list):
                    issue("INVALID_HEAP_METADATA", "Heap page list is invalid.", category="heap", **context)
                    heap_pages = []
                if not isinstance(indexes, list):
                    issue("INVALID_INDEX_METADATA", "Index list is invalid.", category="indexes", **context)
                    indexes = []
                row_by_rid, valid_rows, seen_pages, overflow_pages = {}, {}, set(), set()
                for page_id in heap_pages:
                    if not claim(page_id, f"heap {table_name}", **context): continue
                    if page_id in seen_pages:
                        issue("DUPLICATE_HEAP_PAGE", "Heap page is listed more than once.", category="heap", page=page_id, **context)
                        continue
                    seen_pages.add(page_id)
                    page = read(reader, page_id, category="heap", **context)
                    if not page: continue
                    if page.get("kind") != "heap" or not isinstance(page.get("rows"), list):
                        issue("INVALID_HEAP_PAGE", "Table reference does not point to a heap page.", category="heap", page=page_id, **context)
                        continue
                    table_report["heap_pages"] += 1
                    for slot, entry in enumerate(page["rows"]):
                        if entry is None: continue
                        rid = (page_id, slot)
                        table_report["rows"] += 1
                        report["summary"]["rows"] += 1
                        row = None
                        if isinstance(entry, dict) and set(entry) == {"v"} and isinstance(entry["v"], dict): row = entry["v"]
                        elif isinstance(entry, dict) and set(entry) == {"overflow"}:
                            target = entry["overflow"]
                            if claim(target, f"overflow {table_name} {rid}", **context):
                                overflow_pages.add(target)
                                overflow = read(reader, target, category="heap", **context)
                                if overflow and overflow.get("kind") == "overflow" and isinstance(overflow.get("values"), dict): row = overflow["values"]
                                elif overflow: issue("INVALID_FORWARDING", "Forwarding target is not an overflow row.", category="heap", page=target, **context)
                        if row is None:
                            issue("INVALID_HEAP_ROW", "Heap record or forwarding reference is malformed.", category="heap", page=page_id, slot=slot, **context)
                            continue
                        row_by_rid[rid] = row
                        if set(row) != set(names):
                            issue("ROW_SCHEMA_MISMATCH", "Row fields differ from table columns.", category="constraints", page=page_id, slot=slot, **context)
                            continue
                        valid_rows[rid] = row
                        for c in columns:
                            if not check_value(row[c["name"]], c):
                                issue("ROW_TYPE_OR_NULL_CONSTRAINT", "Row value violates column type, length or NOT NULL.", category="constraints", column=c["name"], page=page_id, slot=slot, **context)
                table_report["overflow_pages"] = len(overflow_pages)
                if type(meta.get("row_count")) is not int or meta["row_count"] != table_report["rows"]:
                    issue("ROW_COUNT_MISMATCH", f"Catalog count {meta.get('row_count')!r}, observed {table_report['rows']} occupied slots.", category="heap", **context)
                if type(meta.get("next_rid")) is not int or meta["next_rid"] < table_report["rows"] + 1:
                    issue("INVALID_INSERT_SEQUENCE", "Historical insertion sequence cannot be smaller than live rows plus one.", category="heap", **context)
                if "insert_hint" in meta and meta["insert_hint"] is not None and (not _page_id(meta["insert_hint"], pager.page_count) or meta["insert_hint"] not in seen_pages):
                    issue("INVALID_INSERT_HINT", "Append hint does not reference a table heap page.", category="heap", **context)
                if "reusable_pages" in meta:
                    reusable = meta["reusable_pages"]
                    if not isinstance(reusable, list) or any(not _page_id(p, pager.page_count) or p not in seen_pages for p in reusable) or len(set(reusable)) != len(reusable):
                        issue("INVALID_REUSE_HINT", "Reusable-page hints contain invalid or duplicate pages.", category="heap", **context)
                for c in columns:
                    if c.get("unique") or c.get("primary"):
                        seen_keys = set()
                        for rid, row in valid_rows.items():
                            value = row[c["name"]]
                            if value is None: continue
                            try: key = _unique_key(value)
                            except _DAMAGE: continue  # Its row-type issue already reports invalid data.
                            if key in seen_keys:
                                issue("UNIQUE_HEAP_DUPLICATE", "Heap contains a duplicate constrained key.", category="constraints", column=c["name"], page=rid[0], slot=rid[1], **context)
                            seen_keys.add(key)
                        if not any(isinstance(ix, dict) and ix.get("column") == c["name"] and ix.get("unique") and ix.get("protected") for ix in indexes):
                            issue("MISSING_CONSTRAINT_INDEX", "UNIQUE/PRIMARY column has no protected unique index.", category="indexes", column=c["name"], **context)

                for index in indexes:
                    report["summary"]["indexes"] += 1
                    index_name = index.get("name") if isinstance(index, dict) else None
                    ix_context = dict(context, index=index_name)
                    ix_report = {"name": index_name, "ok": False, "pages": 0, "entries": 0,
                                 "expected_entries": 0}
                    table_report["indexes"].append(ix_report)
                    before_index = report["error_count"]
                    if not isinstance(index, dict) or not isinstance(index_name, str) or not index_name or index.get("column") not in names or not _page_id(index.get("root"), pager.page_count):
                        issue("INVALID_INDEX_METADATA", "Index name, column or root is invalid.", category="indexes", **ix_context)
                        continue
                    if any(field in index and type(index[field]) is not bool for field in ("unique", "protected")):
                        issue("INVALID_INDEX_METADATA", "Index constraint flags must be boolean.", category="indexes", **ix_context)
                    if index_name in index_names:
                        issue("DUPLICATE_INDEX_NAME", "Index name is duplicated in the catalog.", category="indexes", **ix_context)
                    index_names.add(index_name)
                    # First walk references with bounds and cycle protection. It
                    # also records reachability before validate() might reject a
                    # topology and prevents bool/list child ids reaching hashing.
                    stack, seen_nodes, safe = [index["root"]], set(), True
                    while stack:
                        page_id = stack.pop()
                        if not _page_id(page_id, pager.page_count):
                            issue("INVALID_INDEX_REFERENCE", f"Invalid B+ tree page reference {page_id!r}.", category="indexes", **ix_context)
                            safe = False
                            continue
                        claim(page_id, f"index {table_name}.{index_name}", **ix_context)
                        if page_id in seen_nodes:
                            issue("INDEX_NODE_REUSED", "B+ tree cycles or references the same node twice.", category="indexes", page=page_id, **ix_context)
                            safe = False
                            continue
                        seen_nodes.add(page_id)
                        page = read(reader, page_id, category="indexes", **ix_context)
                        if not page:
                            safe = False
                            continue
                        kind = page.get("kind")
                        if kind == "bplus_internal" and isinstance(page.get("keys"), list) and isinstance(page.get("children"), list) and len(page["children"]) == len(page["keys"]) + 1:
                            stack.extend(reversed(page["children"]))
                            entries = page["keys"]
                        elif kind == "bplus_leaf" and isinstance(page.get("entries"), list) and "next" in page:
                            entries = page["entries"]
                            if page["next"] is not None and not _page_id(page["next"], pager.page_count):
                                issue("INVALID_LEAF_LINK", "Leaf next link is outside the allocated range.", category="indexes", page=page_id, **ix_context)
                                safe = False
                        else:
                            issue("INVALID_INDEX_NODE", "Index reference points to a malformed B+ tree node.", category="indexes", page=page_id, **ix_context)
                            safe = False
                            continue
                        for entry in entries:
                            if not _index_entry(entry, pager.page_count):
                                issue("INVALID_INDEX_ENTRY", "Index key/RID entry is malformed.", category="indexes", page=page_id, **ix_context)
                                safe = False
                    ix_report["pages"] = len(seen_nodes)
                    if not safe: continue
                    tree = BPlusTree(reader, index)
                    try:
                        ix_report.update(tree.validate())
                    except _DAMAGE as exc:
                        issue("INDEX_INVARIANT", exc, category="indexes", **ix_context)
                        continue
                    expected = Counter()
                    for rid, row in valid_rows.items():
                        key = row[index["column"]]
                        if key is None: continue
                        try: expected[_token(key, rid)] += 1
                        except _DAMAGE: continue
                    ix_report["expected_entries"] = sum(expected.values())
                    actual = Counter()
                    unique_keys = set()
                    try:
                        for key, rid in tree.items():
                            actual[_token(key, rid)] += 1
                            row = row_by_rid.get(tuple(rid))
                            if row is None:
                                issue("INDEX_DANGLING_RID", "Index RID does not reference a live row in this table.", category="indexes", page=rid[0], slot=rid[1], **ix_context)
                            elif index["column"] not in row or _token(key, rid) != _token(row[index["column"]], rid):
                                issue("INDEX_WRONG_KEY", "Index key differs from the heap row value.", category="indexes", page=rid[0], slot=rid[1], **ix_context)
                            if index.get("unique"):
                                token = _unique_key(key)
                                if token in unique_keys:
                                    issue("UNIQUE_INDEX_DUPLICATE", "Unique index contains duplicate keys.", category="indexes", **ix_context)
                                unique_keys.add(token)
                    except _DAMAGE as exc:
                        issue("INDEX_SCAN_ERROR", exc, category="indexes", **ix_context)
                        continue
                    missing, unexpected = expected - actual, actual - expected
                    if missing:
                        issue("INDEX_MISSING_ENTRIES", f"Index is missing {sum(missing.values())} expected heap entries.", category="indexes", **ix_context)
                    if unexpected:
                        issue("INDEX_EXTRA_ENTRIES", f"Index has {sum(unexpected.values())} unexpected entries.", category="indexes", **ix_context)
                    ix_report["ok"] = report["error_count"] == before_index
                table_report["ok"] = report["error_count"] == errors_before

        try:
            with open(pager._data_path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                expected_size = pager._page_count * PAGE_SIZE
                if size != expected_size:
                    issue("DATA_FILE_SIZE_MISMATCH", f"Data file has {size} bytes; committed allocation expects {expected_size} bytes.", category="pages")
                run(_Reader(pager, handle))
        except OSError as exc:
            issue("DATA_FILE_UNREADABLE", exc, category="pages")
        report["summary"]["reachable_pages"] = len(reachable)
        report["summary"]["retained_pages"] = max(0, pager.page_count - len(reachable))
        retained = report["summary"]["retained_pages"]
        if retained:
            issue("RETAINED_ALLOCATED_PAGES", f"{retained} allocated pages are retained outside the current catalog; this engine does not reclaim dropped or old overflow/catalog pages.", severity="info", category="reachability")
        descriptions = {
            "pages": "All allocated current-view page images and basic page structures",
            "catalog": "Catalog root, ordered chunks and committed catalog agreement",
            "schema": "Column schema, defaults and constraint metadata",
            "heap": "Heap slots, stable RIDs, forwarding references and row counts",
            "constraints": "Row fields, column types, NULL and UNIQUE/PRIMARY constraints",
            "indexes": "B+ tree invariants and bidirectional index/heap agreement",
            "structure": "Allocated reference bounds and exclusive page ownership",
        }
        report["checks"] = [dict(name=name, status="fail" if failures[name] else "pass",
                                  detail=detail, errors=failures[name]) for name, detail in descriptions.items()]
        report["ok"] = report["error_count"] == 0
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return report
