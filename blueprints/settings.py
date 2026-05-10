# -*- coding: utf-8 -*-
import os
from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, flash
from dotenv import set_key

from config import cfg
from services import blogger_client, token_store

bp = Blueprint("settings", __name__)

_ENV_FILE = Path(__file__).parent.parent / ".env"

_EDITABLE_KEYS = [
    "OPENAI_API_KEY",
    "THREADS_ACCESS_TOKEN",
    "THREADS_USER_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "BLOGGER_BLOG_URL",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "IMGBB_API_KEY",
    "APP_BASE_URL",
    "CRAWL_INTERVAL_HOURS",
    "AUTO_POST_HOUR",
    "AUTO_POST_ENABLED",
    "ADMIN_TOKEN",
]


@bp.route("/settings", methods=["GET"])
def settings_page():
    current = {k: os.getenv(k, "") for k in _EDITABLE_KEYS}
    blogger_authed = blogger_client.is_authenticated()
    return render_template(
        "settings.html",
        settings=current,
        blogger_authed=blogger_authed,
        oauth_redirect_uri=cfg.OAUTH_REDIRECT_URI,
    )


@bp.route("/settings", methods=["POST"])
def settings_save():
    _ENV_FILE.touch(exist_ok=True)
    changed = []
    for key in _EDITABLE_KEYS:
        val = request.form.get(key, "").strip()
        if val:
            set_key(str(_ENV_FILE), key, val)
            os.environ[key] = val
            changed.append(key)
    flash(f"설정 저장 완료: {', '.join(changed) or '변경 없음'}", "success")
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/revoke-google", methods=["POST"])
def revoke_google():
    path = cfg.TOKEN_FILE
    if path.exists():
        path.unlink()
    blogger_client._service_cache = None
    flash("Google Blogger 인증이 취소되었습니다.", "info")
    return redirect(url_for("settings.settings_page"))
