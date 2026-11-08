import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from scripts.inspect_download import MemberStream, load_labels, scan, truth_from_label


def test_explicit_normal_without_annotations_and_conflicting_truth():
    label = {'info': {'db_name': 'NormalDB'}, 'visionqa': {
        'defect_detection_a': 'b', 'defect_detection_option': {'detection_option_b': '아니오'}}}
    assert truth_from_label(label) == 'normal'
    label['info']['db_name'] = 'PositiveDB'
    with pytest.raises(ValueError, match='불일치'):
        truth_from_label(label)
    with pytest.raises(ValueError, match='명시적'):
        truth_from_label({'annotations': []})


def make_tar(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as z:
        z.writestr('/normal.json', json.dumps({
            'info': {'db_name': 'NormalDB'}, 'image': {'filename': 'normal.jpg'},
            'collection': {'location': 'A', 'datetime': '2026-11-07 10:00:00'}}))
    data = payload.getvalue()
    path = tmp_path / 'download.tar'
    with tarfile.open(path, 'w') as t:
        for start, part in [(0, data[:40]), (40, data[40:])]:
            info = tarfile.TarInfo(f'VL_wind.zip.part{start}')
            info.size = len(part)
            t.addfile(info, io.BytesIO(part))
    return path, data


def test_split_zip_reads_across_boundaries_without_extraction(tmp_path):
    path, data = make_tar(tmp_path)
    _, incomplete, groups = scan(path)
    assert not incomplete
    stream = MemberStream(path, groups['VL_wind.zip'])
    try:
        stream.seek(35)
        assert stream.read(15) == data[35:50]
        stream.seek(-12, 2)
        assert stream.read() == data[-12:]
    finally:
        stream.close()
    rows, errors = load_labels(path, groups['VL_wind.zip'])
    assert not errors and rows[0]['truth'] == 'normal'
    assert rows[0]['split'] == 'unassigned'
    assert rows[0]['session'] == 'A|2026-11-07'


def test_incomplete_download_ignores_unfinished_member(tmp_path):
    path, _ = make_tar(tmp_path)
    path.write_bytes(path.read_bytes()[:1100])
    _, incomplete, groups = scan(path)
    assert incomplete
    assert len(groups['VL_wind.zip']) == 1
    with pytest.raises(zipfile.BadZipFile):
        load_labels(path, groups['VL_wind.zip'])


def test_missing_part_is_rejected(tmp_path):
    path, _ = make_tar(tmp_path)
    _, _, groups = scan(path)
    groups['VL_wind.zip'][1].name = 'VL_wind.zip.part41'
    with pytest.raises(ValueError, match='누락'):
        MemberStream(path, groups['VL_wind.zip'])
