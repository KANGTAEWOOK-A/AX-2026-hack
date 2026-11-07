#!/usr/bin/env python3
"""UI smoke test only. Entirely synthetic, deliberately excluded from model training/evaluation."""
import io
import random
import sqlite3
import sys
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.main import insert_image, db

random.seed(17)
for idx in range(18):
    img = Image.new('RGB', (800, 480), (29 + (idx % 3) * 7, 44 + (idx % 4) * 5, 59))
    draw = ImageDraw.Draw(img)
    draw.polygon([(125, 115), (727, 165), (697, 304), (108, 248)], fill=(205, 218, 225))
    draw.line([(125, 115), (727, 165), (697, 304), (108, 248), (125, 115)], width=5, fill=(147, 166, 179))
    for j in range(10):
        x = 145 + j * 57
        draw.line([(x, 142), (x - 10, 240)], width=1, fill=(186, 202, 210))
    has_defect = idx % 4 == 2
    if has_defect:
        x = 250 + (idx * 43) % 330
        draw.line([(x, 165), (x+31, 207), (x+66, 191)], width=3, fill=(67, 77, 79))
    draw.text((32, 28), 'SIMULATED UI FIXTURE - NOT FIELD EVIDENCE', fill=(181, 226, 141))
    out = io.BytesIO();img.save(out, 'JPEG')
    item_id = insert_image(out.getvalue(), f'SIMULATION_BLADE_{idx+1:03}.jpg',
                           truth='defect' if has_defect else 'normal',
                           split='test', session=f'SIM_{idx//6}', origin='simulated')
    pred = 'normal' if idx % 5 else 'defect'
    with db() as c:
        c.execute('UPDATE items SET pred=?, confidence=?, api_state=?, reason=? WHERE id=?',
                  (pred, 0.80 + (idx % 5) * 0.03, 'simulated',
                   '시뮬레이션용 가짜 판정 · 실제 AIStream 결과 아님', item_id))
print('UI 시뮬레이션 이미지 18건 추가 (실제 실험 제외). 실행 후 반드시 실 데이터로 교체하세요.')
