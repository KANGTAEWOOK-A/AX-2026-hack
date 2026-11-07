import io
import os
import csv
import zipfile
import tempfile
import pytest
from PIL import Image

_test_data = tempfile.TemporaryDirectory(prefix='windrecheck_pytest_')
os.environ['WR_DATA_DIR'] = _test_data.name
# Unit tests use direct HTTP routes; production HTTPS proxy cookies are checked in the real browser.
os.environ['WR_ROOT_PATH'] = ''
from fastapi.testclient import TestClient
from app.main import app, db, MODEL, UPLOADS
from app.aistream import parse_response
from app.engine import evaluate, priority

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_test_data():
    with db() as c:
        c.execute('DELETE FROM crop_results')
        c.execute('DELETE FROM items')
    MODEL.unlink(missing_ok=True)
    for path in UPLOADS.iterdir():
        path.unlink()


def sample_jpg(color=(130, 130, 130)):
    f = io.BytesIO()
    Image.new('RGB',(80,80),color).save(f,'JPEG')
    return f.getvalue()


def test_parser_is_conservative(monkeypatch):
    monkeypatch.delenv('AISTREAM_EMPTY_IS_NORMAL', raising=False)
    assert parse_response({'detections': []})['pred'] == 'unknown'
    assert parse_response({'label':'normal','confidence':0.91})['pred'] == 'normal'
    assert parse_response({'is_defect':True})['pred'] == 'defect'
    assert parse_response({'result': {'prediction': 'defect', 'score':0.71}})['confidence'] == 0.71
    assert parse_response({'unexpected':{'prob':0.95}})['pred'] == 'unknown'


def test_upload_and_queue():
    response = client.post('/api/upload', files={'file':('wind.jpg',sample_jpg(),'image/jpeg')})
    assert response.status_code == 200, response.text
    ident = response.json()['id']
    assert client.get(f'/api/image/{ident}').status_code == 200
    assert client.get('/api/summary').json()['total'] > 0
    with db() as c:
        c.execute("UPDATE items SET pred='normal',confidence=0.9,api_state='success' WHERE id=?",(ident,))
    q=client.get('/api/queue?k=2').json()
    assert q['eligible'] >= 1
    assert any(row['id']==ident for row in q['items'])
    response=client.post('/api/review',json={'id':ident,'status':'completed','note':'checked'})
    assert response.status_code==200
    assert client.get('/api/summary').json()['reviewed']>=1
    assert client.get('/api/response/'+ident).json()['response'] is None


def test_manifest_zip_import_and_no_truth_in_operational_api():
    csvdata='image_file,truth,split,session,part\nimages/test.jpg,defect,test,group2,BladeA\n'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as f:
        f.writestr('manifest.csv',csvdata)
        f.writestr('images/test.jpg',sample_jpg())
    r=client.post('/api/import-zip',files={'file':('test.zip',stream.getvalue(),'application/zip')})
    assert r.status_code==200 and r.json()['imported']==1,r.text
    all_rows=client.get('/api/items').json()['items']
    assert all('truth' not in row for row in all_rows)


def test_evaluation_same_k_and_test_only():
    dataset=[]
    for i in range(10):
        dataset.append({'id':str(i),'split':'test' if i<8 else 'train','pred':'normal',
                        'truth':'defect' if i in (0,2,5,9) else 'normal',
                        'origin':'real','confidence':0.6+(i%4)*0.06,
                        'brightness':0.3+i*0.05,'contrast':0.3,'blur_log':2,
                        'dark_ratio':0,'bright_ratio':0})
    out=evaluate(dataset,3)
    assert out['test_count']==8 and out['missed_defect_count']==3
    assert all(x['selected']==3 for x in out['methods'])
    assert all(x['remaining']==3-x['found'] for x in out['methods'])
    assert any(x['method']=='낮은 신뢰도순' for x in out['methods'])
    assert out['random_expectation']['capture_at_k'] == 0.375
    assert out['random_expectation']['precision_at_k'] == 0.375
    assert out['random_expectation']['remaining'] == 1.875
    for r in dataset: r['truth']='normal'
    zero=evaluate(dataset,3)
    assert zero['missed_defect_count']==0
    assert all(x['capture_at_k'] is None for x in zero['methods'])


def test_missing_url_never_simulates_real_api():
    from app.aistream import infer
    os.environ.pop('AISTREAM_WIND_URL',None)
    try:
        infer(sample_jpg(),'wind.jpg')
        assert False,'must fail'
    except ValueError as e:
        assert '미설정' in str(e)


def test_mixed_uninterpretable_response_is_not_normal():
    assert parse_response({'results': [{'label': 'normal'}, {'unrecognized': True}]})['pred'] == 'unknown'
    assert parse_response({'results': [{'label': 'defect'}, {'unrecognized': True}]})['pred'] == 'defect'


