from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import fitz
import re
import requests
import json
import time
import base64
import jwt
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

def download_file_bytes(file_id, token):
    resp = requests.get(
        f'https://www.googleapis.com/drive/v3/files/{file_id}',
        headers={'Authorization': f'Bearer {token}'},
        params={'alt': 'media'}
    )
    return resp.content

def stamp_qr_code_on_pdf(pdf_bytes, url):
    # Stamps a QR code linking to the file's own public URL onto the bottom-right corner
    # of every page, beside the Boise Cascade tree logo - so a printed copy of the layout
    # can be scanned on-site to instantly pull up the digital version, even if the printed
    # text is too small to read. Sized as a percentage of the page rather than a fixed
    # point size, so it scales sensibly whether the layout is a regular letter-size sheet
    # or a large-format architectural drawing.
    import qrcode
    from io import BytesIO
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    img_buf = BytesIO()
    img.save(img_buf, format='PNG')
    img_bytes = img_buf.getvalue()

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    qr_size = 50  # fixed size in points (~0.7in) - small enough to sit beside a title-block
    # logo without overlapping it, regardless of how wide/large the overall page is
    base_margin = 15
    right_margin = base_margin + 2.0 * qr_size  # shifted left 2.0 QR-widths from the corner
    bottom_margin = base_margin + 0.5 * qr_size  # shifted up 0.5 QR-heights from the corner
    for page in doc:
        pw, ph = page.rect.width, page.rect.height

        # Remove any QR code stamped by a previous run of this same function before adding
        # a new one - otherwise every re-stamp (e.g. each time the layout gets revised)
        # would leave the old one behind, stacking up duplicates. Detected by shape/size/
        # location rather than exact prior coordinates, so this cleans up correctly even if
        # the intended position has been adjusted since the last time this ran. A generous
        # "corner zone" (bigger than the QR itself) is checked so it also catches anything
        # from earlier position tweaks, not just the current exact spot.
        corner_zone = fitz.Rect(pw - 300, ph - 200, pw, ph)
        for xref in list(page.get_images(full=False)):
            xref_id = xref[0]
            for img_rect in page.get_image_rects(xref_id):
                # get_image_rects() returns coordinates in the page's NATIVE (unrotated)
                # space, while corner_zone above is in VISUAL (rotated) space to match
                # page.rect - same mismatch as the placement bug, just affecting detection
                # instead of insertion this time. Transform back to visual space first.
                visual_img_rect = img_rect * page.rotation_matrix
                is_small_square = abs(visual_img_rect.width - visual_img_rect.height) < 5 and visual_img_rect.width < 100
                if is_small_square and corner_zone.intersects(visual_img_rect):
                    page.delete_image(xref_id)
                    break

        visual_rect = fitz.Rect(pw - right_margin - qr_size, ph - bottom_margin - qr_size, pw - right_margin, ph - bottom_margin)
        # Many CAD/architectural PDF exports define landscape pages as a rotated portrait
        # page (a rotation flag applied to portrait dimensions) rather than a true landscape
        # page. page.rect already reports the correct VISUAL (rotated) dimensions, but
        # insert_image places content using the page's own native (unrotated) coordinate
        # system - so the visual-space rect has to be transformed through the derotation
        # matrix first, or the image ends up somewhere else entirely (or effectively
        # invisible) on any page that uses this common rotation technique.
        native_rect = visual_rect * page.derotation_matrix
        page.insert_image(native_rect, stream=img_bytes)
    out_buf = BytesIO()
    doc.save(out_buf, garbage=4, deflate=True)
    doc.close()
    return out_buf.getvalue()

def upload_file_content(file_id, pdf_bytes, token):
    # Replaces an existing Drive file's CONTENT in place (same file ID, same sharing link
    # and permissions) - this is how the QR-stamped version becomes what everyone actually
    # sees when they open the layout, whether through this app or directly in Drive/email.
    resp = requests.patch(
        f'https://www.googleapis.com/upload/drive/v3/files/{file_id}',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/pdf'},
        params={'uploadType': 'media'},
        data=pdf_bytes
    )
    return resp.json()

def make_file_public(file_id, token):
    requests.post(
        f'https://www.googleapis.com/drive/v3/files/{file_id}/permissions',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'role': 'reader', 'type': 'anyone'}
    )

