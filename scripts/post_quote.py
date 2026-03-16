"""
Workflow 2: 기사 선택 확인 → Gemini 명언+글 생성 → Imagen 이미지 → SNS 포스팅
실행: 매일 10:00 KST (am 슬롯), 16:00 KST (pm 슬롯)
"""
import os, json, re, base64, time, io, textwrap, requests
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw, ImageFont
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 환경변수 ───────────────────────────────────────────────────
SA_JSON        = os.environ['GOOGLE_SA_JSON']
SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
GEMINI_KEY     = os.environ['GEMINI_API_KEY']
IG_TOKEN       = os.environ['IG_ACCESS_TOKEN']
IG_USER_ID     = os.environ['IG_USER_ID']
TH_TOKEN       = os.environ['THREADS_ACCESS_TOKEN']
TH_USER_ID     = os.environ['THREADS_USER_ID']
SLOT              = os.environ.get('SLOT', 'am')   # 'am' or 'pm'
CLOUDINARY_CLOUD  = os.environ['CLOUDINARY']
CLOUDINARY_PRESET = os.environ['UPLOAD_PRESET']

KST   = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta'


# ── 헬퍼: Gemini API 재시도 래퍼 ──────────────────────────────
def gemini_post(url: str, payload: dict, retries: int = 4, backoff: float = 10.0) -> dict:
    """일시적 서버 과부하(503/high demand) 시 최대 retries회 재시도."""
    for attempt in range(retries):
        r = requests.post(url, json=payload, timeout=60)
        resp = r.json()
        err_msg = resp.get('error', {}).get('message', '')
        if 'high demand' in err_msg or 'temporarily' in err_msg or r.status_code == 503:
            wait = backoff * (attempt + 1)
            print(f"[RETRY {attempt+1}/{retries}] 서버 과부하, {wait:.0f}초 후 재시도...")
            time.sleep(wait)
            continue
        return resp
    raise RuntimeError(f"Gemini API 재시도 초과: {resp}")

# ── Google 서비스 초기화 ───────────────────────────────────────
sa_info = json.loads(SA_JSON)
creds   = service_account.Credentials.from_service_account_info(
    sa_info, scopes=['https://www.googleapis.com/auth/spreadsheets']
)
sheets = build('sheets', 'v4', credentials=creds)


# ── 헬퍼: Gemini 마크다운 코드블록 제거 ───────────────────────
def strip_markdown(text: str) -> str:
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*',     '', text)
    return text.strip()


# ── STEP 1: 선택기사 시트에서 오늘 선택 번호 읽기 ──────────────
def get_selected_article() -> dict | None:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range='선택기사!A:E'
    ).execute()
    rows = result.get('values', [])

    # 헤더 제외, 오늘 날짜 행 찾기
    for row in rows[1:]:
        if not row or row[0] != TODAY:
            continue
        # row: [날짜, am_기사번호, pm_기사번호, am_처리상태, pm_처리상태]
        while len(row) < 5:
            row.append('')
        num_col = 1 if SLOT == 'am' else 2
        stat_col = 3 if SLOT == 'am' else 4
        article_num = row[num_col].strip()
        status      = row[stat_col].strip()

        if not article_num:
            print(f"[SKIP] {SLOT} 기사 번호가 입력되지 않았습니다.")
            return None
        if status == '완료':
            print(f"[SKIP] {SLOT} 슬롯 이미 포스팅 완료.")
            return None
        return {'num': int(article_num), 'row_index': rows.index(row) + 1, 'stat_col': stat_col}

    print(f"[SKIP] 오늘({TODAY}) 선택기사 행이 없습니다.")
    return None


