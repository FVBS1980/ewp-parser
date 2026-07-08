from flask import Flask, request, jsonify
from flask_cors import CORS
import fitz
import re
import requests
import json
import time
import smtplib
import jwt
from email.mime.text import MIMEText
from collections import defaultdict

app = Flask(__name__)
CORS(app)

FOLDER_NAME = "EWP Jobs"

# OAuth refresh token for unattended Google Drive uploads - loaded from environment variables
import os
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")
OAUTH_REFRESH_TOKEN = os.environ.get("OAUTH_REFRESH_TOKEN", "")

_oauth_access_token = None
_oauth_token_expiry = 0

def get_oauth_token():
    global _oauth_access_token, _oauth_token_expiry
    if _oauth_access_token and time.time() < _oauth_token_expiry - 60:
        return _oauth_access_token
    resp = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': OAUTH_CLIENT_ID,
        'client_secret': OAUTH_CLIENT_SECRET,
        'refresh_token': OAUTH_REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    })
    data = resp.json()
    if 'access_token' not in data:
        raise Exception(f'Failed to refresh token: {data}')
    _oauth_access_token = data['access_token']
    _oauth_token_expiry = time.time() + data.get('expires_in', 3600)
    return _oauth_access_token

def make_file_public(file_id, token):
    requests.post(
        f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'role': 'reader', 'type': 'anyone'}
    )

EMAIL_ADDRESS = 'fvbsewphub@gmail.com'
EMAIL_APP_PASSWORD = os.environ.get('EMAIL_APP_PASSWORD', '')

def send_gmail(to_email, subject, html_body, from_name='FVBS EWP Hub'):
    msg = MIMEText(html_body, 'html')
    msg['to'] = to_email
    msg['from'] = f'{from_name} <{EMAIL_ADDRESS}>'
    msg['subject'] = subject
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, [to_email], msg.as_string())

def get_or_create_folder(parent_id, name, token):
    headers = {"Authorization": f"Bearer {token}"}
    # Check if exists
    resp = requests.get(
        f'https://www.googleapis.com/drive/v3/files?q="{parent_id}"+in+parents+and+name="{name}"+and+trashed=false+and+mimeType="application/vnd.google-apps.folder"&fields=files(id)',
        headers=headers
    )
    files = resp.json().get("files", [])
    if files:
        return files[0]["id"]
    # Create
    resp = requests.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    )
    return resp.json()["id"]

