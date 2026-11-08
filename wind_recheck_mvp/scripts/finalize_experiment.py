#!/usr/bin/env python3
"""Publish a reproducible summary only after the real pilot has completed."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'reports/pilot_experiment.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/final_evaluation.json')
    parser.add_argument('--training-report', type=Path, default=ROOT / 'reports/training_expansion.json')
    args = parser.parse_args()
    pilot = json.loads(args.input.read_text())
    if pilot.get('state') != 'completed':
        parser.error('실제 추론이 완료되지 않았습니다. 진행 중·실패 결과를 최종 성능으로 내보낼 수 없습니다.')
    from app.main import rows, MODEL, UPLOADS
    from app.engine import evaluate, load_model
    ids = {r['id'] for r in pilot['items']}
    base_count = len(ids)
    expansion = None
    if args.training_report.exists():
        expansion = json.loads(args.training_report.read_text())
        if expansion.get('state') != 'completed':
            parser.error('추가 학습 수집이 아직 완료되지 않았습니다.')
        ids.update(r['id'] for r in expansion['items'] if 'id' in r)
    cohort = [r for r in rows() if r['id'] in ids]
    registered_count = len(rows())
    if base_count != pilot['planned'] or len(cohort) != len(ids):
        raise ValueError('계획·추론·DB의 표본 수가 다릅니다.')
    assignments = pilot['group_assignment']
    for row in cohort:
        if row['origin'] != 'real' or row['split'] != assignments.get(row['session']):
            raise ValueError('실제 데이터 또는 그룹 분리 검증 실패: ' + row['filename'])
    if len({r['filename'] for r in cohort}) != len(cohort):
        raise ValueError('선택 표본의 중복 이미지 파일명입니다.')
    hashes = {}
    for row in cohort:
        digest = hashlib.sha256((UPLOADS / row['storage_name']).read_bytes()).hexdigest()
        if digest in hashes and hashes[digest] != row['split']:
            raise ValueError('분할 간 동일 이미지 바이트가 발견되었습니다.')
        hashes[digest] = row['split']
    training = expansion['training'] if expansion else pilot['training']
    model = load_model(MODEL) if training.get('ok') else None
    output = {k: pilot[k] for k in ('selection', 'data_permission', 'source_split', 'group_counts',
              'image_counts', 'group_assignment', 'selection_manifest', 'training', 'limitations')}
    output.update(state='completed', generated_at=datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
                  selected_count=len(cohort),
                  registered_count=registered_count,
                  excluded_from_experiment_count=registered_count-len(cohort),
                  api_success_count=sum(r['api_state'] == 'success' for r in cohort),
                  api_error_count=sum(r['api_state'] == 'error' for r in cohort),
                  unknown_count=sum(r['pred'] == 'unknown' for r in cohort),
                  model_input='App JPEG RGB normalization, quality=90, full image dimensions',
                  confidence_feature='Not provided; constant 0.5 imputation cannot distinguish images',
                  validation_tuning_performed=False,
                  capture_meaning='Ground-truth defects included in selected K; human discovery rate is not measured',
                  comparisons_excluded={'low_confidence': 'Actual API does not return confidence'})
    output['training'] = training
    output['learned_model_sha256'] = hashlib.sha256(MODEL.read_bytes()).hexdigest() if model is not None else None
    output['image_counts'] = dict(Counter(r['split'] for r in cohort))
    output['base_pilot_count'] = base_count
    output['extra_training_count'] = len(ids) - base_count
    output['cross_split_exact_image_duplicates'] = 0
    if expansion:
        output['source_split'] = 'Validation pilot + original Training imagery restricted to fixed TRAIN groups'
        output['training_expansion'] = {k: expansion[k] for k in ('selection', 'stopping_reason', 'training_candidates')}
        output['limitations'].append('The original 4 test candidates were inspected before filling the pre-existing training quota. Hyperparameters, features and test split were unchanged. This is an exploratory pilot, not a fresh confirmatory test.')
        output['selection_manifest'].extend({'image_file': 'Training/' + r['filename'],
                                             'session': r['session'], 'split': 'train'}
                                            for r in expansion['items'] if 'id' in r)
    output['predictions'] = dict(Counter(r['pred'] for r in cohort))
    output['per_split'] = {}
    for split in ('train', 'val', 'test'):
        subset = [r for r in cohort if r['split'] == split]
        output['per_split'][split] = {
            'image_count': len(subset),
            'truth_defect': sum(r['truth'] == 'defect' for r in subset),
            'truth_normal': sum(r['truth'] == 'normal' for r in subset),
            'model_normal': sum(r['pred'] == 'normal' for r in subset),
            'model_defect': sum(r['pred'] == 'defect' for r in subset),
            'missed_defect': sum(r['pred'] == 'normal' and r['truth'] == 'defect' for r in subset),
            'false_positive': sum(r['pred'] == 'defect' and r['truth'] == 'normal' for r in subset),
            'api_errors': sum(r['api_state'] == 'error' for r in subset)}
    output['evaluations'] = []
    for k in (1, 3, 5, 10, 20):
        result = evaluate(cohort, k, model)
        result['requested_k'] = k
        output['evaluations'].append(result)
    output['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (ROOT / 'app').rglob('*')
                               if p.is_file() and '__pycache__' not in p.parts}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    public_metrics = {k: v for k, v in output.items()
                      if k not in ('group_assignment', 'selection_manifest')}
    for evaluation in public_metrics['evaluations']:
        for method in evaluation['methods']:
            method.pop('ids', None)
    args.out.with_name('final_metrics.json').write_text(
        json.dumps(public_metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: output[k] for k in ('state', 'selected_count', 'api_success_count',
                     'api_error_count', 'predictions', 'training', 'per_split', 'evaluations')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
