#!/usr/bin/env python3
"""Approved validation imagery: group-separated pilot, real AIStream only."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.inspect_download import MemberStream, load_labels, scan


def key(value):
    return hashlib.sha256(('wind-recheck-pilot-v1:' + value).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--tar', type=Path)
    source.add_argument('--source-zip', type=Path)
    parser.add_argument('--manifest', type=Path, help='Audited Validation manifest for a completed source ZIP.')
    parser.add_argument('--per-session', type=int, default=2, choices=range(1, 6))
    parser.add_argument('--out', type=Path, default=ROOT / 'reports' / 'pilot_experiment.json')
    parser.add_argument('--run', action='store_true', help='Actually import and call the model; default is plan only.')
    args = parser.parse_args()
    if args.tar:
        size, incomplete, groups = scan(args.tar)
        label_base = next(n for n in groups if Path(n).name.startswith('VL_'))
        image_base = next(n for n in groups if Path(n).name.startswith('VS_'))
        labels, errors = load_labels(args.tar, groups[label_base])
    else:
        if not args.manifest:
            parser.error('--manifest is required with --source-zip')
        size, incomplete = args.source_zip.stat().st_size, False
        with args.manifest.open(encoding='utf-8-sig', newline='') as f:
            labels = list(csv.DictReader(f))
        errors = [r for r in labels if r.get('truth') not in ('normal', 'defect') or not r.get('session')]
    if errors:
        raise ValueError('Label audit errors: ' + str(errors[:3]))
    sessions = sorted({r['session'] for r in labels}, key=key)
    if len(sessions) < 5 or any(not s.split('|')[0] or not s.split('|')[-1] for s in sessions):
        raise ValueError('At least five explicit location/date groups are required.')
    first, second = int(len(sessions) * .6), int(len(sessions) * .8)
    assignment = {s: 'train' if i < first else 'val' if i < second else 'test'
                  for i, s in enumerate(sessions)}
    selected = []
    for session in sessions:
        group = sorted((r for r in labels if r['session'] == session), key=lambda r: key(r['image_file']))
        selected.extend([{**r, 'split': assignment[session]} for r in group[:args.per_session]])
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'planned',
              'selection': 'SHA256 filename order, max per location/date group; no truth-based sampling',
              'data_permission': 'organizer approval confirmed by user on 2026-11-07',
              'source_bytes_at_scan': size, 'tar_incomplete': incomplete,
              'source_split': 'Validation', 'per_session': args.per_session,
              'group_counts': dict(Counter(assignment.values())),
              'image_counts': dict(Counter(r['split'] for r in selected)),
              'group_assignment': assignment,
              'selection_manifest': [{k: r[k] for k in ('image_file', 'session', 'split')} for r in selected],
              'planned': len(selected), 'processed': 0, 'items': [],
              'limitations': ['Small exploratory pilot; no generalization claim.',
                             'Overlap with base model training data is unknown.',
                             'Location/date separation does not prove equipment independence.']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    def save():
        temporary = args.out.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(args.out)
    save()
    print(json.dumps({k: v for k, v in report.items() if k != 'items'}, ensure_ascii=False), flush=True)
    if not args.run:
        return
    from app.main import db, infer_one, insert_image, MODEL, rows, MAX_IMAGE_BYTES
    from app.engine import evaluate, fit_model, load_model
    report['state'] = 'running'
    stream = MemberStream(args.tar, groups[image_base]) if args.tar else args.source_zip.open('rb')
    try:
        with zipfile.ZipFile(stream) as archive:
            lookup = {}
            for entry in archive.infolist():
                if not entry.is_dir():
                    lookup.setdefault(Path(entry.filename).name, []).append(entry)
            for record in selected:
                name = Path(record['image_file']).name
                matches = lookup.get(name, [])
                if len(matches) != 1 or matches[0].file_size > MAX_IMAGE_BYTES:
                    raise ValueError('Image missing, ambiguous or oversize: ' + name)
                with db() as c:
                    existing = c.execute('SELECT * FROM items WHERE filename=? AND session=? AND origin=?',
                                         (name, record['session'], 'real')).fetchall()
                if len(existing) > 1:
                    raise ValueError('Duplicate existing image: ' + name)
                if existing:
                    old = dict(existing[0])
                    if old['truth'] != record['truth'] or old['split'] not in ('unassigned', record['split']):
                        raise ValueError('Existing label/split conflict: ' + name)
                    ident = old['id']
                    with db() as c:
                        c.execute('UPDATE items SET split=? WHERE id=?', (record['split'], ident))
                else:
                    ident = insert_image(archive.read(matches[0]), name, truth=record['truth'],
                                         split=record['split'], session=record['session'], part=record['part'])
                    old = {}
                started = time.monotonic()
                try:
                    result = {'pred': old['pred'], 'reused': True} if old.get('api_state') == 'success' else infer_one(ident)
                    outcome = {'id': ident, 'filename': name, 'split': record['split'],
                               'truth': record['truth'], 'result': result}
                except Exception as error:
                    outcome = {'id': ident, 'filename': name, 'split': record['split'],
                               'error': str(getattr(error, 'detail', error))}
                outcome['seconds'] = round(time.monotonic() - started, 3)
                report['items'].append(outcome)
                report['processed'] += 1
                save()
                print(json.dumps({'progress': report['processed'], 'total': len(selected), **outcome}, ensure_ascii=False), flush=True)
                if report['processed'] >= 3 and all('error' in x for x in report['items'][-3:]):
                    raise RuntimeError('Three consecutive real API errors; stop rather than fabricate outcomes.')
    except Exception as error:
        report['state'] = 'failed'
        report['error'] = str(error)
        save()
        raise
    finally:
        stream.close()
    selected_ids = {x['id'] for x in report['items']}
    cohort = [r for r in rows() if r['id'] in selected_ids]
    try:
        report['training'] = {'ok': True, **fit_model(cohort, MODEL)}
    except ValueError as error:
        report['training'] = {'ok': False, 'reason': str(error)}
    model = load_model(MODEL) if report['training']['ok'] else None
    report['evaluations'] = [evaluate(cohort, k, model) for k in (5, 10, 20)]
    report['predictions'] = dict(Counter(r['pred'] for r in cohort))
    report['state'] = 'completed'
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps({'state': report['state'], 'training': report['training'],
                      'predictions': report['predictions'], 'evaluations': report['evaluations']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