SKU_MAP = {
    # JOISTS
    '9-1/2" BCI 6000-1.8 DF': 'BCI60009',
    '11-7/8" BCI 6000-1.8 DF': 'BCI600011',
    '14" BCI 6000-1.8 DF': 'BCI600014',
    '16" BCI 6000-1.8 DF': 'BCI600016',
    '11-7/8" BCI 60-2.0 DF': 'BCI6011',
    '14" BCI 60-2.0 DF': 'BCI6014',
    '16" BCI 60-2.0 DF': 'BCI6016',
    '9-1/2" BCI 90-2.0 DF': 'BCI909',
    '11-7/8" BCI 90-2.0 DF': 'BCI9011',
    '14" BCI 90-2.0 DF': 'BCI9014',
    '16" BCI 90-2.0 DF': 'BCI9016',
    # POSTS
    '3-1/2" x 3-1/2" VERSA-LAM LVL 1.8E 2650 DF': 'VC33',
    '3-1/2" x 5-1/4" VERSA-LAM LVL 1.8E 2650 DF': 'VC35',
    '3-1/2" x 7" VERSA-LAM LVL 1.8E 2650 DF': 'VC37',
    '5-1/4" x 5-1/4" VERSA-LAM LVL 1.8E 2650 DF': 'VC55',
    '5-1/4" x 7" VERSA-LAM LVL 1.8E 2650 DF': 'VC57',
    '7" x 7" VERSA-LAM LVL 1.8E 2650 DF': 'VC77',
    # BEAMS
    '1-3/4" x 9-1/2" VERSA-LAM LVL 1.8E 2400 DF': 'VLSL19',
    '1-3/4" x 11-7/8" VERSA-LAM LVL 1.8E 2400 DF': 'VLSL111',
    '1-3/4" x 14" VERSA-LAM LVL 1.8E 2400 DF': 'VLSL114',
    '1-3/4" x 9-1/2" VERSA-LAM LVL 2.1E 2800 DF': 'VL195',
    '1-3/4" x 11-7/8" VERSA-LAM LVL 2.1E 2800 DF': 'VL111',
    '1-3/4" x 14" VERSA-LAM LVL 2.1E 2800 DF': 'VL114',
    '1-3/4" x 16" VERSA-LAM LVL 2.1E 2800 DF': 'VL116',
    '1-3/4" x 18" VERSA-LAM LVL 2.1E 2800 DF': 'VL118',
    '1-3/4" x 20" VERSA-LAM LVL 2.1E 2800 DF': 'VL120',
    '1-3/4" x 22" VERSA-LAM LVL 2.1E 2800 DF': 'VL122',
    '1-3/4" x 24" VERSA-LAM LVL 2.1E 2800 DF': 'VL124',
    '3-1/2" x 11-7/8" VERSA-LAM LVL 1.8E 3100 DF': 'VLSL311',
    '3-1/2" x 9-1/2" VERSA-LAM LVL 2.3E 3100 DF': 'VL395',
    '3-1/2" x 11-7/8" VERSA-LAM LVL 2.3E 3100 DF': 'VL311',
    '3-1/2" x 14" VERSA-LAM LVL 2.3E 3100 DF': 'VL314',
    '3-1/2" x 16" VERSA-LAM LVL 2.3E 3100 DF': 'VL316',
    '3-1/2" x 18" VERSA-LAM LVL 2.3E 3100 DF': 'VL318',
    '3-1/2" x 19" VERSA-LAM LVL 2.3E 3100 DF': 'VL319',
    '3-1/2" x 20" VERSA-LAM LVL 2.3E 3100 DF': 'VL320',
    '3-1/2" x 22" VERSA-LAM LVL 2.3E 3100 DF': 'VL322',
    '3-1/2" x 24" VERSA-LAM LVL 2.3E 3100 DF': 'VL324',
    '5-1/4" x 9-1/2" VERSA-LAM LVL 2.3E 3100 DF': 'VL59',
    '5-1/4" x 11-7/8" VERSA-LAM LVL 2.3E 3100 DF': 'VL511',
    '5-1/4" x 14" VERSA-LAM LVL 2.3E 3100 DF': 'VL514',
    '5-1/4" x 16" VERSA-LAM LVL 2.3E 3100 DF': 'VL516',
    '5-1/4" x 18" VERSA-LAM LVL 2.3E 3100 DF': 'VL518',
    '5-1/4" x 19" VERSA-LAM LVL 2.3E 3100 DF': 'VL519',
    '5-1/4" x 20" VERSA-LAM LVL 2.3E 3100 DF': 'VL520',
    '5-1/4" x 22" VERSA-LAM LVL 2.3E 3100 DF': 'VL522',
    '5-1/4" x 24" VERSA-LAM LVL 2.3E 3100 DF': 'VL524',
    '7" x 9-1/2" VERSA-LAM LVL 2.3E 3100 DF': 'VL795',
    '7" x 9-1/4" VERSA-LAM LVL 2.3E 3100 DF': 'VL795',
    '7" x 11-7/8" VERSA-LAM LVL 2.3E 3100 DF': 'VL711',
    '7" x 14" VERSA-LAM LVL 2.3E 3100 DF': 'VL714',
    '7" x 16" VERSA-LAM LVL 2.3E 3100 DF': 'VL716',
    '7" x 18" VERSA-LAM LVL 2.3E 3100 DF': 'VL718',
    '7" x 19" VERSA-LAM LVL 2.3E 3100 DF': 'VL719',
    '7" x 20" VERSA-LAM LVL 2.3E 3100 DF': 'VL720',
    '7" x 22" VERSA-LAM LVL 2.3E 3100 DF': 'VL722',
    '7" x 24" VERSA-LAM LVL 2.3E 3100 DF': 'VL724',
    # RIMBOARD
    '1-1/4" x 9-1/2" BC RIM BOARD PLUS OSB': '710016',
    '1-1/4" x 11-7/8" BC RIM BOARD PLUS OSB': '710056',
    '1-1/4" x 14" BC RIM BOARD PLUS OSB': '710076',
    '1-1/4" x 16" BC RIM BOARD PLUS OSB': '71003357',
    # HANGERS
    'IUS2.37/9.5': '8589072',
    'IUS2.37/11.88': '35945110',
    'IUS2.37/14': '8589078',
    'IUS3.56/9.5': 'IUS3.56/9.5',
    'IUS3.56/11.88': '35945123',
    'IUS3.56/14': '46305009',
    'HU3511': 'HU3511',
    'HUC410': '35945420',
    'HUC410-2': 'HUC410-2',
    'HUC414': 'HUC414',
    'HUC610': '35945222',
    'HGUS410': '8589042',
    'HGUS412': '8589043',
    'HGUS414': '8589107',
    'HUCQ610': '35945381',
    'HGUS5.50/10': '8589044',
    'HGUS5.50/12': '8589045',
    'HGUS7.25/10': '8589046',
    'HGUS7.25/12': '8589108',
    'HGUS7.25/14': '8589117',
    'HUS1.81/10': '8589041',
    # SHEATHING
    '3/4" 4x8 OSB (Floor Decking)': '2332OSB',
    '23/32" 4x8 OSB (Floor Decking)': '2332OSB',
}

