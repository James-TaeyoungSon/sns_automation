# -*- coding: utf-8 -*-
from flask import Blueprint, redirect, request, session, url_for, flash

from services import blogger_client

bp = Blueprint("auth", __name__)


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
        blogger_client.exchange_code(code=code, state=state)
        flash("Google Blogger 인증 완료!", "success")
    except Exception as e:
        flash(f"토큰 교환 실패: {e}", "error")

    return redirect(url_for("settings.settings_page"))
