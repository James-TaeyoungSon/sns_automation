"""
Workflow 1: 뉴스 수집 → Google Sheets 저장 → Gmail 발송
실행: 매일 09:00 KST (GitHub Actions cron)
"""
import os, json, smtplib, requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── 환경변수 로드 ──────────────────────────────────────────────
SA_JSON        = os.environ['GOOGLE_SA_JSON']       # GitHub Secret (JSON 문자열)
SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
GDRIVE_FOLDER  = os.environ['GDRIVE_FOLDER_ID']
GMAIL_USER     = os.environ['GMAIL_USER']
GMAIL_PASS     = os.environ['GMAIL_APP_PASSWORD']

KST   = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime('%Y-%m-%d')
RSS_URL = 'https://api.rss2json.com/v1/api.json?rss_url=https://www.yna.co.kr/rss/news.xml&count=10'

# ── Google 서비스 초기화 ───────────────────────────────────────
sa_info = json.loads(SA_JSON)
creds = service_account.Credentials.from_service_account_info(
    sa_info,
    scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
)
sheets = build('sheets', 'v4', credentials=creds)
drive  = build('drive',  'v3', credentials=creds)


def fetch_news() -> list[dict]:
    """RSS → 뉴스 10개 반환"""
    r = requests.get(RSS_URL, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get('status') != 'ok':
        raise RuntimeError(f"RSS 오류: {data}")
    items = data['items'][:10]
    return [{'num': i+1, 'title': it['title'], 'url': it['link']} for i, it in enumerate(items)]


def save_to_sheets(news: list[dict]):
    """뉴스이력 시트에 오늘 행 추가"""
    row = [TODAY]
    for item in news:
        row += [item['title'], item['url']]

    # 기존에 오늘 날짜 행이 있으면 스킵
    existing = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='뉴스이력!A:A'
    ).execute().get('values', [])
    if any(r[0] == TODAY for r in existing if r):
        print(f"[SKIP] 뉴스이력에 오늘({TODAY}) 데이터 이미 존재")
        return

    sheets.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range='뉴스이력!A1',
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [row]}
    ).execute()

    # 선택기사 시트에도 오늘 행 생성 (빈 상태)
    sel_existing = sheets.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='선택기사!A:A'
    ).execute().get('values', [])
    if not any(r[0] == TODAY for r in sel_existing if r):
        sheets.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range='선택기사!A1',
            valueInputOption='RAW',
            insertDataOption='INSERT_ROWS',
            body={'values': [[TODAY, '', '', '대기', '대기']]}
        ).execute()

    print(f"[OK] 뉴스이력 + 선택기사 시트 저장 완료")


def save_to_drive(news: list[dict]):
    """뉴스 JSON을 Google Drive에 저장"""
    filename = f"news_{TODAY}.json"
    content  = json.dumps(news, ensure_ascii=False, indent=2).encode('utf-8')

    # 기존 파일 삭제 (중복 방지)
    existing = drive.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER}' in parents and trashed=false",
        fields='files(id)'
    ).execute().get('files', [])
    for f in existing:
        drive.files().delete(fileId=f['id']).execute()

    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(content, mimetype='application/json')
    drive.files().create(
        body={'name': filename, 'parents': [GDRIVE_FOLDER]},
        media_body=media
    ).execute()
    print(f"[OK] Drive 저장: {filename}")


def send_gmail(news: list[dict]):
    """Gmail로 뉴스 리스트 발송"""
    sheets_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
    today_kr   = datetime.now(KST).strftime('%Y년 %m월 %d일')

    # HTML 이메일 본문
    rows_html = ''.join(
        f'<tr><td style="padding:8px;font-size:15px;font-weight:bold;width:30px">{n["num"]}</td>'
        f'<td style="padding:8px"><a href="{n["url"]}" style="color:#1a73e8;text-decoration:none">{n["title"]}</a></td></tr>'
        for n in news
    )
    html = f"""
<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto">
  <h2 style="color:#333">{today_kr} 주요 뉴스 10선</h2>
  <table style="width:100%;border-collapse:collapse">
    {rows_html}
  </table>
  <hr style="margin:24px 0">
  <p style="color:#555;font-size:14px">
    👇 아래 시트에서 <strong>am_기사번호</strong>와 <strong>pm_기사번호</strong>에 숫자(1~10)를 입력하세요.<br>
    오전(10:00) 포스팅용 1개 + 오후(16:00) 포스팅용 1개
  </p>
  <a href="{sheets_url}" style="display:inline-block;margin-top:12px;padding:12px 24px;
     background:#1a73e8;color:#fff;border-radius:6px;text-decoration:none;font-weight:bold">
    📝 기사 선택하러 가기
  </a>
</div>
"""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'[명언 자동포스팅] {today_kr} 뉴스 선택 요청'
    msg['From']    = GMAIL_USER
    msg['To']      = GMAIL_USER
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASS)
        smtp.send_message(msg)
    print(f"[OK] Gmail 발송 완료 → {GMAIL_USER}")


if __name__ == '__main__':
    print(f"=== 뉴스 수집 시작: {TODAY} ===")
    news = fetch_news()
    print(f"[OK] RSS 수집: {len(news)}개")
    for n in news:
        print(f"  {n['num']}. {n['title'][:50]}")

    save_to_sheets(news)
    save_to_drive(news)
    send_gmail(news)
    print("=== 완료 ===")
