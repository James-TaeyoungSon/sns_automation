# -*- coding: utf-8 -*-
import base64

from flask import Blueprint, redirect, request, session, url_for, flash, render_template_string

from services import blogger_client

bp = Blueprint("auth", __name__)

_AUTH_SUCCESS_HTML = """
<!doctype html><html lang="ko"><head><meta charset="UTF-8">
<title>Blogger 인증 완료</title>
<style>
body{font-family:sans-serif;max-width:720px;margin:60px auto;padding:0 20px;}
h1{color:#16a34a;}
.box{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:20px;margin:20px 0;}
.token-box{background:#1e293b;color:#86efac;padding:16px;border-radius:6px;font-family:monospace;font-size:.8rem;word-break:break-all;margin:10px 0;}
.btn{background:#4f46e5;color:#fff;border:none;padding:10px 20px;border-radius:6px;cursor:pointer;font-size:.95rem;}
.steps{counter-reset:step;}
.steps li{counter-increment:step;margin:10px 0;}
.steps li::before{content:counter(step)". ";}
</style>
</head><body>
<h1>✅ Google Blogger 인증 완료!</h1>
<div class="box">
  <p><strong>⚠️ 중요:</strong> Render 무료 플랜은 재시작 시 토큰이 사라집니다.<br>
  아래 값을 Render 환경변수에 저장하면 재시작 후에도 인증이 유지됩니다.</p>
</div>
<h2>📋 Render에 저장할 값</h2>
<p><strong>변수명:</strong> <code>BLOGGER_TOKEN_B64</code></p>
<p><strong>값 (전체 복사):</strong></p>
<div class="token-box" id="token">{{ token_b64 }}</div>
<button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('token').textContent).then(()=>this.textContent='✅ 복사됨!')">📋 복사</button>

<h2>📌 저장 방법</h2>
<ol class="steps">
  <li>위 값을 복사하세요.</li>
  <li>Render 대시보드 → 서비스 → <strong>Environment</strong> 탭 이동</li>
  <li><strong>Edit</strong> 클릭 → <code>BLOGGER_TOKEN_B64</code> 키로 새 변수 추가</li>
  <li>복사한 값 붙여넣기 → <strong>Save Changes</strong></li>
  <li>Render가 자동 재배포되면 이후 재시작에도 Blogger 인증이 유지됩니다.</li>
</ol>

<p style="margin-top:30px;"><a href="/">← 대시보드로 이동</a></p>
</body></html>
"""


@bp.route("/auth/google")
def google_login():
    url, state = blogger_client.get_authorization_url()
    session["oauth_state"] = state
    return redirect(url)


@bp.route("/auth/google/callback")
def google_callback():
    error = request.args.get("error")
    if error:
        flash(f"Google 인증 실패: {error}", "error")
        return redirect(url_for("settings.settings_page"))

    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        flash("인증 코드가 없습니다.", "error")
        return redirect(url_for("settings.settings_page"))

    try:
        creds = blogger_client.exchange_code(code=code, state=state)
        token_b64 = base64.b64encode(creds.to_json().encode()).decode()
        return render_template_string(_AUTH_SUCCESS_HTML, token_b64=token_b64)
    except Exception as e:
        flash(f"토큰 교환 실패: {e}", "error")
        return redirect(url_for("settings.settings_page"))
