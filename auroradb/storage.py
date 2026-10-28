"""Independent, checksummed paged storage and a durable no-steal buffer pool.

Only committed page images reach ``pages.dat``.  The WAL commit record authenticates
the entire transaction; recovery never applies an incomplete transaction.  Heap RIDs
are the stable JSON pair [heap_page, slot].  A B+ tree orders (key, RID) composites so
duplicate SQL keys can span any number of linked leaves.
"""
from __future__ import annotations

import bisect
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
import random
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from typing import Iterator
import zlib

PAGE_SIZE = 4096
_PAGE_MAGIC = b"AURPAGE1"
_PAGE_HEADER = struct.Struct("<8sIII")
_WAL_MAGIC = b"AURWAL1\0"
_FRAME_HEADER = struct.Struct("<II")
_BEGIN = struct.Struct("<QQI")
_COMMIT = struct.Struct("<QI")
MAX_INDEX_KEY_BYTES = 1024


class StorageError(Exception):
    """A storage invariant, capacity limit, lock, or I/O operation failed."""


def _payload(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise StorageError("A page must be a JSON object.")
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise StorageError(f"Page is not valid finite JSON: {exc}") from exc
    if len(data) > PAGE_SIZE - _PAGE_HEADER.size:
        raise StorageError(f"Record or page exceeds the {PAGE_SIZE - _PAGE_HEADER.size}-byte page payload limit.")
    return data


def _fits(value: dict) -> bool:
    try:
        _payload(value)
        return True
    except StorageError:
        return False


def _encode_page(page_id: int, value: dict) -> bytes:
    data = _payload(value)
    if not 0 <= page_id <= 0xFFFFFFFF:
        raise StorageError("Page identifier exceeds the on-disk format limit.")
    body = data.ljust(PAGE_SIZE - _PAGE_HEADER.size, b"\0")
    checksum = zlib.crc32(struct.pack("<II", page_id, len(data)) + body) & 0xFFFFFFFF
    return _PAGE_HEADER.pack(_PAGE_MAGIC, page_id, len(data), checksum) + body


def _decode_page(page_id: int, image: bytes) -> dict:
    if len(image) != PAGE_SIZE:
        raise StorageError(f"Truncated page {page_id}.")
    magic, stored_id, length, checksum = _PAGE_HEADER.unpack_from(image)
    if magic != _PAGE_MAGIC or stored_id != page_id or length > PAGE_SIZE - _PAGE_HEADER.size:
        raise StorageError(f"Invalid header in page {page_id}.")
    data = image[_PAGE_HEADER.size:_PAGE_HEADER.size + length]
    if zlib.crc32(struct.pack("<II", page_id, length) + image[_PAGE_HEADER.size:]) & 0xFFFFFFFF != checksum:
        raise StorageError(f"CRC mismatch in page {page_id}; data is damaged.")
    try:
        result = json.loads(data.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError) as exc:
        raise StorageError(f"Invalid JSON in page {page_id}.") from exc
    if not isinstance(result, dict):
        raise StorageError(f"Page {page_id} does not contain an object.")
    return result


class DiskPager:
    """Fixed-size pages, a bounded read cache, and atomic single-writer transactions.

    LRU is the default. The optional predictive policy uses only real, local scan
    hints and bounded, decaying access history. It is an experimental adaptation
    of sampling-based predictive buffering, not a concurrent PBM reproduction.

    On a commit failure the instance is poisoned: close and reopen it to resolve
    the durable WAL.  Rollback never removes a possibly committed WAL.  The optional
    private ``_fault_hook`` is used by crash tests at precise durability boundaries.
    """
    def __init__(self, directory, cache_size=64):
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 1:
            raise StorageError("Buffer cache size must be a positive integer.")
        self.directory = Path(directory)
        self.cache_size = cache_size
        self._cache = OrderedDict()
        self._cache_policy = "lru"
        self._sample_size = 8
        self._policy_seed = 20261010
        self._policy_random = random.Random(self._policy_seed)
        self._cache_slots = []
        self._cache_positions = {}
        self._cache_history = OrderedDict()
        self._history_limit = cache_size * 4
        self._decay_period = max(16, cache_size * 4)
        self._access_clock = 0
        self._scan_serial = 0
        self._scans = {}
        self._staged = {}
        self._active = False
        self._closed = False
        self._poisoned = False
        self._mutex = threading.RLock()
        self._lock_file = None
        self._data_file = None
        self._fault_hook = None
        self._metrics = dict(cache_hits=0, cache_misses=0, disk_reads=0, disk_writes=0,
                             cache_evictions=0, staged_hits=0, wal_bytes=0,
                             wal_recoveries=0, wal_cleanup_errors=0, commits=0, rollbacks=0,
                             predictive_evictions=0, predictive_fallbacks=0,
                             predictive_samples=0, predictive_admission_bypasses=0,
                             cache_policy_cpu_ns=0,
                             scan_page_accesses=0, scan_contexts=0)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._acquire_lock()
            self._data_path = self.directory / "pages.dat"
            self._wal_path = self.directory / "redo.wal"
            self._data_file = open(self._data_path, "r+b" if self._data_path.exists() else "w+b", buffering=0)
            # Keep the WAL filename present across transactions; directory metadata
            # is persisted when available, rather than relying on rename atomicity.
            if not self._wal_path.exists():
                with open(self._wal_path, "w+b") as wal:
                    wal.flush()
                    os.fsync(wal.fileno())
                self._sync_directory()
            self._recover()
            size = os.fstat(self._data_file.fileno()).st_size
            if size % PAGE_SIZE:
                raise StorageError("Data file has a partial page and no complete redo transaction can repair it.")
            self._page_count = size // PAGE_SIZE
            self._next_page_id = self._page_count
        except Exception as exc:
            self.close()
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"Cannot open database storage: {exc}") from exc

    def _sync_directory(self):
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _acquire_lock(self):
        self._lock_file = open(self.directory / "writer.lock", "a+b", buffering=0)
        if os.fstat(self._lock_file.fileno()).st_size == 0:
            self._lock_file.write(b"\0")
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StorageError("Database is already open by another writer. Close that connection first.") from exc

    def _ensure_open(self):
        if self._closed:
            raise StorageError("Storage is closed.")
        if self._poisoned:
            raise StorageError("Commit outcome requires WAL recovery; close and reopen the database.")

    def _ensure_transaction(self):
        self._ensure_open()
        if not self._active:
            raise StorageError("Page mutation requires an active transaction.")

    @property
    def page_count(self):
        return self._next_page_id if self._active else self._page_count

    @property
    def in_transaction(self):
        return self._active

    @property
    def poisoned(self):
        """Whether a commit needs reopen/recovery before any further SQL work."""
        return self._poisoned

    @staticmethod
    def _valid_id(page_id):
        if isinstance(page_id, bool) or not isinstance(page_id, int) or page_id < 0:
            raise StorageError("Page identifier must be a nonnegative integer.")

    def cache_policy_status(self):
        """Describe volatile buffering configuration; no durable state is changed."""
        with self._mutex:
            return dict(policy=self._cache_policy, sample_size=self._sample_size,
                        active_scans=len(self._scans), history_pages=len(self._cache_history),
                        history_limit=self._history_limit, seed=self._policy_seed,
                        prediction="explicit next-page scan hints; decaying weighted access history",
                        fallback="LRU when sampled pages have no scan evidence",
                        scope="committed read copies only", persistent=False)

    def set_cache_policy(self, policy, sample_size=8):
        """Select 'lru' or experimental 'predictive', preserving pages/transactions.

        Switching resets training history and the deterministic sampling stream.
        Existing cached values and their LRU order are retained. Publication of
        committed writes is never counted as a read or a training observation.
        """
        with self._mutex:
            self._ensure_open()
            if policy not in ("lru", "predictive"):
                raise StorageError("Cache policy must be 'lru' or 'predictive'.")
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 1 <= sample_size <= 4096:
                raise StorageError("Predictive sample size must be an integer from 1 to 4096.")
            self._cache_policy = policy
            self._sample_size = sample_size
            self._policy_random = random.Random(self._policy_seed)
            self._cache_history.clear()
            self._cache_slots = list(self._cache) if policy == "predictive" else []
            self._cache_positions = {pid: i for i, pid in enumerate(self._cache_slots)}
            if policy == "predictive":
                for offset, (pid, page) in enumerate(self._cache.items()):
                    self._cache_history[pid] = dict(last=self._access_clock-len(self._cache)+offset,
                                                   weight=0.0, scan=False, kind=page.get("kind"))
            return self.cache_policy_status()

    @contextmanager
    def scan_context(self, kind="scan"):
        """Register one actual traversal, cleaned up even on early generator close.

        The caller supplies the next page only when its metadata already proves
        that traversal order. Unknown successors stay unknown; there is no
        synthetic concurrent scan or assumption that a later query will repeat.
        """
        with self._mutex:
            self._ensure_open()
            self._scan_serial += 1
            token = self._scan_serial
            self._scans[token] = dict(kind=kind, next_page=None, next_tick=None,
                                     last_tick=None, step=1)
            self._metrics["scan_contexts"] += 1
        try:
            yield token
        finally:
            with self._mutex:
                self._scans.pop(token, None)

    def advance_scan(self, scan, next_page):
        """Publish an observed successor without reading/prefetching its page."""
        with self._mutex:
            state = self._scans.get(scan)
            if state is not None:
                if next_page is not None:
                    self._valid_id(next_page)
                state["next_page"] = next_page
                state["next_tick"] = self._access_clock + state["step"] if next_page is not None else None

    def _observe_read(self, page_id, value, scan, next_page):
        started = time.perf_counter_ns()
        try:
            self._access_clock += 1
            state = self._scans.get(scan)
            is_scan = state is not None
            if is_scan:
                self._metrics["scan_page_accesses"] += 1
                previous = state["last_tick"]
                state["step"] = max(1, self._access_clock-previous) if previous is not None else 1
                state["last_tick"] = self._access_clock
                state["next_page"] = next_page
                state["next_tick"] = self._access_clock+state["step"] if next_page is not None else None
            if self._cache_policy != "predictive":
                return
            # Branch/catalog metadata serve many queries even during a scan;
            # classify them as point accesses rather than disposable scan pages.
            kind = value.get("kind")
            scan_page = is_scan and kind not in ("bplus_internal", "catalog", "catalog_root", "catalog_chunk")
            prior = self._cache_history.get(page_id)
            weight = self._decayed_weight(prior) if prior is not None else 0.0
            if prior is None:
                prior = {}
                self._cache_history[page_id] = prior
            # Reuse an existing history record on repeated reads. Its position
            # and access clock still change exactly as they do for a new record.
            prior["last"] = self._access_clock
            prior["weight"] = min(8.0, weight+(0.125 if scan_page else 1.0))
            prior["scan"] = scan_page
            prior["kind"] = kind
            self._cache_history.move_to_end(page_id)
            while len(self._cache_history) > self._history_limit:
                oldest = next(iter(self._cache_history))
                if oldest in self._cache:
                    # Retain one tiny record for each resident. Moving history
                    # order does not change its last-access clock or LRU order.
                    self._cache_history.move_to_end(oldest)
                else:
                    self._cache_history.popitem(last=False)
        finally:
            self._metrics["cache_policy_cpu_ns"] += time.perf_counter_ns()-started

    def _decayed_weight(self, entry):
        if entry is None:
            return 0.0
        age = max(0, self._access_clock-entry["last"])
        # Access-based aging avoids stale hotspots staying protected forever.
        return entry["weight"] * (0.5 ** min(64, age/self._decay_period))

    def _eviction_candidate(self):
        if self._cache_policy == "lru":
            return next(iter(self._cache))
        started = time.perf_counter_ns()
        try:
            count = min(self._sample_size, len(self._cache_slots))
            # Include the LRU victim, then sample other residents in O(sample)
            # space. A swap-remove array avoids copying the whole cache on each
            # small-sample eviction. The new page is also a legitimate resident.
            oldest = next(iter(self._cache))
            candidates = self._policy_random.sample(self._cache_slots, count)
            if oldest not in candidates:
                candidates[-1] = oldest
            self._metrics["predictive_samples"] += len(candidates)
            future = {}
            for state in self._scans.values():
                pid = state["next_page"]
                tick = state["next_tick"]
                if pid is not None and tick is not None:
                    future[pid] = min(tick, future.get(pid, tick))
            history = self._cache_history
            has_evidence = False
            for pid in candidates:
                entry = history.get(pid)
                if pid in future or (entry is not None and entry["scan"]):
                    has_evidence = True
                    break
            if not has_evidence:
                self._metrics["predictive_fallbacks"] += 1
                return oldest
            victim = None
            lowest = None
            for pid in candidates:
                entry = history.get(pid)
                # Near, explicitly observed scan successors take precedence.
                # Every other future access is unknown: weighted frequency and
                # recency provide a heuristic, not an oracle/probability claim.
                known = pid in future
                retention = (known, -future[pid] if known else 0,
                             self._decayed_weight(entry), entry["last"] if entry else -1)
                if lowest is None or retention < lowest:
                    lowest = retention
                    victim = pid
            self._metrics["predictive_evictions"] += 1
            return victim
        finally:
            self._metrics["cache_policy_cpu_ns"] += time.perf_counter_ns()-started

    def _cache_remove(self, page_id):
        del self._cache[page_id]
        if self._cache_policy == "predictive":
            position = self._cache_positions.pop(page_id)
            last = self._cache_slots.pop()
            if position < len(self._cache_slots):
                self._cache_slots[position] = last
                self._cache_positions[last] = position

    def _bypass_scan_page(self, page_id):
        """Avoid polluting a full cache with a low-reuse scan admission.

        Only a freshly decoded read can take this path. Preserve the LRU
        resident when it has stronger point-access history, unless another
        active traversal explicitly needs the incoming page. Ordinary reads
        and ambiguous cases still use sampled replacement.
        """
        if self._cache_policy != "predictive" or len(self._cache) < self.cache_size:
            return False
        started = time.perf_counter_ns()
        try:
            incoming = self._cache_history.get(page_id)
            if incoming is None or not incoming["scan"]:
                return False
            oldest = next(iter(self._cache))
            resident = self._cache_history.get(oldest)
            if resident is None or resident["scan"]:
                return False
            if any(state["next_page"] == page_id and state["next_tick"] is not None
                   for state in self._scans.values()):
                return False
            if self._decayed_weight(resident) <= self._decayed_weight(incoming):
                return False
            # Equivalent to admitting and immediately evicting the new read
            # copy, but without a cache copy, slot maintenance or sampling.
            self._metrics["predictive_admission_bypasses"] += 1
            self._metrics["predictive_evictions"] += 1
            self._metrics["cache_evictions"] += 1
            return True
        finally:
            self._metrics["cache_policy_cpu_ns"] += time.perf_counter_ns()-started

    def _cache_put(self, page_id, value, *, owned=False) -> bool:
        """Admit a page and report whether the cache retains its value."""
        # A decoder-created object has no caller alias. Cache it directly;
        # staged/commit objects still need an independent cache copy.
        if owned and self._bypass_scan_page(page_id):
            return False
        is_new = page_id not in self._cache
        self._cache[page_id] = value if owned else copy.deepcopy(value)
        self._cache.move_to_end(page_id)
        if is_new and self._cache_policy == "predictive":
            self._cache_positions[page_id] = len(self._cache_slots)
            self._cache_slots.append(page_id)
        while len(self._cache) > self.cache_size:
            self._cache_remove(self._eviction_candidate())
            self._metrics["cache_evictions"] += 1
        return page_id in self._cache

    def clear_cache(self):
        """Drop only committed read copies for reproducible access experiments."""
        with self._mutex:
            self._ensure_open()
            self._cache.clear()
            self._cache_slots.clear()
            self._cache_positions.clear()
            self._cache_history.clear()

    def read_page(self, page_id, *, scan=None, next_page=None):
        with self._mutex:
            self._ensure_open()
            self._valid_id(page_id)
            if next_page is not None:
                self._valid_id(next_page)
            if page_id in self._staged:
                self._metrics["staged_hits"] += 1
                return copy.deepcopy(self._staged[page_id])
            if page_id >= self._page_count:
                return None
            if page_id in self._cache:
                self._metrics["cache_hits"] += 1
                self._cache.move_to_end(page_id)
                self._observe_read(page_id, self._cache[page_id], scan, next_page)
                return copy.deepcopy(self._cache[page_id])
            self._metrics["cache_misses"] += 1
            try:
                self._data_file.seek(page_id * PAGE_SIZE)
                value = _decode_page(page_id, self._data_file.read(PAGE_SIZE))
            except OSError as exc:
                raise StorageError(f"Cannot read page {page_id}: {exc}") from exc
            self._metrics["disk_reads"] += 1
            self._observe_read(page_id, value, scan, next_page)
            if self._cache_put(page_id, value, owned=True):
                return copy.deepcopy(value)
            # No cache or transaction retains this decoded object. Transfer
            # ownership to the reader instead of copying an unshared page.
            return value

    def write_page(self, page_id, value):
        with self._mutex:
            self._ensure_transaction()
            self._valid_id(page_id)
            if page_id >= self._next_page_id:
                raise StorageError("Cannot write an unallocated page.")
            _encode_page(page_id, value)  # Validate before mutating staged state.
            self._staged[page_id] = copy.deepcopy(value)

    def allocate(self, value):
        with self._mutex:
            self._ensure_transaction()
            page_id = self._next_page_id
            _encode_page(page_id, value)
            self._staged[page_id] = copy.deepcopy(value)
            self._next_page_id += 1
            return page_id

    def begin(self):
        with self._mutex:
            self._ensure_open()
            if self._active:
                raise StorageError("A transaction is already active.")
            self._staged = {}
            self._next_page_id = self._page_count
            self._active = True

    @staticmethod
    def _frame(payload):
        return _FRAME_HEADER.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF) + payload

    def _fault(self, phase, page_id=None):
        if self._fault_hook is not None:
            self._fault_hook(phase, page_id)

    def _write_wal(self, images, target_count):
        begin = b"B" + _BEGIN.pack(self._page_count, target_count, len(images))
        digest = hashlib.sha256(begin)
        with open(self._wal_path, "wb", buffering=0) as wal:
            self._write_all(wal, _WAL_MAGIC)
            self._write_all(wal, self._frame(begin))
            self._fault("after_wal_begin")
            for page_id, image in images:
                record = b"P" + struct.pack("<Q", page_id) + image
                digest.update(record)
                self._write_all(wal, self._frame(record))
                self._fault("after_wal_page", page_id)
            commit = b"C" + _COMMIT.pack(target_count, len(images)) + digest.digest()
            self._write_all(wal, self._frame(commit))
            self._fault("before_wal_fsync")
            os.fsync(wal.fileno())
            self._metrics["wal_bytes"] += wal.tell()
        self._fault("after_wal_fsync")

    @staticmethod
    def _write_all(file, data):
        view = memoryview(data)
        while view:
            count = file.write(view)
            if count is None or count <= 0:
                raise OSError("Short write without progress.")
            view = view[count:]

    def _apply_images(self, images, target_count):
        for page_id, image in images:
            self._data_file.seek(page_id * PAGE_SIZE)
            self._write_all(self._data_file, image)
            self._metrics["disk_writes"] += 1
            self._fault("after_page_write", page_id)
        self._data_file.truncate(target_count * PAGE_SIZE)
        os.fsync(self._data_file.fileno())
        self._fault("after_data_fsync")

    def _clear_wal(self):
        try:
            with open(self._wal_path, "wb", buffering=0) as wal:
                os.fsync(wal.fileno())
        except OSError:
            # The data fsync already succeeded. A redundant WAL can be safely
            # replayed, so cleanup failure cannot turn success into a false rollback.
            self._metrics["wal_cleanup_errors"] += 1

    def commit(self):
        with self._mutex:
            self._ensure_transaction()
            if not self._staged:
                self._active = False
                self._metrics["commits"] += 1
                return
            images = [(pid, _encode_page(pid, value)) for pid, value in sorted(self._staged.items())]
            try:
                self._write_wal(images, self._next_page_id)
                self._apply_images(images, self._next_page_id)
                # Publishing the durable result can allocate memory too. Until
                # this finishes, retain the complete WAL and treat every failure
                # as an uncertain outcome requiring a fresh recovery/open.
                self._page_count = self._next_page_id
                for page_id, value in self._staged.items():
                    self._cache_put(page_id, value)
                self._staged = {}
                self._active = False
                self._metrics["commits"] += 1
                # Cleanup normally tolerates OSError after the data fsync. Other
                # failures or cancellation must still poison the connection so
                # the SQL layer cannot restore and then use a stale catalog.
                self._clear_wal()
            except BaseException as exc:
                # Even an fsync error has an uncertain durable outcome. Preserve
                # both files and forbid further work until a fresh open recovers.
                self._poisoned = True
                if not isinstance(exc, Exception):
                    raise  # Preserve KeyboardInterrupt/SystemExit semantics.
                raise StorageError(f"Commit failed; close and reopen for recovery: {exc}") from exc

    def rollback(self):
        with self._mutex:
            if self._closed:
                return
            if self._active:
                self._staged = {}
                self._next_page_id = self._page_count
                self._active = False
                self._metrics["rollbacks"] += 1

    def _recover(self):
        images = self._read_committed_wal()
        if images is None:
            # Incomplete WAL was never allowed to touch the data file.
            self._clear_wal()
            return
        base_count, target_count, pages = images
        current_size = os.fstat(self._data_file.fileno()).st_size
        if current_size < base_count * PAGE_SIZE or current_size > target_count * PAGE_SIZE:
            raise StorageError("Committed WAL does not match the data-file extent.")
        self._apply_images(pages, target_count)
        self._metrics["wal_recoveries"] += 1
        self._clear_wal()

    def _read_committed_wal(self):
        """Validate one complete transaction, including every image and digest.

        A torn or corrupt prefix is ignored, never partly applied.  If a complete
        commit marker exists after corruption, report corruption instead of clearing
        a WAL that may be needed to repair partially written data pages.
        """
        with open(self._wal_path, "rb") as wal:
            raw = wal.read()
        if not raw:
            return None
        # An intact commit frame anywhere makes corruption an error, not an
        # uncommitted transaction. Do not rely on its absolute tail position:
        # prefix damage plus appended bytes must not erase durable evidence.
        # This scan only triggers conservative refusal, never permits replay;
        # all page images and the transaction digest must still validate below.
        commit_len = 1 + _COMMIT.size + 32
        has_commit_evidence = False
        needle = struct.pack("<I", commit_len)
        position = raw.find(needle)
        while position != -1:
            end = position + _FRAME_HEADER.size + commit_len
            if end <= len(raw):
                _, checksum = _FRAME_HEADER.unpack_from(raw, position)
                body = raw[position + _FRAME_HEADER.size:end]
                if body[:1] == b"C" and zlib.crc32(body) & 0xFFFFFFFF == checksum:
                    has_commit_evidence = True
                    break
            position = raw.find(needle, position + 1)
        if not has_commit_evidence:
            # A damaged C-frame length header must not erase evidence of a
            # committed transaction.  The frame checksum is immediately before
            # the body and authenticates the complete fixed-size commit body,
            # so scan for that pair independently of the (possibly torn) length
            # field.  This is conservative: it only changes the decision to
            # preserve the WAL; the strict parser below still has to validate
            # the original frame and digest before replay is permitted.
            body_position = raw.find(b"C")
            while body_position != -1:
                body_end = body_position + commit_len
                checksum_position = body_position - _FRAME_HEADER.size + 4
                if body_position >= _FRAME_HEADER.size and body_end <= len(raw):
                    checksum = struct.unpack_from("<I", raw, checksum_position)[0]
                    body = raw[body_position:body_end]
                    if zlib.crc32(body) & 0xFFFFFFFF == checksum:
                        has_commit_evidence = True
                        break
                body_position = raw.find(b"C", body_position + 1)

        # Keep these available to ``invalid`` so a damaged commit header can
        # still be recognized after all page records have been parsed.
        offset = len(_WAL_MAGIC)
        records = []

        def invalid(message):
            if has_commit_evidence:
                raise StorageError(f"Committed WAL is corrupt: {message}; files preserved.")
            # If a valid B frame is followed by every declared P frame and
            # non-empty trailing bytes, the writer reached the commit boundary
            # even if both the C-frame length and its tag were damaged.  This
            # is conservative evidence only: the strict parser below never
            # replays this transaction after any such corruption.
            if records and records[0][:1] == b"B" and len(records[0]) == 1 + _BEGIN.size:
                try:
                    _, _, count = _BEGIN.unpack(records[0][1:])
                except struct.error:
                    count = -1
                page_prefix = records[1:1 + count] if count >= 0 else []
                complete_pages = (len(page_prefix) == count and
                                  all(record[:1] == b"P" and len(record) == 9 + PAGE_SIZE
                                      for record in page_prefix))
                # Use the end of the valid B/P prefix, not the parser cursor:
                # an unknown complete trailing frame may already have been
                # consumed when invalid() is called at EOF. It must still
                # preserve recovery evidence after partial data publication.
                prefix_end = len(_WAL_MAGIC) + sum(_FRAME_HEADER.size + len(record)
                                                   for record in records[:1 + count])
                if complete_pages and prefix_end < len(raw):
                    raise StorageError(f"Committed WAL has damaged commit evidence: {message}; files preserved.")
            return None

        if not raw.startswith(_WAL_MAGIC):
            return invalid("invalid magic")
        while offset < len(raw):
            if len(raw) - offset < _FRAME_HEADER.size:
                return invalid("torn record header")
            length, checksum = _FRAME_HEADER.unpack_from(raw, offset)
            offset += _FRAME_HEADER.size
            if length < 1 or length > PAGE_SIZE + 9 or offset + length > len(raw):
                return invalid("torn record body")
            record = raw[offset:offset + length]
            offset += length
            if zlib.crc32(record) & 0xFFFFFFFF != checksum:
                # A full-sized bad record is corruption, whereas an interrupted
                # append above has too few bytes. Never erase evidence of damage.
                raise StorageError("WAL record CRC mismatch; files preserved for diagnosis.")
            records.append(record)
            if record[:1] == b"C" and len(record) == commit_len and offset != len(raw):
                # This WAL format contains exactly one transaction. A complete
                # commit record followed by even one byte is anomalous, not an
                # incomplete transaction that may be discarded. Refuse recovery
                # and preserve evidence rather than losing a durable transaction.
                raise StorageError("Committed WAL has trailing bytes after commit; files preserved for diagnosis.")
        if len(records) < 2 or records[0][:1] != b"B" or records[-1][:1] != b"C":
            return invalid("missing begin or commit")
        if len(records[0]) != 1 + _BEGIN.size or len(records[-1]) != commit_len:
            return invalid("invalid transaction header")
        base, target, count = _BEGIN.unpack(records[0][1:])
        end_target, end_count = _COMMIT.unpack(records[-1][1:1 + _COMMIT.size])
        if target < base or target > 0x100000000 or end_target != target or count != end_count or count != len(records) - 2:
            return invalid("transaction extent/count mismatch")
        digest = hashlib.sha256(records[0])
        pages = []
        seen = set()
        for record in records[1:-1]:
            if record[:1] != b"P" or len(record) != 9 + PAGE_SIZE:
                return invalid("invalid page image")
            page_id = struct.unpack("<Q", record[1:9])[0]
            if page_id >= target or page_id in seen:
                return invalid("invalid or duplicate page identifier")
            try:
                _decode_page(page_id, record[9:])
            except StorageError:
                return invalid("page-image CRC mismatch")
            seen.add(page_id)
            pages.append((page_id, record[9:]))
            digest.update(record)
        if target - base > count or sum(page_id >= base for page_id in seen) != target - base:
            return invalid("missing newly allocated page")
        if digest.digest() != records[-1][-32:]:
            return invalid("transaction digest mismatch")
        return base, target, pages

    def stats(self):
        with self._mutex:
            return dict(self._metrics, cache_size=self.cache_size, cache_used=len(self._cache),
                        cache_policy=self._cache_policy, predictive_sample_size=self._sample_size,
                        cache_history_pages=len(self._cache_history), active_scans=len(self._scans),
                        cached_pages=len(self._cache), staged_pages=len(self._staged),
                        page_count=getattr(self, "_next_page_id", 0) if self._active else getattr(self, "_page_count", 0),
                        page_size=PAGE_SIZE, transaction_active=self._active, poisoned=self._poisoned)

    def close(self):
        with self._mutex:
            if self._closed:
                return
            self.rollback()
            # A caller may still hold a scan generator when the database is
            # being closed.  Drop its bookkeeping now so a retained pager
            # cannot report phantom active scans or keep traversal state alive.
            # The generator's context-manager cleanup remains safe because it
            # uses pop(token, None).
            self._scans.clear()
            self._closed = True
            if self._data_file is not None:
                self._data_file.close()
                self._data_file = None
            if self._lock_file is not None:
                try:
                    self._lock_file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                self._lock_file.close()
                self._lock_file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _rid(rid):
    if not isinstance(rid, (list, tuple)) or len(rid) != 2 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in rid):
        raise StorageError("Record identifier must be [nonnegative page, nonnegative slot].")
    return [rid[0], rid[1]]


