#!/usr/bin/env python3
"""Real configured API smoke test. Synthetic note; not field model accuracy evidence."""
import asyncio
import json
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from app.agentstream import classify_note, AgentStreamError

async def main():
    note='후연부 표면에 가느다란 자국이 보입니다. 균열 여부는 확실하지 않습니다. 다른 각도에서 다시 촬영해 주세요. 검토 담당자는 아직 미정입니다.'
    expected=[{'observation'},{'uncertainty'},{'requested_action'},{'assignee'}]
    try:
        result=await classify_note(note)
    except AgentStreamError as e:
        print(json.dumps({'ok':False,'status':e.status,'detail':str(e)},ensure_ascii=False))
        raise SystemExit(1)
    correct=sum(set(row['tags'])==target for row,target in zip(result['segments'],expected))
    report={'state':'completed','source':'Actual AgentStream external REST API',
            'checked_at':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
            'data_type':'Synthetic developer smoke-test note, not industrial ground truth',
            'claim_boundary':'Single functional example; not a quality benchmark or measured workflow improvement',
            'sentence_count':len(expected),'exact_tag_matches':correct,'result':result}
    path=ROOT/'reports/agentstream_api_smoke.json';path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'ok':True,'sentence_count':len(expected),'exact_tag_matches':correct,
                      'latency_ms':result['latency_ms'],'report':str(path)},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
