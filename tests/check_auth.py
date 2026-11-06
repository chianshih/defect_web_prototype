"""Regression checks for name-pick login, admin gate, and attribution. No pytest, no network.

Run:  .venv/bin/python tests/check_auth.py
Copies the app into a temp dir so the real data/ and .env are never touched; analyze_image is faked.
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp(prefix='defect_auth_'))
for item in ('app.py', 'seed.json', 'seed_images', 'templates'):
    src = ROOT / item
    (shutil.copytree if src.is_dir() else shutil.copy2)(src, tmp / item)
sys.path.insert(0, str(tmp))
os.environ['OPENAI_API_KEY'] = 'fake'
os.environ.pop('ADMIN_PASSWORD', None)

import app as A  # noqa: E402
from PIL import Image  # noqa: E402

failures = []


def check(name, cond, detail=''):
    print(('PASS ' if cond else 'FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        failures.append(name)


counter = [0]


def fake_ai(content):
    counter[0] += 1
    n = counter[0]
    if n % 2 == 0:  # low confidence -> goes to the manual-review queue
        return dict(photo_type='defect', has_after_label=False, board_no=None, full_id='', id_conflict=False,
                    confidence=0.3, found_date='2026-11-10', floor='5F', description=f'unclear {n}')
    return dict(photo_type='defect', has_after_label=False, board_no=None, full_id='', id_conflict=False,
                confidence=0.95, found_date='2026-11-11', floor=f'{n}F', description=f'unique defect text number {n} ' + 'q' * n * 9)


A.analyze_image = fake_ai


def photo(color):
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(buf, 'JPEG')
    return io.BytesIO(buf.getvalue())


def upload(client, color):
    return client.post('/api/upload', data={'photos': [(photo(color), 'p.jpg')]}, content_type='multipart/form-data')


def rows(sql, *args):
    with A.connect() as db:
        return [dict(r) for r in db.execute(sql, args)]


# --- access control while logged out ------------------------------------------------
anon = A.app.test_client()
r = anon.get('/')
check('anon GET / -> redirect to /login', r.status_code == 302 and r.headers['Location'].endswith('/login'), str(r.status_code))
for path in ('/api/defects', '/api/config', '/api/review', '/api/export.xlsx', '/api/admin/audit', '/images/x.jpg'):
    r = anon.get(path)
    check(f'anon GET {path} -> 401 JSON', r.status_code == 401 and r.get_json().get('login_required'), str(r.status_code))
check('anon POST /api/upload -> 401', anon.post('/api/upload').status_code == 401)
r = anon.get('/login')
check('login page is public and offers first-user bootstrap', r.status_code == 200 and 'name="name"'.encode() in r.data and '第一次使用'.encode() in r.data)
check('secret_key persisted to data/', (tmp / 'data' / 'secret_key').exists() and len((tmp / 'data' / 'secret_key').read_text().strip()) == 64)
check('secret_key reused, not regenerated', A.load_secret_key() == A.app.secret_key)

# --- bootstrap + name pick ------------------------------------------------------------
alice = A.app.test_client()
r = alice.post('/api/login', json={'name': ''})
check('empty name rejected', r.status_code == 400)
r = alice.post('/api/login', json={'name': '王小明'})
check('first login creates first user', r.status_code == 200 and rows("SELECT * FROM users WHERE name='王小明' AND is_admin=1 AND active=1"))
cookie = r.headers.get('Set-Cookie', '')
check('session cookie is persistent (Expires ~30 days)', 'Expires=' in cookie, cookie[:120])
me = alice.get('/api/config').get_json()
check('/api/config reports user, not admin', me['user'] == '王小明' and me['admin'] is False and me['admin_enabled'] is False, str(me))
check('logged-in user can open /', alice.get('/').status_code == 200 and '切換使用者'.encode() in alice.get('/').data)
check('index has uploader filter + 上傳者 label', b'uploader-filter' in alice.get('/').data and '上傳者'.encode() in alice.get('/').data)
r = A.app.test_client().post('/api/login', json={'name': '陌生人'})
check('unknown name rejected once users exist', r.status_code == 403, r.get_data(as_text=True)[:80])
check('login page now shows a dropdown', '<select'.encode() in anon.get('/login').data and '王小明'.encode() in anon.get('/login').data)

# --- admin gate -------------------------------------------------------------------------
check('admin API 403 for normal user', alice.get('/api/admin/defects').status_code == 403)
check('admin password without ADMIN_PASSWORD env -> 400', A.app.test_client().post('/api/login', json={'name': '王小明', 'admin_password': 'x'}).status_code == 400)
os.environ['ADMIN_PASSWORD'] = 'letmein-123'
bad = A.app.test_client().post('/api/login', json={'name': '王小明', 'admin_password': 'wrong'})
check('wrong admin password -> 403', bad.status_code == 403)
boss = A.app.test_client()
check('right admin password -> admin session', boss.post('/api/login', json={'name': '王小明', 'admin_password': 'letmein-123'}).get_json().get('admin') is True)
check('admin can read admin API', boss.get('/api/admin/defects').status_code == 200)
check('ordinary session of same name still not admin', alice.get('/api/admin/audit').status_code == 403)

# --- user management ---------------------------------------------------------------------
check('admin adds user', boss.post('/api/admin/users', json={'name': '李大華'}).status_code == 200)
check('duplicate name -> 409', boss.post('/api/admin/users', json={'name': '李大華'}).status_code == 409)
bob = A.app.test_client()
check('new user can log in', bob.post('/api/login', json={'name': '李大華'}).status_code == 200)
uid = rows("SELECT id FROM users WHERE name='李大華'")[0]['id']
check('cannot deactivate yourself', boss.post(f'/api/admin/users/{rows("SELECT id FROM users WHERE name=?", "王小明")[0]["id"]}/active', json={'active': False}).status_code == 400)
check('admin deactivates user', boss.post(f'/api/admin/users/{uid}/active', json={'active': False}).status_code == 200)
check('deactivated user session -> 401', bob.get('/api/defects').status_code == 401)
check('deactivated user cannot log in', A.app.test_client().post('/api/login', json={'name': '李大華'}).status_code == 403)
check('name kept in table (never deleted)', len(rows("SELECT * FROM users WHERE name='李大華' AND active=0")) == 1)
boss.post(f'/api/admin/users/{uid}/active', json={'active': True})
bob = A.app.test_client()
check('reactivated user can log in again', bob.post('/api/login', json={'name': '李大華'}).status_code == 200)

# --- attribution --------------------------------------------------------------------------
r = upload(alice, (200, 10, 10))
check('alice upload creates a defect', r.get_json()['results'][0]['status'] == 'created', str(r.get_json()))
r = upload(alice, (10, 200, 10))  # low confidence -> pending
check('second upload goes to review', r.get_json()['results'][0]['status'] == 'pending')
sub = rows('SELECT * FROM submissions ORDER BY id DESC LIMIT 2')
check('submissions.uploaded_by recorded', all(s['uploaded_by'] == '王小明' for s in sub), str([s['uploaded_by'] for s in sub]))
d = rows('SELECT * FROM defects ORDER BY id DESC LIMIT 1')[0]
check('defects.created_by = uploader', d['created_by'] == '王小明', str(d['created_by']))
api_item = [x for x in alice.get('/api/defects').get_json()['items'] if x['id'] == d['id']][0]
check('/api/defects exposes created_by', api_item['created_by'] == '王小明')
check('seed defects have no uploader', rows("SELECT COUNT(*) n FROM defects WHERE serial_no<=32 AND created_by IS NOT NULL")[0]['n'] == 0)
a = rows("SELECT * FROM audit_log WHERE action='created' ORDER BY id DESC LIMIT 1")[0]
check('audit row has actor and IP', a['actor'] == '王小明' and a['ip'] == '127.0.0.1', f"{a['actor']} {a['ip']}")

pend = alice.get('/api/review').get_json()['items'][0]
r = bob.post(f'/api/review/{pend["id"]}/confirm', json={'photo_type': 'defect', 'found_date': '2026-11-10', 'floor': '5F', 'description': 'manual'})
check('other user confirms pending photo', r.status_code == 200, r.get_data(as_text=True)[:80])
nd = rows('SELECT * FROM defects ORDER BY id DESC LIMIT 1')[0]
check('created_by keeps the uploader, not the confirmer', nd['created_by'] == '王小明', str(nd['created_by']))
a = rows("SELECT * FROM audit_log WHERE action='confirmed' ORDER BY id DESC LIMIT 1")[0]
check('confirm audit actor = confirmer', a['actor'] == '李大華')

alice.patch(f'/api/defects/{nd["id"]}/status', json={'status': '改善中'})
a = rows("SELECT * FROM audit_log WHERE action='status' ORDER BY id DESC LIMIT 1")[0]
check('status change audit actor', a['actor'] == '王小明')
boss.patch(f'/api/admin/defects/{nd["id"]}', json={'notes': 'edited'})
a = rows("SELECT * FROM audit_log WHERE action='edit' ORDER BY id DESC LIMIT 1")[0]
check('admin action audit actor', a['actor'] == '王小明' and a['ip'])
check('login recorded as audit action', len(rows("SELECT * FROM audit_log WHERE action='login' AND actor='李大華'")) >= 1)
check('failed admin login recorded', len(rows("SELECT * FROM audit_log WHERE action='login_failed'")) == 1)
feed = boss.get('/api/admin/audit').get_json()['items']
check('/api/admin/audit exposes actor and ip', feed and 'actor' in feed[0] and 'ip' in feed[0])
check('upload recorded as audit action', len(rows("SELECT * FROM audit_log WHERE action='upload'")) == 2)

bob.post('/api/logout')
check('logout recorded', len(rows("SELECT * FROM audit_log WHERE action='logout' AND actor='李大華'")) == 1)
check('after logout -> 401', bob.get('/api/defects').status_code == 401)

# --- export --------------------------------------------------------------------------------
x = alice.get('/api/export.xlsx')
check('export works', x.status_code == 200)
with zipfile.ZipFile(io.BytesIO(x.data)) as z:
    strings = z.read('xl/sharedStrings.xml').decode('utf-8')
check('export has 上傳者 column and uploader name', '上傳者' in strings and '王小明' in strings)

# --- additive migration of a legacy database -------------------------------------------------
legacy = tmp / 'legacy.sqlite3'
with sqlite3.connect(legacy) as db:
    db.executescript('''CREATE TABLE defects(id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT UNIQUE NOT NULL, board_no INTEGER,
        found_date TEXT, floor TEXT, location TEXT, item TEXT, description TEXT, contractor TEXT, status TEXT NOT NULL DEFAULT '待改善',
        initial_photo TEXT NOT NULL, improvement_photo TEXT, improved_date TEXT, verified_date TEXT, notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, serial_no INTEGER UNIQUE, deleted_at TEXT, updated_at TEXT);
        CREATE TABLE submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, photo TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL);
        INSERT INTO audit_log(action,entity_type,entity_id) VALUES ('old','defect',1);''')
A.DATABASE = legacy
A.init_db()
with A.connect() as db:
    cols = lambda t: {r['name'] for r in db.execute(f'PRAGMA table_info({t})')}
    ok = ('created_by' in cols('defects') and 'uploaded_by' in cols('submissions') and {'actor', 'ip'} <= cols('audit_log')
          and bool(cols('users')) and db.execute('SELECT COUNT(*) n FROM audit_log').fetchone()['n'] == 1)
check('legacy DB upgraded additively (columns + users table, old rows kept)', ok)

shutil.rmtree(tmp, ignore_errors=True)
print(f'\n{"ALL PASSED" if not failures else str(len(failures)) + " FAILED"}')
sys.exit(1 if failures else 0)