# ── STEP 2: 뉴스이력 시트에서 기사 정보 가져오기 ──────────────
def get_article_info(article_num: int) -> dict:
    result = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range='뉴스이력!A:U'
    ).execute()
    rows = result.get('values', [])

    for row in rows[1:]:
        if not row or row[0] != TODAY:
            continue
        # 컬럼 구조: 날짜, 기사1_제목, 기사1_URL, 기사2_제목, 기사2_URL, ...
        idx   = 1 + (article_num - 1) * 2
        title = row[idx]     if len(row) > idx     else ''
        url   = row[idx + 1] if len(row) > idx + 1 else ''
        return {'title': title, 'url': url, 'num': article_num}

    raise RuntimeError(f"뉴스이력에서 오늘({TODAY}) 기사 {article_num}번을 찾을 수 없습니다.")


# ── STEP 3: Gemini로 명언 + 해석 + 캡션 생성 ──────────────────
def generate_content(article: dict) -> dict:
    # 3-1: Google Search ON → 실존 명언 검색 (plain text)
    search_prompt = f"""다음 뉴스 기사와 연관된 실존 인물의 실제 명언 하나를 웹에서 찾아줘.

검색 우선순위:
1순위: 한국 인물(역사적 인물, 정치인, 기업인, 사상가 등)의 한국어 명언 — 한국어 사이트(나무위키, 위키인용집, 한국어 뉴스 등)에서 우선 검색
2순위: 외국 인물의 명언 (영어 원문 그대로)

기사 제목: {article['title']}

반드시 아래 형식으로만 답해줘 (다른 설명 없이):
QUOTE_ORIGINAL: [명언 원문 — 한국인이면 한국어, 외국인이면 영어 등 원어 그대로]
QUOTE_KO: [명언 한국어 번역 — 원문이 이미 한국어면 그대로 복사]
AUTHOR: [인물 이름 (한국어)]
AUTHOR_INFO: [인물 한 줄 소개]"""

    resp1 = gemini_post(
        f'{GEMINI_BASE}/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}',
        {'contents': [{'parts': [{'text': search_prompt}]}], 'tools': [{'google_search': {}}]}
    )
    if 'error' in resp1:
        raise RuntimeError(f"Gemini 명언검색 오류: {resp1['error']['message']}")

    quote_raw = resp1['candidates'][0]['content']['parts'][0]['text']
    # plain text 파싱
    def extract(key, text):
        for line in text.splitlines():
            if line.startswith(f'{key}:'):
                return line.split(':', 1)[1].strip()
        return ''
    quote_original = extract('QUOTE_ORIGINAL', quote_raw)
    quote_ko       = extract('QUOTE_KO',       quote_raw)
    author         = extract('AUTHOR',         quote_raw)
    author_info    = extract('AUTHOR_INFO',    quote_raw)
    print(f"[OK] 명언 검색: {quote_ko[:60]}... — {author}")

    # 3-2: Google Search OFF + response_mime_type json → 캡션/이미지 프롬프트 생성
    caption_prompt = f"""다음 뉴스 기사와 명언을 기반으로 SNS 포스팅 콘텐츠를 작성해줘.

기사 제목: {article['title']}
명언 원문: {quote_original}
명언 (한국어): {quote_ko}
명언 출처: {author} ({author_info})

조건:
- Instagram 캡션: 기사를 만평하듯 날카롭게 분석하고 명언과 연결, 해시태그 포함, 1500자 이내
- Threads 캡션: 핵심만 간결하게, 해시태그 포함, 450자 이내
- image_prompt: 명언 카드 이미지 생성용 영문 프롬프트 (100자 이내)"""

    resp2 = gemini_post(
        f'{GEMINI_BASE}/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}',
        {
            'contents': [{'parts': [{'text': caption_prompt}]}],
            'generationConfig': {'response_mime_type': 'application/json',
                                 'response_schema': {
                                     'type': 'object',
                                     'properties': {
                                         'caption_ig':    {'type': 'string'},
                                         'caption_th':    {'type': 'string'},
                                         'image_prompt':  {'type': 'string'},
                                     },
                                     'required': ['caption_ig', 'caption_th', 'image_prompt']
                                 }}
        }
    )
    if 'error' in resp2:
        raise RuntimeError(f"Gemini 캡션생성 오류: {resp2['error']['message']}")

    captions = json.loads(resp2['candidates'][0]['content']['parts'][0]['text'])
    return {
        'quote_original': quote_original,
        'quote_ko':       quote_ko,
        'author':         author,
        'author_info':    author_info,
        'caption_ig':     captions['caption_ig'],
        'caption_th':     captions['caption_th'],
        'image_prompt':   captions['image_prompt'],
    }


