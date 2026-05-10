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
from services import batch_store, notion_store

bp = Blueprint("batch", __name__)


@bp.route("/batch/review")
def review_from_notion():
    """Notion page ID 목록으로 배치 감수 페이지 구성 (Render 재시작에 강함)."""
    pages_param = request.args.get("pages", "")
    if not pages_param:
        return "Notion 페이지 ID가 없습니다.", 400

    notion_ids = [p.strip() for p in pages_param.split(",") if p.strip()]
    articles = []
    for notion_id in notion_ids:
        props = notion_store.get_article_props(notion_id)
        if not props:
            continue
        html = notion_store.get_page_html(notion_id)

        article_id = None
        try:
            with db_conn() as con:
                row = con.execute(
                    "SELECT id FROM articles WHERE notion_page_id=?", (notion_id,)
                ).fetchone()
                if row:
                    article_id = row["id"]
        except Exception:
            pass

        articles.append({
            "id": article_id,
            "notion_page_id": notion_id,
            "title": props.get("title", ""),
            "url": props.get("url", ""),
            "source": props.get("source", ""),
            "status": props.get("status", ""),
            "blogspot_title": props.get("blogspot_title", ""),
            "blogspot_html": html,
            "threads_text": props.get("threads_text", ""),
            "seo_keyword": props.get("seo_keyword", ""),
            "image_urls": [],
            "edited": 0,
        })

    if not articles:
        return "Notion에서 기사를 불러오지 못했습니다. Notion 설정을 확인하세요.", 404

    return render_template("batch_review.html", articles=articles, batch_id="notion")


@bp.route("/batch/notion/publish", methods=["POST"])
def publish_batch_notion():
    """Notion 기반 배치 발행 (SQLite 없이도 동작)."""
    data = request.get_json(force=True)
    edits: list[dict] = data.get("articles", [])

    if not edits:
        return jsonify({"ok": False, "error": "발행할 기사가 없습니다."}), 404

    now = datetime.now()
    schedule = []

    for i, edit in enumerate(edits):
        notion_page_id = edit.get("notion_page_id") or None
        article_id = edit.get("article_id") or None
        title = edit.get("blogspot_title") or f"기사 {i + 1}"

        # SQLite article_id 복구 시도
        if not article_id and notion_page_id:
            try:
                with db_conn() as con:
                    row = con.execute(
                        "SELECT id FROM articles WHERE notion_page_id=?", (notion_page_id,)
                    ).fetchone()
                    if row:
                        article_id = row["id"]
            except Exception:
                pass

        # 편집 내용 SQLite 저장
        if article_id:
            try:
                with db_conn() as con:
                    con.execute(
                        """INSERT INTO generated_content
                           (article_id, blogspot_title, blogspot_html, threads_text, edited)
                           VALUES (?,?,?,?,1)
                           ON CONFLICT(article_id) DO UPDATE SET
                             blogspot_title=excluded.blogspot_title,
                             blogspot_html=excluded.blogspot_html,
                             threads_text=excluded.threads_text,
                             edited=1, generated_at=datetime('now')""",
                        (article_id, edit.get("blogspot_title"),
                         edit.get("blogspot_html"), edit.get("threads_text")),
                    )
            except Exception as e:
                print(f"[batch/notion] SQLite 저장 실패 (무시): {e}")

        result = {
            "blogspot": {
                "title": edit.get("blogspot_title", ""),
                "body_html": edit.get("blogspot_html", ""),
            },
            "threads": {"threads_text": edit.get("threads_text", "")},
            "seo_keyword": edit.get("seo_keyword", ""),
            "image_urls": [],
        }

        pub_time = now + timedelta(hours=i)
        schedule.append({
            "index": i + 1,
            "title": title[:50],
            "publish_at": "즉시" if i == 0 else pub_time.strftime("%H:%M"),
        })

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

    return jsonify({"ok": True, "scheduled": len(edits), "schedule": schedule})


@bp.route("/batch/<batch_id>")
def review(batch_id: str):
    # 1차: SQLite 조회
    articles = None
    try:
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
        if rows:
            articles = [dict(r) for r in rows]
    except Exception as e:
        print(f"[batch] SQLite 조회 실패: {e}")

    # 2차: 메모리 캐시 fallback
    if not articles:
        cached = batch_store.load(batch_id)
        if cached:
            articles = cached

    if not articles:
        return "배치를 찾을 수 없습니다. 텔레그램 링크를 다시 확인하세요.", 404

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
