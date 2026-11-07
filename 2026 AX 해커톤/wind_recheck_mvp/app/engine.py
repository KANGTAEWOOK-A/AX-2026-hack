"""Image-only triage signals, optional learned error ranking, and honest evaluation."""
import hashlib
from functools import lru_cache
import math
import os
from pathlib import Path

import cv2
import joblib
import numpy as np
from PIL import Image
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ['confidence', 'brightness', 'contrast', 'blur_log', 'dark_ratio', 'bright_ratio']


def image_features(path):
    with Image.open(path) as im:
        gray = np.array(im.convert('L').resize((256, 256)), dtype=np.uint8)
    return {
        'brightness': round(float(gray.mean()) / 255.0, 6),
        'contrast': round(float(gray.std()) / 128.0, 6),
        'blur_log': round(math.log1p(max(0, float(cv2.Laplacian(gray, cv2.CV_64F).var()))), 6),
        'dark_ratio': round(float(np.mean(gray <= 15)), 6),
        'bright_ratio': round(float(np.mean(gray >= 245)), 6),
    }


def vector(row):
    conf = row.get('confidence')
    conf = 0.5 if conf is None else max(0, min(1, float(conf)))
    return [conf] + [float(row.get(f) or 0) for f in FEATURES[1:]]


def quality_risk(row):
    """Non-learned, uncalibrated 0..1 priority heuristic. NOT failure probability."""
    brightness = 0.5 if row.get('brightness') is None else float(row['brightness'])
    blur = float(row.get('blur_log') or 0)
    high_exposure = float(row.get('bright_ratio') or 0)
    low_exposure = float(row.get('dark_ratio') or 0)
    return round(min(1.0, 0.40 * min(1, abs(brightness - 0.5) * 2)
                     + 0.25 * (1 - min(1, blur / 5))
                     + 0.35 * min(1, low_exposure + high_exposure)), 6)


def confidence_risk(row):
    conf = row.get('confidence')
    return None if conf is None else round(1 - max(0, min(1, float(conf))), 6)


def fit_model(rows, out_path):
    """Only use explicitly TRAIN-split, model-predicted-normal items."""
    train = [r for r in rows if r['split'] == 'train'
             and r['pred'] == 'normal' and r['truth'] in ('normal', 'defect')
             and r.get('origin') != 'simulated']
    y = [1 if r['truth'] == 'defect' else 0 for r in train]
    if len(train) < 12 or sum(y) < 3 or len(train) - sum(y) < 3:
        raise ValueError('학습용 실제 정상·미검출 표본이 최소 12건, 누락 결함·정상 각각 3건 이상 필요합니다.')
    model = Pipeline([('scaler', StandardScaler()),
                      ('classifier', LogisticRegression(class_weight='balanced', max_iter=2000, random_state=41))])
    model.fit(np.array([vector(r) for r in train]), np.array(y))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({'model': model, 'features': FEATURES, 'train_count': len(train), 'positive_count': sum(y)}, out_path)
    return {'train_count': len(train), 'missed_defect_count': sum(y), 'note': '점수는 비보정 순위 지표이며 고장 확률이 아닙니다.'}


@lru_cache(maxsize=8)
def _load_model_by_mtime(path, mtime):
    # Load ONLY the model created locally by this app, never an uploaded pickle.
    return joblib.load(path)['model']


def load_model(path):
    if not os.path.exists(path):
        return None
    return _load_model_by_mtime(str(path), os.stat(path).st_mtime_ns)


def priority(row, model=None):
    q = quality_risk(row)
    conf = confidence_risk(row)
    if model is not None and row.get('origin') != 'simulated':
        return round(float(model.predict_proba(np.array([vector(row)]))[0, 1]), 6), '학습형 오류 순위(비보정)'
    if conf is not None:
        return round(0.65 * conf + 0.35 * q, 6), '신뢰도·영상품질 규칙(학습 전)'
    return q, '영상품질 규칙(학습 전)'


def evaluate(rows, k, model=None, seed=42):
    """Evaluate TEST only and never fill missing truth. Same candidate population and K."""
    cohort = [r for r in rows if r['split'] == 'test' and r['pred'] == 'normal'
              and r['truth'] in ('normal', 'defect') and r.get('origin') != 'simulated']
    cohort = sorted(cohort, key=lambda r: r['id'])
    missed = sum(r['truth'] == 'defect' for r in cohort)
    k = max(0, min(k, len(cohort)))
    methods = {
        '무작위': sorted(cohort, key=lambda r: hashlib.sha256((str(seed) + r['id']).encode()).hexdigest()),
        '영상 품질 규칙': sorted(cohort, key=quality_risk, reverse=True),
        'Wind Recheck': sorted(cohort, key=lambda r: priority(r, model)[0], reverse=True)
    }
    if cohort and all(r.get('confidence') is not None for r in cohort):
        methods['낮은 신뢰도순'] = sorted(cohort, key=lambda r: confidence_risk(r), reverse=True)
    results = []
    for name, ranked in methods.items():
        chosen = ranked[:k]
        found = sum(r['truth'] == 'defect' for r in chosen)
        results.append({
            'method': name, 'selected': k, 'found': found,
            'capture_at_k': round(found / missed, 4) if missed else None,
            'precision_at_k': round(found / k, 4) if k else None,
            'remaining': missed - found,
            'ids': [r['id'] for r in chosen],
        })
    return {'test_count': len(cohort), 'missed_defect_count': missed, 'k': k,
            'methods': results, 'trained': model is not None,
            'random_expectation': {
                'capture_at_k': round(k / len(cohort), 4) if cohort and missed else None,
                'precision_at_k': round(missed / len(cohort), 4) if cohort and k else None,
                'remaining': round(missed * (1 - k / len(cohort)), 4) if cohort else 0,
                'note': '균등 무작위 비복원 추출의 이론적 기대값. methods의 무작위는 고정 seed 1회 결과.'},
            'warning': '제공 모델의 원래 학습 데이터 중복은 별도 확인 필요. 실제 현장 일반화 성능으로 해석하지 마세요.'}
