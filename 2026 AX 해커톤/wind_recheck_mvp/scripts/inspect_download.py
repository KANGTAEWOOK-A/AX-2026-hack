#!/usr/bin/env python3
"""Audit complete members of an AI-Hub tar while its download may still be growing.

Never extract archive paths. Optional small import ZIP preserves image bytes and
keeps split=unassigned. Labels are never sent to a model by this tool.
"""
import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile


class MemberStream:
    """Seekable logical ZIP over offset-numbered tar members, without copying GBs."""
    def __init__(self, path, members):
        self.file = path.open('rb')
        self.pos = 0
        self.parts = []
        expected = 0
        for member in sorted(members, key=lambda m: int(m.name.rsplit('.part', 1)[1])):
            offset = int(member.name.rsplit('.part', 1)[1])
            if offset != expected:
                self.close()
                raise ValueError('분할 ZIP에 누락 또는 중복 구간이 있습니다.')
            self.parts.append((offset, member.offset_data, member.size))
            expected += member.size
        self.size = expected

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset
        if target < 0:
            raise ValueError('negative seek')
        self.pos = target
        return target

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def read(self, count=-1):
        count = max(0, self.size - self.pos) if count < 0 else min(count, max(0, self.size - self.pos))
        chunks = []
        for start, physical, size in self.parts:
            if not count:
                break
            if start <= self.pos < start + size:
                length = min(count, start + size - self.pos)
                self.file.seek(physical + self.pos - start)
                data = self.file.read(length)
                if len(data) != length:
                    raise EOFError('다운로드 원본이 읽는 중 변경되거나 잘렸습니다.')
                chunks.append(data)
                count -= length
                self.pos += length
        return b''.join(chunks)

    def close(self):
        self.file.close()


def scan(path):
    size = path.stat().st_size
    groups = {}
    incomplete = True
    with tarfile.open(path, 'r:') as archive:
        while True:
            next_offset = archive.offset
            try:
                member = archive.next()
            except (tarfile.ReadError, EOFError):
                break
            if member is None:
                # tarfile can silently return None for a partial header at EOF.
                with path.open('rb') as source:
                    source.seek(next_offset)
                    incomplete = source.read(1024) != bytes(1024)
                break
            if member.offset_data + member.size > size:
                break
            if member.isfile() and '.zip.part' in member.name:
                base, offset = member.name.rsplit('.part', 1)
                if offset.isdecimal():
                    groups.setdefault(base, []).append(member)
    return size, incomplete, groups


def truth_from_label(data):
    truth = {'normaldb': 'normal', 'positivedb': 'defect'}.get(
        str(data.get('info', {}).get('db_name', '')).strip().casefold())
    if truth is None:
        raise ValueError('명시적 NormalDB/PositiveDB 없음')
    qa = data.get('visionqa', {})
    answer = qa.get('defect_detection_a')
    choice = qa.get('defect_detection_option', {}).get('detection_option_' + str(answer))
    qa_truth = {'예': 'defect', '아니오': 'normal'}.get(choice)
    if qa_truth and truth != qa_truth:
        raise ValueError('db_name과 VQA 정답 불일치')
    annotations = data.get('annotations')
    if truth == 'normal' and annotations:
        raise ValueError('정상 라벨에 결함 주석 존재')
    if truth == 'defect' and not annotations:
        raise ValueError('결함 라벨의 주석 누락')
    return truth


def load_labels(path, members):
    stream = MemberStream(path, members)
    rows, errors = [], []
    try:
        with zipfile.ZipFile(stream) as labels:
            for info in labels.infolist():
                if not info.filename.lower().endswith('.json'):
                    continue
                try:
                    if info.file_size > 4 * 1024 * 1024:
                        raise ValueError('JSON 크기 제한 초과')
                    data = json.loads(labels.read(info))
                    truth = truth_from_label(data)
                    image = data['image']
                    name = image['filename']
                    if PurePosixPath(name).name != name or '\\' in name:
                        raise ValueError('이미지 파일명에 경로 포함')
                    collection = data.get('collection', {})
                    session = f"{collection.get('location', '')}|{collection.get('datetime', '')[:10]}"
                    rows.append({'image_file': 'images/' + name, 'truth': truth,
                                 'split': 'unassigned', 'session': session,
                                 'part': data.get('info', {}).get('part_tag', '')})
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    errors.append({'label': info.filename, 'error': str(error)})
    finally:
        stream.close()
    return rows, errors


