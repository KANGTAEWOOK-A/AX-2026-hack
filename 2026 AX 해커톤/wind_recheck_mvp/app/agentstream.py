"""AgentStream note assistance: classify source sentences; never invent or rewrite observations."""
import hashlib
import json
import os
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Tag = Literal['observation', 'uncertainty', 'requested_action', 'assignee', 'other']
TAG_LABELS = {'observation': '관찰 내용', 'uncertainty': '불확실한 내용',
              'requested_action': '추가 확인 요청', 'assignee': '담당자 언급', 'other': '기타'}


class SegmentTags(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: int
    tags: list[Tag] = Field(min_length=1, max_length=5)


class Classification(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    segments: list[SegmentTags] = Field(min_length=1, max_length=20)


def note_hash(note):
    return hashlib.sha256(note.encode('utf-8')).hexdigest()


def split_note(note):
    if not note.strip() or len(note) > 1000:
        raise ValueError('검토 메모는 1~1000자로 입력하세요.')
    parts = [part.strip() for part in re.split(r'(?<=[.!?。])[ \t]+|\n+', note) if part.strip()]
    if len(parts) > 20:
        raise ValueError('한 번에 최대 20문장까지 정리할 수 있습니다.')
    return [{'id': i, 'text': text} for i, text in enumerate(parts)]


def model_input(segments):
    # The remote model needs sentence categories, not contact details.
    def redact(text):
        text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[이메일 생략]', text)
        text = re.sub(r'(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)', '[전화번호 생략]', text)
        text = re.sub(r'(?<!\d)\d{6}-[1-4]\d{6}(?!\d)', '[개인식별번호 생략]', text)
        return text
    return {'segments': [{'id': r['id'], 'text': redact(r['text'])} for r in segments]}


def validate_classification(content, original_note):
    if not isinstance(content, str) or len(content) > 20000:
        raise ValueError('AgentStream 응답 형식을 확인할 수 없습니다.')
    text = content.strip()
    fenced = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = Classification.model_validate(json.loads(text))
    except (ValueError, TypeError, ValidationError):
        raise ValueError('AgentStream 응답이 허용된 메모 분류 형식과 다릅니다. 원문은 변경하지 않았습니다.') from None
    original = split_note(original_note)
    if [r.id for r in parsed.segments] != list(range(len(original))):
        raise ValueError('AgentStream이 입력 문장 번호·개수를 변경했습니다. 원문은 변경하지 않았습니다.')
    result = []
    for source, classified in zip(original, parsed.segments):
        if len(set(classified.tags)) != len(classified.tags) or ('other' in classified.tags and len(classified.tags) > 1):
            raise ValueError('AgentStream의 분류 태그가 중복되거나 충돌합니다.')
        result.append({**source, 'tags': classified.tags,
                       'labels': [TAG_LABELS[tag] for tag in classified.tags]})
    return {'provider': 'AgentStream', 'original_preserved': True,
            'note_sha256': note_hash(original_note), 'segments': result,
            'notice': '메모 분류 보조 결과입니다. 결함 판정·심각도·정비 긴급도·우선순위를 산정하지 않습니다.'}


class AgentStreamError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def configured():
    return bool(os.getenv('AGENTSTREAM_API_KEY') and os.getenv('AGENTSTREAM_CHATBOT_ID'))


class ReplyStream:
    """Only assistant text from completed NDJSON turns can become a draft."""
    def __init__(self):
        self.parts, self.seen, self.completed = [], set(), False
        self.size = 0

    def feed(self, line):
        if not line.strip():
            return
        self.size += len(line)
        if self.size > 200000:
            raise AgentStreamError('AgentStream 응답 크기 제한을 초과했습니다.')
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise AgentStreamError('AgentStream 스트림 JSON 형식이 올바르지 않습니다.') from None
        event_id = event.get('event_id')
        if event_id is not None:
            if not isinstance(event_id, str):
                raise AgentStreamError('AgentStream 이벤트 식별자 형식이 올바르지 않습니다.')
            if event_id in self.seen:
                return
            self.seen.add(event_id)
        # The actual API uses event for protocol names and type for LangChain message types.
        kind = event.get('event', event.get('type'))
        if kind in ('turn.failed', 'tool.failed'):
            raise AgentStreamError('AgentStream 처리가 실패했습니다. 메모 원문은 유지됩니다.')
        if kind == 'turn.completed':
            self.completed = True
        elif kind == 'assistant.delta':
            if self.completed:
                raise AgentStreamError('완료 이후 응답이 추가되어 결과를 적용하지 않았습니다.')
            content = event.get('content')
            if isinstance(content, str):
                self.parts.append(content)
            elif isinstance(content, list):
                self.parts.extend(b['text'] for b in content if isinstance(b, dict)
                                  and b.get('type') == 'text' and isinstance(b.get('text'), str))
            else:
                raise AgentStreamError('AgentStream 응답 텍스트 형식이 올바르지 않습니다.')

    def finish(self):
        if not self.completed:
            raise AgentStreamError('AgentStream 완료 이벤트를 받지 못했습니다. 다시 시도하기 전에 상태를 확인하세요.')
        return ''.join(self.parts)


async def classify_note(note):
    import asyncio
    import time
    import uuid
    from urllib.parse import urlparse
    if not configured():
        raise AgentStreamError('AgentStream API Key와 챗봇 설정이 필요합니다.', 503)
    base = os.getenv('AGENTSTREAM_BASE_URL', 'https://api-gateway.jntp-ax.or.kr/agentstream').rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentStreamError('AgentStream 서버 URL 설정을 확인하세요.', 503)
    try:
        bot = str(uuid.UUID(os.environ['AGENTSTREAM_CHATBOT_ID']))
    except ValueError:
        raise AgentStreamError('AgentStream 챗봇 UUID 설정을 확인하세요.', 503) from None
    request_data = model_input(split_note(note))
    message = ('설정된 검토 메모 분류 작업을 수행하세요. 설명 없이 segments의 id와 tags만 JSON으로 반환하세요. '
               '입력 데이터:\n' + json.dumps(request_data, ensure_ascii=False))
    headers = {'X-API-Key': os.environ['AGENTSTREAM_API_KEY']}
    started = time.monotonic()

    def check(response, stage, expected):
        if response.status_code != expected:
            code = response.status_code
            hint = {401:'API Key를 확인하세요.',403:'워크스페이스·공개 설정·호출 권한을 확인하세요.',
                    429:'호출 한도에 도달했습니다.',503:'모델 또는 동시 처리 상태를 확인하세요.'}.get(code, '관리자에게 호출 상태를 확인해 주세요.')
            raise AgentStreamError(f'AgentStream {stage} 실패(HTTP {code}). {hint}', 503 if code in (429,503) else 502)

    try:
        async with asyncio.timeout(120):
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15), follow_redirects=False) as client:
                response = await client.get(f'{base}/open/v1/chatbots/{bot}/health', headers=headers)
                check(response, '상태 확인', 200)
                if ('application/json' not in response.headers.get('content-type','')
                        or not isinstance(response.json(), dict) or response.json().get('status') != 'healthy'):
                    raise AgentStreamError('AgentStream 모델 상태가 healthy가 아닙니다.', 503)
                response = await client.post(f'{base}/open/v1/chatbots/{bot}/sessions', headers=headers,
                                             json={'query':message})
                check(response, '세션 생성', 201)
                session = str(uuid.UUID(response.json()['session_id']))
                # Each invocation has a fresh server-owned session. Client never chooses a session or chatbot.
                stream = ReplyStream()
                async with client.stream('POST', f'{base}/open/v1/chat-sessions/{session}/messages',
                                         headers=headers, json={'message':message}) as response:
                    check(response, '메모 분류', 200)
                    if 'application/x-ndjson' not in response.headers.get('content-type',''):
                        raise AgentStreamError('AgentStream이 NDJSON 응답을 반환하지 않았습니다.')
                    async for line in response.aiter_lines():
                        stream.feed(line)
                result = validate_classification(stream.finish(), note)
                result['latency_ms'] = round((time.monotonic()-started)*1000)
                return result
    except AgentStreamError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise AgentStreamError('AgentStream 응답 시간이 초과되었습니다. 자동 재전송하지 않았습니다.', 504) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise AgentStreamError('AgentStream 통신 또는 응답 검증에 실패했습니다. 원문은 변경하지 않았습니다.') from None
