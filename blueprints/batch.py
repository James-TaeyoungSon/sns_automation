# -*- coding: utf-8 -*-
"""
배치 감수/발행 라우트.
텔레그램에서 선택한 기사들을 웹에서 한 번에 확인·수정 후 시간차 발행.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, jsonify

from database import db_conn
from scheduler_tasks import _publish_generated

bp = Blueprint("batch", __name__)


@bp.route("/batch/<batch_id>")
def review(batch_id: str):
    with db_conn() as con:
        rows = con.execute(
            """SELECT a.id, a.title, a.url, a.source, a.status,
                      gc.blogspot_title, gc.blogspot_html, gc.threads_text,
                      gc.seo_keyword, gc.image_urls, gc.edited
               FROM articles a
               LEFT JOIN generated_content gc ON gc.article_id = a.id
               WHERE a.batch_id = ?
               ORDER BY a.id ASC""",
            (batch_id,),
        ).fetchall()

    if not rows:
        return "배치를 찾을 수 없습니다. 텔레그램 링크를 다시 확인하세요.", 404

    articles = [dict(r) for r in rows]
    return render_template("batch_review.html", articles=articles, batch_id=batch_id)


@bp.route("/batch/<batch_id>/publish", methods=["POST"])
def publish_batch(batch_id: str):
    """편집 내용 저장 + 시간차 발행 스케줄."""
    data = request.get_json(force=True)
    edits: list[dict] = data.get("articles", [])

    # 편집 내용 저장
    for edit in edits:
        article_id = edit.get("article_id")
        if not article_id:
            continue
        with db_conn() as con:
            con.execute(
                """INSERT INTO generated_content
                   (article_id, blogspot_title, blogspot_html, threads_text, edited)
                   VALUES (?,?,?,?,1)
                   ON CONFLICT(article_id) DO UPDATE SET
                     blogspot_title = excluded.blogspot_title,
                     blogspot_html  = excluded.blogspot_html,
                     threads_text   = excluded.threads_text,
                     edited         = 1,
                     generated_at   = datetime('now')""",
                (article_id,
                 edit.get("blogspot_title"),
                 edit.get("blogspot_html"),
                 edit.get("threads_text")),
            )

    # 발행 대상 조회 (generated / failed 모두 포함, 콘텐츠 있는 것만)
    with db_conn() as con:
        rows = con.execute(
            """SELECT a.id, a.title, a.notion_page_id,
                      gc.blogspot_title, gc.blogspot_html, gc.threads_text,
                      gc.seo_keyword, gc.image_urls
               FROM articles a
               JOIN generated_content gc ON gc.article_id = a.id
               WHERE a.batch_id = ?
                 AND a.status NOT IN ('published', 'publishing', 'dismissed')
               ORDER BY a.id ASC""",
            (batch_id,),
        ).fetchall()

    if not rows:
        return jsonify({"ok": False, "error": "발행할 기사가 없습니다."}), 404

    now = datetime.now()
    schedule = []
    for i, row in enumerate(rows):
        pub_time = now + timedelta(hours=i)
        schedule.append({
            "index": i + 1,
            "title": row["title"],
            "publish_at": "즉시" if i == 0 else pub_time.strftime("%H:%M"),
        })

        article_id = row["id"]
        notion_page_id = row["notion_page_id"]
        title = row["title"]
        result = {
            "blogspot": {
                "title": row["blogspot_title"],
                "body_html": row["blogspot_html"],
            },
            "threads": {
                "threads_text": row["threads_text"],
            },
            "seo_keyword": row["seo_keyword"] or "",
            "image_urls": json.loads(row["image_urls"] or "[]"),
        }

        if i == 0:
            threading.Thread(
                target=_publish_generated,
                args=[article_id, notion_page_id, title, result, 0],
                daemon=True,
            ).start()
        else:
            t = threading.Timer(
                i * 3600,
                _publish_generated,
                args=[article_id, notion_page_id, title, result, i],
            )
            t.daemon = True
            t.start()
            print(f"[batch] '{title[:30]}' → {i}시간 후 발행 예약")

    return jsonify({"ok": True, "scheduled": len(rows), "schedule": schedule})
