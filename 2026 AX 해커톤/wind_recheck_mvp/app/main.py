"""Wind Recheck MVP: AI-assisted inspection triage; not a safety certification system."""
import csv
import base64
import asyncio
import hashlib
import secrets
from html import escape
import io
import json
import os
import sqlite3
import time
import uuid
import zipfile
import httpx
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
from app import agentstream
from app.aistream import infer
from app.engine import evaluate, fit_model, image_features, load_model, priority, quality_risk

DATA = Path(os.getenv('WR_DATA_DIR', str(ROOT / 'data')))
UPLOADS = DATA / 'uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = DATA / 'recheck.sqlite3'
MODEL = DATA / 'ranker.joblib'
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_ZIP_BYTES = 130 * 1024 * 1024
MAX_IMPORT_ITEMS = 300
AGENT_GATE = asyncio.Semaphore(2)
CROP_GATE = Lock()

ROOT_PATH = os.getenv('WR_ROOT_PATH', '').rstrip('/')
app = FastAPI(title='Wind Recheck', version='0.1.0', root_path=ROOT_PATH)
app.mount('/static', StaticFiles(directory=ROOT / 'app' / 'static'), name='static')


def conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@contextmanager
def db():
    c = conn()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def setup_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, storage_name TEXT NOT NULL,
            uploaded_at REAL NOT NULL, truth TEXT, split TEXT NOT NULL DEFAULT 'unassigned',
            session TEXT, part TEXT, origin TEXT NOT NULL DEFAULT 'real',
            pred TEXT DEFAULT 'pending', confidence REAL, reason TEXT, raw_response TEXT,
            api_state TEXT DEFAULT 'pending', api_latency_ms INTEGER,
            brightness REAL, contrast REAL, blur_log REAL, dark_ratio REAL, bright_ratio REAL,
            review_status TEXT DEFAULT 'pending', review_note TEXT DEFAULT '', reviewed_at REAL
        );
        CREATE TABLE IF NOT EXISTS crop_results (
            item_id TEXT NOT NULL, region INTEGER NOT NULL, pred TEXT NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(item_id, region)
        );
        CREATE TABLE IF NOT EXISTS reviewer_sessions (owner TEXT PRIMARY KEY, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS review_drafts (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL, owner TEXT NOT NULL,
            note_hash TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS draft_attempts (owner TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_draft_attempt_time ON draft_attempts(created_at);
        CREATE INDEX IF NOT EXISTS idx_pred ON items(pred);
        CREATE INDEX IF NOT EXISTS idx_split ON items(split);
        ''')
        columns = {r[1] for r in c.execute('PRAGMA table_info(items)')}
        if 'full_revision' not in columns:
            c.execute('ALTER TABLE items ADD COLUMN full_revision TEXT')
        if 'review_draft_id' not in columns:
            c.execute('ALTER TABLE items ADD COLUMN review_draft_id TEXT')


setup_db()


def compact_response(value):
    """Keep defect metadata inspectable without storing bulky PNG strings."""
    if isinstance(value, dict):
        return {key: (f'[PNG base64 생략: {len(item)}문자]'
                      if key == 'mask_png_base64' and isinstance(item, str)
                      else compact_response(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_response(item) for item in value]
    return value


def rows():
    with db() as c:
        return [dict(x) for x in c.execute('SELECT * FROM items ORDER BY uploaded_at DESC')]


def public_row(r):
    out = {k: v for k, v in r.items() if k not in ('truth', 'storage_name', 'raw_response', 'full_revision')}
    draft_id = out.pop('review_draft_id', None)
    out['review_classification'] = None
    if draft_id:
        with db() as c:
            draft = c.execute('SELECT result,note_hash FROM review_drafts WHERE id=? AND item_id=?',
                              (draft_id, r['id'])).fetchone()
        if draft and draft['note_hash'] == agentstream.note_hash(r.get('review_note') or ''):
            out['review_classification'] = json.loads(draft['result'])
    out['image_url'] = f"api/image/{r['id']}"
    out['thumbnail_url'] = f"api/thumbnail/{r['id']}"
    out['quality_risk'] = quality_risk(r)
    if r.get('pred') == 'normal':
        value, method = priority(r, load_model(MODEL))
        out['priority'] = value
        out['priority_method'] = method
    else:
        out['priority'] = None
        out['priority_method'] = None
    return out


def validate_save(image_bytes, filename):
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ValueError('이미지 크기는 32MB 이하여야 합니다.')
    if not image_bytes:
        raise ValueError('빈 이미지입니다.')
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.width * img.height > 95_000_000:
                raise ValueError('이미지 최대 픽셀 수를 초과했습니다.')
            rgb = img.convert('RGB')
            out = io.BytesIO()
            rgb.save(out, format='JPEG', quality=90)
            safe_bytes = out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError('지원되지 않거나 손상된 이미지입니다.') from None
    item_id = uuid.uuid4().hex
    stored = item_id + '.jpg'
    target = UPLOADS / stored
    target.write_bytes(safe_bytes)
    try:
        feature = image_features(target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    original = Path(str(filename).replace('\\', '/')).name[:120] or 'image.jpg'
    return item_id, original, stored, feature


def insert_image(image_bytes, filename, truth=None, split='unassigned', session='', part='', origin='real'):
    if truth not in (None, 'normal', 'defect'):
        raise ValueError('정답 라벨은 normal 또는 defect이어야 합니다.')
    if split not in ('train', 'val', 'test', 'unassigned'):
        raise ValueError('split은 train/val/test/unassigned 중 하나여야 합니다.')
    item_id, original, stored, feature = validate_save(image_bytes, filename)
    try:
        with db() as c:
            c.execute('''INSERT INTO items
            (id, filename, storage_name, uploaded_at, truth, split, session, part, origin,
             brightness, contrast, blur_log, dark_ratio, bright_ratio)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (item_id, original, stored, time.time(), truth, split, str(session)[:100], str(part)[:100],
             origin, feature['brightness'], feature['contrast'], feature['blur_log'],
             feature['dark_ratio'], feature['bright_ratio']))
    except Exception:
        (UPLOADS / stored).unlink(missing_ok=True)
        raise
    return item_id


@app.get('/')
def home():
    html = (ROOT / 'app' / 'static' / 'index.html').read_text(encoding='utf-8')
    return HTMLResponse(html.replace('<head>', '<head>\n  <base href="' + escape(ROOT_PATH + '/', quote=True) + '" />', 1))


@app.get('/api/health')
def health():
    return {'ok': True, 'aistream_configured': bool(os.getenv('AISTREAM_WIND_URL')),
            'agentstream_configured': agentstream.configured(),
            'ranker_trained': MODEL.exists(), 'test_data': sum(r['split'] == 'test' for r in rows()),
            'api_mode': os.getenv('AISTREAM_REQUEST_MODE', 'multipart'),
            'notice': '실제 성능 검증에는 제공 데이터·정답 라벨과 AIStream 실제 응답이 필요합니다.'}


@app.get('/api/summary')
def summary():
    all_rows = rows()
    return {'total': len(all_rows),
            'analyzed': sum(r['pred'] in ('normal', 'defect') for r in all_rows),
            'normals': sum(r['pred'] == 'normal' for r in all_rows),
            'defects': sum(r['pred'] == 'defect' for r in all_rows),
            'unknown': sum(r['pred'] == 'unknown' for r in all_rows),
            'pending': sum(r['api_state'] == 'pending' for r in all_rows),
            'reviewed': sum(r['review_status'] == 'completed' for r in all_rows),
            'simulation': sum(r['origin'] == 'simulated' for r in all_rows)}


@app.get('/api/items')
def list_items(limit: int = Query(default=200, ge=1, le=500), pred: str = '', split: str = ''):
    all_rows = rows()
    if pred: all_rows = [r for r in all_rows if r['pred'] == pred]
    if split: all_rows = [r for r in all_rows if r['split'] == split]
    return {'items': [public_row(r) for r in all_rows[:limit]], 'count': len(all_rows)}


@app.get('/api/image/{item_id}')
def get_image(item_id: str):
    with db() as c:
        row = c.execute('SELECT storage_name FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None: raise HTTPException(404, '이미지를 찾을 수 없습니다.')
    return FileResponse(UPLOADS / row['storage_name'], media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=30'})


@app.get('/api/thumbnail/{item_id}')
def thumbnail(item_id: str):
    with db() as c:
        row = c.execute('SELECT storage_name FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '이미지를 찾을 수 없습니다.')
    cache = DATA / 'thumbnails'
    cache.mkdir(exist_ok=True)
    target = cache / row['storage_name']
    if not target.exists():
        with Image.open(UPLOADS / row['storage_name']) as image:
            image.thumbnail((320, 240))
            buffer = io.BytesIO()
            image.convert('RGB').save(buffer, format='JPEG', quality=80)
        temporary = cache / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(buffer.getvalue())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return FileResponse(target, media_type='image/jpeg', headers={'Cache-Control':'private, max-age=3600'})


@app.post('/api/upload')
async def upload_image(file: UploadFile = File(...)):
    content = await file.read(MAX_IMAGE_BYTES + 1)
    try:
        item_id = insert_image(content, file.filename or 'image.jpg')
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    return {'ok': True, 'id': item_id, 'message': '이미지 등록 완료. AIStream 추론을 실행하세요.'}


@app.post('/api/import-zip')
async def import_zip(file: UploadFile = File(...)):
    binary = await file.read(MAX_ZIP_BYTES + 1)
    if len(binary) > MAX_ZIP_BYTES:
        raise HTTPException(400, 'ZIP 파일은 130MB 이하여야 합니다. 여러 개로 나눠 업로드하세요.')
    successes, errors = [], []
    try:
        with zipfile.ZipFile(io.BytesIO(binary)) as archive:
            member_names = archive.namelist()
            if any(x.endswith('/') for x in member_names):
                member_names = [x for x in member_names if not x.endswith('/')]
            manifest_name = next((n for n in member_names if Path(n).name.lower() == 'manifest.csv'), None)
            if not manifest_name:
                raise ValueError('ZIP 루트 또는 하위 폴더에 manifest.csv가 필요합니다. README를 참조하세요.')
            if archive.getinfo(manifest_name).file_size > 3_000_000:
                raise ValueError('manifest.csv 크기 제한 3MB')
            csv_text = archive.read(manifest_name).decode('utf-8-sig')
            manifest = list(csv.DictReader(io.StringIO(csv_text)))
            if len(manifest) > MAX_IMPORT_ITEMS:
                raise ValueError(f'한 번에 최대 {MAX_IMPORT_ITEMS}개 이미지만 가져올 수 있습니다.')
            if not manifest or not {'image_file', 'truth'}.issubset(manifest[0]):
                raise ValueError('manifest.csv에 image_file,truth 열이 필요합니다.')
            image_members = {n.replace('\\', '/').lstrip('./'): n for n in member_names}
            for row in manifest:
                filename = (row.get('image_file') or '').replace('\\', '/').lstrip('./')
                if filename not in image_members:
                    errors.append({'filename': filename, 'error': 'ZIP 안에 이미지 없음'}); continue
                info = archive.getinfo(image_members[filename])
                if info.file_size > MAX_IMAGE_BYTES:
                    errors.append({'filename': filename, 'error': '이미지 크기 제한 초과'}); continue
                try:
                    truth = row['truth'].strip().lower()
                    split = (row.get('split') or 'unassigned').strip().lower()
                    item_id = insert_image(archive.read(image_members[filename]), filename, truth=truth,
                                           split=split, session=row.get('session') or '', part=row.get('part') or '')
                    successes.append(item_id)
                except (ValueError, OSError) as e:
                    errors.append({'filename': filename, 'error': str(e)})
    except (ValueError, zipfile.BadZipFile, UnicodeDecodeError, KeyError) as e:
        raise HTTPException(400, str(e)) from None
    return {'imported': len(successes), 'errors': errors[:25], 'total_errors': len(errors)}


@app.post('/api/infer/{item_id}')
def infer_one(item_id: str):
    with db() as c:
        row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, '이미지 없음')
    if row['origin'] == 'simulated':
        raise HTTPException(409, '시뮬레이션 자료는 실제 API 성능 평가에 사용할 수 없습니다.')
    # A failed retry must never leave a previous normal decision eligible for ranking.
    revision = uuid.uuid4().hex
    with db() as c:
        c.execute('''UPDATE items SET pred='pending',confidence=NULL,raw_response=NULL,
                  api_latency_ms=NULL,api_state='pending',reason=NULL,full_revision=? WHERE id=?''', (revision, item_id))
        c.execute('DELETE FROM crop_results WHERE item_id=?', (item_id,))
    started = time.monotonic()
    try:
        parsed, raw = infer((UPLOADS / row['storage_name']).read_bytes(), row['filename'])
    except ValueError as e:
        with db() as c:
            c.execute('UPDATE items SET api_state=?, reason=? WHERE id=? AND full_revision=?', ('error', str(e)[:500], item_id, revision))
        raise HTTPException(400, str(e)) from None
    except httpx.TimeoutException:
        message = 'AIStream 응답 대기시간을 초과했습니다. 서버에서 추론이 계속될 수 있으므로 잠시 후 재시도하세요.'
        with db() as c:
            c.execute('UPDATE items SET api_state=?, reason=? WHERE id=? AND full_revision=?', ('error', message, item_id, revision))
        raise HTTPException(504, message) from None
    except Exception:
        with db() as c:
            c.execute('UPDATE items SET api_state=?, reason=? WHERE id=? AND full_revision=?', ('error', 'API 통신 실패', item_id, revision))
        raise HTTPException(502, 'AIStream API 연결 실패. URL·네트워크·요청 형식을 확인하세요.') from None
    elapsed = round((time.monotonic() - started) * 1000)
    raw_json = json.dumps(compact_response(raw), ensure_ascii=False)
    if len(raw_json) > 50000:
        raw_json = json.dumps({'truncated': True, 'preview': raw_json[:48000]}, ensure_ascii=False)
    with db() as c:
        updated = c.execute('''UPDATE items SET pred=?, confidence=?, reason=?, raw_response=?, api_state=?, api_latency_ms=?
                  WHERE id=? AND full_revision=?''', (parsed['pred'], parsed['confidence'], parsed['reason'],
                  raw_json, 'success', elapsed, item_id, revision))
        if not updated.rowcount:
            raise HTTPException(409, '더 최근의 전체 분석 요청이 있습니다.')
    return {'id': item_id, **parsed, 'api_latency_ms': elapsed}


def store_crop(item_id, revision, region, result):
    with db() as c:
        saved = c.execute(
            """INSERT OR REPLACE INTO crop_results(item_id, region, pred, result)
               SELECT id, ?, ?, ? FROM items
               WHERE id=? AND full_revision IS ? AND pred='normal' AND api_state='success'""",
            (region, result['pred'], json.dumps(result, ensure_ascii=False), item_id, revision),
        )
        if not saved.rowcount:
            raise HTTPException(409, '전체 분석 결과가 변경됐습니다. 검토창을 다시 열어 주세요.')


@app.get('/api/reanalysis/{item_id}')
def read_crop_results(item_id: str):
    with db() as c:
        if not c.execute('SELECT 1 FROM items WHERE id=?', (item_id,)).fetchone():
            raise HTTPException(404, '이미지 없음')
        results = c.execute('SELECT result FROM crop_results WHERE item_id=? ORDER BY region', (item_id,))
        return {'results': [json.loads(row['result']) for row in results]}


@app.post('/api/reanalyze/{item_id}/{region}')
def reanalyze_region(item_id: str, region: int):
    if region not in (1, 2, 3, 4):
        raise HTTPException(400, '부분 영역 번호는 1~4입니다.')
    if not CROP_GATE.acquire(blocking=False):
        raise HTTPException(429, '다른 부분 분석이 진행 중입니다. 잠시 후 다시 시도하세요.')
    try:
        with db() as c:
            row = c.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '이미지 없음')
        if row['origin'] == 'simulated' or row['api_state'] != 'success' or row['pred'] != 'normal':
            raise HTTPException(409, '전체 분석에서 결함 미검출로 판정된 실제 이미지만 부분 분석할 수 있습니다.')
        revision = row['full_revision']
        store_crop(item_id, revision, region, {'region': region, 'pred': 'pending'})
        try:
            with Image.open(UPLOADS / row['storage_name']) as image:
                width, height = image.size
                if min(width, height) < 2:
                    raise ValueError('이미지가 너무 작아 부분 분석을 할 수 없습니다.')
                # The quadrants overlap to retain features along the seams.
                left, right = (0, (width * 55 + 99) // 100) if region % 2 else (width * 45 // 100, width)
                top, bottom = (0, (height * 55 + 99) // 100) if region <= 2 else (height * 45 // 100, height)
                crop = image.crop((left, top, right, bottom)).convert('RGB')
            payload = io.BytesIO()
            crop.save(payload, 'JPEG', quality=90)
            started = time.monotonic()
            parsed, raw = infer(payload.getvalue(), f'{item_id}-T{region}.jpg')
            elapsed = round((time.monotonic() - started) * 1000)
            crop.thumbnail((480, 320))
            preview = io.BytesIO()
            crop.save(preview, 'JPEG', quality=76)
        except httpx.TimeoutException:
            message = '부분 분석 응답 대기시간을 초과했습니다.'
            store_crop(item_id, revision, region, {'region': region, 'pred': 'error', 'error': message})
            raise HTTPException(504, message) from None
        except (httpx.HTTPError, ValueError, OSError):
            message = '부분 이미지를 분석하지 못했습니다.'
            store_crop(item_id, revision, region, {'region': region, 'pred': 'error', 'error': message})
            raise HTTPException(502, message) from None
        defects = raw.get('defects') if isinstance(raw, dict) else None
        labels = [d['class_name'] for d in defects
                  if isinstance(d, dict) and isinstance(d.get('class_name'), str)] if isinstance(defects, list) else []
        result = {'region': region, 'bounds': [left, top, right, bottom], 'source_size': [width, height],
                  'pred': parsed['pred'], 'labels': list(dict.fromkeys(labels)), 'latency_ms': elapsed,
                  'preview_jpeg': base64.b64encode(preview.getvalue()).decode('ascii')}
        store_crop(item_id, revision, region, result)
        return result
    finally:
        CROP_GATE.release()


@app.get('/api/response/{item_id}')
def raw_response(item_id: str):
    with db() as c:
        row = c.execute('SELECT raw_response FROM items WHERE id=?', (item_id,)).fetchone()
    if not row: raise HTTPException(404, '이미지 없음')
    return {'response': json.loads(row['raw_response']) if row['raw_response'] else None}


@app.post('/api/train')
def train_ranker():
    try:
        return {'ok': True, **fit_model(rows(), MODEL)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.get('/api/queue')
def queue(k: int = Query(default=10, ge=1, le=200)):
    all_rows = [r for r in rows() if r['pred'] == 'normal' and r['api_state'] == 'success'
                and r['review_status'] == 'pending' and r['origin'] != 'simulated']
    if not all_rows:
        all_rows = [r for r in rows() if r['pred'] == 'normal' and r['review_status'] == 'pending'
                    and r['origin'] == 'simulated']
    with db() as c:
        evidence = {}
        analyzed = set()
        for crop in c.execute('SELECT item_id, region, pred FROM crop_results ORDER BY region'):
            analyzed.add(crop['item_id'])
            if crop['pred'] == 'defect':
                evidence.setdefault(crop['item_id'], []).append(crop['region'])
    model = load_model(MODEL)
    verified = [row for row in all_rows if row['id'] in evidence]
    provisional = [row for row in all_rows if row['id'] not in analyzed]
    provisional.sort(key=lambda row: priority(row, model)[0], reverse=True)
    ranked = verified + provisional
    items = []
    for row in ranked[:k]:
        item = public_row(row)
        item['detected_regions'] = evidence.get(row['id'], [])
        items.append(item)
    return {'eligible': len(ranked), 'k': len(items), 'trained': model is not None,
            'evidence_count': len(verified),
            'simulation_only': bool(ranked) and all(r['origin'] == 'simulated' for r in ranked),
            'items': items}


def reviewer_owner(request):
    token = request.cookies.get('wr_reviewer', '')
    owner = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        exists = c.execute('SELECT 1 FROM reviewer_sessions WHERE owner=? AND expires_at>?',
                           (owner, time.time())).fetchone()
    if not exists or request.headers.get('X-WR-Review') != '1':
        raise HTTPException(403, '검토창을 다시 열고 메모 정리를 요청하세요.')
    return owner


@app.get('/api/review-session')
def reviewer_session(request: Request, response: Response):
    token = request.cookies.get('wr_reviewer', '')
    owner = hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        known = c.execute('SELECT 1 FROM reviewer_sessions WHERE owner=? AND expires_at>?',
                          (owner, time.time())).fetchone()
        if not known:
            token = secrets.token_urlsafe(32)
            owner = hashlib.sha256(token.encode()).hexdigest()
        c.execute('INSERT OR REPLACE INTO reviewer_sessions VALUES (?,?)', (owner, time.time()+86400))
    response.set_cookie('wr_reviewer', token, max_age=86400, httponly=True,
                        secure=bool(ROOT_PATH) or request.url.scheme == 'https',
                        samesite='strict', path=ROOT_PATH+'/' if ROOT_PATH else '/')
    response.headers['Cache-Control'] = 'no-store'
    return {'ready': True, 'configured': agentstream.configured()}


class ReviewDraftRequest(BaseModel):
    id: str
    note: str


@app.post('/api/review-draft')
async def review_draft(body: ReviewDraftRequest, request: Request):
    owner = reviewer_owner(request)
    try:
        agentstream.split_note(body.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not agentstream.configured():
        raise HTTPException(503, 'AgentStream API Key 설정이 필요합니다.')
    if AGENT_GATE.locked():
        raise HTTPException(503, '메모 정리를 처리 중입니다. 잠시 후 다시 시도하세요.')
    now = time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM items WHERE id=?', (body.id,)).fetchone():
            raise HTTPException(404, '대상 이미지 없음')
        personal = c.execute('SELECT count(*) FROM draft_attempts WHERE owner=? AND created_at>?',
                              (owner, now-3600)).fetchone()[0]
        total = c.execute('SELECT count(*) FROM draft_attempts WHERE created_at>?', (now-3600,)).fetchone()[0]
        if personal >= 6 or total >= 60:
            raise HTTPException(429, '메모 정리 호출 한도입니다. 잠시 후 다시 시도하세요.')
        c.execute('INSERT INTO draft_attempts VALUES (?,?)', (owner, now))
    try:
        async with AGENT_GATE:
            result = await agentstream.classify_note(body.note)
    except agentstream.AgentStreamError as e:
        raise HTTPException(e.status, str(e)) from None
    draft_id = uuid.uuid4().hex
    with db() as c:
        c.execute('INSERT INTO review_drafts VALUES (?,?,?,?,?,?)',
                  (draft_id, body.id, owner, agentstream.note_hash(body.note),
                   json.dumps(result, ensure_ascii=False), time.time()))
    return {**result, 'draft_id': draft_id}


class Review(BaseModel):
    id: str
    status: str
    note: str = ''
    draft_id: str | None = None


@app.post('/api/review')
def review_item(body: Review, request: Request):
    if body.status not in ('pending', 'completed', 'hold'):
        raise HTTPException(400, 'status는 pending/completed/hold 중 하나')
    if len(body.note) > 1000:
        raise HTTPException(400, '검토 메모는 최대 1000자입니다.')
    if body.draft_id:
        owner = reviewer_owner(request)
        with db() as c:
            draft = c.execute('SELECT 1 FROM review_drafts WHERE id=? AND item_id=? AND owner=? AND note_hash=?',
                              (body.draft_id, body.id, owner, agentstream.note_hash(body.note))).fetchone()
        if not draft:
            raise HTTPException(409, '메모가 바뀌었거나 다른 검토자의 분류입니다. 메모를 다시 정리하세요.')
    with db() as c:
        exists = c.execute('SELECT 1 FROM items WHERE id=?', (body.id,)).fetchone()
        if not exists: raise HTTPException(404, '대상 이미지 없음')
        c.execute('UPDATE items SET review_status=?,review_note=?,reviewed_at=?,review_draft_id=? WHERE id=?',
                  (body.status, body.note, time.time(), body.draft_id, body.id))
    return {'ok': True}


@app.get('/api/evaluate')
def get_evaluation(k: int = Query(default=10, ge=1, le=200)):
    return evaluate(rows(), k, load_model(MODEL))


@app.get('/api/reveal')
def reveal(k: int = Query(default=10, ge=1, le=200), method: str = 'Wind Recheck'):
    """Explicit reveal action for evaluation slides; never used for prioritization."""
    all_rows = {r['id']: r for r in rows()}
    trial = evaluate(list(all_rows.values()), k, load_model(MODEL))
    matched = next((m for m in trial['methods'] if m['method'] == method), None)
    if matched is None: raise HTTPException(404, '평가 방법 없음')
    return {'method': method, 'items': [{'id': x, 'filename': all_rows[x]['filename'],
                  'truth': all_rows[x]['truth'], 'image_url': f'api/image/{x}', 'thumbnail_url': f'api/thumbnail/{x}'} for x in matched['ids']]}


@app.get('/api/export')
def export_records():
    f = io.StringIO()
    names = ['id', 'filename', 'split', 'session', 'part', 'pred', 'confidence', 'api_state',
             'review_status', 'review_note', 'api_latency_ms']
    writer = csv.DictWriter(f, fieldnames=names)
    writer.writeheader()
    def csv_value(value):
        if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
            return "'" + value
        return value
    for row in rows(): writer.writerow({n: csv_value(row[n]) for n in names})
    f.seek(0)
    return StreamingResponse(iter([f.getvalue().encode('utf-8-sig')]), media_type='text/csv',
                             headers={'Content-Disposition': 'attachment; filename=wind_recheck_records.csv'})
