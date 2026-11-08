#!/usr/bin/env python3
"""Collect additional TRAIN-group imagery only; never change the frozen test split."""
import argparse
import csv
import json
from pathlib import Path
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_validation_experiment import key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-zip', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--pilot', type=Path, default=ROOT / 'reports/pilot_experiment.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/training_expansion.json')
    args = parser.parse_args()
    pilot = json.loads(args.pilot.read_text())
    if pilot['state'] != 'completed':
        parser.error('The initial real pilot must be completed first.')
    from app.main import db, insert_image, infer_one, rows, MODEL, MAX_IMAGE_BYTES
    from app.engine import fit_model, evaluate, load_model
    assignments = pilot['group_assignment']
    base_ids = {r['id'] for r in pilot['items']}
    with args.manifest.open(encoding='utf-8-sig', newline='') as f:
        labels = list(csv.DictReader(f))
    labels = [r for r in labels if assignments.get(r['session']) == 'train']
    if any(r['truth'] not in ('normal', 'defect') for r in labels):
        raise ValueError('Unrecognized truth label.')
    report = {'state': 'running', 'purpose': 'Fill pre-existing minimum TRAIN sample requirements',
              'test_split_changed': False, 'hyperparameters_changed': False,
              'source_split': 'Training', 'items': [], 'processed': 0,
              'selection': 'Two filename-hash candidates per original TRAIN group, then filename-hash order; stop on training minimum quota. Training selection is adaptive, not a prevalence estimate.'}
    def save():
        temp = args.out.with_suffix('.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temp.replace(args.out)
    def cohort():
        ids = base_ids | {r['id'] for r in report['items'] if 'id' in r}
        return [r for r in rows() if r['id'] in ids]
    def counts():
        selected = [r for r in cohort() if r['split'] == 'train' and r['pred'] == 'normal'
                    and r['truth'] in ('normal', 'defect')]
        positive = sum(r['truth'] == 'defect' for r in selected)
        return {'total': len(selected), 'missed': positive, 'normal': len(selected) - positive}
    def ready():
        c = counts()
        return c['total'] >= 12 and c['missed'] >= 3 and c['normal'] >= 3
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.source_zip) as archive:
        lookup = {}
        for entry in archive.infolist():
            if not entry.is_dir():
                lookup.setdefault(Path(entry.filename).name, []).append(entry)
        existing_names = {r['filename'] for r in rows()}
        pool = [r for r in labels if Path(r['image_file']).name not in existing_names
                and len(lookup.get(Path(r['image_file']).name, [])) == 1
                and lookup[Path(r['image_file']).name][0].file_size <= MAX_IMAGE_BYTES]
        candidates = []
        for session, split in assignments.items():
            if split == 'train':
                candidates.extend(sorted((r for r in pool if r['session'] == session),
                                         key=lambda r: key(r['image_file']))[:2])
        candidates.sort(key=lambda r: key(r['image_file']))
        report['planned_max'] = len(candidates)
        report['initial_training_candidates'] = counts()
        save()
        print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False), flush=True)
        for record in candidates:
            if ready():
                break
            assert assignments[record['session']] == 'train'
            ident = insert_image(archive.read(lookup[Path(record['image_file']).name][0]),
                                 Path(record['image_file']).name, truth=record['truth'], split='train',
                                 session=record['session'], part=record['part'])
            started = time.monotonic()
            item = {'id': ident, 'filename': Path(record['image_file']).name,
                    'session': record['session'], 'split': 'train', 'source_split': 'Training'}
            try:
                item['result'] = infer_one(ident)
            except Exception as error:
                item['error'] = str(getattr(error, 'detail', error))
            item['seconds'] = round(time.monotonic() - started, 3)
            report['items'].append(item)
            report['processed'] += 1
            report['training_candidates'] = counts()
            save()
            print(json.dumps(item | {'training_candidates': counts()}, ensure_ascii=False), flush=True)
            if len(report['items']) >= 3 and all('error' in r for r in report['items'][-3:]):
                report.update(state='failed', error='Three consecutive API failures')
                save()
                return
    try:
        report['training'] = {'ok': True, **fit_model(cohort(), MODEL)}
    except ValueError as error:
        report['training'] = {'ok': False, 'reason': str(error)}
    report['state'] = 'completed'
    report['stopping_reason'] = 'minimum training quota met' if ready() else 'candidate budget exhausted'
    report['training_candidates'] = counts()
    if report['training']['ok']:
        report['test_evaluations'] = [evaluate(cohort(), k, load_model(MODEL)) for k in (1, 3, 5)]
    save()
    print(json.dumps({'state': report['state'], 'training': report['training'],
                      'training_candidates': counts()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
