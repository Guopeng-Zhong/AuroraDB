"""Reproduce the five-round comparison without overwriting archived evidence."""
import argparse, hashlib, importlib.util, json, platform, random, statistics, sys, tempfile
from pathlib import Path


def main():
    root=Path(__file__).resolve().parent
    PROJECT=root.parents[1]
    sys.path.insert(0,str(PROJECT))
    from auroradb import storage as new
    parser=argparse.ArgumentParser(description="Compare the archived pre-admission implementation with current storage.")
    parser.add_argument("--output",type=Path,default=PROJECT/"docs"/"qa"/"paired_buffer_comparison.json")
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('storage_before',root/'storage_before.py')
    old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
    spec=importlib.util.spec_from_file_location('buffer_exp',PROJECT/'tools'/'buffer_experiment.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    config={'workload':'hot_points_with_large_scans','rows':6000,'capacity_pages':24,'sample_size':8,'trials':5,'order_seed':20261028}
    runs=[]
    with tempfile.TemporaryDirectory() as folder:
     records,fixture=m.make_fixture(folder,config['rows'])
     operations=m.workloads(config['rows'],False)[config['workload']]
     for trial in range(config['trials']):
      variants=[('original_lru',old,'lru'),('original_predictive',old,'predictive'),('optimized_predictive',new,'predictive'),('optimized_lru',new,'lru')]
      random.Random(config['order_seed']+trial).shuffle(variants)
      for variant,module,policy in variants:
       m.DiskPager,m.HeapTable,m.BPlusTree=module.DiskPager,module.HeapTable,module.BPlusTree
       run=m.measure(folder,records,operations,config['workload'],24,policy,8,trial)
       run['variant']=variant;runs.append(run)
      print(f'Paired trial {trial+1}/{config["trials"]} verified',flush=True)
    assert len({r['result_sha256'] for r in runs})==1
    summary={}
    for variant in ['original_lru','original_predictive','optimized_lru','optimized_predictive']:
     rr=[r for r in runs if r['variant']==variant]
     summary[variant]={'mean_total_ms':statistics.mean(r['total_operation_ms'] for r in rr),
      'median_total_ms':statistics.median(r['total_operation_ms'] for r in rr),
      'trials_ms':[r['total_operation_ms'] for r in rr],
      'mean_bookkeeping_ms':statistics.mean(r['counters']['cache_policy_cpu_ns']/1e6 for r in rr),
      'mean_p95_ms':statistics.mean(r['operation_p95_ms'] for r in rr),
      'file_page_reads':sorted({r['file_page_reads'] for r in rr})}
    p={'config':config,'environment':{'python':sys.version,'platform':platform.platform()},
       'source_sha256':{'before':hashlib.sha256((root/'storage_before.py').read_bytes()).hexdigest(),
        'after':hashlib.sha256(Path(new.__file__).read_bytes()).hexdigest()},
       'all_results_match':True,'summary':summary,'runs':runs}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(p,indent=2))
    print(json.dumps(summary,indent=2))


if __name__ == "__main__":
    main()
