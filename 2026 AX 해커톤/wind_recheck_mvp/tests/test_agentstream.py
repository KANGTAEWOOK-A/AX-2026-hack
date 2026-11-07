import json
import asyncio
import pytest
from app.agentstream import split_note, model_input, validate_classification


@pytest.mark.parametrize('reference', ['T2-D1', 'T1-D1'])
def test_review_questions_only_reference_observed_crops(monkeypatch, reference):
    from app import agentstream

    async def reply(message, bot_setting):
        assert bot_setting == agentstream.QUESTION_BOT
        assert '도장 손상' in message
        return json.dumps({'case_id': 'case', 'status': 'review_required', 'questions': [
            {'region_id': 'T2', 'detection_id': reference, 'question': '도장 손상으로 표시된 흔적이 보이나요?'}
        ]}), 10

    monkeypatch.setattr(agentstream, 'call_chatbot', reply)
    evidence = [{'region_id': 'T2', 'result': 'detected',
                 'detections': [{'detection_id': 'T2-D1', 'class_name': '도장 손상'}]}]
    if reference == 'T1-D1':
        with pytest.raises(agentstream.AgentStreamError):
            asyncio.run(agentstream.review_questions('case', evidence))
    else:
        result = asyncio.run(agentstream.review_questions('case', evidence))
        assert result['questions'][0]['detection_id'] == reference


def test_preserves_full_negation_and_uncertainty_sentence():
    note = '자국은 있지만 균열인지 모르겠습니다.\n담당자는 아직 미정입니다.'
    reply = json.dumps({'segments': [{'id': 0, 'tags': ['observation', 'uncertainty']},
                                     {'id': 1, 'tags': ['assignee']}]})
    result = validate_classification(reply, note)
    assert result['original_preserved'] is True
    assert result['segments'][0]['text'] == '자국은 있지만 균열인지 모르겠습니다.'
    assert result['segments'][1]['text'] == '담당자는 아직 미정입니다.'


@pytest.mark.parametrize('payload', [
    {'segments': [{'id': 0, 'tags': ['observation']}], 'risk_score': 0.9},
    {'segments': [{'id': 0, 'tags': ['urgent']}]},
    {'segments': [{'id': 1, 'tags': ['observation']}]},
    {'segments': [{'id': 0, 'tags': ['other', 'observation']}]},
    {'segments': [{'id': True, 'tags': ['observation']}]},
    {'segments': [{'id': 0, 'tags': ['observation'], 'text': '균열 확정'}]},
])
def test_rejects_new_facts_unsafe_tags_and_changed_identifiers(payload):
    with pytest.raises(ValueError):
        validate_classification(json.dumps(payload), '균열인지는 아직 모릅니다.')


def test_contact_redaction_does_not_change_local_original():
    original = split_note('담당자 연락처 010-1234-5678, a@example.com')
    request = model_input(original)
    assert '010-1234-5678' not in request['segments'][0]['text']
    assert 'a@example.com' not in request['segments'][0]['text']
    assert '010-1234-5678' in original[0]['text']


def test_decimal_is_not_split_and_extra_sentences_rejected():
    assert len(split_note('0.5mm로 보이나 실측하지 않았습니다. 재확인 부탁합니다.')) == 2
    with pytest.raises(ValueError):
        split_note('\n'.join(['메모'] * 21))


def test_ndjson_only_accepts_completed_assistant_text_and_deduplicates():
    from app.agentstream import ReplyStream
    stream = ReplyStream()
    events = [
        {'type':'tool.content','content':'이 내용은 답변에 넣지 않음'},
        {'event_id':'a','type':'assistant.delta','content':[{'type':'thinking','text':'숨김'}, {'type':'text','text':'한글'}]},
        {'event_id':'a','type':'assistant.delta','content':'중복'},
        {'type':'assistant.delta','content':' 응답'},
        {'type':'turn.completed'}]
    for event in events: stream.feed(json.dumps(event,ensure_ascii=False))
    assert stream.finish() == '한글 응답'


@pytest.mark.parametrize('ending', ['missing', 'turn.failed', 'tool.failed', 'invalid'])
def test_stream_failure_never_becomes_completed_draft(ending):
    from app.agentstream import ReplyStream, AgentStreamError
    stream = ReplyStream()
    stream.feed(json.dumps({'type':'assistant.delta','content':'부분 응답'}))
    with pytest.raises(AgentStreamError):
        if ending == 'invalid': stream.feed('not-json')
        elif ending != 'missing': stream.feed(json.dumps({'type':ending}))
        stream.finish()


def test_real_api_contract_client_uses_fresh_sessions_and_no_truth(monkeypatch):
    import asyncio
    import httpx
    import uuid
    from app import agentstream
    sessions, requests = [], []
    real_client = httpx.AsyncClient
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/health'):
            return httpx.Response(200,json={'status':'healthy'})
        if request.url.path.endswith('/sessions'):
            ident=str(uuid.uuid4());sessions.append(ident)
            return httpx.Response(201,json={'session_id':ident})
        content=json.dumps({'segments':[{'id':0,'tags':['uncertainty']}]})
        chunks=[{'type':'assistant.delta','event_id':'one','content':content}, {'type':'turn.completed'}]
        return httpx.Response(200,headers={'content-type':'application/x-ndjson'},
                              text='\n'.join(json.dumps(e) for e in chunks))
    monkeypatch.setenv('AGENTSTREAM_API_KEY','mock-key-not-real')
    monkeypatch.setenv('AGENTSTREAM_CHATBOT_ID',str(uuid.uuid4()))
    monkeypatch.setattr(agentstream.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw))
    for _ in range(2):
        result=asyncio.run(agentstream.classify_note('균열인지 확실하지 않습니다.'))
        assert result['segments'][0]['text']=='균열인지 확실하지 않습니다.'
    assert len(set(sessions))==2
    for request in requests:
        assert request.headers['X-API-Key']=='mock-key-not-real'
        assert 'truth' not in request.content.decode()
        if request.method=='POST':
            payload=json.loads(request.content)
            assert set(payload) in ({'query'},{'message'})


def test_actual_api_event_field_overrides_langchain_message_type():
    from app.agentstream import ReplyStream
    stream=ReplyStream()
    stream.feed(json.dumps({'type':'AIMessageChunk','event':'assistant.delta','content':'{"segments":'}))
    stream.feed(json.dumps({'type':'AIMessageChunk','event':'assistant.delta','content':'[]}'}))
    stream.feed(json.dumps({'type':'ai','event':'assistant.final','content':'not a duplicate delta'}))
    stream.feed(json.dumps({'event':'turn.completed'}))
    assert stream.finish()=='{"segments":[]}'
