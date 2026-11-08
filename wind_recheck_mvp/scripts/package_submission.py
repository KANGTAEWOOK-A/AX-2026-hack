#!/usr/bin/env python3
"""Build a source-only submission ZIP from an explicit allowlist; no datasets or secrets."""
import hashlib
import json
from pathlib import Path
import zipfile
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
files=[]
for folder in ('app','scripts','tests'):
    files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file()
                 and '__pycache__' not in p.parts and p.suffix in {'.py','.html','.css','.js','.sh'})
for name in ('.env.example','.gitignore','README.md','VALIDATION.md','DEPLOYMENT.md','Mac_Quick_Start.md',
             'Start_Wind_Recheck_Mac.command','requirements.txt','requirements.lock.txt','pytest.ini','data/.gitkeep',
             'reports/final_metrics.json','reports/agentstream_api_smoke.json','reports/agentstream_web_smoke.json'):
    p=ROOT/name
    if p.exists():files.append(p)
secrets=[v for k,v in dotenv_values(ROOT/'.env').items()
         if v and len(v)>=12 and (k.endswith('_API_KEY') or k.endswith('_AUTH_VALUE'))]
entries={}
for p in sorted(set(files)):
    blob=p.read_bytes()
    if any(line.startswith(b'-----BEGIN ') and b'PRIVATE KEY-----' in line for line in blob.splitlines()):
        raise SystemExit('비공개 키 내용이 감지되어 패키지를 만들지 않았습니다.')
    if any(value.encode() in blob for value in secrets):
        raise SystemExit('실제 인증값이 감지되어 패키지를 만들지 않았습니다.')
    entries[str(p.relative_to(ROOT))]=hashlib.sha256(blob).hexdigest()
out=ROOT/'output/submission';out.mkdir(parents=True,exist_ok=True)
zip_path=out/'Wind_Recheck_9팀_소스코드.zip'
with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
    for name in entries:z.write(ROOT/name,'wind_recheck_mvp/'+name)
    z.writestr('wind_recheck_mvp/SOURCE_SHA256.json',json.dumps(entries,ensure_ascii=False,indent=2))
with zipfile.ZipFile(zip_path) as z:
    assert z.testzip() is None
    assert not any(p.endswith(('.pem','.key','.sqlite3','.joblib')) or Path(p).name=='.env' for p in z.namelist())
print(json.dumps({'path':str(zip_path),'source_files':len(entries),'sha256':hashlib.sha256(zip_path.read_bytes()).hexdigest()},ensure_ascii=False))