def test_failed_retry_invalidates_previous_prediction(monkeypatch):
    import app.main as main
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    with db() as c:
        c.execute("UPDATE items SET pred='normal',confidence=0.9,api_state='success' WHERE id=?", (ident,))
    def fail(*args):
        raise ValueError('test inference failure')
    monkeypatch.setattr(main, 'infer', fail)
    assert client.post(f'/api/infer/{ident}').status_code == 400
    item = client.get('/api/items').json()['items'][0]
    assert item['pred'] == 'pending' and item['api_state'] == 'error'
    assert item['confidence'] is None
    assert client.get('/api/queue').json()['eligible'] == 0
    assert client.get('/api/evaluate').json()['test_count'] == 0


def test_completed_and_held_items_do_not_consume_next_budget():
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    with db() as c:
        c.execute("UPDATE items SET pred='normal',api_state='success' WHERE id=?", (ident,))
    assert client.get('/api/queue').json()['eligible'] == 1
    for status in ('completed', 'hold'):
        assert client.post('/api/review', json={'id': ident, 'status': status}).status_code == 200
        assert client.get('/api/queue').json()['eligible'] == 0
    client.post('/api/review', json={'id': ident, 'status': 'pending'})
    assert client.get('/api/queue').json()['eligible'] == 1


def test_black_image_keeps_zero_brightness():
    from app.engine import quality_risk
    assert quality_risk({'brightness': 0, 'blur_log': 5}) == 0.4


def test_large_response_keeps_valid_json(monkeypatch):
    import app.main as main
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    monkeypatch.setattr(main, 'infer', lambda *_: (
        {'pred': 'normal', 'confidence': None, 'reason': 'test fixture'},
        {'label': 'normal', 'long_content': 'x' * 60000}))
    assert client.post(f'/api/infer/{ident}').status_code == 200
    response = client.get(f'/api/response/{ident}')
    assert response.status_code == 200
    assert response.json()['response']['truncated'] is True


def test_deployment_base_path(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, 'ROOT_PATH', '/i/test-team')
    html = client.get('/').text
    assert '<base href="/i/test-team/"' in html
    assert 'href="static/styles.css?' in html
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    assert client.get('/api/items').json()['items'][0]['image_url'] == f'api/image/{ident}'


def test_verified_wind_segmentation_response():
    payload = {'asset_type': 'wind', 'width': 640, 'height': 480,
               'classes': [{'class_id': 1, 'class_name': 'Contamination'}], 'defects': []}
    assert parse_response(payload)['pred'] == 'normal'
    assert parse_response(payload)['confidence'] is None
    payload['defects'] = [{'class_id': 1, 'class_name': 'Contamination', 'area': 100,
                           'bbox': [1, 2, 10, 10], 'mask_png_base64': 'mask omitted in test'}]
    assert parse_response(payload)['pred'] == 'defect'
    assert parse_response(payload)['confidence'] is None
    payload['defects'] = [{'unrecognized': True}]
    assert parse_response(payload)['pred'] == 'unknown'
    assert parse_response({'defects': []})['pred'] == 'unknown'
    payload['defects'] = []
    payload.pop('width')
    assert parse_response(payload)['pred'] == 'unknown'


def test_export_neutralizes_user_formula():
    ident = client.post('/api/upload', files={'file': ('=1+1.jpg', sample_jpg())}).json()['id']
    client.post('/api/review', json={'id': ident, 'status': 'hold', 'note': '  =1+1'})
    response = client.get('/api/export')
    records = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    row = next(r for r in records if r['id'] == ident)
    assert row['filename'] == "'=1+1.jpg"
    assert row['review_note'] == "'  =1+1"


def test_model_timeout_is_explicit_and_excluded(monkeypatch):
    import httpx
    import app.main as main
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    def timeout(*args):
        raise httpx.ReadTimeout('test timeout')
    monkeypatch.setattr(main, 'infer', timeout)
    response = client.post('/api/infer/' + ident)
    assert response.status_code == 504
    assert '계속될 수' in response.json()['detail']
    assert client.get('/api/queue').json()['eligible'] == 0


def test_large_masks_do_not_hide_defect_metadata(monkeypatch):
    import app.main as main
    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    raw = {'asset_type': 'wind', 'width': 640, 'height': 480, 'classes': [],
           'mask_png_base64': 'A' * 100000, 'defects': []}
    monkeypatch.setattr(main, 'infer', lambda *_: (
        {'pred': 'normal', 'confidence': None, 'reason': 'test fixture'}, raw))
    assert client.post('/api/infer/' + ident).status_code == 200
    response = client.get('/api/response/' + ident).json()['response']
    assert response['defects'] == [] and response['asset_type'] == 'wind'
    assert '생략' in response['mask_png_base64']
    assert raw['mask_png_base64'] == 'A' * 100000