def get_file_modified_time(file_id, token):
    # Changing a file's sharing permissions can itself bump Google Drive's reported
    # modifiedTime, even though nothing about the file's actual content changed. Fetching
    # the CURRENT modifiedTime right after making a file public (rather than relying on
    # whatever timestamp we saw before that call) avoids the sync loop mistaking that
    # permission-change side effect for a genuine new file revision on the next cycle.
    resp = requests.get(
        f'https://www.googleapis.com/drive/v3/files/{file_id}',
        headers={'Authorization': f'Bearer {token}'},
        params={'fields': 'modifiedTime'}
    )
    data = resp.json()
    return data.get('modifiedTime')

EMAIL_ADDRESS = 'fvbsewphub@gmail.com'
APPS_SCRIPT_MAILER_URL = os.environ.get('APPS_SCRIPT_MAILER_URL', 'https://script.google.com/macros/s/AKfycby_6b_APrJuqYQtwYBmwvg0zkW3gNFo_ua_58hG04pWahsIbMbeCJ38O6JGxd4Vpvlk/exec')

def send_gmail(to_email, subject, html_body, from_name='FVBS EWP Hub'):
    if not APPS_SCRIPT_MAILER_URL:
        raise Exception('APPS_SCRIPT_MAILER_URL is not configured')
    resp = requests.post(
        APPS_SCRIPT_MAILER_URL,
        headers={'Content-Type': 'application/json'},
        json={'to': to_email, 'subject': subject, 'html': html_body},
        allow_redirects=True
    )
    if not resp.ok:
        raise Exception(f'Apps Script mailer failed: {resp.status_code} {resp.text}')
    try:
        result = resp.json()
    except Exception:
        raise Exception(f'Apps Script mailer returned unexpected response: {resp.text[:200]}')
    if not result.get('success'):
        raise Exception(f'Apps Script mailer error: {result.get("error", "unknown error")}')

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
    '3-1/2" x 11-7/8" VERSA-LAM LVL 1.8E 2650 DF': 'VLSL311',
    '5-1/4" x 5-1/4" VERSA-LAM LVL 1.8E 2650 DF': 'VC55',
    '5-1/4" x 7" VERSA-LAM LVL 1.8E 2650 DF': 'VC57',
    '5-1/4" x 9-1/4" VERSA-LAM LVL 1.8E 2650 DF': 'VL59',
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
    '5-1/4" x 9-1/4" VERSA-LAM LVL 2.3E 3100 DF': 'VL59',
    '5 1/4" x 9 1/4" VERSA-LAM LVL 2.3E 3100 DF': 'VL59',
    '5 1/4" x 9 1/2" VERSA-LAM LVL 2.3E 3100 DF': 'VL59',
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
    '1" x 9-1/2" BC RIM BOARD OSB': '710016',
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