def norm(s): return re.sub(r'[®™©]','',s).replace('  ',' ').strip()
def get_sku(p): return SKU_MAP.get(norm(p))
def is_rim(p): return 'BC RIM BOARD' in p
def is_joist(p): return 'BCI' in p and not is_rim(p)
def is_post(p): return 'VERSA-LAM' in p and '1.8E 2650' in p
def is_beam(p): return 'VERSA-LAM' in p and not is_post(p)
def ignore(p): return bool(re.search(r'web stiffener|generic material',p,re.I))

def add_product_line(f, cur_section, prod, qty, length, tag):
    """Route a parsed product line into the right bucket on floor dict f.
    Floor Accessories / Roof Accessories sections are 'Blocking': only
    joist, beam, or rim products count there, and joists/beams are
    collapsed to a total footage per (product, tag) instead of per-line
    qty/length rows. Rim is still converted to 16' pieces as normal, but
    kept in its own blocking rim total, separate from the main structural rim."""
    in_blocking = cur_section in ('Floor Accessories', 'Roof Accessories')
    if is_rim(prod):
        s = get_sku(prod)
        if in_blocking:
            f['blockingRimFt'] += qty*length
            if s: f['blockingRimSku'] = s
            f['blockingRimProd'] = norm(prod)
        else:
            f['rimFt'] += qty*length
            f['rimSku'] = s or f['rimSku']
            f['rimProd'] = norm(prod)
    elif in_blocking and is_joist(prod):
        key = (norm(prod), tag)
        e = f['blockingJoists'].setdefault(key, {'sku': get_sku(prod), 'totalFt': 0})
        e['totalFt'] += qty*length
    elif in_blocking and is_beam(prod):
        key = (norm(prod), tag)
        e = f['blockingBeams'].setdefault(key, {'sku': get_sku(prod), 'totalFt': 0})
        e['totalFt'] += qty*length
    elif in_blocking:
        return  # not joist/beam/rim (e.g. web stiffener, misc) - skip in Blocking
    elif is_joist(prod):
        f['joists'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})
    elif is_post(prod):
        f['posts'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})
    elif is_beam(prod):
        f['beams'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})

