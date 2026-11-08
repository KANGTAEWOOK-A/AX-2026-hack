import csv
import json
import sys
from scripts.run_validation_experiment import main


def test_plan_separates_groups_and_ignores_truth_for_sampling(tmp_path, monkeypatch):
    source = tmp_path / 'source.zip'
    source.write_bytes(b'plan does not read image contents or call the model')
    manifest = tmp_path / 'manifest.csv'
    output = tmp_path / 'report.json'
    fields = ['image_file', 'truth', 'split', 'session', 'part']
    rows = [dict(zip(fields, [f'images/site{i}_{j}.jpg', 'defect' if j % 2 else 'normal',
                              'unassigned', f'site{i}|2026-11-07', 'blade']))
            for i in range(6) for j in range(4)]
    def write_manifest():
        with manifest.open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    write_manifest()
    monkeypatch.setattr(sys, 'argv', ['experiment', '--source-zip', str(source),
                                      '--manifest', str(manifest), '--out', str(output)])
    main()
    first = json.loads(output.read_text())
    assert first['state'] == 'planned' and first['processed'] == 0
    assert first['image_counts'] == {'train': 6, 'val': 2, 'test': 4}
    assert len(first['group_assignment']) == 6
    for record in first['selection_manifest']:
        assert record['split'] == first['group_assignment'][record['session']]
    for row in rows:
        row['truth'] = 'normal' if row['truth'] == 'defect' else 'defect'
    write_manifest()
    main()
    second = json.loads(output.read_text())
    assert first['selection_manifest'] == second['selection_manifest']
