"""Create and verify a clean, source-complete course submission archive."""
import hashlib
import json
from datetime import datetime,timezone,timedelta
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from auroradb import __version__
EXCLUDED={'.build','build','__pycache__','data','.git','qa','tmp','presentation'}


def include_file(path):
    """Keep local credentials out even when a custom JSON filename was used."""
    name = path.name.casefold()
    if (any(x.casefold() in EXCLUDED for x in path.relative_to(ROOT).parts)
            or name in {'contract.txt','manifest.json','.env','pages.dat','redo.wal',
                        'auroradb_四人答辩运行与讲解指南.pdf'}
            or name.endswith('.local.json')
            or (name.startswith('.env.') and name!='.env.example')
            or path.suffix.casefold() in {'.pyc','.spec','.log','.db','.sqlite','.sqlite3'}
            or any(name.endswith(suffix) for suffix in ('.db-wal','.db-shm','.sqlite-wal',
                     '.sqlite-shm','.sqlite3-wal','.sqlite3-shm'))):
        return False
    if path.suffix.lower()=='.json' and path.stat().st_size<=16*1024:
        try:
            value=json.loads(path.read_text(encoding='utf-8-sig'))
        except (UnicodeError,ValueError):
            value=None
        if isinstance(value,dict) and value.get('api_key'):
            return False
    return True


def main():
    required=['dist/AuroraDB.exe','启动工作台.cmd','README.md',
              'docs/AuroraDB_设计与测试报告.pdf','docs/AuroraDB_使用说明.pdf',
              'docs/AuroraDB_项目论文.pdf','docs/AuroraDB_软件工程文档.pdf',
              'docs/RESEARCH_DESIGN.txt',
              'docs/test_results.json','docs/benchmark.json','docs/executable_smoke.json',
              'docs/research_executable_smoke.json','docs/research_ui_smoke.json','docs/pdf_layout_qa.json',
              'docs/optimizer_research.json','docs/buffer_research.json','docs/semantic_research.json',
              'docs/chinook_import.json']
    assert all((ROOT/p).is_file() for p in required),'A required deliverable is missing'
    assert json.loads((ROOT/'docs/test_results.json').read_text(encoding='utf-8'))['passed']
    assert json.loads((ROOT/'docs/executable_smoke.json').read_text(encoding='utf-8'))['passed']
    evidence_names = ['test_results.json','optimizer_research.json','buffer_research.json',
                      'semantic_research.json','research_ui_smoke.json','research_executable_smoke.json','pdf_layout_qa.json']
    campus_path = ROOT/'docs/campus_api_smoke.json'
    if campus_path.is_file():
        evidence_names.append('campus_api_smoke.json')
    for optional in ('sql_edge_audit.json','deep_review.json'):
        if (ROOT/'docs'/optional).is_file():
            evidence_names.append(optional)
    chinook = json.loads((ROOT/'docs/chinook_import.json').read_text(encoding='utf-8'))
    snapshot = ROOT/chinook['snapshot']
    assert snapshot.is_file(), 'Chinook snapshot is missing'
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == chinook['snapshot_sha256'], 'Stale Chinook snapshot evidence'
    assert chinook.get('total_rows') == 15607 and len(chinook.get('tables', {})) == 11, 'Invalid Chinook dataset evidence'
    exe_digest = hashlib.sha256((ROOT/'dist/AuroraDB.exe').read_bytes()).hexdigest()
    for filename in evidence_names:
        value=json.loads((ROOT/'docs'/filename).read_text(encoding='utf-8'))
        assert value['passed'],f'Failed evidence: {filename}'
        for source,digest in value.get('source_sha256',{}).items():
            assert hashlib.sha256((ROOT/source).read_bytes()).hexdigest()==digest,f'Stale evidence: {filename}: {source}'
        if 'exe_sha256' in value:
            assert value['exe_sha256']==exe_digest,f'Stale EXE evidence: {filename}'
    smoke=json.loads((ROOT/'docs/research_executable_smoke.json').read_text(encoding='utf-8'))
    assert smoke['exe_sha256']==hashlib.sha256((ROOT/'dist/AuroraDB.exe').read_bytes()).hexdigest(),'Stale research EXE evidence'
    ui=json.loads((ROOT/'docs/research_ui_smoke.json').read_text(encoding='utf-8'))
    assert ui['exe_sha256']==smoke['exe_sha256'],'UI evidence must reference the same final EXE'
    if campus_path.is_file():
        campus = json.loads(campus_path.read_text(encoding='utf-8'))
        assert campus['exe_sha256']==smoke['exe_sha256'],'Stale campus API EXE evidence'
    files=sorted(p for p in ROOT.rglob('*') if p.is_file() and include_file(p))
    entries=[]
    for p in files:
        entries.append({'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,
                        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    manifest={'project':'AuroraDB','version':__version__,
              'packaged_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'files':entries}
    (ROOT/'MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    archive=ROOT.parent/'AuroraDB_提交包.zip'
    with ZipFile(archive,'w',compression=ZIP_DEFLATED,compresslevel=9) as bundle:
        for p in files: bundle.write(p, 'AuroraDB/'+p.relative_to(ROOT).as_posix())
        bundle.write(ROOT/'MANIFEST.json','AuroraDB/MANIFEST.json')
    with ZipFile(archive) as bundle:
        assert bundle.testzip() is None,'Archive CRC verification failed'
        for entry in entries:
            assert hashlib.sha256(bundle.read('AuroraDB/'+entry['path'])).hexdigest()==entry['sha256']
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.sha256.txt').write_text(digest+'  '+archive.name+'\n',encoding='utf-8')
    print(json.dumps({'archive':str(archive),'bytes':archive.stat().st_size,'files':len(entries)+1,'sha256':digest},ensure_ascii=True,indent=2))


if __name__=='__main__': main()
