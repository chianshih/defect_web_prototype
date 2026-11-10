"""本機用：施工缺失／改善照片辨識與歸檔。上線前需加入身分驗證、權限、HTTPS 與備份。"""
from __future__ import annotations

import base64
import csv
import io
import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import uuid
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory, render_template, session, redirect, g, has_request_context
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DATA = BASE / 'data'
IMAGES = DATA / 'images'
DATABASE = DATA / 'defects.sqlite3'
IMAGES.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True


def load_secret_key():
    '簽署登入 cookie 的金鑰：第一次啟動產生並存在 data/secret_key（不進版控），之後重複使用，重啟不會登出所有人。'
    path = DATA / 'secret_key'
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    key = secrets.token_hex(32)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


app.secret_key = load_secret_key()
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 65 * 1024 * 1024
VALID_STATUSES = ['待改善', '改善中', '已改善', '待複驗', '退回改善', '已結案']
EDIT_FIELDS = ('found_date', 'floor', 'location', 'item', 'description', 'contractor', 'status', 'improved_date', 'verified_date', 'notes')

def server_host():
    return (os.getenv('HOST') or '127.0.0.1').strip()


def server_port():
    return int(os.getenv('PORT') or '5000')


def lan_ipv4s():
    '只用標準函式庫找出本機的區域網路 IPv4（UDP connect 不會真的送出封包）；略過 127.x 與 169.254.x。'
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    out = []
    for ip in found:
        if not ip.startswith(('127.', '169.254.', '0.')) and ip not in out:
            out.append(ip)
    return out


def share_urls():
    'HOST=0.0.0.0 時回傳本機、區域網路 IP 及主機名稱網址；否則只有本機網址。'
    port, host = server_port(), server_host()
    urls = [f'http://127.0.0.1:{port}']
    if host in ('0.0.0.0', ''):
        urls = [f'http://{ip}:{port}' for ip in lan_ipv4s()] + urls
        try:
            urls.append(f'http://{socket.gethostname()}:{port}')
        except OSError:
            pass
    elif host not in ('127.0.0.1', 'localhost'):
        urls = [f'http://{host}:{port}']
    return urls


def display_id(serial):
    # 只修改展示用編號；序列值與舊 public_id 不更動。超過 999 會自然顯示 RT-1000，不會重複。
    return f'RT-{int(serial):03d}'