# When these specific joists show up in Floor/Roof Accessories (Blocking), they use a
# different SKU than when they're used as normal structural joists.
BLOCKING_SKU_OVERRIDES = {
    '11-7/8" BCI 6000-1.8 DF': 'BCI600011BLOCK',
    '11-7/8" BCI 90-2.0 DF': 'BCI9011BLOCK',
}
def get_blocking_joist_sku(prod):
    override = BLOCKING_SKU_OVERRIDES.get(norm(prod))
    return override if override else get_sku(prod)
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
    tag_upper = (tag or '').upper()
    # Tag-based overrides take priority over product-name-based classification. The same
    # VERSA-LAM product can be used as rim board on one job and as a joist on another,
    # depending on which tag it was assigned - the tag is the authoritative signal here,
    # not the product name. Ca-prefixed tags (Ca1, Ca2, ...) always mean rim board; any tag
    # that's a single uppercase letter followed by a number (L8, K15, etc.) always means joist.
    tag_is_rim = bool(re.match(r'^CA\d+$', tag_upper))
    tag_is_joist = bool(re.match(r"^[A-Z]\d+'?$", tag or ''))
    if is_rim(prod) or tag_is_rim:
        s = get_sku(prod)
        if in_blocking:
            f['blockingRimFt'] += qty*length
            if s: f['blockingRimSku'] = s
            f['blockingRimProd'] = norm(prod)
        else:
            key = s or norm(prod)
            e = f['rimboards'].setdefault(key, {'sku': s, 'product': norm(prod), 'footage': 0})
            e['footage'] += qty*length
    elif in_blocking and (is_joist(prod) or tag_is_joist):
        key = (norm(prod), tag)
        e = f['blockingJoists'].setdefault(key, {'sku': get_blocking_joist_sku(prod), 'totalFt': 0})
        e['totalFt'] += qty*length
    elif in_blocking and is_beam(prod):
        key = (norm(prod), tag)
        e = f['blockingBeams'].setdefault(key, {'sku': get_sku(prod), 'totalFt': 0})
        e['totalFt'] += qty*length
    elif in_blocking:
        return  # not joist/beam/rim (e.g. web stiffener, misc) - skip in Blocking
    elif is_joist(prod) or tag_is_joist:
        f['joists'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})
    elif 'VERSA-LAM' in prod and tag_upper.startswith('LG'):
        # Ledger beams (tag starts with LG) get pulled out into their own section,
        # aggregated to a total footage per SKU (no per-line/per-tag rows). Checked
        # before BM/PT/grade rules since LG is the most specific signal.
        s = get_sku(prod)
        key = s or norm(prod)
        e = f['ledger'].setdefault(key, {'product': norm(prod), 'sku': s, 'totalFt': 0})
        e['totalFt'] += qty*length
    elif 'VERSA-LAM' in prod and tag_upper.startswith('BM'):
        # Some VERSA-LAM grades (e.g. 1.8E 2650) are used as BOTH posts and beams depending
        # on the job - the tag prefix is the authoritative signal for which one this line is,
        # regardless of the product's grade or which section it was listed under.
        f['beams'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})
    elif 'VERSA-LAM' in prod and tag_upper.startswith('PT'):
        f['posts'].append({'product':norm(prod),'sku':get_sku(prod),'tag':tag,'qty':qty,'length':length})
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
                floors[cur_floor] = {'rimboards':{},'joists':[],'beams':[],'posts':[],'decking':0,'connectors':{},'connectorSkus':{},
                                      'blockingRimFt':0,'blockingRimSku':'710056','blockingRimProd':'1-1/4" x 11-7/8" BC RIM BOARD PLUS OSB','blockingJoists':{},'blockingBeams':{},'ledger':{}}
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
        fl['ledger'] = [{'product':v['product'],'sku':v['sku'],'totalFt':v['totalFt']} for v in fl['ledger'].values()]
        fl['rimboards'] = sorted([{'sku':v['sku'],'product':v['product'],'footage':v['footage']} for v in fl['rimboards'].values()], key=lambda r: -r['footage'])

    return {'project':project,'builder':builder,'floors':floors}

@app.route('/list-files', methods=['POST','OPTIONS'])
def list_files_endpoint():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp
    try:
        token = get_oauth_token()
        headers = {'Authorization': f'Bearer {token}'}

        # Find the EWP Jobs root folder
        root_resp = requests.get(
            'https://www.googleapis.com/drive/v3/files',
            headers=headers,
            params={
                'q': f'name="{FOLDER_NAME}" and mimeType="application/vnd.google-apps.folder" and trashed=false',
                'fields': 'files(id,name)'
            }
        )
        root_data = root_resp.json()
        if not root_data.get('files'):
            return jsonify({'error': f'Could not find "{FOLDER_NAME}" folder in Google Drive'}), 404
        root_folder_id = root_data['files'][0]['id']

        # Get sales person subfolders
        sp_resp = requests.get(
            'https://www.googleapis.com/drive/v3/files',
            headers=headers,
            params={
                'q': f'"{root_folder_id}" in parents and mimeType="application/vnd.google-apps.folder" and trashed=false',
                'fields': 'files(id,name)'
            }
        )
        sp_data = sp_resp.json()

        all_files = []
        for sp_folder in sp_data.get('files', []):
            files_resp = requests.get(
                'https://www.googleapis.com/drive/v3/files',
                headers=headers,
                params={
                    'q': f'"{sp_folder["id"]}" in parents and name contains ".pdf" and trashed=false',
                    'fields': 'files(id,name,webViewLink,modifiedTime)'
                }
            )
            files_data = files_resp.json()
            for f in files_data.get('files', []):
                all_files.append({
                    'id': f['id'],
                    'name': f['name'],
                    'webViewLink': f.get('webViewLink', ''),
                    'modifiedTime': f.get('modifiedTime', ''),
                    'salesPerson': sp_folder['name']
                })

        response = jsonify({'success': True, 'files': all_files})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

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
        if not file_id:
            return jsonify({'error': 'Missing fileId'}), 400
        token = get_oauth_token()
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
                'file': (file_name, pdf_bytes, pdf_file.mimetype or 'application/octet-stream')
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

