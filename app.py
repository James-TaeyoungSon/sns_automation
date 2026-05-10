# -*- coding: utf-8 -*-
"""Flask 앱 진입점. Gunicorn --workers 1 --threads 4 로 실행."""
from flask import Flask, jsonify

from config import cfg
from database import init_db
from services import token_store as _token_store
from blueprints.auth import bp as auth_bp
from blueprints.dashboard import bp as dashboard_bp
from blueprints.article import bp as article_bp
from blueprints.batch import bp as batch_bp
from blueprints.history import bp as history_bp
from blueprints.settings import bp as settings_bp
from scheduler_tasks import create_scheduler
from services import blogger_client

app = Flask(__name__)
app.secret_key = cfg.FLASK_SECRET_KEY

# DB 초기화
with app.app_context():
    init_db()

# BLOGGER_TOKEN_B64 환경변수에서 토큰 파일 복원 (Render 재시작 대응)
_token_store.restore_from_env()

# 블루프린트 등록
app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(article_bp)
app.register_blueprint(batch_bp)
app.register_blueprint(history_bp)
app.register_blueprint(settings_bp)


@app.route("/api/status")
def api_status():
    from database import db_conn
    db_ok = False
    queued = 0
    try:
        with db_conn() as con:
            queued = con.execute(
                "SELECT COUNT(*) FROM articles WHERE status NOT IN ('dismissed','published')"
            ).fetchone()[0]
            db_ok = True
    except Exception:
        pass
    return jsonify({
        "db_ok": db_ok,
        "blogger_authed": blogger_client.is_authenticated(),
        "queued": queued,
        "threads_configured": bool(cfg.THREADS_ACCESS_TOKEN),
    })


@app.route("/api/auth-debug")
def auth_debug():
    import os, base64
    from services import token_store
    from pathlib import Path

    token_file = cfg.TOKEN_FILE
    b64_env = os.getenv("BLOGGER_TOKEN_B64", "")

    file_exists = Path(token_file).exists()
    file_size = Path(token_file).stat().st_size if file_exists else 0

    b64_len = len(b64_env)
    b64_decode_ok = False
    b64_decode_err = ""
    if b64_env:
        try:
            base64.b64decode(b64_env).decode("utf-8")
            b64_decode_ok = True
        except Exception as e:
            b64_decode_err = str(e)

    load_result = token_store.load()

    blogger_err = ""
    blogger_ok = False
    try:
        blogger_client.get_service()
        blogger_ok = True
    except Exception as e:
        blogger_err = str(e)

    return jsonify({
        "token_file_path": str(token_file),
        "token_file_exists": file_exists,
        "token_file_size_bytes": file_size,
        "BLOGGER_TOKEN_B64_length": b64_len,
        "BLOGGER_TOKEN_B64_set": bool(b64_env),
        "b64_decode_ok": b64_decode_ok,
        "b64_decode_error": b64_decode_err,
        "token_store_load_ok": load_result is not None,
        "blogger_service_ok": blogger_ok,
        "blogger_error": blogger_err,
    })


# APScheduler 시작 (테스트 환경 및 Flask 개발서버 리로더 프로세스 제외)
import os
_is_reloader = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
if not app.testing and (not app.debug or _is_reloader):
    _scheduler = create_scheduler()
    _scheduler.start()


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)
