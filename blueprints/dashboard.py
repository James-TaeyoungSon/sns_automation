# -*- coding: utf-8 -*-
import json
from flask import Blueprint, render_template, request, jsonify, redirect, url_for

from database import db_conn
from services.news_crawler import crawl_all_sources, crawl_and_rank
from services import telegram_service

bp = Blueprint("dashboard", __name__)

_STATUS_ORDER = {
    "new": 0, "reviewed": 1, "generating": 2, "generated": 3,
    "publishing": 4, "published": 5, "failed": 6, "dismissed": 7,
}


@bp.route("/")
def index():
    status_filter = request.args.get("status", "active")
    with db_conn() as con:
        if status_filter == "active":
            rows = con.execute(
                """SELECT * FROM articles
                   WHERE status NOT IN ('dismissed','published')
                   ORDER BY fetched_at DESC LIMIT 100"""
            ).fetchall()
        elif status_filter == "all":
            rows = con.execute(
                "SELECT * FROM articles ORDER BY fetched_at DESC LIMIT 200"
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM articles WHERE status=? ORDER BY fetched_at DESC LIMIT 100",
                (status_filter,),
            ).fetchall()

    articles = [dict(r) for r in rows]
    return render_template("dashboard.html", articles=articles, status_filter=status_filter)


@bp.route("/article/<int:article_id>/mark-reviewed", methods=["POST"])
def mark_reviewed(article_id: int):
    with db_conn() as con:
        con.execute(
            "UPDATE articles SET status='reviewed' WHERE id=? AND status='new'",
            (article_id,),
        )
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.route("/article/<int:article_id>/dismiss", methods=["POST"])
def dismiss(article_id: int):
    with db_conn() as con:
        con.execute(
            "UPDATE articles SET status='dismissed' WHERE id=?",
            (article_id,),
        )
    return redirect(request.referrer or url_for("dashboard.index"))


@bp.route("/api/crawl", methods=["POST"])
def api_crawl():
    inserted = crawl_all_sources()
    return jsonify({"ok": True, "inserted": inserted})


@bp.route("/api/digest", methods=["POST"])
def api_digest():
    """최신 Top10 기사를 Telegram으로 즉시 발송."""
    articles = crawl_and_rank(limit_per_source=8, top_n=10)
    if not articles:
        return jsonify({"ok": False, "error": "크롤된 기사 없음"})
    msg_id = telegram_service.send_digest(articles)
    if msg_id:
        return jsonify({"ok": True, "msg_id": msg_id, "count": len(articles)})
    return jsonify({"ok": False, "error": "Telegram 발송 실패 (봇 설정 확인)"})
