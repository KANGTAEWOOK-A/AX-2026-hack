#!/usr/bin/env python3
"""From hackathon-supplied Wind JSON sidecars, build explicit truth manifest and optional small ZIP.

Do NOT guess truth from annotations=[]; only explicit info.db_name is accepted.
Do NOT auto-split near-duplicate imagery into train/test; caller must assign independently.
"""
import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description='대회 제공 풍력 이미지+JSON -> manifest.csv + import ZIP')
    p.add_argument('--images', required=True, type=Path)
    p.add_argument('--labels', required=True, type=Path)
    p.add_argument('--out', type=Path, default=Path('manifest.csv'))
    p.add_argument('--zip', dest='zip_path', type=Path, help='웹 가져오기용 ZIP 저장 경로')
    p.add_argument('--limit', type=int, default=100)
    p.add_argument('--split', choices=['train', 'val', 'test', 'unassigned'], default='unassigned',
                   help='명시적 그룹 분리 후에만 지정. 임의 분할 금지.')
    args = p.parse_args()
    if not args.images.is_dir() or not args.labels.is_dir():
        p.error('이미지 폴더와 JSON 폴더가 필요합니다.')
    imgs = {}
    for f in args.images.rglob('*'):
        if f.suffix.lower() in ('.jpg', '.jpeg', '.png') and f.is_file():
            imgs.setdefault(f.stem.casefold(), []).append(f)
    labels = list(args.labels.rglob('*.json'))
    selected, skipped = [], []
    for lf in labels:
        if len(selected) >= args.limit: break
        try:
            data = json.loads(lf.read_text(encoding='utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            skipped.append((lf.name, 'JSON 읽기 실패'));continue
        if not isinstance(data, dict):
            skipped.append((lf.name, 'JSON 최상위 객체가 아님'));continue
        info = data.get('info') or {}
        if not isinstance(info, dict): info = {}
        generator = str(info.get('generator_name', '')).lower()
        if generator and not any(x in generator for x in ('풍력', 'wind')):
            continue
        db_name = str(info.get('db_name', '')).replace(':','').strip().lower()
        if db_name == 'normaldb': truth = 'normal'
        elif db_name == 'positivedb': truth = 'defect'
        else:
            skipped.append((lf.name, 'info.db_name 정상·결함 명시 안 됨'));continue
        desc = data.get('images') or data.get('image') or {}
        if isinstance(desc, list): desc = desc[0] if desc else {}
        if not isinstance(desc, dict): desc = {}
        image_name = desc.get('filename') or desc.get('file_name') or lf.stem
        stem = Path(str(image_name)).stem.casefold()
        matches = imgs.get(stem, [])
        if len(matches) != 1:
            skipped.append((lf.name, f'이미지 매칭 {len(matches)}건 (중복/누락)'));continue
        image = matches[0]
        # Use unique destination names to avoid collisions across source subdirectories.
        dest = f'images/{len(selected):04d}_{image.name}'
        collection = data.get('collection') or {}
        if not isinstance(collection, dict): collection = {}
        session = str(collection.get('location') or '')[:90]
        selected.append((image, {'image_file': dest, 'truth': truth, 'split': args.split,
                                 'session': session, 'part': str(info.get('part_tag') or '')[:90]}))
    if not selected:
        p.error('명시적 NormalDB/PositiveDB와 유일 이미지 일치 사례가 없습니다. 실제 JSON 구조를 먼저 검토하세요.')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = ['image_file', 'truth', 'split', 'session', 'part']
    with args.out.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields);w.writeheader()
        for _, row in selected: w.writerow(row)
    if args.zip_path:
        args.zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=2) as z:
            z.write(args.out, 'manifest.csv')
            for src, meta in selected: z.write(src, meta['image_file'])
    print(f'성공 {len(selected)}건 / 제외 {len(skipped)}건, manifest={args.out}')
    if args.zip_path: print(f'웹 가져오기 ZIP={args.zip_path} ({args.zip_path.stat().st_size / 1e6:.1f}MB)')
    if skipped:
        print('제외 샘플(최대 12건):', file=sys.stderr)
        for name, why in skipped[:12]: print(f'  {name}: {why}', file=sys.stderr)
    print('주의: split=unassigned인 경우 평가/학습에 포함되지 않습니다. 촬영 세션 등 그룹 분리 후 train/test를 설정하세요.')

if __name__ == '__main__':
    main()