@app.route('/get-file-bytes', methods=['POST','OPTIONS'])
def get_file_bytes_endpoint():
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
        file_resp = requests.get(
            f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
            headers={'Authorization': f'Bearer {token}'}
        )
        if not file_resp.ok:
            return jsonify({'error': f'Drive fetch failed: {file_resp.status_code}'}), 500
        b64 = base64.b64encode(file_resp.content).decode('ascii')
        response = jsonify({'success': True, 'base64': b64})
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

        # Stamp a QR code (linking to this file's own public URL) onto every page of the
        # layout. If this fails for any reason, the layout still gets published normally -
        # a missing QR code is better than blocking the whole sync over it.
        print(f"[QR] Starting QR stamp for file {file_id}")
        try:
            pdf_bytes = download_file_bytes(file_id, token)
            print(f"[QR] Downloaded {len(pdf_bytes)} bytes")
            stamped_bytes = stamp_qr_code_on_pdf(pdf_bytes, public_url)
            print(f"[QR] Stamped PDF, {len(stamped_bytes)} bytes")
            upload_result = upload_file_content(file_id, stamped_bytes, token)
            print(f"[QR] Upload result: {upload_result}")
        except Exception as qr_err:
            import traceback
            print(f"[QR] STAMPING FAILED: {qr_err}")
            print(traceback.format_exc())

        modified_time = get_file_modified_time(file_id, token)
        response = jsonify({'success': True, 'publicUrl': public_url, 'modifiedTime': modified_time})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

