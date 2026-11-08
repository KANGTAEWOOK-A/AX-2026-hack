"""Strict, inspectable adapter. Never silently invent an API response or confidence."""
import base64
import os
import httpx


def env_bool(key, default=False):
    return os.getenv(key, str(default)).strip().lower() in ('1', 'true', 'yes')


def _aliases(env_key, defaults):
    return set(t.strip().casefold() for t in os.getenv(env_key, defaults).split(',') if t.strip())


def parse_response(payload):
    normal = _aliases('AISTREAM_NORMAL_LABELS', 'normal,no_defect,defect_free,negative,정상,양품')
    defect = _aliases('AISTREAM_DEFECT_LABELS', 'defect,abnormal,damage,positive,불량,결함')
    result = {'pred': 'unknown', 'confidence': None, 'reason': 'API 출력 형식 미확인'}

    # Verified against the Wind / Solar Defect Segmentation 1.0.0 /docs.
    # classes is the class catalogue, not the detected defect list.
    if isinstance(payload, dict) and payload.get('asset_type') == 'wind':
        valid_size = all(type(payload.get(k)) is int and payload[k] > 0 for k in ('width', 'height'))
        defects = payload.get('defects')
        if not valid_size or not isinstance(payload.get('classes'), list) or not isinstance(defects, list):
            return result
        if not defects:
            return {'pred': 'normal', 'confidence': None,
                    'reason': '풍력 분할 모델: 후처리 결함 미검출(defects=[]), 무결함 보증 아님'}
        valid_defects = [d for d in defects if isinstance(d, dict)
                         and type(d.get('class_id')) is int and d['class_id'] > 0
                         and isinstance(d.get('class_name'), str) and d['class_name'].strip()]
        if valid_defects:
            names = ', '.join(dict.fromkeys(d['class_name'] for d in valid_defects))
            return {'pred': 'defect', 'confidence': None,
                    'reason': f'풍력 분할 모델: {len(valid_defects)}개 결함 영역 ({names})'}
        return result

    def decode_label(v):
        if isinstance(v, bool):
            return 'defect' if v else 'normal'
        if isinstance(v, str):
            s = v.strip().casefold()
            if s in normal: return 'normal'
            if s in defect: return 'defect'
        return 'unknown'

    def confidence(d):
        for key in ('confidence', 'score', 'probability', 'conf'):
            if key in d:
                try:
                    c = float(d[key]); return c if 0 <= c <= 1 else None
                except (TypeError, ValueError): pass
        return None

    def decode(data, depth=0):
        if depth > 4: return None
        if isinstance(data, str):
            state = decode_label(data)
            return (state, None, '명시적 라벨') if state != 'unknown' else None
        if isinstance(data, list):
            if not data and env_bool('AISTREAM_EMPTY_IS_NORMAL'):
                return ('normal', None, '빈 탐지 목록(/docs 확인 설정)')
            parsed = [decode(x, depth + 1) for x in data]
            found = [x for x in parsed if x is not None]
            if any(x[0] == 'defect' for x in found):
                return ('defect', max((x[1] for x in found if x[0] == 'defect' and x[1] is not None), default=None), '탐지 결과')
            if found and len(found) == len(parsed) and all(x[0] == 'normal' for x in found):
                return ('normal', found[0][1], '탐지 결과')
            return None
        if not isinstance(data, dict): return None
        for key in ('has_defect', 'is_defect', 'defect_detected'):
            if isinstance(data.get(key), bool):
                return (decode_label(data[key]), confidence(data), key)
        for key in ('label', 'class', 'class_name', 'status', 'prediction', 'category_name'):
            if key in data and isinstance(data[key], str):
                state = decode_label(data[key])
                if state != 'unknown':
                    return (state, confidence(data), key)
        for key in ('predictions', 'detections', 'results', 'result', 'output', 'data'):
            if key in data:
                parsed = decode(data[key], depth + 1)
                if parsed is not None: return parsed
        return None

    decoded = decode(payload)
    if decoded:
        result.update(pred=decoded[0], confidence=decoded[1], reason=decoded[2])
    return result


def infer(image_bytes, filename):
    url = os.getenv('AISTREAM_WIND_URL', '').strip()
    if not url:
        raise ValueError('AISTREAM_WIND_URL이 미설정입니다. AIStream 배포 URL과 /docs를 확인해 .env에 입력하세요.')
    headers = {}
    auth_header = os.getenv('AISTREAM_AUTH_HEADER', '').strip()
    auth_value = os.getenv('AISTREAM_AUTH_VALUE', '').strip()
    if auth_header and auth_value:
        headers[auth_header] = auth_value
    mode = os.getenv('AISTREAM_REQUEST_MODE', 'multipart').lower()
    timeout_seconds = float(os.getenv('AISTREAM_TIMEOUT_SECONDS', '180'))
    if not 0 < timeout_seconds <= 600:
        raise ValueError('AISTREAM_TIMEOUT_SECONDS는 0보다 크고 600 이하여야 합니다.')
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=15), follow_redirects=False) as client:
        if mode == 'multipart':
            field = os.getenv('AISTREAM_IMAGE_FIELD', 'file')
            res = client.post(url, files={field: (filename, image_bytes, 'image/jpeg')}, headers=headers)
        elif mode == 'json_base64':
            field = os.getenv('AISTREAM_IMAGE_FIELD', 'image')
            res = client.post(url, json={field: base64.b64encode(image_bytes).decode()}, headers=headers)
        else:
            raise ValueError(f'지원하지 않는 AISTREAM_REQUEST_MODE: {mode}')
    try:
        res.raise_for_status()
    except httpx.HTTPStatusError:
        raise ValueError(f'AIStream HTTP {res.status_code}; /docs에서 요청 포맷 및 인증을 확인하세요.') from None
    try:
        payload = res.json()
    except ValueError:
        raise ValueError('AIStream 응답이 JSON이 아닙니다. /docs에서 출력 형식을 확인하세요.') from None
    parsed = parse_response(payload)
    return parsed, payload
