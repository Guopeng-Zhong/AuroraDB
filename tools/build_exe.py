"""Package the engine and its teaching assets into one Windows executable.

Install the build-only dependency with:
python -m pip install --target .build/deps pyinstaller
Then: python tools/build_exe.py
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'.build'/'deps'))
import PyInstaller.__main__
PyInstaller.__main__.run([
    str(ROOT/'tools'/'launcher.py'), '--name','AuroraDB','--onefile','--console','--noconfirm',
    '--paths',str(ROOT),'--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'.build'/'exe'),
    '--specpath',str(ROOT/'.build'),'--add-data',str(ROOT/'auroradb'/'web')+';auroradb/web',
    '--add-data',str(ROOT/'auroradb'/'examples')+';auroradb/examples',
    '--exclude-module','sqlite3','--exclude-module','tkinter','--exclude-module','numpy',
    '--exclude-module','pandas','--exclude-module','matplotlib','--exclude-module','PIL',
    '--exclude-module','scipy','--exclude-module','pytest','--exclude-module','IPython',
])
