#!/usr/bin/env python3
"""Read an existing API key privately; never create/rotate credentials."""
import getpass
import os
from pathlib import Path
from dotenv import set_key, dotenv_values

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / '.env'
key = getpass.getpass('발급받은 9팀 AgentStream API Key (화면에 표시되지 않음): ').strip()
if not key or any(c.isspace() for c in key):
    raise SystemExit('빈 값이나 공백 포함 키는 저장하지 않았습니다.')
if not path.exists():
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
path.chmod(0o600)
for name, value in {'AGENTSTREAM_API_KEY':key,
                    'AGENTSTREAM_BASE_URL':'https://api-gateway.jntp-ax.or.kr/agentstream',
                    'AGENTSTREAM_CHATBOT_ID':'736c4933-f3cb-427a-915e-e536bee307ee'}.items():
    set_key(path, name, value)
print('로컬 .env에 저장했습니다. 키 원문은 출력하지 않았습니다.')
