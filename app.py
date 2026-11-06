"""本機用：施工缺失／改善照片辨識與歸檔。上線前需加入身分驗證、權限、HTTPS 與備份。"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory, render_template
from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import RequestEntityTooLarge

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DATA = BASE / 'data'
IMAGES = DATA / 'images'
DATABASE = DATA / 'defects.sqlite3'
IMAGES.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 65 * 1024 * 1024
VALID_STATUSES = ['待改善', '改善中', '已改善', '待複驗', '退回改善', '已結案']


def connect():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


def init_db():
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
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_defect_board ON defects(board_no);
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo TEXT NOT NULL, original_filename TEXT,
            photo_type TEXT, board_no INTEGER,
            ai_result TEXT, status TEXT NOT NULL,
            matched_defect_id INTEGER REFERENCES defects(id),
            reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_id INTEGER NOT NULL REFERENCES defects(id),
            photo TEXT NOT NULL, improved_date TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            source_submission INTEGER REFERENCES submissions(id)
        );
        ''')
        existing = db.execute('SELECT COUNT(*) n FROM defects').fetchone()['n']
        if not existing:
            seeds = json.loads((BASE / 'seed.json').read_text(encoding='utf-8'))
            for r in seeds:
                shutil.copy2(BASE / 'seed_images' / r['initial_photo'], IMAGES / r['initial_photo'])
                db.execute('''INSERT INTO defects(public_id,board_no,found_date,floor,location,item,description,contractor,status,initial_photo,notes)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                           (r['public_id'], r['board_no'], r['date'], r['floor'], r['location'], r['item'], r['description'], r['contractor'], r['status'], r['initial_photo'], r['notes']))


def json_defect(row):
    d = dict(row)
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
2. 白板有「缺失 21」「缺失編號 21」等，board_no 填整數 21；若有完整系統編號如 MEP-20261104-021，則 full_id 也填完整編號，board_no 填 21。看不清楚填 null 或空字串。
3. 白板寫「缺失」但未寫「改善後」時通常 photo_type=defect；如不確定一律 unknown。
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
    obj['full_id'] = raw_id if re.fullmatch(r'MEP-\d{8}-\d{3}(?:-\d+)?', raw_id) else ''
    if obj['board_no'] is None and obj['full_id']:
        obj['board_no'] = int(re.match(r'MEP-\d{8}-(\d{3})',obj['full_id']).group(1))
    try:
        obj['confidence'] = max(0., min(1., float(obj.get('confidence') or 0)))
    except (ValueError, TypeError):
        obj['confidence'] = 0.
    obj['found_date'] = roc_date(obj.get('found_date'))
    for k in ('floor','location','item','description','contractor','reason'):
        obj[k] = str(obj.get(k) or '').strip()[:500]
    return obj


def issue_id(db, day, board_no):
    day = (day or date.today().isoformat()).replace('-', '')
    root = f'MEP-{day}-{int(board_no):03d}'
    val = root
    suffix = 2
    while db.execute('SELECT 1 FROM defects WHERE public_id=?',(val,)).fetchone():
        val = f'{root}-{suffix}'
        suffix += 1
    return val


def add_defect(db, meta, image_file):
    day = roc_date(meta.get('found_date')) or date.today().isoformat()
    no = meta.get('board_no')
    if not no:
        raise ValueError('新缺失必須有白板缺失編號')
    did = issue_id(db, day, no)
    cur = db.execute('''INSERT INTO defects(public_id,board_no,found_date,floor,location,item,description,contractor,status,initial_photo,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (did, int(no), day, meta.get('floor',''), meta.get('location',''), meta.get('item','聯合巡檢'),meta.get('description',''),meta.get('contractor',''),'待改善',image_file,'AI辨識初稿，請現場確認'))
    return cur.lastrowid


def add_improvement(db, defect_id, photo, day, submission_id):
    row = db.execute('SELECT * FROM defects WHERE id=?',(defect_id,)).fetchone()
    if row is None:
        raise ValueError('找不到指定缺失編號')
    db.execute('INSERT INTO improvements(defect_id,photo,improved_date,source_submission) VALUES (?,?,?,?)', (defect_id,photo,day,submission_id))
    db.execute('UPDATE defects SET improvement_photo=?, improved_date=?, status=? WHERE id=?', (photo,day,'已改善',defect_id))


def decide(db, meta, file_name, submission_id):
    typ = meta.get('photo_type','unknown')
    num = meta.get('board_no')
    confidence = float(meta.get('confidence') or 0)
    day = meta.get('found_date') or ''
    if confidence < 0.8 or num is None:
        return ('pending', None, '白板編號不明確或辨識信心不足，請人工核對')
    if typ == 'improvement':
        if not meta.get('has_after_label'):
            return ('pending',None,'未能確定白板有寫「改善後」，禁止自動配對')
        if meta.get('full_id'):
            found = db.execute('SELECT * FROM defects WHERE public_id=?',(meta['full_id'],)).fetchall()
        else:
            found = db.execute('SELECT * FROM defects WHERE board_no=?',(num,)).fetchall()
        if len(found) != 1:
            # 改善日期不是缺失開立日期，因此不能以改善日期消除編號歧義
            return ('pending',None,f'缺失編號 {num} 有 {len(found)} 筆對應資料，請確認')
        did = found[0]['id']
        add_improvement(db,did,file_name,day or date.today().isoformat(),submission_id)
        return ('matched',did,'已依編號配對，狀態更新為「已改善」（仍須複驗）')
    if typ == 'defect':
        existing = db.execute('SELECT id FROM defects WHERE board_no=? AND found_date=?',(num, day)).fetchall() if day else []
        if existing:
            return ('pending',None,'同日期與白板編號已存在，避免重複建檔；如不同廠商請人工確認新增')
        if not day:
            return ('pending',None,'白板日期未辨識，請人工確認')
        did = add_defect(db,meta,file_name)
        return ('created',did,'已新增缺失')
    return ('pending',None,'照片無法辨別為新缺失或改善回覆')


@app.get('/')
def home():
    return render_template('index.html')

@app.get('/api/config')
def config():
    return jsonify({'ai_enabled': bool(os.getenv('OPENAI_API_KEY')), 'model': os.getenv('OPENAI_MODEL','gpt-4.1-mini')})

@app.get('/api/defects')
def list_defects():
    with connect() as db:
        rows = db.execute('SELECT * FROM defects ORDER BY found_date DESC, board_no, id').fetchall()
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
    feedback = []
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
            meta, reason = {}, ''
            if not os.getenv('OPENAI_API_KEY'):
                reason = '尚未設定 OPENAI_API_KEY，請人工核對這張照片；AI 並未執行'
            else:
                try:
                    meta = analyze_image(content)
                except Exception as exc:
                    app.logger.warning('AI 分析失敗：%s',type(exc).__name__)
                    reason = 'AI 分析暫時失敗，照片已保存，請人工核對'
            with connect() as db:
                cur = db.execute('INSERT INTO submissions(photo,original_filename,photo_type,board_no,ai_result,status,reason) VALUES (?,?,?,?,?,?,?)',
                    (name, photo.filename, meta.get('photo_type','unknown'),meta.get('board_no'),json.dumps(meta,ensure_ascii=False),'pending',reason))
                sid = cur.lastrowid
                if not reason:
                    outcome, did, reason = decide(db,meta,name,sid)
                    db.execute('UPDATE submissions SET status=?,matched_defect_id=?,reason=? WHERE id=?', (outcome,did,reason,sid))
                else:
                    outcome = 'pending'
                    db.execute('UPDATE submissions SET reason=? WHERE id=?',(reason,sid))
            feedback.append({'filename':photo.filename,'status':outcome,'board_no':meta.get('board_no'), 'photo_type':meta.get('photo_type','unknown'),'reason':reason})
        except (UnidentifiedImageError,OSError,ValueError) as exc:
            feedback.append({'filename':photo.filename,'status':'error','reason':str(exc)})
    return jsonify({'results':feedback})

@app.post('/api/review/<int:sid>/confirm')
def confirm(sid):
    data = request.get_json(silent=True) or {}
    typ = data.get('photo_type')
    if typ not in ('defect','improvement'):
        return jsonify({'error':'請選擇「新缺失」或「改善後」'}),400
    with connect() as db:
        sub = db.execute('SELECT * FROM submissions WHERE id=? AND status="pending"',(sid,)).fetchone()
        if sub is None:
            return jsonify({'error':'待確認資料不存在或已處理'}),404
        if typ == 'improvement':
            did = data.get('defect_id')
            try:
                did = int(did)
            except (ValueError,TypeError):
                return jsonify({'error':'請指定已有的缺失編號'}),400
            if not db.execute('SELECT 1 FROM defects WHERE id=?',(did,)).fetchone():
                return jsonify({'error':'該缺失編號不存在'}),404
            day = roc_date(data.get('found_date')) or date.today().isoformat()
            add_improvement(db,did,sub['photo'],day,sid)
            new_status = 'matched'
        else:
            meta = {key:str(data.get(key) or '').strip() for key in ('found_date','floor','location','item','description','contractor')}
            num = data.get('board_no')
            if not str(num).isdigit():
                return jsonify({'error':'請輸入正確的白板缺失編號'}),400
            if not roc_date(meta.get('found_date')):
                return jsonify({'error':'請輸入有效日期，例如 2026-11-04'}),400
            meta['board_no'] = int(num)
            did = add_defect(db,meta,sub['photo'])
            new_status = 'created'
        db.execute('UPDATE submissions SET status=?, matched_defect_id=?, reason=? WHERE id=?', (new_status,did,'人工確認歸檔',sid))
    return jsonify({'ok':True})

@app.patch('/api/defects/<int:did>/status')
def change_status(did):
    payload = request.get_json(silent=True) or {}
    status = payload.get('status')
    if status not in VALID_STATUSES:
        return jsonify({'error':'改善狀態不合法'}),400
    with connect() as db:
        old = db.execute('SELECT id FROM defects WHERE id=?',(did,)).fetchone()
        if not old:
            return jsonify({'error':'缺失不存在'}),404
        db.execute('UPDATE defects SET status=?, verified_date=CASE WHEN ?="已結案" THEN ? ELSE verified_date END WHERE id=?', (status,status,date.today().isoformat(),did))
    return jsonify({'ok':True})

@app.get('/api/export.xlsx')
def export_xlsx():
    import xlsxwriter
    with connect() as db:
        rows = [dict(x) for x in db.execute('SELECT * FROM defects ORDER BY found_date DESC, board_no, id')]
    out=io.BytesIO()
    book=xlsxwriter.Workbook(out,{'in_memory':True})
    ws=book.add_worksheet('缺失清冊')
    deep=book.add_format({'bold':True,'font_color':'#FFFFFF','bg_color':'#18324B','align':'center','valign':'vcenter','border':1,'border_color':'#DCE4EC'})
    normal=book.add_format({'text_wrap':True,'valign':'vcenter','border':1,'border_color':'#E1E7EE'})
    date_fmt=book.add_format({'num_format':'yyyy-mm-dd','valign':'vcenter','border':1,'border_color':'#E1E7EE'})
    ws.merge_range('A1:P1','施工缺失管理清冊｜照片與改善回覆', book.add_format({'font_size':18,'bold':True,'font_color':'#18324B'}))
    headers=['系統缺失編號','白板缺失編號','發現日期','樓層','空間編號','檢查項目','缺失內容','權責廠商','改善狀態','原始缺失照片','改善後照片','改善日期','複驗日期','備註','原始圖檔','改善圖檔']
    for c,h in enumerate(headers): ws.write(2,c,h,deep)
    ws.set_row(2,32)
    widths=[22,15,15,12,15,16,38,16,15,37,37,15,15,33,28,28]
    for c,width in enumerate(widths): ws.set_column(c,c,width)
    ws.freeze_panes(3,0)
    for i,r in enumerate(rows,3):
        ws.set_row(i,156)
        vals=[r['public_id'],r['board_no'],r['found_date'],r['floor'],r['location'],r['item'],r['description'],r['contractor'],r['status'],'','',r['improved_date'],r['verified_date'],r['notes'],r['initial_photo'],r['improvement_photo']]
        for c,value in enumerate(vals): ws.write(i,c,value or '',normal)
        for col,key in ((9,'initial_photo'),(10,'improvement_photo')):
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
        rr=db.execute('SELECT d.public_id,i.photo,i.improved_date FROM improvements i JOIN defects d ON i.defect_id=d.id ORDER BY i.id').fetchall()
    for i,r in enumerate(rr,1):
        ws.set_row(i,155)
        ws.write(i,0,r['public_id'],normal);ws.write(i,1,r['improved_date'] or '',normal);ws.write(i,2,r['photo'],normal)
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
    app.run(host='127.0.0.1',port=int(os.getenv('PORT','5000')),debug=False)
