"""Verify the shipped EXE without invoking any installed Python runtime."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request,urlopen
ROOT=Path(__file__).resolve().parents[1]
EXE=ROOT/'dist'/'AuroraDB.exe'


def main():
    checks=[]
    child_env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
    def run(folder, *args, expect=0):
        p=subprocess.run([str(EXE),'--db',str(folder),*args],cwd=tempfile.gettempdir(),
                         env=child_env,capture_output=True,timeout=30)
        assert p.returncode==expect,(p.returncode,p.stdout,p.stderr)
        return p.stdout.decode('utf-8',errors='replace')
    with tempfile.TemporaryDirectory(prefix='AuroraDB exe ') as temp:
        folder=Path(temp)/'database'
        output=run(folder,'--demo','--execute','SELECT COUNT(*) FROM students;')
        assert '12' in output; checks.append('EXE bundled demo assets and query execution')
        output=run(folder,'--execute','SELECT id FROM students WHERE id=1005;')
        assert '1005' in output; checks.append('EXE database persistence across independent processes')
        run(folder,'--execute','SELECT missing FROM students;',expect=1)
        checks.append('EXE error exit status')
        output=run(folder,'--execute',
                   'CREATE TABLE numeric_limits(value REAL); '
                   'INSERT INTO numeric_limits VALUES (1e308),(1e308); '
                   'SELECT ROUND(2.5),ROUND(-2.5),AVG(value) FROM numeric_limits;')
        assert '3.0' in output and '-3.0' in output and '1e+308' in output,output
        checks.append('EXE bundled Decimal rounding and finite mean with overflowing intermediate sum')
        output=run(folder,'--execute','CHECK DATABASE;')
        assert 'Database integrity OK' in output,output
        checks.append('EXE bundled physical integrity checker through SQL')
        ai_config=Path(temp)/'ai.local.json'
        ai_config.write_text(json.dumps({'mode':'mock'}),encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        proc=subprocess.Popen([str(EXE),'--db',str(folder),'--web','--no-browser','--port',str(port),'--ai-config',str(ai_config)],
                              cwd=tempfile.gettempdir(),env=child_env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              creationflags=creationflags)
        base=f'http://127.0.0.1:{port}'
        try:
            token=None
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                if proc.poll() is not None: raise AssertionError(proc.communicate())
                try:
                    with urlopen(base+'/api/session',timeout=.5) as response:
                        token=json.load(response)['token']; break
                except OSError: time.sleep(.1)
            assert token,'EXE web server did not start'
            with urlopen(base+'/',timeout=2) as response:
                markup=response.read().decode('utf-8')
                assert 'SQL 工作台' in markup and 'EXPLORE YOUR DATA' not in markup and '让每一次查询清晰可见' not in markup
            with urlopen(base+'/styles.css',timeout=2) as response:
                assert 'color-scheme:light' in response.read().decode('utf-8')
            request=Request(base+'/api/query',data=json.dumps({'sql':'SELECT COUNT(*) FROM students s LEFT JOIN enrollments e ON s.id=e.student_id;'}).encode(),headers={'Content-Type':'application/json','X-Aurora-Token':token})
            with urlopen(request,timeout=5) as response: data=json.load(response)
            assert data['results'][0]['rows']==[[21]]
            checks.append('EXE bundled HTML JS CSS and authenticated HTTP SQL API')
            def post(path,payload):
                request=Request(base+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Aurora-Token':token})
                with urlopen(request,timeout=5) as response:
                    return json.load(response)
            integrity=post('/api/integrity',{})
            assert integrity['ok'] and integrity['summary']['rows']==38,integrity
            checks.append('EXE authenticated physical integrity HTTP API')
            comparison=post('/api/compare',{'sql':'SELECT id FROM students WHERE id=1005;'})
            assert comparison['matched'] and comparison['scan']['stats']['rows_scanned']==12 and comparison['optimized']['stats']['rows_scanned']==1,comparison
            checks.append('EXE authenticated scan and index comparison with real counters')
            with urlopen(base+'/api/ai/status',timeout=2) as response:
                status=json.load(response)
            assert status['mode']=='mock' and status['available'] and 'api_key' not in status,status
            suggestion=post('/api/ai',{'action':'generate','prompt':'查找 GPA 大于 3.8 的学生'})
            assert suggestion['mode']=='mock' and suggestion['validation']['valid'] and suggestion['validation']['read_only'],suggestion
            assert post('/api/query',{'sql':'SELECT COUNT(*) FROM students;'})['results'][0]['rows']==[[12]]
            checks.append('EXE local JSON AI configuration and explicitly labeled nonexecuting mock SQL suggestion')
        finally:
            # One-file PyInstaller launches a child. Stop the owned process tree,
            # otherwise killing only the bootloader leaves a live DB writer.
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=10)
            else:
                proc.terminate()
            proc.communicate(timeout=10)
        output=run(folder,'--execute','SELECT COUNT(*) FROM enrollments;')
        assert '19' in output; checks.append('EXE reopen after server process termination')
    evidence={'passed':True,'checks':checks,'exe_bytes':EXE.stat().st_size,
              'note':'Executed from a separate working directory with a temporary database; no external Python executable or database engine launched by the EXE.'}
    (ROOT/'docs'/'executable_smoke.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=True,indent=2))


if __name__=='__main__': main()