class HeapTable:
    """Slotted heap pages; forwarding preserves RIDs when updates outgrow a page."""
    def __init__(self, pager: DiskPager, meta: dict):
        self.pager = pager
        self.meta = meta
        self._candidate_space = {}
        for field, default in (("pages", []), ("next_rid", 1), ("row_count", 0), ("indexes", [])):
            meta.setdefault(field, copy.deepcopy(default))

    def _insertion_metadata(self):
        # A legacy table without these optional hints is inspected once on its
        # next mutation. New tables initialize them before their first row.
        if "reusable_pages" not in self.meta:
            self.meta["reusable_pages"] = [page_id for page_id in self.meta["pages"]
                                           if None in self._page(page_id)["rows"]]
        self.meta.setdefault("insert_hint", self.meta["pages"][-1] if self.meta["pages"] else None)

    def _page(self, page_id, *, scan=None, next_page=None):
        page = self.pager.read_page(page_id, scan=scan, next_page=next_page)
        if page is None or page.get("kind") != "heap" or not isinstance(page.get("rows"), list):
            raise StorageError(f"Invalid heap page {page_id}.")
        return page

    def _values(self, entry, *, scan=None, next_page=None):
        if not isinstance(entry, dict):
            raise StorageError("Invalid heap record.")
        if "v" in entry and isinstance(entry["v"], dict):
            return copy.deepcopy(entry["v"])
        if "overflow" in entry:
            page = self.pager.read_page(entry["overflow"], scan=scan, next_page=next_page)
            if page is not None and page.get("kind") == "overflow" and isinstance(page.get("values"), dict):
                return copy.deepcopy(page["values"])
        raise StorageError("Invalid heap record or overflow reference.")

    def rows(self) -> Iterator[tuple[list[int], dict]]:
        with self.pager.scan_context("heap_scan") as scan:
            pages = self.meta["pages"]
            for position, page_id in enumerate(pages):
                next_page = pages[position+1] if position+1 < len(pages) else None
                page = self._page(page_id, scan=scan, next_page=next_page)
                for slot, entry in enumerate(page["rows"]):
                    if entry is not None:
                        yield [page_id, slot], self._values(entry, scan=scan, next_page=next_page)

    def get(self, rid):
        page_id, slot = _rid(rid)
        if page_id not in self.meta["pages"]:
            return None
        page = self._page(page_id)
        if slot >= len(page["rows"]) or page["rows"][slot] is None:
            return None
        return self._values(page["rows"][slot])

    def insert(self, values):
        self.pager._ensure_transaction()
        if not isinstance(values, dict):
            raise StorageError("Heap row values must be an object.")
        entry = {"v": copy.deepcopy(values)}
        empty = {"kind": "heap", "rows": [entry]}
        _payload(empty)
        self._insertion_metadata()
        # Deleted slots take priority, then the append page. Fully occupied older
        # pages are never read repeatedly on the common append path. Variable-size
        # holes that cannot fit this row remain available for a later smaller row.
        candidates = list(self.meta["reusable_pages"])
        hint = self.meta["insert_hint"]
        if hint is not None and hint not in candidates:
            candidates.append(hint)
        minimum_extra = len(_payload(entry)) - len(b"null")
        for page_id in candidates:
            if self._candidate_space.get(page_id, PAGE_SIZE) < minimum_extra:
                continue
            page = self._page(page_id)
            available = PAGE_SIZE - _PAGE_HEADER.size - len(_payload(page))
            self._candidate_space[page_id] = available
            try:
                slot = page["rows"].index(None)
                page["rows"][slot] = entry
            except ValueError:
                if page_id in self.meta["reusable_pages"]:
                    self.meta["reusable_pages"].remove(page_id)
                slot = len(page["rows"])
                page["rows"].append(entry)
            if _fits(page):
                self.pager.write_page(page_id, page)
                self._candidate_space[page_id] = PAGE_SIZE - _PAGE_HEADER.size - len(_payload(page))
                if None not in page["rows"] and page_id in self.meta["reusable_pages"]:
                    self.meta["reusable_pages"].remove(page_id)
                break
        else:
            page_id = self.pager.allocate(empty)
            slot = 0
            self.meta["pages"].append(page_id)
            self.meta["insert_hint"] = page_id
            self._candidate_space[page_id] = PAGE_SIZE - _PAGE_HEADER.size - len(_payload(empty))
        self.meta["row_count"] += 1
        self.meta["next_rid"] += 1
        return [page_id, slot]

    def update(self, rid, values):
        self.pager._ensure_transaction()
        if not isinstance(values, dict):
            raise StorageError("Heap row values must be an object.")
        page_id, slot = _rid(rid)
        if page_id not in self.meta["pages"]:
            raise StorageError("Cannot update a missing record.")
        page = self._page(page_id)
        if slot >= len(page["rows"]) or page["rows"][slot] is None:
            raise StorageError("Cannot update a missing record.")
        old = page["rows"][slot]
        entry = {"v": copy.deepcopy(values)}
        _payload({"kind": "heap", "rows": [entry]})
        page["rows"][slot] = entry
        if _fits(page):
            self.pager.write_page(page_id, page)
            self._candidate_space.pop(page_id, None)
            return
        overflow = {"kind": "overflow", "values": copy.deepcopy(values)}
        _payload(overflow)
        overflow_id = old.get("overflow")
        if overflow_id is None:
            overflow_id = self.pager.page_count
        page["rows"][slot] = {"overflow": overflow_id}
        _payload(page)
        if "overflow" in old:
            self.pager.write_page(overflow_id, overflow)
        else:
            allocated = self.pager.allocate(overflow)
            if allocated != overflow_id:
                raise StorageError("Concurrent allocation changed heap overflow placement.")
        self.pager.write_page(page_id, page)
        self._candidate_space.pop(page_id, None)

    def delete(self, rid):
        self.pager._ensure_transaction()
        page_id, slot = _rid(rid)
        if page_id not in self.meta["pages"]:
            raise StorageError("Cannot delete a missing record.")
        page = self._page(page_id)
        if slot >= len(page["rows"]) or page["rows"][slot] is None:
            raise StorageError("Cannot delete a missing record.")
        self._insertion_metadata()
        page["rows"][slot] = None
        self.pager.write_page(page_id, page)
        self.meta["row_count"] -= 1
        self._candidate_space.pop(page_id, None)
        if page_id not in self.meta["reusable_pages"]:
            self.meta["reusable_pages"].append(page_id)


