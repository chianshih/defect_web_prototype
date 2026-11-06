"""Regression checks for decide()/upload matching rules. No pytest, no network.

Run:  .venv/bin/python tests/check_matching.py
Copies the app into a temp dir so the real data/ is never touched; analyze_image is faked.
"""
import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix='defect_check_'))
for item in ('app.py', 'seed.json', 'seed_images', 'templates'):
    src = ROOT / item
    (shutil.copytree if src.is_dir() else shutil.copy2)(src, tmp / item)
sys.path.insert(0, str(tmp))
os.environ['OPENAI_API_KEY'] = 'fake'  # only so upload() takes the AI path; the call is faked

import app as A  # noqa: E402
from PIL import Image  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


def improvement(**kw):
    base = dict(photo_type='improvement', has_after_label=True, board_no=None, full_id='',
                id_conflict=False, confidence=0.95, found_date='2026-11-12', description='')
    return {**base, **kw}


def new_defect(**kw):
    base = dict(photo_type='defect', has_after_label=False, board_no=None, full_id='',
                id_conflict=False, confidence=0.95, found_date='2026-11-10', floor='6F', description='')
    return {**base, **kw}


# --- Fix 1: a bare number must not be read as an RT serial -------------------------
with A.connect() as db:
    A.add_defect(db, dict(board_no=3, found_date='2026-11-10', description='second inspection, board 3'), 'x.jpg')
    out = A.decide(db, improvement(board_no=3), 'after.jpg', None)
check('bare "缺失 3" with two board-3 defects -> pending', out[0] == 'pending', out[2])

with A.connect() as db:
    out = A.decide(db, improvement(board_no=7), 'after7.jpg', None)
    row = db.execute('SELECT board_no FROM defects WHERE id=?', (out[1],)).fetchone() if out[1] else None
check('bare "缺失 7" with one board-7 defect -> matched to board 7',
      out[0] == 'matched' and row and row['board_no'] == 7, out[2])

with A.connect() as db:
    out = A.decide(db, improvement(full_id='RT-005', board_no=5), 'after5.jpg', None)
    row = db.execute('SELECT serial_no FROM defects WHERE id=?', (out[1],)).fetchone() if out[1] else None
check('explicit RT-005 -> matched to serial 5', out[0] == 'matched' and row and row['serial_no'] == 5, out[2])

# --- Fix 3: near-duplicate new defects without a board number -----------------------
with A.connect() as db:
    first = A.decide(db, new_defect(description='風管吊架未施作'), 'n1.jpg', None)
    again = A.decide(db, new_defect(description='風管吊架 未施作。'), 'n2.jpg', None)
    other_floor = A.decide(db, new_defect(floor='3F', description='風管吊架未施作'), 'n3.jpg', None)
    different = A.decide(db, new_defect(description='消防管支撐鏽蝕需除鏽補漆'), 'n4.jpg', None)
check('first unnumbered defect -> created', first[0] == 'created', first[2])
check('same date/floor, near-identical text -> pending', again[0] == 'pending', again[2])
check('same text on another floor -> created', other_floor[0] == 'created', other_floor[2])
check('different text, same date/floor -> created', different[0] == 'created', different[2])

# --- Fix 2: duplicate photos never reach the AI ------------------------------------
calls = []


def fake_ai(content):
    calls.append(len(content))
    time.sleep(0.2)
    return new_defect(found_date='2026-11-20', description=f'unique text {len(calls)} ' + 'x' * len(calls) * 7)


A.analyze_image = fake_ai
client = A.app.test_client()
buf = io.BytesIO()
Image.new('RGB', (64, 48), (10, 20, 30)).save(buf, 'JPEG')
photo = buf.getvalue()


def upload(*names):
    files = [(io.BytesIO(photo), n) for n in names]
    return client.post('/api/upload', data={'photos': files}, content_type='multipart/form-data').get_json()['results']


r1 = upload('a.jpg', 'a_copy.jpg')
check('same photo twice in one batch -> 1 AI call', len(calls) == 1, f'calls={len(calls)}')
check('in-batch copy -> pending as duplicate', r1[1]['status'] == 'pending' and '同一批次' in r1[1]['reason'], r1[1]['reason'])
r2 = upload('a_again.jpg')
check('re-upload in a later batch -> no AI call', len(calls) == 1, f'calls={len(calls)}')
check('re-upload -> pending as duplicate', r2[0]['status'] == 'pending' and '已經上傳過' in r2[0]['reason'], r2[0]['reason'])

# --- LAN share URLs ------------------------------------------------------------------
os.environ.pop('HOST', None)
os.environ['PORT'] = '5099'
check('default HOST -> only local URL', A.share_urls() == ['http://127.0.0.1:5099'], str(A.share_urls()))
os.environ['HOST'] = '0.0.0.0'
urls = A.share_urls()
check('HOST=0.0.0.0 -> local + hostname URLs, no 127.x/169.254 LAN entries',
      'http://127.0.0.1:5099' in urls and f'http://{A.socket.gethostname()}:5099' in urls
      and not any(u.startswith(('http://169.254.', 'http://0.')) for u in urls), str(urls))
check('/api/config exposes share_urls', client.get('/api/config').get_json().get('share_urls') == urls)

shutil.rmtree(tmp, ignore_errors=True)
print(f'\n{"ALL PASSED" if not failures else str(len(failures)) + " FAILED"}')
sys.exit(1 if failures else 0)
