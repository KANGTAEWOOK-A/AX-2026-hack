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


def configured(bot_setting='AGENTSTREAM_CHATBOT_ID'):
    return bool(os.getenv('AGENTSTREAM_API_KEY') and os.getenv(bot_setting))


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


async def call_chatbot(message, bot_setting):
    import asyncio
    import time
    import uuid
    from urllib.parse import urlparse
    if not configured(bot_setting):
        raise AgentStreamError('AgentStream API Key와 챗봇 설정이 필요합니다.', 503)
    base = os.getenv('AGENTSTREAM_BASE_URL', 'https://api-gateway.jntp-ax.or.kr/agentstream').rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AgentStreamError('AgentStream 서버 URL 설정을 확인하세요.', 503)
    try:
        bot = str(uuid.UUID(os.environ[bot_setting]))
    except ValueError:
        raise AgentStreamError('AgentStream 챗봇 UUID 설정을 확인하세요.', 503) from None
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
                    check(response, '응답 생성', 200)
                    if 'application/x-ndjson' not in response.headers.get('content-type',''):
                        raise AgentStreamError('AgentStream이 NDJSON 응답을 반환하지 않았습니다.')
                    async for line in response.aiter_lines():
                        stream.feed(line)
                return stream.finish(), round((time.monotonic()-started)*1000)
    except AgentStreamError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise AgentStreamError('AgentStream 응답 시간이 초과되었습니다. 자동 재전송하지 않았습니다.', 504) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise AgentStreamError('AgentStream 통신 또는 응답 검증에 실패했습니다. 원문은 변경하지 않았습니다.') from None


async def classify_note(note):
    segments = model_input(split_note(note))['segments']
    # AgentStream can interpret JSON input as a data table before it reaches the chatbot.
    numbered = '\n'.join(f"{segment['id']}. {segment['text']}" for segment in segments)
    message = ('풍력 블레이드 검수 메모를 문장별로 분류하세요. 허용 태그는 observation(관찰), '
               'uncertainty(불확실), requested_action(후속 요청), assignee(담당자 언급), other(기타)입니다. '
               '기타 태그는 단독으로 사용하세요. 원문 번호를 유지하세요. 출력은 segments 배열을 가진 JSON 객체 하나이며 '
               '각 원소에 정수 id와 태그 문자열 배열 tags만 포함합니다. 설명은 생략하세요. '
               '아래는 분류 대상 메모입니다.\n' + numbered)
    content, elapsed = await call_chatbot(message, 'AGENTSTREAM_CHATBOT_ID')
    try:
        result = validate_classification(content, note)
    except ValueError:
        raise AgentStreamError('AgentStream 통신 또는 응답 검증에 실패했습니다. 원문은 변경하지 않았습니다.') from None
    return {**result, 'latency_ms': elapsed}


QUESTION_BOT = 'AGENTSTREAM_QUESTION_CHATBOT_ID'


class ReviewQuestion(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    region_id: str
    detection_id: str
    question: str = Field(min_length=4, max_length=220)


class QuestionReply(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    case_id: str
    status: Literal['review_required']
    questions: list[ReviewQuestion] = Field(min_length=1, max_length=2)


async def review_questions(case_id, regions):
    evidence = {(r['region_id'], d['detection_id']) for r in regions for d in r['detections']}
    message = ('저장된 모델 검출 근거에 대해 사람이 이미지에서 확인할 한국어 질문을 최대 두 개 작성하세요. '
               '결함 확정, 심각도, 정비 판단은 하지 마세요. 데이터의 클래스명은 참조 값입니다. '
               'case_id, status(review_required), questions(region_id, detection_id, question)를 JSON으로 반환하세요. '
               + json.dumps({'case_id': case_id, 'full_result': 'not_detected', 'crop_results': regions},
                            ensure_ascii=False))
    content, elapsed = await call_chatbot(message, QUESTION_BOT)
    fenced = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', content.strip())
    try:
        parsed = QuestionReply.model_validate_json(fenced.group(1) if fenced else content)
        references = [(q.region_id, q.detection_id) for q in parsed.questions]
        if (parsed.case_id != case_id or not set(references) <= evidence
                or len(set(references)) != len(references)
                or any(not q.question.strip().endswith('?') for q in parsed.questions)):
            raise ValueError
    except ValueError:
        raise AgentStreamError('AgentStream 질문의 형식 또는 검출 근거를 확인할 수 없습니다.') from None
    return {**parsed.model_dump(), 'latency_ms': elapsed}