def _key_order(key):
    if isinstance(key, (bool, int, float)):
        if isinstance(key, float) and not math.isfinite(key):
            raise StorageError("An index key must be finite.")
        return (0, key)
    if isinstance(key, str):
        return (1, key)
    raise StorageError("Only numeric, boolean, and text index keys are supported.")


def _composite_order(composite):
    return (_key_order(composite[0]), composite[1][0], composite[1][1])


class BPlusTree:
    """Variable fanout B+ tree with composite separators and linked leaf scans.

    Deletion keeps fence separators and empty leaves (a correct lazy-merge policy).
    Pages are never silently reused, so existing RID and root references stay stable.
    """
    def __init__(self, pager: DiskPager, index_meta: dict):
        self.pager = pager
        self.meta = index_meta
        if index_meta.get("root") is None:
            index_meta["root"] = pager.allocate({"kind": "bplus_leaf", "entries": [], "next": None})

    def _node(self, page_id, *, scan=None):
        page = self.pager.read_page(page_id, scan=scan)
        if page is None:
            raise StorageError(f"Missing B+ tree page {page_id}.")
        if page.get("kind") == "bplus_leaf":
            if not isinstance(page.get("entries"), list) or "next" not in page:
                raise StorageError(f"Malformed B+ tree leaf {page_id}.")
        elif page.get("kind") == "bplus_internal":
            if not isinstance(page.get("keys"), list) or not isinstance(page.get("children"), list) or len(page["children"]) != len(page["keys"]) + 1:
                raise StorageError(f"Malformed B+ tree branch {page_id}.")
        else:
            raise StorageError(f"Page {page_id} is not a B+ tree node.")
        return page

    def _leaf_for(self, composite, *, scan=None):
        page_id = self.meta["root"]
        seen = set()
        while True:
            if page_id in seen:
                raise StorageError("B+ tree contains a branch cycle.")
            seen.add(page_id)
            node = self._node(page_id, scan=scan)
            if node["kind"] == "bplus_leaf":
                return page_id, node
            position = bisect.bisect_right([_composite_order(x) for x in node["keys"]], _composite_order(composite))
            page_id = node["children"][position]

    def _first_leaf(self, *, scan=None):
        page_id = self.meta["root"]
        seen = set()
        while True:
            if page_id in seen:
                raise StorageError("B+ tree contains a branch cycle.")
            seen.add(page_id)
            node = self._node(page_id, scan=scan)
            if node["kind"] == "bplus_leaf":
                return page_id, node
            page_id = node["children"][0]

    def _validate_entry(self, key, rid):
        _key_order(key)
        rid = _rid(rid)
        try:
            key_bytes = json.dumps(key, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise StorageError(f"Invalid index key: {exc}") from exc
        if len(key_bytes) > MAX_INDEX_KEY_BYTES:
            raise StorageError(f"Index key exceeds the {MAX_INDEX_KEY_BYTES}-byte limit.")
        return [copy.deepcopy(key), rid]

    def insert(self, key, rid):
        if key is None:
            return
        self.pager._ensure_transaction()
        entry = self._validate_entry(key, rid)
        if self.meta.get("unique"):
            existing = self.search(key)
            if existing and entry[1] not in existing:
                raise StorageError(f"Duplicate key violates unique index {self.meta.get('name', '')}.")
        split = self._insert(self.meta["root"], entry, set())
        if split:
            separator, right = split
            old_root = self.meta["root"]
            root = {"kind": "bplus_internal", "keys": [separator], "children": [old_root, right]}
            self.meta["root"] = self.pager.allocate(root)

    def _insert(self, page_id, entry, ancestors):
        if page_id in ancestors:
            raise StorageError("B+ tree contains a branch cycle.")
        ancestors = ancestors | {page_id}
        node = self._node(page_id)
        if node["kind"] == "bplus_leaf":
            order = _composite_order(entry)
            position = bisect.bisect_left([_composite_order(x) for x in node["entries"]], order)
            if position < len(node["entries"]) and _composite_order(node["entries"][position]) == order:
                return None
            node["entries"].insert(position, entry)
            if _fits(node):
                self.pager.write_page(page_id, node)
                return None
            right_id = self.pager.page_count
            candidates = []
            for middle in range(1, len(node["entries"])):
                left = {"kind": "bplus_leaf", "entries": node["entries"][:middle], "next": right_id}
                right = {"kind": "bplus_leaf", "entries": node["entries"][middle:], "next": node["next"]}
                if _fits(left) and _fits(right):
                    candidates.append((abs(len(_payload(left)) - len(_payload(right))), middle, left, right))
            if not candidates:
                raise StorageError("Index leaf cannot be split into valid pages.")
            _, _, left, right = min(candidates, key=lambda x: x[:2])
            self.pager.allocate(right)
            self.pager.write_page(page_id, left)
            return copy.deepcopy(right["entries"][0]), right_id
        position = bisect.bisect_right([_composite_order(x) for x in node["keys"]], _composite_order(entry))
        split = self._insert(node["children"][position], entry, ancestors)
        if split is None:
            return None
        separator, right_id = split
        node["keys"].insert(position, separator)
        node["children"].insert(position + 1, right_id)
        if _fits(node):
            self.pager.write_page(page_id, node)
            return None
        candidates = []
        for middle in range(len(node["keys"])):
            left = {"kind": "bplus_internal", "keys": node["keys"][:middle], "children": node["children"][:middle + 1]}
            right = {"kind": "bplus_internal", "keys": node["keys"][middle + 1:], "children": node["children"][middle + 1:]}
            if _fits(left) and _fits(right):
                candidates.append((abs(len(_payload(left)) - len(_payload(right))), middle, left, right))
        if not candidates:
            raise StorageError("Index branch cannot be split into valid pages.")
        _, middle, left, right = min(candidates, key=lambda x: x[:2])
        right_id = self.pager.allocate(right)
        self.pager.write_page(page_id, left)
        return copy.deepcopy(node["keys"][middle]), right_id

    def _scan(self, low=None, high=None, include_low=True, include_high=True, *, access="range"):
        if low is not None:
            _key_order(low)
        if high is not None:
            _key_order(high)
        if low is not None and high is not None and _key_order(low) > _key_order(high):
            return
        context = self.pager.scan_context("index_range") if access == "range" else nullcontext(None)
        with context as scan:
            page_id, node = self._first_leaf(scan=scan) if low is None else self._leaf_for([low, [-1, -1]], scan=scan)
            seen = set()
            while True:
                if page_id in seen:
                    raise StorageError("B+ tree contains a linked-leaf cycle.")
                seen.add(page_id)
                self.pager.advance_scan(scan, node["next"])
                for key, rid in node["entries"]:
                    order = _key_order(key)
                    if low is not None and (order < _key_order(low) or (order == _key_order(low) and not include_low)):
                        continue
                    if high is not None and (order > _key_order(high) or (order == _key_order(high) and not include_high)):
                        return
                    yield copy.deepcopy(key), list(rid)
                page_id = node["next"]
                if page_id is None:
                    return
                node = self._node(page_id, scan=scan)
                if node["kind"] != "bplus_leaf":
                    raise StorageError("A leaf link points to a branch page.")

    def search(self, key):
        if key is None:
            return []
        return [rid for _, rid in self._scan(key, key, access="point")]

    def range(self, low=None, high=None, include_low=True, include_high=True):
        return [rid for _, rid in self._scan(low, high, include_low, include_high)]

    def items(self):
        return self._scan()

    def remove(self, key, rid):
        if key is None:
            return
        self.pager._ensure_transaction()
        entry = self._validate_entry(key, rid)
        page_id, node = self._leaf_for(entry)
        order = _composite_order(entry)
        position = bisect.bisect_left([_composite_order(x) for x in node["entries"]], order)
        if position < len(node["entries"]) and _composite_order(node["entries"][position]) == order:
            node["entries"].pop(position)
            self.pager.write_page(page_id, node)

    def validate(self):
        """Check ordering, separator fences, balanced depth, and leaf-chain topology."""
        visited = set()
        leaves = []
        depths = set()

        def visit(page_id, low, high, depth):
            if page_id in visited:
                raise StorageError("B+ tree reuses or cycles a node.")
            visited.add(page_id)
            node = self._node(page_id)
            if node["kind"] == "bplus_leaf":
                orders = [_composite_order(x) for x in node["entries"]]
                if orders != sorted(set(orders)) or any((low is not None and x < low) or (high is not None and x >= high) for x in orders):
                    raise StorageError("B+ leaf ordering or separator fence is invalid.")
                leaves.append((page_id, node))
                depths.add(depth)
                return
            keys = [_composite_order(x) for x in node["keys"]]
            if keys != sorted(set(keys)) or any((low is not None and x < low) or (high is not None and x >= high) for x in keys):
                raise StorageError("B+ branch ordering or separator fence is invalid.")
            for i, child in enumerate(node["children"]):
                visit(child, low if i == 0 else keys[i - 1], high if i == len(keys) else keys[i], depth + 1)

        visit(self.meta["root"], None, None, 0)
        if len(depths) != 1:
            raise StorageError("B+ leaves have unequal depths.")
        for i, (_, leaf) in enumerate(leaves):
            expected = leaves[i + 1][0] if i + 1 < len(leaves) else None
            if leaf["next"] != expected:
                raise StorageError("B+ linked leaves disagree with tree traversal.")
        return {"pages": len(visited), "leaves": len(leaves), "height": next(iter(depths)) + 1,
                "entries": sum(len(node["entries"]) for _, node in leaves)}
