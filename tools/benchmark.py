"""Reproducible scan/index experiment; all numbers come from actual execution."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auroradb.engine import Database


def measure(directory, sql):
    with Database(directory,cache_size=16) as db:
        cold=db.execute(sql)[-1]
        warm=db.execute(sql)[-1]
        return {"cold":cold,"warm":warm,"database_stats":db.stats()}


def main():
    count=5000
    with tempfile.TemporaryDirectory() as folder:
        with Database(folder,cache_size=16) as db:
            db.execute('CREATE TABLE measurements (id INTEGER PRIMARY KEY, code INTEGER, reading REAL, note TEXT);')
            values=','.join(f"({i},{i*17%count},{i%997/10.0},'sample-{i:05d}-"+"x"*48+"')" for i in range(count))
            db.execute('INSERT INTO measurements VALUES '+values+';')
        query='SELECT id,reading FROM measurements WHERE code=3210;'
        scan=measure(folder,query)
        with Database(folder,cache_size=16) as db:
            db.execute('CREATE INDEX code_idx ON measurements(code);')
        index=measure(folder,query)
        assert scan['cold']['rows']==index['cold']['rows']
        result={"seed":20261009,"rows":count,"cache_pages":16,"query":query,
                "table_scan":scan,"index_scan":index,
                "scanned_row_reduction":scan['cold']['stats']['rows_scanned']/max(1,index['cold']['stats']['rows_scanned']),
                "data_file_bytes":(Path(folder)/'pages.dat').stat().st_size,
                "note":"Elapsed time is environment-specific. Index planning reads candidate entries; rows_scanned counts fetched heap records. Warm scan can still miss with a cache smaller than the working set."}
    out=Path(__file__).resolve().parents[1]/'docs'/'benchmark.json'
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({"rows":count,"scan_ms":scan['cold']['elapsed_ms'],"index_ms":index['cold']['elapsed_ms'],"scan_records":scan['cold']['stats']['rows_scanned'],"index_records":index['cold']['stats']['rows_scanned'],"scan_reads":scan['cold']['stats']['disk_reads'],"index_reads":index['cold']['stats']['disk_reads']},indent=2))


if __name__=='__main__': main()
