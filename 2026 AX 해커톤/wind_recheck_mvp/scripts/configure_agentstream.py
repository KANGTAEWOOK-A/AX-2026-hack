#!/usr/bin/env python3
"""Read an existing API key privately; never create/rotate credentials."""
import argparse
import getpass
import os
from pathlib import Path
from uuid import UUID
from dotenv import set_key, dotenv_values

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / '.env'
parser = argparse.ArgumentParser(description='AgentStream 챗봇과 API Key를 로컬에 저장합니다.')
parser.add_argument('--chatbot-id', type=UUID, help='AgentStream 챗봇 UUID')
args = parser.parse_args()
current = dotenv_values(path) if path.exists() else {}
chatbot_id = str(args.chatbot_id) if args.chatbot_id else current.get('AGENTSTREAM_CHATBOT_ID')
if not chatbot_id:
    parser.error('--chatbot-id에 AgentStream 챗봇 UUID를 지정하세요.')
key = getpass.getpass('발급받은 9팀 AgentStream API Key (화면에 표시되지 않음): ').strip()
if not key or any(c.isspace() for c in key):
    raise SystemExit('빈 값이나 공백 포함 키는 저장하지 않았습니다.')
if not path.exists():
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
path.chmod(0o600)
for name, value in {'AGENTSTREAM_API_KEY':key,
                    'AGENTSTREAM_BASE_URL':'https://api-gateway.jntp-ax.or.kr/agentstream',
                    'AGENTSTREAM_CHATBOT_ID':chatbot_id}.items():
    set_key(path, name, value)
print('로컬 .env에 저장했습니다. 키 원문은 출력하지 않았습니다.')