def test_agent_draft_preserves_review_and_enforces_owner_and_note_hash(monkeypatch):
    from app import agentstream
    client.cookies.clear()
    note='균열인지는 확실하지 않습니다.'
    ident=client.post('/api/upload',files={'file':('memo.jpg',sample_jpg(),'image/jpeg')}).json()['id']
    monkeypatch.setattr(agentstream,'configured',lambda:True)
    async def classify(note):
        return agentstream.validate_classification('{"segments":[{"id":0,"tags":["uncertainty"]}]}',note)
    monkeypatch.setattr(agentstream,'classify_note',classify)
    client.get('/api/review-session')
    header={'X-WR-Review':'1'}
    draft=client.post('/api/review-draft',headers=header,json={'id':ident,'note':note})
    assert draft.status_code==200,draft.text
    draft_id=draft.json()['draft_id']
    row=client.get('/api/items').json()['items'][0]
    assert row['review_status']=='pending' and row['review_note']==''
    assert row['review_classification'] is None
    other=TestClient(app)
    other.get('/api/review-session')
    payload={'id':ident,'status':'completed','note':note,'draft_id':draft_id}
    assert other.post('/api/review',headers=header,json=payload).status_code==409
    assert client.post('/api/review',headers=header,json={**payload,'note':'변경된 메모'}).status_code==409
    assert client.post('/api/review',headers=header,json=payload).status_code==200
    row=client.get('/api/items').json()['items'][0]
    assert row['review_note']==note
    assert row['review_classification']['segments'][0]['tags']==['uncertainty']
    assert client.post('/api/review',json={'id':ident,'status':'hold','note':'수정'}).status_code==200
    assert client.get('/api/items').json()['items'][0]['review_classification'] is None


def test_agent_missing_session_and_failure_do_not_save(monkeypatch):
    from app import agentstream
    client.cookies.clear()
    ident=client.post('/api/upload',files={'file':('memo.jpg',sample_jpg(),'image/jpeg')}).json()['id']
    payload={'id':ident,'note':'자국인지 모르겠습니다.'}
    assert client.post('/api/review-draft',json=payload).status_code==403
    client.get('/api/review-session')
    monkeypatch.setattr(agentstream,'configured',lambda:True)
    async def failed(note): raise agentstream.AgentStreamError('모의 타임아웃',504)
    monkeypatch.setattr(agentstream,'classify_note',failed)
    assert client.post('/api/review-draft',headers={'X-WR-Review':'1'},json=payload).status_code==504
    row=client.get('/api/items').json()['items'][0]
    assert row['review_status']=='pending' and row['review_note']==''


def test_crop_analysis_requires_full_miss_and_preserves_full_result(monkeypatch):
    import app.main as main

    ident = client.post('/api/upload', files={'file': ('wind.jpg', sample_jpg())}).json()['id']
    assert client.post(f'/api/reanalyze/{ident}/2').status_code == 409
    with db() as connection:
        connection.execute("UPDATE items SET pred='normal', api_state='success' WHERE id=?", (ident,))

    def detect(payload, filename):
        assert filename.endswith('-T2.jpg')
        with Image.open(io.BytesIO(payload)) as crop:
            assert crop.size == (44, 44)
        return {'pred': 'defect'}, {'defects': [{'class_name': '도장 손상'}]}

    monkeypatch.setattr(main, 'infer', detect)
    response = client.post(f'/api/reanalyze/{ident}/2')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['bounds'] == [36, 0, 80, 44]
    assert result['pred'] == 'defect'
    assert result['labels'] == ['도장 손상']
    assert client.get('/api/reanalysis/' + ident).json()['results'] == [result]
    other = client.post('/api/upload', files={'file': ('other.jpg', sample_jpg())}).json()['id']
    with db() as connection:
        connection.execute("UPDATE items SET pred='normal', api_state='success' WHERE id=?", (other,))
    queued = client.get('/api/queue?k=1').json()
    assert queued['evidence_count'] == 1
    assert queued['items'][0]['id'] == ident
    assert queued['items'][0]['detected_regions'] == [2]

    monkeypatch.setattr(main, 'infer', lambda *_: (
        {'pred': 'normal', 'confidence': None, 'reason': 'test'}, {'defects': []}))
    assert client.post('/api/infer/' + ident).status_code == 200
    assert client.get('/api/reanalysis/' + ident).json()['results'] == []
    assert client.get('/api/queue').json()['evidence_count'] == 0