def make_sample(path, members, rows, output, count):
    # Balance only for functional integration checking, never performance estimation.
    selected = []
    for truth in ('normal', 'defect'):
        selected.extend([r for r in rows if r['truth'] == truth][:count])
    stream = MemberStream(path, members)
    try:
        with zipfile.ZipFile(stream) as source:
            by_name = {}
            for info in source.infolist():
                if not info.is_dir():
                    by_name.setdefault(PurePosixPath(info.filename).name, []).append(info)
            resolved = []
            for row in selected:
                matches = by_name.get(PurePosixPath(row['image_file']).name, [])
                if len(matches) != 1:
                    raise ValueError(f"이미지 매칭 {len(matches)}건: {row['image_file']}")
                if matches[0].file_size > 32 * 1024 * 1024:
                    raise ValueError('앱의 32MB 업로드 제한을 초과하는 이미지입니다.')
                resolved.append((row, matches[0]))
            if sum(info.file_size for _, info in resolved) > 125 * 1024 * 1024:
                raise ValueError('샘플 ZIP이 앱 업로드 크기 제한을 초과합니다.')
            temp = output.with_suffix('.tmp')
            try:
                with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED) as target:
                    csvfile = io.StringIO()
                    writer = csv.DictWriter(csvfile, fieldnames=list(rows[0]))
                    writer.writeheader()
                    for row, info in resolved:
                        writer.writerow(row)
                        target.writestr(row['image_file'], source.read(info))
                    target.writestr('manifest.csv', csvfile.getvalue().encode('utf-8-sig'))
                temp.replace(output)
            finally:
                temp.unlink(missing_ok=True)
        return len(resolved)
    finally:
        stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tar', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=Path('reports'))
    parser.add_argument('--sample-per-class', type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.sample_per_class <= 5:
        parser.error('--sample-per-class는 0~5입니다.')
    size, incomplete, groups = scan(args.tar)
    report = {'source_bytes_at_scan': size, 'tar_incomplete': incomplete,
              'splits': {}, 'sample_count': 0,
              'model_inference_performed': False, 'competition_data_permission': 'unverified'}
    args.out.mkdir(parents=True, exist_ok=True)
    split_rows = {}
    for split, prefix in [('Validation', 'VL_'), ('Training', 'TL_')]:
        base = next((n for n in groups if PurePosixPath(n).name.startswith(prefix)), None)
        if base is None:
            continue
        rows, errors = load_labels(args.tar, groups[base])
        split_rows[split] = rows
        names = [r['image_file'] for r in rows]
        report['splits'][split] = {'label_archive': base, 'count': len(rows),
                                  'truth_counts': dict(Counter(r['truth'] for r in rows)),
                                  'duplicate_filenames': len(names) - len(set(names)),
                                  'session_count': len(set(r['session'] for r in rows)), 'errors': errors}
        if rows:
            with (args.out / f'{split.lower()}_manifest.csv').open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    train, val = split_rows.get('Training', []), split_rows.get('Validation', [])
    report['shared_filenames'] = sorted({r['image_file'] for r in train} & {r['image_file'] for r in val})
    report['shared_location_date_groups'] = sorted({r['session'] for r in train} & {r['session'] for r in val})
    if args.sample_per_class and val:
        image_base = next(n for n in groups if PurePosixPath(n).name.startswith('VS_'))
        report['sample_count'] = make_sample(args.tar, groups[image_base], val,
                                            args.out / 'validation_smoke.zip', args.sample_per_class)
    (args.out / 'data_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
