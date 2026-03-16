"""
Workflow 2: 기사 선택 확인 → Gemini 명언+글 생성 → Imagen 이미지 → SNS 포스팅
실행: 매일 10:00 KST (am 슬롯), 16:00 KST (pm 슬롯)
"""
import os, json, re, base64, time, requests
from datetime import datetime, timezone, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ── 환경변수 ───────────────────────────────────────────────────
SA_JSON        = os.environ['GOOGLE_SA_JSON']
SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
GDRIVE_FOLDER  = os.environ['GDRIVE_FOLDER_ID']
GEMINI_KEY     = os.environ['GEMINI_API_KEY']
IG_TOKEN       = os.environ['IG_ACCESS_TOKEN']
IG_USER_ID     = os.environ['IG_USER_ID']
TH_TOKEN       = os.environ['THREADS_ACCESS_TOKEN']
SLOT           = os.environ.get('SLOT', 'am')   # 'am' or 'pm'

KST   = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta'

# ── Google 서비스 초기화 ───────────────────────────────────────
sa_info = json.loads(SA_JSON)
creds   = service_account.Credentials.from_service_account_info(
    sa_info,
    scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
)
sheets = build('sheets', 'v4', credentials=creds)
drive  = build('drive',  'v3', credentials=creds)


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
    prompt = f"""다음 뉴스 기사를 분석해서 관련 명언 콘텐츠를 만들어줘.

기사 제목: {article['title']}
기사 URL: {article['url']}

조건:
1. 실존 인물이 실제로 한 말 중에서 기사 주제와 연관된 명언을 찾아줘
2. 기사를 만평하듯 날카롭게 분석하면서 명언과 연결짓는 글을 써줘
3. Instagram용 긴 캡션(1500자 이내)과 Threads용 짧은 캡션(450자 이내) 모두 작성

아래 JSON 형식으로만 반환 (코드블록 없이):
{{
  "quote_ko": "명언 한국어 번역",
  "quote_en": "명언 원문",
  "author": "인물 이름",
  "author_info": "인물 한 줄 소개",
  "caption_ig": "Instagram 캡션 (명언 + 기사 만평 + 해석 + 해시태그, 1500자 이내)",
  "caption_th": "Threads 캡션 (핵심만 간결하게 + 해시태그, 450자 이내)",
  "image_prompt": "Gemini Imagen용 영문 이미지 프롬프트 (명언 카드 스타일, 100자 이내)"
}}"""

    r = requests.post(
        f'{GEMINI_BASE}/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}',
        json={
            'contents': [{'parts': [{'text': prompt}]}],
            'tools': [{'google_search': {}}],   # 실존 명언 웹 검색
        },
        timeout=40
    )
    resp = r.json()
    if 'error' in resp:
        raise RuntimeError(f"Gemini 텍스트 오류: {resp['error']['message']}")

    raw = resp['candidates'][0]['content']['parts'][0]['text']
    data = json.loads(strip_markdown(raw))
    print(f"[OK] 명언: {data['quote_en'][:60]}... — {data['author']}")
    return data


# ── STEP 4: Gemini Imagen 4.0으로 이미지 생성 ─────────────────
def generate_image(content: dict) -> bytes:
    prompt = (
        content.get('image_prompt') or
        f'Minimalist motivational quote card, dark navy background, '
        f'white serif font: "{content["quote_en"]}" — {content["author"]}. '
        f'Premium clean design, Instagram square format.'
    )
    r = requests.post(
        f'{GEMINI_BASE}/models/imagen-4.0-generate-001:predict?key={GEMINI_KEY}',
        json={
            'instances': [{'prompt': prompt}],
            'parameters': {'sampleCount': 1, 'aspectRatio': '1:1', 'outputMimeType': 'image/jpeg'}
        },
        timeout=60
    )
    resp = r.json()
    if 'error' in resp:
        raise RuntimeError(f"Imagen 오류: {resp['error']['message']}")
    img_bytes = base64.b64decode(resp['predictions'][0]['bytesBase64Encoded'])
    print(f"[OK] 이미지 생성: {len(img_bytes)//1024}KB")
    return img_bytes


# ── STEP 5: Google Drive에 이미지 저장 ────────────────────────
def save_image_to_drive(img_bytes: bytes, filename: str) -> str:
    media = MediaInMemoryUpload(img_bytes, mimetype='image/jpeg')
    file_meta = {
        'name': filename,
        'parents': [GDRIVE_FOLDER],
    }
    # 누구나 볼 수 있도록 공개 설정 (Instagram/Threads URL로 사용)
    uploaded = drive.files().create(
        body=file_meta, media_body=media, fields='id,webContentLink'
    ).execute()
    file_id = uploaded['id']

    # 공개 읽기 권한 부여
    drive.permissions().create(
        fileId=file_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()

    # 직접 다운로드 URL (인스타/스레드 서버가 접근 가능한 형식)
    direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    print(f"[OK] Drive 이미지 저장: {filename} → {direct_url}")
    return direct_url


# ── STEP 6: catbox.moe CDN 업로드 (Drive 대안) ────────────────
def upload_to_catbox(img_bytes: bytes) -> str:
    r = requests.post(
        'https://catbox.moe/user/api.php',
        data={'reqtype': 'fileupload'},
        files={'fileToUpload': ('quote.jpg', img_bytes, 'image/jpeg')},
        timeout=30
    )
    url = r.text.strip()
    if not url.startswith('https://'):
        raise RuntimeError(f"catbox 업로드 실패: {url}")
    print(f"[OK] catbox.moe 업로드: {url}")
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
    # Threads user ID 조회
    r0 = requests.get(
        'https://graph.threads.net/v1.0/me',
        params={'fields': 'id', 'access_token': TH_TOKEN}
    )
    th_uid = r0.json()['id']

    r1 = requests.post(
        f'https://graph.threads.net/v1.0/{th_uid}/threads',
        params={'access_token': TH_TOKEN},
        json={'media_type': 'IMAGE', 'image_url': img_url, 'text': caption[:500]}
    )
    resp1 = r1.json()
    if 'error' in resp1:
        raise RuntimeError(f"Threads 컨테이너 오류: {resp1['error']['message']}")

    time.sleep(3)
    r2 = requests.post(
        f'https://graph.threads.net/v1.0/{th_uid}/threads_publish',
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
            content['quote_ko'], content['quote_en'], content['author'],
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

    # 이미지 CDN 업로드 (catbox.moe 우선, 실패 시 Drive)
    try:
        img_url = upload_to_catbox(img_bytes)
    except Exception as e:
        print(f"catbox 실패({e}), Drive로 대체...")
        fname   = f"quote_{TODAY}_{SLOT}.jpg"
        img_url = save_image_to_drive(img_bytes, fname)

    # Drive에도 항상 백업 저장
    try:
        fname = f"quote_{TODAY}_{SLOT}.jpg"
        save_image_to_drive(img_bytes, fname)
    except Exception as e:
        print(f"[WARN] Drive 백업 실패: {e}")

    ig_id = post_instagram(img_url, content['caption_ig'])
    th_id = post_threads(img_url,   content['caption_th'])

    update_history(article, content, img_url, ig_id, th_id, sel_info)

    print(f"=== 완료: Instagram={ig_id}, Threads={th_id} ===")
