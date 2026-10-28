"""Serial, result-verified buffer experiment; no claimed physical-disk counters.

Run ``python tools/buffer_experiment.py --quick`` for a small reproducible grid.
Full mode varies three capacities and four sample sizes with three repetitions.
Only this tool uses its scratch fixture, leaving user databases untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from auroradb.storage import BPlusTree, DiskPager, HeapTable, PAGE_SIZE

SEED = 20261010
PAPER = "https://www.vldb.org/pvldb/vol18/p5569-khalaji.pdf"


def percentile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered)-1)*probability
    lower = int(position)
    upper = min(lower+1, len(ordered)-1)
    return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)


def make_fixture(directory, count):
    records = [{"id": i, "reading": i*17 % 997, "note": f"row-{i:06d}-"+"x"*240}
               for i in range(count)]
    with DiskPager(directory) as pager:
        pager.begin()
        pager.allocate({"kind": "catalog"})
        table = {"pages": []}
        index = {"root": None, "name": "id_index", "unique": True}
        heap, tree = HeapTable(pager, table), BPlusTree(pager, index)
        for record in records:
            tree.insert(record["id"], heap.insert(record))
        pager.write_page(0, {"kind": "catalog", "table": table, "index": index})
        pager.commit()
        topology = tree.validate()
    # Warm file contents through ordinary reads for every policy; never flush
    # the machine's OS cache. Page misses below are still actual pager file reads.
    with open(Path(directory)/"pages.dat", "rb") as source:
        while source.read(1024*1024):
            pass
    return records, {"rows": count, "heap_pages": len(table["pages"]),
                     "index": topology, "data_file_bytes": (Path(directory)/"pages.dat").stat().st_size}


def workloads(count, quick):
    randomizer = random.Random(SEED)
    rounds = 4 if quick else 8
    hot = [int(count*fraction) for fraction in (0.03, 0.18, 0.31, 0.46)]
    mixed = []
    for phase in range(rounds):
        mixed += [("point", randomizer.choice(hot), None, phase) for _ in range(32)]
        mixed.append(("scan", None, None, phase))
        mixed += [("point", randomizer.choice(hot), None, phase) for _ in range(32)]
    width = max(80, count//5)
    low = count//4
    repeated = [("range", low, low+width-1, phase) for phase in range(rounds*3)]
    migration = []
    for phase in range(4):
        start = count*phase//4
        moving_hot = [start+offset for offset in (2, 16, 31, 47)]
        for cycle in range(2 if quick else 4):
            migration += [("point", randomizer.choice(moving_hot), None, phase) for _ in range(32)]
            migration.append(("scan", None, None, phase))
            migration += [("point", randomizer.choice(moving_hot), None, phase) for _ in range(16)]
    pure = [("scan", None, None, phase) for phase in range(rounds)]
    return {"hot_points_with_large_scans": mixed, "repeated_ranges": repeated,
            "hotspot_migration": migration, "pure_scans": pure}


def expected_for(operation, records):
    kind, low, high, _ = operation
    if kind == "point":
        return [records[low]]
    if kind == "range":
        return records[low:high+1]
    return records


def measure(directory, records, operations, workload, capacity, policy, sample, trial):
    with DiskPager(directory, cache_size=capacity) as pager:
        catalog = pager.read_page(0)
        heap = HeapTable(pager, catalog["table"])
        tree = BPlusTree(pager, catalog["index"])
        pager.clear_cache()
        pager.set_cache_policy(policy, sample_size=sample)
        before = pager.stats()
        duration = []
        reads = []
        digest = hashlib.sha256()
        phases = {}
        returned_rows = 0
        for operation in operations:
            kind, low, high, phase = operation
            before_reads = pager.stats()["disk_reads"]
            start = time.perf_counter_ns()
            if kind == "point":
                actual = [heap.get(rid) for rid in tree.search(low)]
            elif kind == "range":
                actual = [heap.get(rid) for rid in tree.range(low, high)]
            else:
                actual = [values for _, values in heap.rows()]
            duration.append((time.perf_counter_ns()-start)/1_000_000)
            operation_reads = pager.stats()["disk_reads"]-before_reads
            reads.append(operation_reads)
            expected = expected_for(operation, records)
            if actual != expected:
                raise AssertionError(f"Result mismatch in {workload}, {policy}, trial {trial}, {operation}")
            digest.update(json.dumps(actual, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
            digest.update(b"\n")
            returned_rows += len(actual)
            bucket = phases.setdefault(str(phase), {"operations": 0, "file_page_reads": 0, "elapsed_ms": 0.0})
            bucket["operations"] += 1
            bucket["file_page_reads"] += operation_reads
            bucket["elapsed_ms"] += duration[-1]
        after = pager.stats()
        delta = {key: after[key]-before[key] for key in (
            "cache_hits", "cache_misses", "disk_reads", "cache_evictions", "predictive_evictions",
            "predictive_fallbacks", "predictive_samples", "cache_policy_cpu_ns", "scan_page_accesses")}
        # Additive diagnostic; data, workloads and measured operation boundaries
        # are unchanged. Older pager implementations report zero bypasses.
        delta["predictive_admission_bypasses"] = (
            after.get("predictive_admission_bypasses", 0)
            - before.get("predictive_admission_bypasses", 0))
        count_reads = delta["cache_hits"]+delta["cache_misses"]
        assert delta["disk_reads"] == delta["cache_misses"] == sum(reads)
        assert pager.cache_policy_status()["active_scans"] == 0
        return {"workload": workload, "capacity_pages": capacity, "policy": policy,
                "sample_size": sample, "trial": trial, "operations": len(operations),
                "returned_rows": returned_rows, "matched_oracle": True,
                "result_sha256": digest.hexdigest(), "file_page_reads": delta["disk_reads"],
                "file_page_bytes": delta["disk_reads"]*PAGE_SIZE,
                "hit_ratio": delta["cache_hits"]/count_reads if count_reads else 0.0,
                "total_operation_ms": sum(duration), "operation_p50_ms": percentile(duration, 0.5),
                "operation_p95_ms": percentile(duration, 0.95),
                "operation_p95_file_reads": percentile(reads, 0.95),
                "policy_bookkeeping_ns_per_access": delta["cache_policy_cpu_ns"]/count_reads if count_reads else 0.0,
                "phase_metrics": phases, "counters": delta, "policy_status": pager.cache_policy_status()}


def compare(runs, capacities, samples, names):
    comparisons = []
    for name in names:
        for capacity in capacities:
            baseline = [run for run in runs if run["workload"] == name and
                        run["capacity_pages"] == capacity and run["policy"] == "lru"]
            lru_reads = statistics.mean(run["file_page_reads"] for run in baseline)
            lru_time = statistics.mean(run["total_operation_ms"] for run in baseline)
            for sample in samples:
                treatment = [run for run in runs if run["workload"] == name and
                             run["capacity_pages"] == capacity and run["policy"] == "predictive" and
                             run["sample_size"] == sample]
                treatment_reads = statistics.mean(run["file_page_reads"] for run in treatment)
                comparisons.append({"workload": name, "capacity_pages": capacity, "sample_size": sample,
                    "lru_mean_file_page_reads": lru_reads, "predictive_mean_file_page_reads": treatment_reads,
                    "file_read_reduction_pct": 100*(lru_reads-treatment_reads)/lru_reads if lru_reads else 0.0,
                    "lru_mean_total_ms": lru_time,
                    "predictive_mean_total_ms": statistics.mean(run["total_operation_ms"] for run in treatment),
                    "lru_mean_p95_ms": statistics.mean(run["operation_p95_ms"] for run in baseline),
                    "predictive_mean_p95_ms": statistics.mean(run["operation_p95_ms"] for run in treatment),
                    "result_match": len({run["result_sha256"] for run in baseline+treatment}) == 1})
    return comparisons


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--output", type=Path, default=PROJECT/"docs"/"buffer_research.json")
    args = parser.parse_args(argv)
    source_path = PROJECT/"auroradb"/"storage.py"
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    count = 1500 if args.quick else 6000
    capacities = [8, 24] if args.quick else [8, 24, 64]
    samples = [2, 8, 32] if args.quick else [1, 4, 8, 32]
    trials = 2 if args.quick else 3
    operations = workloads(count, args.quick)
    runs = []
    scratch_root = PROJECT/".build"/"tmp"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="buffer-", dir=scratch_root) as folder:
        records, fixture = make_fixture(folder, count)
        configurations = [(name, capacity, policy, sample, trial)
            for name in operations for capacity in capacities for trial in range(trials)
            for policy, sample in [("lru", 8)]+[("predictive", value) for value in samples]]
        random.Random(SEED).shuffle(configurations)
        for position, (name, capacity, policy, sample, trial) in enumerate(configurations, 1):
            runs.append(measure(folder, records, operations[name], name, capacity, policy, sample, trial))
            if position % 16 == 0:
                print(f"Verified {position}/{len(configurations)} buffer runs", flush=True)
    comparisons = compare(runs, capacities, samples, operations)
    assert all(row["result_match"] for row in comparisons)
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source_digest:
        raise RuntimeError("Storage source changed during the experiment; evidence was not published.")
    report = {
        "experiment": "serial_scan_aware_buffer_v1", "created_utc": datetime.now(timezone.utc).isoformat(),
        "passed": bool(runs and comparisons) and all(run["matched_oracle"] for run in runs)
                  and all(row["result_match"] for row in comparisons),
        "source_sha256": {"auroradb/storage.py": source_digest},
        "paper": {"title": "Sampling-based Predictive Database Buffer Management", "year": 2025,
                  "venue": "PVLDB 18(13):5569-5581", "doi": "10.14778/3773731.3773734", "url": PAPER},
        "implementation": "Research-inspired serial adaptation: explicit next-page scan hints, bounded decaying weighted history, low-reuse scan admission bypass, sampled eviction, LRU fallback; not a reproduction of concurrent PBM.",
        "environment": {"python": sys.version, "platform": platform.platform(), "page_size": PAGE_SIZE, "seed": SEED},
        "settings": {"quick": args.quick, "trials": trials, "cache_capacities_pages": capacities,
                     "predictive_sample_sizes": samples, "configuration_order": "seeded shuffle"},
        "fixture": fixture,
        "workloads": {"hot_points_with_large_scans": "Four spread hot keys, point reads around whole-table scans; operations serialized.",
                      "repeated_ranges": "Repeated identical B+ range traversal followed by existing HeapTable.get RID fetches.",
                      "hotspot_migration": "Four moving hot sets with intervening full scans; phase metrics retained.",
                      "pure_scans": "Repeated whole-table scans exceeding each buffer capacity."},
        "metric_notes": ["file_page_reads counts actual pager reads of 4096-byte file pages, NOT physical disk I/O.",
                         "OS file cache was warmed, never flushed. Each run starts with an empty application cache and policy history.",
                         "Operation timing excludes result equality checks and hashing; includes Python decoding and deepcopy.",
                         "cache_policy_cpu_ns is instrumented bookkeeping wall time, not hardware CPU-cycle profiling.",
                         "Policy metadata is bounded by four records per cache page; resident arrays also use O(cache pages) memory."],
        "limitations": ["Single-process serialized workload; no synthetic concurrent scans and no prediction of future queries.",
                        "Index range RID fetching retains the engine's existing point-read semantics; only B+ traversal carries scan tags.",
                        "Results may regress in latency even when file reads decrease; report all grid cells including regressions.",
                        "Small controlled fixtures are not TPC-H/Redshift reproduction or evidence of publication-level novelty.",
                        "Fixed seed yields deterministic eviction decisions; timing remains machine- and OS-cache-dependent."],
        "runs": runs, "comparisons": comparisons,
        "summary": {"verified_runs": len(runs), "verified_comparisons": len(comparisons),
                    "all_results_match": True, "best_file_read_reduction_pct": max(row["file_read_reduction_pct"] for row in comparisons),
                    "worst_file_read_reduction_pct": min(row["file_read_reduction_pct"] for row in comparisons)}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))
    print(f"Saved {args.output}")
    return report


if __name__ == "__main__":
    main()
