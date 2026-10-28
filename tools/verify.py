"""Run the full suite and retain machine-readable evidence for the report."""
import json
import hashlib
import sys
import time
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class EvidenceResult(unittest.TextTestResult):
    def startTestRun(self): self.cases=[]; super().startTestRun()
    def addSuccess(self,test): self.cases.append({"test":test.id(),"status":"passed"}); super().addSuccess(test)
    def addFailure(self,test,error): self.cases.append({"test":test.id(),"status":"failed"}); super().addFailure(test,error)
    def addError(self,test,error): self.cases.append({"test":test.id(),"status":"error"}); super().addError(test,error)

if __name__=='__main__':
    start=time.perf_counter()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'),top_level_dir=str(ROOT))
    result=unittest.TextTestRunner(verbosity=2,resultclass=EvidenceResult).run(suite)
    evidence={"timestamp":datetime.now(timezone(timedelta(hours=8))).isoformat(),"python":sys.version,
              "tests_run":result.testsRun,"passed":result.wasSuccessful(),"failures":len(result.failures),
              "errors":len(result.errors),"duration_seconds":round(time.perf_counter()-start,3),"cases":result.cases,
              "differential_selects":180,"differential_seed":20261009,
              "source_sha256":{path.relative_to(ROOT).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                               for directory in ('auroradb','tests')
                               for path in sorted((ROOT/directory).rglob('*'))
                               if path.is_file() and path.suffix in {'.py','.html','.css','.js'}}}
    (ROOT/'docs'/'test_results.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