def parse_pdf(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    all_lines = []
    for page in doc:
        words = page.get_text("words")
        by_y = defaultdict(list)
        for w in words:
            by_y[round(w[1])].append({"x":round(w[0]),"str":w[4]})
        for y in sorted(by_y.keys()):
            row = sorted(by_y[y], key=lambda x: x["x"])
            all_lines.append(" ".join(r["str"] for r in row))

    full = '\n'.join(all_lines)
    project = re.search(r'File Name: ([\w\s\-/]+?)(?= Date:| Misc:|\n)', full)
    project = project.group(1).strip() if project else 'Unknown'
    builder = re.search(r'Builder: ([\w\s,\.]+?)(?= Date:| Job|\n)', full)
    builder = builder.group(1).strip() if builder else ''

    SEC_RE = re.compile(r'^(Floor Framing|Floor Accessories|Roof Framing|Roof Accessories|Beams|Posts|Decking|Connectors) - (.+)$')
    DATA_RE = re.compile(r'(\d+) (\d+)\' \d+\'$')
    TAG_RE = re.compile(r"\d+'- \d+[\d\s/]*[\"']?\s+(\S+)\s+\d+ \d+'")
    CONN_RE = re.compile(r'^([A-Z][A-Z0-9\/\.\-,]{2,}) .+ (\d+)$')

    floors = {}
    cur_section = cur_floor = cur_product = None

    for raw in all_lines:
        line = raw.strip()
        if not line: continue
        if re.match(r'^(Product Depth|Subtotal|AJS|BOISE CASCADE|Page \d|\d{2}/\d{2}/\d{4}|STUD)', line): continue
        sm = SEC_RE.match(line)
        if sm:
            cur_section, cur_floor, cur_product = sm.group(1), sm.group(2).strip(), None
            if cur_floor not in floors:
                floors[cur_floor] = {'rimFt':0,'rimSku':'710056','rimProd':'1-1/4" x 11-7/8" BC RIM BOARD PLUS OSB','joists':[],'beams':[],'posts':[],'decking':0,'connectors':{},'connectorSkus':{},
                                      'blockingRimFt':0,'blockingRimSku':'710056','blockingRimProd':'1-1/4" x 11-7/8" BC RIM BOARD PLUS OSB','blockingJoists':{},'blockingBeams':{}}
            continue
        if not cur_section: continue
        f = floors[cur_floor]
        if cur_section == 'Decking':
            dm = re.search(r'(?:23/32"|3/4") 4x8 OSB \(Floor Decking\) (\d+)', line)
            if dm: f['decking'] = int(dm.group(1))
            continue
        if cur_section == 'Connectors':
            cm = CONN_RE.match(line)
            if cm:
                prod, qty = cm.group(1), int(cm.group(2))
                if not ignore(prod):
                    f['connectors'][prod] = f['connectors'].get(prod,0) + qty
                    s = get_sku(prod)
                    if s: f['connectorSkus'][prod] = s
            continue
        em = DATA_RE.search(line)
        if not em: continue
        qty, length = int(em.group(1)), int(em.group(2))
        tag_m = TAG_RE.search(line)
        tag = tag_m.group(1).rstrip("'") if tag_m else ''
        # Find depth marker - could be 0'-, 1'-, 2'- etc
        di_match = re.search(r" \d+'- ", line)
        di = di_match.start() if di_match else -1
        if di > 0:
            prod = line[:di].strip()
            if not prod or ignore(prod): cur_product = None; continue
            cur_product = prod
            add_product_line(f, cur_section, prod, qty, length, tag)
        elif re.match(r"\d+'- ", line) and cur_product and not ignore(cur_product):
            add_product_line(f, cur_section, cur_product, qty, length, tag)

    # Flatten blocking dicts (keyed by product+tag) into lists for JSON output
    for fl in floors.values():
        fl['blockingJoists'] = [{'product':k[0],'tag':k[1],'sku':v['sku'],'totalFt':v['totalFt']} for k,v in fl['blockingJoists'].items()]
        fl['blockingBeams'] = [{'product':k[0],'tag':k[1],'sku':v['sku'],'totalFt':v['totalFt']} for k,v in fl['blockingBeams'].items()]

    return {'project':project,'builder':builder,'floors':floors}

@app.route('/parse', methods=['POST','OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp
    try:
        data = request.get_json()
        file_id = data.get('fileId')
        token = data.get('gdriveToken')
        if not file_id or not token:
            return jsonify({'error': 'Missing fileId or gdriveToken'}), 400
        resp = requests.get(
            f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
            headers={'Authorization': f'Bearer {token}'}
        )
        if not resp.ok:
            return jsonify({'error': f'Failed to download PDF: {resp.status_code}'}), 500
        cut_list = parse_pdf(resp.content)
        response = jsonify({'success': True, 'cutList': cut_list})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

@app.route('/upload-plans', methods=['POST','OPTIONS'])
def upload_plans():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp
    try:
        sales_person = request.form.get('salesPerson')
        file_name = request.form.get('fileName')
        pdf_file = request.files.get('file')

        if not sales_person or not file_name or not pdf_file:
            return jsonify({'error': 'Missing salesPerson, fileName or file'}), 400

        token = get_oauth_token()
        headers = {'Authorization': f'Bearer {token}'}

        # Find EWP Jobs root folder
        resp = requests.get(
            f'https://www.googleapis.com/drive/v3/files?q=name="{FOLDER_NAME}"+and+mimeType="application/vnd.google-apps.folder"+and+trashed=false&fields=files(id)',
            headers=headers
        )
        files = resp.json().get('files', [])
        if not files:
            return jsonify({'error': f'Could not find "{FOLDER_NAME}" folder in Google Drive'}), 500
        root_folder_id = files[0]['id']

        # Get or create sales person folder
        sp_folder_id = get_or_create_folder(root_folder_id, sales_person, token)

        # Upload the PDF
        pdf_bytes = pdf_file.read()
        metadata = {'name': file_name, 'parents': [sp_folder_id]}

        resp = requests.post(
            'https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink',
            headers={'Authorization': f'Bearer {token}'},
            files={
                'metadata': ('metadata', json.dumps(metadata), 'application/json'),
                'file': (file_name, pdf_bytes, 'application/pdf')
            }
        )
        data = resp.json()
        if 'id' not in data:
            return jsonify({'error': 'Upload failed', 'details': data}), 500

        make_file_public(data['id'], token)
        public_url = f"https://drive.google.com/file/d/{data['id']}/view"
        response = jsonify({'success': True, 'fileId': data['id'], 'webViewLink': public_url})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

@app.route('/make-public', methods=['POST','OPTIONS'])
def make_public_endpoint():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    try:
        data = request.get_json()
        file_id = data.get('fileId')
        if not file_id:
            return jsonify({'error': 'Missing fileId'}), 400
        token = get_oauth_token()
        make_file_public(file_id, token)
        public_url = f"https://drive.google.com/file/d/{file_id}/view"
        response = jsonify({'success': True, 'publicUrl': public_url})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

@app.route('/send-email', methods=['POST','OPTIONS'])
def send_email_endpoint():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp
    try:
        data = request.get_json()
        to_email = data.get('to')
        subject = data.get('subject')
        html_body = data.get('html')
        if not to_email or not subject or not html_body:
            return jsonify({'error': 'Missing to, subject, or html'}), 400
        send_gmail(to_email, subject, html_body)
        response = jsonify({'success': True})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

@app.route('/')
def health():
    return jsonify({'status': 'ok', 'service': 'EWP Cut List Parser'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