def connect():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def init_db():
    # 舊版自動備份：第一次升級 v2 前複製 SQLite，避免更新程式碼時覆蓋使用者資料。
    if DATABASE.exists() and DATABASE.stat().st_size:
        try:
            with sqlite3.connect(DATABASE) as source:
                columns = {row[1] for row in source.execute('PRAGMA table_info(defects)')}
                if columns and 'serial_no' not in columns:
                    backups = DATA / 'backups'
                    backups.mkdir(exist_ok=True)
                    target = backups / ('defects_before_v2_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.sqlite3')
                    with sqlite3.connect(target) as dest:
                        source.backup(dest)
        except sqlite3.DatabaseError:
            app.logger.exception('舊版資料庫備份失敗，已停止更新以免破壞資料')
            raise
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS defects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT UNIQUE NOT NULL, board_no INTEGER,
            found_date TEXT, floor TEXT, location TEXT, item TEXT,
            description TEXT, contractor TEXT,
            status TEXT NOT NULL DEFAULT '待改善',
            initial_photo TEXT NOT NULL, improvement_photo TEXT,
            improved_date TEXT, verified_date TEXT, notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            serial_no INTEGER UNIQUE, deleted_at TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_defect_board ON defects(board_no);
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo TEXT NOT NULL, original_filename TEXT,
            photo_type TEXT, board_no INTEGER,
            ai_result TEXT, status TEXT NOT NULL,
            matched_defect_id INTEGER REFERENCES defects(id),
            reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            photo_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_id INTEGER NOT NULL REFERENCES defects(id),
            photo TEXT NOT NULL, improved_date TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source_submission INTEGER REFERENCES submissions(id),
            voided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
            before_json TEXT, after_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        # v1 資料庫原地升級：不移除照片、舊編號、改善歷程與 API 金鑰。
        for tab, cols in {
            'defects': [('serial_no','INTEGER'),('deleted_at','TEXT'),('updated_at','TEXT'),('created_by','TEXT')],
            'submissions': [('photo_sha256','TEXT'),('uploaded_by','TEXT')],
            'audit_log': [('actor','TEXT'),('ip','TEXT')],
            'improvements': [('voided_at','TEXT')],
        }.items():
            present = {r['name'] for r in db.execute(f'PRAGMA table_info({tab})')}
            for column, coltype in cols:
                if column not in present:
                    db.execute(f'ALTER TABLE {tab} ADD COLUMN {column} {coltype}')
        # 依既有資料建檔順序（id）分配不可重用之流水號。
        serial = db.execute('SELECT COALESCE(MAX(serial_no),0) n FROM defects').fetchone()['n']
        for row in db.execute('SELECT id FROM defects WHERE serial_no IS NULL ORDER BY id').fetchall():
            serial += 1
            db.execute('UPDATE defects SET serial_no=? WHERE id=?',(serial,row['id']))
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_defect_serial ON defects(serial_no)')
        existing = db.execute('SELECT COUNT(*) n FROM defects').fetchone()['n']
        # Only seed a new installation once; an intentionally cleared register stays empty.
        db.execute('CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        seeded = db.execute("SELECT 1 FROM app_metadata WHERE key='seed_initialized'").fetchone()
        if not existing and not seeded:
            seeds = json.loads((BASE / 'seed.json').read_text(encoding='utf-8'))
            for i, r in enumerate(seeds, 1):
                shutil.copy2(BASE / 'seed_images' / r['initial_photo'], IMAGES / r['initial_photo'])
                db.execute('''INSERT INTO defects(public_id,board_no,found_date,floor,location,item,description,contractor,status,initial_photo,notes,serial_no)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (r['public_id'], r['board_no'], r['date'], r['floor'], r['location'], r['item'], r['description'], r['contractor'], r['status'], r['initial_photo'], r['notes'], i))
        db.execute("INSERT OR IGNORE INTO app_metadata(key,value) VALUES ('seed_initialized','1')")


def json_defect(row):
    d = dict(row)
    d['display_id'] = display_id(d['serial_no'])
    d['initial_url'] = '/images/' + d['initial_photo']
    d['improvement_url'] = '/images/' + d['improvement_photo'] if d['improvement_photo'] else None
    return d


def read_json(text):
    text = (text or '').strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        beg, end = text.find('{'), text.rfind('}')
        if beg >= 0 and end > beg:
            return json.loads(text[beg:end+1])
        raise ValueError('辨識服務未回傳正確 JSON')


def roc_date(raw):
    """只轉換有效的 YYYY-MM-DD 或 民國115/11/4。"""
    s = str(raw or '').strip()
    x = re.search(r'(?<!\d)(\d{3,4})[\-/年.](\d{1,2})[\-/月.](\d{1,2})', s)
    if not x:
        return ''
    yr, month, day = map(int, x.groups())
    if yr < 1911:
        yr += 1911
    try:
        return date(yr, month, day).isoformat()
    except ValueError:
        return ''


def analyze_image(data: bytes):
    """利用 OpenAI vision 判讀白板；沒有 API key 時絕不假裝自動判讀。"""
    from openai import OpenAI
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)  # 手機照片依 EXIF 轉正，避免模型看到橫躺白板
    image = image.convert('RGB')
    image.thumbnail((1800, 1800))
    buf = io.BytesIO()
    image.save(buf, 'JPEG', quality=86)
    uri = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    prompt = '''你是台灣營造工程的施工缺失白板紀錄辨識助手。請逐字閱讀白板。
只輸出 JSON 物件，不要 Markdown，不要多餘文字，所有鍵都必須存在：
{"photo_type":"defect|improvement|unknown","has_after_label":false,"board_no":null,"full_id":"","found_date":"","floor":"","location":"","item":"","description":"","contractor":"","confidence":0.0,"uncertain_fields":[],"reason":""}
規則：
1. 白板上有明確「改善後」「改善完成」等語句，才能 photo_type=improvement 且 has_after_label=true；切勿因為工程看起來完成就判斷為改善後。
2. 新缺失不要求白板編號；只有改善後照片必須有「缺失編號」。目前系統使用 RT-001、RT-002、RT-033 等流水編號，full_id 請完整填 RT-033。為相容舊照片，若白板是 DEF-000033 或 MEP-20261104-021，也請照實填在 full_id。如只有「缺失 21」，board_no 填整數 21；不清楚填 null。
3. 白板記載未完成的缺失且沒有「改善後」時可判為 defect；新缺失即使未寫編號也可以判為 defect。如不確定一律 unknown。
4. found_date 盡量用 YYYY-MM-DD；民國115年是西元2026年。檢查日期不等於檔案上傳日期。
5. floor 僅填白板上的 R1/6F/3F 等。不要假設白板沒有的空間編號或權責廠商。
6. description 只擷取可辨識的手寫內容；模糊字不要猜，uncertain_fields 列出。
7. confidence 為 0～1 的文字與編號辨識信心，不是工程施工品質評分。'''
    result = OpenAI(timeout=50.0).responses.create(
        model=os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'),
        input=[{'role':'user', 'content':[
            {'type':'input_text','text':prompt},
            {'type':'input_image','image_url':uri},
        ]}],
    )
    obj = read_json(result.output_text)
    obj['photo_type'] = obj.get('photo_type') if obj.get('photo_type') in ('defect','improvement','unknown') else 'unknown'
    obj['board_no'] = int(obj['board_no']) if str(obj.get('board_no') or '').isdigit() else None
    obj['has_after_label'] = obj.get('has_after_label') is True
    raw_id = str(obj.get('full_id') or '').strip().upper()
    obj['full_id'] = raw_id if re.fullmatch(r'(?:RT-\d{1,9}|DEF-\d{1,9}|MEP-\d{8}-\d{3}(?:-\d+)?)', raw_id) else ''
    obj['id_conflict'] = False
    if obj['full_id']:
        id_no = (int(obj['full_id'].split('-')[1])
                 if obj['full_id'].startswith(('DEF-', 'RT-'))
                 else int(re.match(r'MEP-\d{8}-(\d{3})',obj['full_id']).group(1)))
        if obj['board_no'] is None:
            obj['board_no'] = id_no
        elif obj['board_no'] != id_no:
            obj['id_conflict'] = True  # 完整編號與白板編號矛盾，代表至少一個讀錯
    try:
        obj['confidence'] = max(0., min(1., float(obj.get('confidence') or 0)))
    except (ValueError, TypeError):
        obj['confidence'] = 0.
    obj['found_date'] = roc_date(obj.get('found_date'))
    for k in ('floor','location','item','description','contractor','reason'):
        obj[k] = str(obj.get(k) or '').strip()[:500]
    return obj


def current_name():
    return getattr(g, 'user', None) if has_request_context() else None


def audit(db, action, entity_type, entity_id, before=None, after=None, actor=None):
    '操作者與 IP 自動取自目前請求（g.user、request.remote_addr）；請求外（測試、啟動）則為空。'
    ip = request.remote_addr if has_request_context() else None
    actor = actor or current_name()
    def encode(value):
        return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None
    db.execute('INSERT INTO audit_log(action,entity_type,entity_id,before_json,after_json,actor,ip) VALUES (?,?,?,?,?,?,?)',
               (action, entity_type, entity_id, encode(before), encode(after), actor, ip))


def add_defect(db, meta, image_file, created_by=None):
    '新缺失不需白板編號；依正式歸檔的順序分配全域不重用流水號。'
    day = roc_date(meta.get('found_date')) or date.today().isoformat()
    serial = db.execute('SELECT COALESCE(MAX(serial_no),0)+1 AS n FROM defects').fetchone()['n']
    public_id = display_id(serial)
    board_no = meta.get('board_no')
    board_no = int(board_no) if str(board_no or '').isdigit() else None
    cur = db.execute('''INSERT INTO defects(public_id,serial_no,board_no,found_date,floor,location,item,description,contractor,status,initial_photo,notes,created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (public_id,serial,board_no,day,meta.get('floor',''),meta.get('location',''),
             meta.get('item','聯合巡檢'),meta.get('description',''),meta.get('contractor',''),
             '待改善',image_file,'AI辨識初稿，請現場確認',created_by or current_name()))
    audit(db,'created','defect',cur.lastrowid,None,{'display_id':public_id,'photo':image_file})
    return cur.lastrowid


def refresh_improvement(db, did):
    '歸檔照片重新分類／撤回後，回算該筆最後有效的改善照片。'
    row = db.execute('SELECT * FROM defects WHERE id=?',(did,)).fetchone()
    if row is None:
        return
    recent = db.execute('''SELECT photo, improved_date FROM improvements
                           WHERE defect_id=? AND voided_at IS NULL ORDER BY id DESC LIMIT 1''',(did,)).fetchone()
    if recent:
        newstatus = '已改善' if row['status'] in ('待改善','已改善','改善中','待複驗','退回改善') else row['status']
        db.execute('UPDATE defects SET improvement_photo=?, improved_date=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (recent['photo'],recent['improved_date'],newstatus,did))
    else:
        newstatus = '待改善' if row['status'] in ('已改善','待複驗') else row['status']
        db.execute('UPDATE defects SET improvement_photo=NULL, improved_date=NULL, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (newstatus,did))

def add_improvement(db, defect_id, photo, day, submission_id):
    row = db.execute('SELECT * FROM defects WHERE id=? AND deleted_at IS NULL',(defect_id,)).fetchone()
    if row is None:
        raise ValueError('找不到指定缺失編號')
    db.execute('INSERT INTO improvements(defect_id,photo,improved_date,source_submission) VALUES (?,?,?,?)', (defect_id,photo,day,submission_id))
    refresh_improvement(db,defect_id)
    audit(db,'improvement','defect',defect_id,None,{'photo':photo,'day':day,'submission_id':submission_id})


SIMILAR_RATIO = 0.8


def _norm_text(s):
    # 去除空白與標點，只比對文字本身
    return re.sub(r'[\s\W_]+', '', str(s or '')).lower()


def find_similar_defect(db, day, floor, description):
    '同日期、同樓層（任一方未填則不限）且缺失內容相似度 ≥ SIMILAR_RATIO 的既有缺失；用於無白板編號時的防重複。'
    target = _norm_text(description)
    if not target:
        return None
    floor = _norm_text(floor)
    rows = db.execute('SELECT * FROM defects WHERE found_date=? AND deleted_at IS NULL', (day,)).fetchall()
    for row in rows:
        row_floor = _norm_text(row['floor'])
        if floor and row_floor and floor != row_floor:
            continue
        if difflib.SequenceMatcher(None, target, _norm_text(row['description'])).ratio() >= SIMILAR_RATIO:
            return row
    return None


def decide(db, meta, file_name, submission_id):
    typ = meta.get('photo_type','unknown')
    num = meta.get('board_no')
    confidence = float(meta.get('confidence') or 0)
    day = meta.get('found_date') or ''
    if confidence < 0.80:
        return ('pending',None,'文字辨識信心不足，請人工核對')
    if meta.get('id_conflict'):
        return ('pending', None, f'完整編號 {meta.get("full_id")} 與白板編號 {num} 不一致，請人工核對')
    if typ == 'improvement':
        if not meta.get('has_after_label'):
            return ('pending',None,'白板沒有明確「改善後」字樣，禁止自動配對')
        if meta.get('full_id'):
            fid = meta['full_id']
            if fid.startswith(('RT-','DEF-')):
                serial = int(fid.split('-')[1])
                found = db.execute('SELECT * FROM defects WHERE serial_no=? AND deleted_at IS NULL',(serial,)).fetchall()
            else:
                found = db.execute('SELECT * FROM defects WHERE public_id=? AND deleted_at IS NULL',(fid,)).fetchall()
        elif num is not None:
            # 只有「缺失 3」這種裸編號時，只比對白板編號，不可當成 RT-003：
            # 每次巡檢白板編號都從 1 起算，RT-001～RT-0xx 一定存在，優先比流水號會靜默配錯。
            # 有多筆同號時 len(found)!=1 → 人工確認。
            found = db.execute('SELECT * FROM defects WHERE board_no=? AND deleted_at IS NULL',(num,)).fetchall()
        else:
            return ('pending',None,'改善後照片無法辨識缺失編號')
        if len(found) != 1:
            return ('pending',None,f'無法唯一配對缺失編號：{len(found)} 筆符合，請人工確認')
        if found[0]['status'] == '已結案':
            return ('pending',None,f'{display_id(found[0]["serial_no"])} 已結案，改善照片不自動歸檔，請人工確認')
        did = found[0]['id']
        add_improvement(db,did,file_name,day or date.today().isoformat(),submission_id)
        return ('matched',did,'已配對至 '+display_id(found[0]['serial_no'])+'，狀態更新為「已改善」（仍須複驗）')
    if typ == 'defect':
        if not day:
            return ('pending',None,'缺失日期未辨識，請人工確認')
        if not meta.get('description'):
            return ('pending',None,'缺失內容不清楚，請人工確認')
        # 白板編號不是必要條件，但相同日期同編號仍先檢查可能重複。
        if num is not None:
            existing = db.execute('SELECT id FROM defects WHERE board_no=? AND found_date=? AND deleted_at IS NULL',(num,day)).fetchall()
            if existing:
                return ('pending',None,'同日期同白板編號已存在，請先確認是否為重複照片')
        similar = find_similar_defect(db, day, meta.get('floor',''), meta['description'])
        if similar:
            return ('pending',None,f'與 {display_id(similar["serial_no"])}（同日期、同樓層、缺失內容相近）可能重複，請人工確認')
        did = add_defect(db,meta,file_name)
        return ('created',did,'系統已按建檔順序自動分配 '+display_id(db.execute('SELECT serial_no FROM defects WHERE id=?',(did,)).fetchone()['serial_no']))
    return ('pending',None,'照片無法確定為新缺失或改善回覆')


# ---- 登入（選名字，無密碼）與權限 --------------------------------------------------
# 注意：沒有密碼，任何人都能選別人的名字；這是為了「留下紀錄、可追溯」，不是資安防護。IP 一併記錄。
def admin_password():
    return os.getenv('ADMIN_PASSWORD') or ''


def admin_enabled():
    return bool(admin_password())


def self_register_enabled():
    '登入頁「名單中沒有我」自行新增名字；預設開啟，.env 設 ALLOW_SELF_REGISTER=0 則只能由管理員新增。'
    return (os.getenv('ALLOW_SELF_REGISTER') or '1').strip().lower() not in ('0', 'false', 'no', 'off')


def clean_name(raw):
    return re.sub(r'\s+', ' ', str(raw or '')).strip()[:30]


PUBLIC_ENDPOINTS = {'login_page', 'api_login', 'api_logout', 'static'}


@app.before_request
def require_login():
    g.user, g.admin = None, False
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    name = session.get('user')
    if name:
        with connect() as db:
            row = db.execute('SELECT name FROM users WHERE name=? AND active=1', (name,)).fetchone()
        if row:  # 被停用或不存在的名字等於未登入
            g.user = row['name']
            g.admin = bool(session.get('admin')) and admin_enabled()
    if not g.user:
        if request.path == '/':
            return redirect('/login')
        return jsonify({'error': '請先登入', 'login_required': True}), 401
    if request.path.startswith('/api/admin/') and not g.admin:
        return jsonify({'error': '需要管理員權限'}), 403
    return None


@app.get('/login')
def login_page():
    with connect() as db:
        names = [r['name'] for r in db.execute('SELECT name FROM users WHERE active=1 ORDER BY name')]
        empty = db.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0
    return render_template('login.html', names=names, bootstrap=empty, admin_enabled=admin_enabled(),
                           self_register=self_register_enabled(), current=session.get('user', ''))


@app.post('/api/login')
def api_login():
    data = request.get_json(silent=True) or {}
    name = clean_name(data.get('name'))
    password = str(data.get('admin_password') or '')
    if not name:
        return jsonify({'error': '請選擇或輸入名字'}), 400
    g.user = None
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0:
            uid = db.execute('INSERT INTO users(name,is_admin) VALUES (?,1)', (name,)).lastrowid  # 第一位使用者
            audit(db, 'user_add', 'user', uid, None, {'name': name, 'bootstrap': True}, actor=name)
        elif data.get('new') and self_register_enabled():
            # 自行新增：同名（含已停用）就不重複建立；已存在且啟用則直接以該名字登入
            existing = db.execute('SELECT id FROM users WHERE name=?', (name,)).fetchone()
            if existing is None:
                uid = db.execute('INSERT INTO users(name) VALUES (?)', (name,)).lastrowid
                audit(db, 'user_self_register', 'user', uid, None, {'name': name}, actor=name)
        row = db.execute('SELECT * FROM users WHERE name=? AND active=1', (name,)).fetchone()
        if row is None:
            if db.execute('SELECT 1 FROM users WHERE name=?', (name,)).fetchone():
                return jsonify({'error': '這個名字已被管理員停用，請洽管理員'}), 403
            hint = '請選「名單中沒有我」新增，或洽管理員' if self_register_enabled() else '請洽管理員新增'
            return jsonify({'error': '名單中沒有這個名字，' + hint}), 403
        want_admin = bool(password)
        if want_admin:
            if not admin_enabled():
                return jsonify({'error': '尚未啟用管理員功能（.env 未設定 ADMIN_PASSWORD）'}), 400
            if not hmac.compare_digest(password.encode('utf-8'), admin_password().encode('utf-8')):
                audit(db, 'login_failed', 'user', row['id'], None, {'admin': True}, actor=name)
                return jsonify({'error': '管理員密碼不正確'}), 403
        session.clear()
        session.permanent = True
        session['user'] = row['name']
        session['admin'] = want_admin
        g.user = row['name']
        audit(db, 'login', 'user', row['id'], None, {'admin': want_admin})
    return jsonify({'ok': True, 'name': row['name'], 'admin': want_admin})


@app.post('/api/logout')
def api_logout():
    name = session.get('user')
    if name:
        with connect() as db:
            row = db.execute('SELECT id FROM users WHERE name=?', (name,)).fetchone()
            if row:
                audit(db, 'logout', 'user', row['id'], actor=name)
    session.clear()
    return jsonify({'ok': True})


@app.get('/api/admin/users')
def admin_users():
    with connect() as db:
        rows = db.execute('SELECT * FROM users ORDER BY active DESC, name').fetchall()
    return jsonify({'items': [dict(r) for r in rows]})


@app.post('/api/admin/users')
def admin_add_user():
    name = clean_name((request.get_json(silent=True) or {}).get('name'))
    if not name:
        return jsonify({'error': '請輸入名字'}), 400
    with connect() as db:
        try:
            cur = db.execute('INSERT INTO users(name) VALUES (?)', (name,))
        except sqlite3.IntegrityError:
            return jsonify({'error': '這個名字已存在（若已停用請改按「啟用」）'}), 409
        audit(db, 'user_add', 'user', cur.lastrowid, None, {'name': name})
    return jsonify({'ok': True})


@app.post('/api/admin/users/<int:uid>/active')
def admin_set_user_active(uid):
    active = 1 if (request.get_json(silent=True) or {}).get('active') else 0
    with connect() as db:
        row = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if row is None:
            return jsonify({'error': '找不到使用者'}), 404
        if not active and row['name'] == g.user:
            return jsonify({'error': '不能停用目前登入的自己'}), 400
        db.execute('UPDATE users SET active=? WHERE id=?', (active, uid))
        audit(db, 'user_activate' if active else 'user_deactivate', 'user', uid, {'active': row['active']}, {'active': active, 'name': row['name']})
    return jsonify({'ok': True})

@app.get('/')
def home():
    return render_template('index.html')

@app.get('/api/config')
def config():
    return jsonify({'ai_enabled': bool(os.getenv('OPENAI_API_KEY')), 'model': os.getenv('OPENAI_MODEL','gpt-4.1-mini'), 'share_urls': share_urls(),
                    'user': g.user, 'admin': g.admin, 'admin_enabled': admin_enabled()})

@app.get('/api/defects')
def list_defects():
    with connect() as db:
        rows = db.execute('SELECT * FROM defects WHERE deleted_at IS NULL ORDER BY serial_no DESC').fetchall()
        pending = db.execute("SELECT COUNT(*) n FROM submissions WHERE status='pending'").fetchone()['n']
    items = [json_defect(row) for row in rows]
    return jsonify({'items':items,'summary':{'total':len(items),'improved':sum(x['status'] in ('已改善','待複驗','已結案') for x in items),'closed':sum(x['status']=='已結案' for x in items),'pending':pending}})

@app.get('/api/review')
def review_queue():
    with connect() as db:
        rows = db.execute("SELECT * FROM submissions WHERE status='pending' ORDER BY id DESC").fetchall()
    out = []
    for row in rows:
        obj = dict(row)
        obj['preview_url'] = '/images/' + obj['photo']
        try:
            obj['ai_result'] = json.loads(obj['ai_result'] or '{}')
        except json.JSONDecodeError:
            obj['ai_result'] = {}
        out.append(obj)
    return jsonify({'items':out})

@app.get('/images/<path:name>')
def images(name):
    return send_from_directory(IMAGES,name)

@app.post('/api/upload')
def upload():
    photos = request.files.getlist('photos')
    if not photos:
        return jsonify({'error':'請先選擇照片'}),400
    if len(photos) > 20:
        return jsonify({'error':'一次最多上傳 20 張照片，請分批'}),400
    # 1) 依序驗證並保存原圖
    jobs = []  # 每張：{'filename', 'content', 'name', 'sha256', 'dup'} 或 {'filename', 'error'}
    batch_hashes = {}  # sha256 -> 本批次第一次出現的檔名
    for photo in photos:
        try:
            content = photo.read()
            if not content or len(content) > 12*1024*1024:
                raise ValueError('單張照片必須小於 12 MB')
            with Image.open(io.BytesIO(content)) as im:
                fmt = im.format
                im.verify()
            ext = {'JPEG':'.jpg','PNG':'.png','WEBP':'.webp'}.get(fmt)
            if not ext:
                raise ValueError('只接受 JPG、PNG、WEBP 圖片')
            name = uuid.uuid4().hex + ext
            (IMAGES/name).write_bytes(content)  # 原圖保存，不進行修圖
            hashval = hashlib.sha256(content).hexdigest()
            # 先查重複再呼叫 AI，重複照片不浪費 API 費用
            if hashval in batch_hashes:
                dup = f'同一批次已有相同照片（{batch_hashes[hashval]}），請人工確認避免重複建檔'
            else:
                with connect() as db:
                    prev = db.execute("SELECT id FROM submissions WHERE photo_sha256=? AND status IN ('created','matched','pending') LIMIT 1",(hashval,)).fetchone()
                dup = f'相同照片已經上傳過（紀錄 {prev["id"]}），請人工確認避免重複建檔' if prev else ''
                batch_hashes[hashval] = photo.filename
            jobs.append({'filename':photo.filename,'content':content,'name':name,'sha256':hashval,'dup':dup})
        except (UnidentifiedImageError,OSError,ValueError) as exc:
            jobs.append({'filename':photo.filename,'error':str(exc)})

    # 2) 平行呼叫 AI（網路等待為主，執行緒即可）；單張失敗不影響其他張
    def run_ai(job):
        if 'error' in job:
            return {}, ''
        if job['dup']:
            return {}, job['dup']
        if not os.getenv('OPENAI_API_KEY'):
            return {}, '尚未設定 OPENAI_API_KEY，請人工核對這張照片；AI 並未執行'
        try:
            return analyze_image(job['content']), ''
        except Exception as exc:
            app.logger.warning('AI 分析失敗（%s）：%s: %s', job['filename'], type(exc).__name__, str(exc)[:300])
            return {}, 'AI 分析暫時失敗，照片已保存，請人工核對'
    with ThreadPoolExecutor(max_workers=4) as pool:
        ai_results = list(pool.map(run_ai, jobs))

    # 3) 依上傳順序寫入資料庫，讓同批次的重複編號檢查仍然有效
    feedback = []
    for job, (meta, reason) in zip(jobs, ai_results):
        photo_name = job['filename']
        if 'error' in job:
            feedback.append({'filename':photo_name,'status':'error','reason':job['error']})
            continue
        name = job['name']
        try:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                hashval = job['sha256']
                # 第 1 步已查過；交易內再查一次，防止同時有另一個上傳請求
                duplicate = db.execute("SELECT id FROM submissions WHERE photo_sha256=? AND status IN ('created','matched','pending') LIMIT 1",(hashval,)).fetchone()
                if duplicate and not reason:
                    reason = '相同照片已經上傳過（紀錄 '+str(duplicate['id'])+'），請人工確認避免重複建檔'
                cur = db.execute('INSERT INTO submissions(photo,original_filename,photo_type,board_no,ai_result,status,reason,photo_sha256,uploaded_by) VALUES (?,?,?,?,?,?,?,?,?)',
                    (name, photo_name, meta.get('photo_type','unknown'),meta.get('board_no'),json.dumps(meta,ensure_ascii=False),'pending',reason,hashval,current_name()))
                sid = cur.lastrowid
                if not reason:
                    outcome, did, reason = decide(db,meta,name,sid)
                    db.execute('UPDATE submissions SET status=?,matched_defect_id=?,reason=? WHERE id=?', (outcome,did,reason,sid))
                else:
                    outcome = 'pending'
                    db.execute('UPDATE submissions SET reason=? WHERE id=?',(reason,sid))
            feedback.append({'filename':photo_name,'status':outcome,'board_no':meta.get('board_no'), 'photo_type':meta.get('photo_type','unknown'),'reason':reason})
        except Exception as exc:
            # 交易已回滾；照片檔仍保存在 data/images，可重新上傳
            app.logger.exception('寫入失敗（%s）', photo_name)
            feedback.append({'filename':photo_name,'status':'error','reason':f'儲存失敗（{type(exc).__name__}），此張未建檔，請重新上傳'})
    with connect() as db:
        audit(db,'upload','submission',0,None,{'files':len(photos),'results':[f['status'] for f in feedback]})
    return jsonify({'results':feedback})

@app.post('/api/review/<int:sid>/confirm')
def confirm(sid):
    data = request.get_json(silent=True) or {}
    typ = data.get('photo_type')
    if typ not in ('defect','improvement'):
        return jsonify({'error':'請選擇「新缺失」或「改善後」'}),400
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        sub = db.execute("SELECT * FROM submissions WHERE id=? AND status='pending'",(sid,)).fetchone()
        if sub is None:
            return jsonify({'error':'待確認資料不存在或已處理'}),404
        if typ == 'improvement':
            did = data.get('defect_id')
            try:
                did = int(did)
            except (ValueError,TypeError):
                return jsonify({'error':'請指定已有的缺失編號'}),400
            if not db.execute('SELECT 1 FROM defects WHERE id=? AND deleted_at IS NULL',(did,)).fetchone():
                return jsonify({'error':'該缺失編號不存在'}),404
            day = roc_date(data.get('found_date')) or date.today().isoformat()
            add_improvement(db,did,sub['photo'],day,sid)
            new_status = 'matched'
        else:
            meta = {key:str(data.get(key) or '').strip() for key in ('found_date','floor','location','item','description','contractor')}
            if not roc_date(meta.get('found_date')):
                return jsonify({'error':'請輸入有效日期，例如 2026-11-04'}),400
            meta['board_no'] = None  # 人工新增也由系統自動產生編號
            did = add_defect(db,meta,sub['photo'],sub['uploaded_by'])  # 上傳者沿用原上傳人；確認者記在稽核紀錄
            new_status = 'created'
        db.execute('UPDATE submissions SET status=?, matched_defect_id=?, reason=? WHERE id=?', (new_status,did,'人工確認歸檔',sid))
        audit(db,'confirmed','submission',sid,None,{'status':new_status,'defect_id':did})
    return jsonify({'ok':True})

@app.patch('/api/defects/<int:did>/status')
def change_status(did):
    payload = request.get_json(silent=True) or {}
    status = payload.get('status')
    if status not in VALID_STATUSES:
        return jsonify({'error':'改善狀態不合法'}),400
    with connect() as db:
        old = db.execute('SELECT id,status FROM defects WHERE id=? AND deleted_at IS NULL',(did,)).fetchone()
        if not old:
            return jsonify({'error':'缺失不存在'}),404
        db.execute("UPDATE defects SET status=?, verified_date=CASE WHEN ?='已結案' THEN ? ELSE verified_date END, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status,status,date.today().isoformat(),did))
        audit(db,'status','defect',did,{'status':old['status']},{'status':status})
    return jsonify({'ok':True})

@app.get('/api/admin/defects')
def admin_defects():
    with connect() as db:
        rows = db.execute('SELECT * FROM defects ORDER BY serial_no DESC').fetchall()
    return jsonify({'items':[json_defect(r) for r in rows]})


@app.patch('/api/admin/defects/<int:did>')
def admin_edit_defect(did):
    data = request.get_json(silent=True) or {}
    update = {k: str(data[k]).strip()[:1000] if data[k] is not None else None
              for k in EDIT_FIELDS if k in data}
    if not update:
        return jsonify({'error':'沒有可修改的欄位'}),400
    if 'status' in update and update['status'] not in VALID_STATUSES:
        return jsonify({'error':'狀態不正確'}),400
    for key in ('found_date','improved_date','verified_date'):
        if key in update and update[key]:
            dt = roc_date(update[key])
            if not dt: return jsonify({'error':key+' 日期格式錯誤'}),400
            update[key]=dt
    with connect() as db:
        old = db.execute('SELECT * FROM defects WHERE id=? AND deleted_at IS NULL',(did,)).fetchone()
        if old is None:
            return jsonify({'error':'找不到未刪除的紀錄'}),404
        db.execute('UPDATE defects SET '+', '.join(k+'=?' for k in update)+',updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (*update.values(),did))
        audit(db,'edit','defect',did,{k:old[k] for k in update},update)
    return jsonify({'ok':True})


@app.delete('/api/admin/defects/<int:did>')
def admin_remove_defect(did):
    with connect() as db:
        row = db.execute('SELECT * FROM defects WHERE id=? AND deleted_at IS NULL',(did,)).fetchone()
        if row is None:return jsonify({'error':'紀錄已移除或不存在'}),404
        db.execute('UPDATE defects SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?',(did,))
        audit(db,'remove','defect',did,{'status':row['status'],'display_id':display_id(row['serial_no'])},None)
    return jsonify({'ok':True,'message':'紀錄已從正式清冊移除；原始照片與歷程未刪除，可還原'})


@app.post('/api/admin/defects/<int:did>/restore')
def admin_restore_defect(did):
    with connect() as db:
        row=db.execute('SELECT * FROM defects WHERE id=? AND deleted_at IS NOT NULL',(did,)).fetchone()
        if row is None:return jsonify({'error':'找不到可還原的紀錄'}),404
        db.execute('UPDATE defects SET deleted_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?',(did,))
        audit(db,'restore','defect',did,None,{'display_id':display_id(row['serial_no'])})
    return jsonify({'ok':True})


@app.get('/api/admin/submissions')
def admin_submissions():
    with connect() as db:
        rows=db.execute('''SELECT s.*,d.serial_no,d.description AS defect_description,d.deleted_at AS defect_deleted
                           FROM submissions s LEFT JOIN defects d ON d.id=s.matched_defect_id
                           WHERE s.status IN ('created','matched') ORDER BY s.id DESC''').fetchall()
    out=[]
    for row in rows:
        obj=dict(row);obj['preview_url']='/images/'+obj['photo']
        obj['display_id'] = display_id(obj['serial_no']) if obj['serial_no'] else None
        out.append(obj)
    return jsonify({'items':out})


@app.post('/api/admin/submissions/<int:sid>/reopen')
def admin_reopen_submission(sid):
    '撤銷錯誤分類，送回人工確認：保留照片／追蹤紀錄，可再次分類。'
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        sub=db.execute("SELECT * FROM submissions WHERE id=? AND status IN ('created','matched')",(sid,)).fetchone()
        if sub is None:return jsonify({'error':'這張照片不是已歸檔紀錄'}),404
        did=sub['matched_defect_id']
        if sub['status']=='created':
            linked=db.execute('SELECT COUNT(*) n FROM improvements WHERE defect_id=? AND voided_at IS NULL',(did,)).fetchone()['n']
            if linked:
                return jsonify({'error':'此原始缺失已存在改善回覆，請先處理回覆後才能撤銷分類'}),409
            db.execute('UPDATE defects SET deleted_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?',(did,))
        else:
            current=db.execute('SELECT status FROM defects WHERE id=?',(did,)).fetchone()
            if current and current['status']=='已結案':
                return jsonify({'error':'此筆已結案。請先將原缺失狀態改為「待複驗」或「退回改善」，再撤銷照片分類'}),409
            db.execute('UPDATE improvements SET voided_at=CURRENT_TIMESTAMP WHERE source_submission=? AND voided_at IS NULL',(sid,))
            refresh_improvement(db,did)
        db.execute("UPDATE submissions SET status='pending',matched_defect_id=NULL,reason=? WHERE id=?",
                   ('管理員撤銷原分類，請重新人工確認',sid))
        audit(db,'reopen','submission',sid,{'status':sub['status'],'defect_id':did},{'status':'pending'})
    return jsonify({'ok':True,'message':'已移至人工確認區，可重新選擇新增缺失／改善回覆'})


@app.get('/api/admin/audit')
def admin_audit():
    with connect() as db:
        # created_at 以 UTC 儲存（SQLite CURRENT_TIMESTAMP）；顯示時轉成本機時間，避免台灣時間差 8 小時。
        # 欄位逐一列出：SELECT * 再加同名別名會出現兩個 created_at，取到的仍是 UTC。
        rows=db.execute("SELECT id,action,entity_type,entity_id,before_json,after_json,actor,ip,"
                        "datetime(created_at,'localtime') AS created_at FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({'items':[dict(r) for r in rows]})


@app.get('/api/export.xlsx')
def export_xlsx():
    import xlsxwriter
    with connect() as db:
        rows = [dict(x) for x in db.execute('SELECT * FROM defects WHERE deleted_at IS NULL ORDER BY serial_no')]
    out=io.BytesIO()
    book=xlsxwriter.Workbook(out,{'in_memory':True})
    ws=book.add_worksheet('缺失清冊')
    deep=book.add_format({'bold':True,'font_color':'#FFFFFF','bg_color':'#18324B','align':'center','valign':'vcenter','border':1,'border_color':'#DCE4EC'})
    normal=book.add_format({'text_wrap':True,'valign':'vcenter','border':1,'border_color':'#E1E7EE'})
    date_fmt=book.add_format({'num_format':'yyyy-mm-dd','valign':'vcenter','border':1,'border_color':'#E1E7EE'})
    ws.merge_range('A1:P1','施工缺失管理清冊｜照片與改善回覆', book.add_format({'font_size':18,'bold':True,'font_color':'#18324B'}))
    headers=['系統自動編號','發現日期','樓層','空間編號','檢查項目','缺失內容','權責廠商','改善狀態','原始缺失照片','改善後照片','改善日期','複驗日期','備註','原始圖檔','改善圖檔','上傳者']
    for c,h in enumerate(headers): ws.write(2,c,h,deep)
    ws.set_row(2,32)
    widths=[22,15,12,15,16,38,16,15,37,37,15,15,33,28,28,14]
    for c,width in enumerate(widths): ws.set_column(c,c,width)
    ws.freeze_panes(3,0)
    for i,r in enumerate(rows,3):
        ws.set_row(i,156)
        vals=[display_id(r['serial_no']),r['found_date'],r['floor'],r['location'],r['item'],r['description'],r['contractor'],r['status'],'','',r['improved_date'],r['verified_date'],r['notes'],r['initial_photo'],r['improvement_photo'],r['created_by']]
        for c,value in enumerate(vals): ws.write(i,c,value or '',normal)
        for col,key in ((8,'initial_photo'),(9,'improvement_photo')):
            name=r.get(key)
            if name and (IMAGES/name).exists():
                with Image.open(IMAGES/name) as im:
                    width,height=im.size
                scale=min(270/width,190/height)
                ws.insert_image(i,col,str(IMAGES/name),{'x_scale':scale,'y_scale':scale,'x_offset':6,'y_offset':6,'object_position':1})
    ws.autofilter(2,0,2+len(rows),len(headers)-1)
    ws=book.add_worksheet('改善回覆紀錄')
    heads=['缺失編號','改善日期','回覆照片檔名','回覆照片']
    for c,v in enumerate(heads): ws.write(0,c,v,deep)
    ws.set_column('A:A',24);ws.set_column('B:B',17);ws.set_column('C:C',38);ws.set_column('D:D',40)
    with connect() as db:
        rr=db.execute('SELECT d.serial_no,i.photo,i.improved_date FROM improvements i JOIN defects d ON i.defect_id=d.id WHERE i.voided_at IS NULL AND d.deleted_at IS NULL ORDER BY i.id').fetchall()
    for i,r in enumerate(rr,1):
        ws.set_row(i,155)
        ws.write(i,0,display_id(r['serial_no']),normal);ws.write(i,1,r['improved_date'] or '',normal);ws.write(i,2,r['photo'],normal)
        if (IMAGES/r['photo']).exists():
            with Image.open(IMAGES/r['photo']) as im: w,h=im.size
            sc=min(285/w,195/h)
            ws.insert_image(i,3,str(IMAGES/r['photo']),{'x_scale':sc,'y_scale':sc,'x_offset':5,'y_offset':5,'object_position':1})
    book.close()
    out.seek(0)
    return send_file(out,mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',as_attachment=True,download_name=f'施工缺失清冊_{date.today().isoformat()}.xlsx')


@app.errorhandler(RequestEntityTooLarge)
def too_large(err):
    return jsonify({'error':'上傳檔案太大；一次最多 65 MB、單張最多 12 MB'}),413


init_db()

if __name__ == '__main__':
    from waitress import serve
    host, port = server_host(), server_port()
    print('施工缺失管理網頁已啟動，可使用以下網址：', flush=True)
    for u in share_urls():
        print('  ' + u, flush=True)
    if host not in ('0.0.0.0', ''):
        print('（目前只限本機使用；要讓同網路的人連線，請在 .env 設定 HOST=0.0.0.0）', flush=True)
    serve(app, host=host, port=port, threads=8)