# ── STEP 4: 배경 이미지 생성 (텍스트 없음) ────────────────────
def generate_background(content: dict) -> bytes:
    """AI로 배경 이미지만 생성 — 텍스트는 Pillow로 별도 합성"""
    style = content.get('image_prompt') or 'Minimalist dark navy abstract background'
    prompt = (
        f'{style}. '
        f'Background image only — absolutely NO text, NO letters, NO words anywhere. '
        f'Dark moody atmosphere, high quality, square format 1:1.'
    )
    resp = gemini_post(
        f'{GEMINI_BASE}/models/gemini-2.5-flash-image:generateContent?key={GEMINI_KEY}',
        {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'responseModalities': ['IMAGE', 'TEXT']}
        }
    )
    if 'error' in resp:
        raise RuntimeError(f"이미지 생성 오류: {resp['error']['message']}")
    for part in resp['candidates'][0]['content']['parts']:
        if 'inlineData' in part:
            img_bytes = base64.b64decode(part['inlineData']['data'])
            print(f"[OK] 배경 이미지 생성: {len(img_bytes)//1024}KB")
            return img_bytes
    raise RuntimeError(f"이미지 데이터 없음: {resp}")


# ── STEP 4b: Pillow로 명언 텍스트 합성 ────────────────────────
def overlay_text(img_bytes: bytes, quote_text: str, author: str) -> bytes:
    """배경 위에 명언+저자를 Pillow로 직접 렌더링 — 오타 제로"""
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    w, h = img.size

    # 반투명 어두운 오버레이 (텍스트 가독성)
    overlay = Image.new('RGBA', (w, h), (0, 0, 0, 140))
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    draw = ImageDraw.Draw(img)

    # 폰트 로드 (Ubuntu: NanumMyeongjo, Windows: 맑은고딕)
    font_candidates = [
        '/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf',
        '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        'C:/Windows/Fonts/malgun.ttf',
        'C:/Windows/Fonts/gulim.ttc',
    ]
    q_size = max(40, h // 18)
    a_size = max(28, h // 28)
    q_font = a_font = None
    for fp in font_candidates:
        try:
            q_font = ImageFont.truetype(fp, size=q_size)
            a_font = ImageFont.truetype(fp, size=a_size)
            break
        except OSError:
            continue
    if q_font is None:
        q_font = a_font = ImageFont.load_default()

    # 텍스트 줄바꿈 (한 줄당 약 14자)
    wrap_w = max(10, w // q_size)
    lines = textwrap.wrap(f'"{quote_text}"', width=wrap_w)

    line_h = q_size + 12
    total_h = len(lines) * line_h + a_size + 30
    y = (h - total_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=q_font)
        x = (w - (bbox[2] - bbox[0])) // 2
        draw.text((x, y), line, font=q_font, fill='white')
        y += line_h

    # 저자명
    author_str = f'— {author}'
    bbox = draw.textbbox((0, 0), author_str, font=a_font)
    x = (w - (bbox[2] - bbox[0])) // 2
    draw.text((x, y + 10), author_str, font=a_font, fill='#cccccc')

    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=92)
    print(f"[OK] 텍스트 합성 완료: {buf.tell()//1024}KB")
    return buf.getvalue()


def generate_image(content: dict) -> bytes:
    bg = generate_background(content)
    return overlay_text(bg, content['quote_original'], content['author'])


# ── STEP 5: Cloudinary 업로드 ────────────────────────────────
def upload_to_cloudinary(img_bytes: bytes) -> str:
    r = requests.post(
        f'https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD}/image/upload',
        files={'file': ('quote.jpg', img_bytes, 'image/jpeg')},
        data={'upload_preset': CLOUDINARY_PRESET},
        timeout=30
    )
    resp = r.json()
    if 'secure_url' not in resp:
        raise RuntimeError(f"Cloudinary 업로드 실패: {resp}")
    url = resp['secure_url']
    print(f"[OK] Cloudinary 업로드: {url}")
    return url


# ── STEP 7: Instagram 포스팅 ───────────────────────────────────
def post_instagram(img_url: str, caption: str) -> str:
    r1 = requests.post(
        f'https://graph.instagram.com/v21.0/{IG_USER_ID}/media',
        params={'access_token': IG_TOKEN},
        json={'image_url': img_url, 'caption': caption}
    )
    resp1 = r1.json()
    if 'error' in resp1:
        raise RuntimeError(f"Instagram 컨테이너 오류: {resp1['error']['message']}")

    time.sleep(2)
    r2 = requests.post(
        f'https://graph.instagram.com/v21.0/{IG_USER_ID}/media_publish',
        params={'access_token': IG_TOKEN},
        json={'creation_id': resp1['id']}
    )
    resp2 = r2.json()
    if 'error' in resp2:
        raise RuntimeError(f"Instagram 발행 오류: {resp2['error']['message']}")
    print(f"[OK] Instagram 게시: {resp2['id']}")
    return resp2['id']


# ── STEP 8: Threads 포스팅 ────────────────────────────────────
def post_threads(img_url: str, caption: str) -> str:
    r1 = requests.post(
        f'https://graph.threads.net/v1.0/{TH_USER_ID}/threads',
        params={'access_token': TH_TOKEN},
        json={'media_type': 'IMAGE', 'image_url': img_url, 'text': caption[:500]}
    )
    resp1 = r1.json()
    if 'error' in resp1:
        raise RuntimeError(f"Threads 컨테이너 오류: {resp1['error']['message']}")

    time.sleep(3)
    r2 = requests.post(
        f'https://graph.threads.net/v1.0/{TH_USER_ID}/threads_publish',
        params={'access_token': TH_TOKEN},
        json={'creation_id': resp1['id']}
    )
    resp2 = r2.json()
    if 'error' in resp2:
        raise RuntimeError(f"Threads 발행 오류: {resp2['error']['message']}")
    print(f"[OK] Threads 게시: {resp2['id']}")
    return resp2['id']


# ── STEP 9: 포스팅이력 시트 업데이트 ─────────────────────────
def update_history(article: dict, content: dict, img_url: str, ig_id: str, th_id: str, sel_info: dict):
    # 선택기사 처리상태 → 완료
    stat_range = f"선택기사!{chr(69 + sel_info['stat_col'] - 3)}{sel_info['row_index']}"
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=stat_range,
        valueInputOption='RAW',
        body={'values': [['완료']]}
    ).execute()

    # 포스팅이력 추가
    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range='포스팅이력!A1',
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [[
            TODAY, SLOT, article['title'],
            content['quote_ko'], content['quote_original'], content['author'],
            img_url, ig_id, th_id, '성공'
        ]]}
    ).execute()
    print("[OK] 포스팅이력 업데이트 완료")


# ── 메인 실행 ─────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"=== 포스팅 시작: {TODAY} [{SLOT.upper()}] ===")

    sel_info = get_selected_article()
    if not sel_info:
        print("선택된 기사 없음. 종료.")
        exit(0)

    article = get_article_info(sel_info['num'])
    print(f"[OK] 기사: {article['title'][:50]}")

    content   = generate_content(article)
    img_bytes = generate_image(content)

    img_url = upload_to_cloudinary(img_bytes)

    ig_id = post_instagram(img_url, content['caption_ig'])
    th_id = post_threads(img_url,   content['caption_th'])

    print(f"=== 완료: Instagram={ig_id}, Threads={th_id} ===")