def _ceil16(ft):
    return -(-int(round(ft)) // 16)

def build_cutlist_pdf(cutlist, job_address):
    """Render the cut list (mirroring the app's 'View cut list' content) as new PDF pages."""
    doc = fitz.open()
    page_w, page_h = 612, 792
    margin = 40
    line_h = 14
    state = {'page': doc.new_page(width=page_w, height=page_h), 'y': margin}

    def new_page():
        state['page'] = doc.new_page(width=page_w, height=page_h)
        state['y'] = margin

    def ensure_space(extra=line_h):
        if state['y'] + extra > page_h - margin:
            new_page()

    def draw_title(text, size=16):
        ensure_space(size + 10)
        state['page'].insert_text((margin, state['y'] + size), text, fontsize=size, fontname="helv", color=(0, 0, 0))
        state['y'] += size + 10

    def draw_section_header(text):
        ensure_space(22)
        r = fitz.Rect(margin - 4, state['y'] - 2, margin + 200, state['y'] + 16)
        state['page'].draw_rect(r, color=(0.09, 0.37, 0.65), fill=(0.09, 0.37, 0.65))
        state['page'].insert_text((margin, state['y'] + 12), text.upper(), fontsize=10, fontname="hebo", color=(1, 1, 1))
        state['y'] += 26

    def draw_row(cols, widths, bold=False):
        ensure_space(line_h)
        x = margin
        for c, w in zip(cols, widths):
            state['page'].insert_text((x, state['y'] + 10), str(c), fontsize=9, fontname="hebo" if bold else "helv")
            x += w
        state['y'] += line_h

    draw_title(job_address + " - Cut List")
    state['y'] += 6

    for floor_name, f in (cutlist.get('floors') or {}).items():
        ensure_space(34)
        draw_title(floor_name, size=13)

        rimboards = f.get('rimboards') or []
        if rimboards:
            draw_section_header("Rimboard")
            draw_row(["SKU", "Description", "Qty", "Note"], [70, 260, 50, 140], bold=True)
            for rb in rimboards:
                pcs = _ceil16(rb['footage'])
                draw_row([rb.get('sku') or 'Special order', rb.get('product', ''), str(pcs) + ' EA',
                          "16' pcs (" + str(rb['footage']) + "' / 16)"], [70, 260, 50, 140])
            state['y'] += 6

        for cat, items in [("Joists", f.get('joists', [])), ("Beams", f.get('beams', [])), ("Posts", f.get('posts', []))]:
            if not items:
                continue
            draw_section_header(cat)
            draw_row(["SKU", "Product", "Tag", "Qty", "Length"], [70, 220, 50, 40, 60], bold=True)
            for it in items:
                sku = it.get('sku') or ('' if cat == 'Posts' else 'Special order')
                draw_row([sku, it.get('product', ''), it.get('tag', ''), str(it.get('qty', '')), str(it.get('length', '')) + "'"],
                          [70, 220, 50, 40, 60])
            state['y'] += 6

        if f.get('decking', 0):
            draw_section_header("Decking")
            draw_row(["Sheets"], [100], bold=True)
            draw_row([str(f['decking'])], [100])
            state['y'] += 6

        if f.get('connectors'):
            draw_section_header("Connectors")
            draw_row(["SKU", "Product", "Qty"], [90, 300, 60], bold=True)
            for prod, qty in f['connectors'].items():
                sku = (f.get('connectorSkus') or {}).get(prod, '')
                draw_row([sku or 'Special order', prod, str(qty)], [90, 300, 60])
            state['y'] += 6

        blocking_rows = []
        if f.get('blockingRimFt', 0) > 0:
            pcs = _ceil16(f['blockingRimFt'])
            blocking_rows.append((f.get('blockingRimSku') or 'Special order', f.get('blockingRimProd', ''), '', str(pcs) + ' EA'))
        for it in f.get('blockingJoists', []):
            blocking_rows.append((it.get('sku') or 'Special order', it.get('product', ''), it.get('tag', ''), str(it.get('totalFt', '')) + ' LF'))
        for it in f.get('blockingBeams', []):
            blocking_rows.append((it.get('sku') or 'Special order', it.get('product', ''), it.get('tag', ''), str(it.get('totalFt', '')) + ' LF'))
        if blocking_rows:
            draw_section_header("Blocking")
            draw_row(["SKU", "Product", "Tag", "Qty/Ft"], [90, 260, 50, 60], bold=True)
            for r in blocking_rows:
                draw_row(list(r), [90, 260, 50, 60])
            state['y'] += 10

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes

@app.route('/full-package', methods=['POST', 'OPTIONS'])
def full_package_endpoint():
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        resp.headers['Access-Control-Allow-Methods'] = 'POST'
        return resp
    try:
        data = request.get_json()
        file_id = data.get('fileId')
        cutlist = data.get('cutList')
        job_address = data.get('jobAddress', 'Job')
        if not file_id or not cutlist:
            return jsonify({'error': 'Missing fileId or cutList'}), 400

        token = get_oauth_token()
        # Download the actual layout PDF bytes via the Drive API (not the public view link,
        # which is an HTML viewer page, not the raw file - and wouldn't be CORS-fetchable
        # from the browser anyway).
        dl = requests.get(
            f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media',
            headers={'Authorization': f'Bearer {token}'}
        )
        if not dl.ok:
            return jsonify({'error': f'Failed to download layout: {dl.status_code}'}), 500

        layout_doc = fitz.open(stream=dl.content, filetype='pdf')
        cutlist_bytes = build_cutlist_pdf(cutlist, job_address)
        cutlist_doc = fitz.open(stream=cutlist_bytes, filetype='pdf')

        merged = fitz.open()
        merged.insert_pdf(layout_doc)   # layout first
        merged.insert_pdf(cutlist_doc)  # cut list after
        out_bytes = merged.tobytes()
        merged.close(); layout_doc.close(); cutlist_doc.close()

        safe_name = re.sub(r'[^A-Za-z0-9 _-]', '', job_address)[:60] or 'Job'
        response = Response(out_bytes, mimetype='application/pdf')
        response.headers['Content-Disposition'] = f'attachment; filename="{safe_name} Full Package.pdf"'
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
