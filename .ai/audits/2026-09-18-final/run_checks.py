"""Полный набор проверок с отдельными хранилищами и ограничением времени."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

assert os.environ['DB_URL'].endswith('/orderflow_audit_tests_20260918')
results=[]
for path in sorted(Path('tests').glob('test_*.py')):
    started=time.monotonic()
    try:
        result=subprocess.run([sys.executable,'-m','tests.'+path.stem],capture_output=True,text=True,timeout=90)
        row={'test':path.stem,'code':result.returncode,'seconds':round(time.monotonic()-started,3),'stdout':result.stdout,'stderr':result.stderr}
    except subprocess.TimeoutExpired as exc:
        row={'test':path.stem,'code':124,'seconds':90,'stdout':str(exc.stdout),'stderr':str(exc.stderr)}
    results.append(row)
    print(json.dumps(row,ensure_ascii=False),flush=True)
sys.exit(int(any(row['code'] for row in results)))
