# -*- coding: utf-8 -*-
import json
import threading
import uuid
from datetime import datetime

from flask import Blueprint, render_template, request, jsonify, redirect, url_for

from database import db_conn
from services.article_reader import fetch_article
from services.llm_generator import generate_pair
from services import blogger_client, threads_client, notion_store

bp = Blueprint("article", __name__)


# ── 잡 추적 (메모리 + DB) ────────────────────────────────────────────────────

def _new_job(article_id: int, job_type: str) -> str:
    job_id = uuid.uuid4().hex[:8]
    with db_conn() as con:
        con.execute(
            "INSERT INTO jobs (id, article_id, job_type, status, logs) VALUES (?,?,?,'running','[]')",
            (job_id, article_id, job_type),
        )
    return job_id


def _append_log(job_id: str, msg: str):
    with db_conn() as con:
        row = con.execute("SELECT logs FROM jobs WHERE id=?", (job_id,)).fetchone()
        logs = json.loads(row["logs"]) if row else []
        logs.append(msg)
        con.execute("UPDATE jobs SET logs=? WHERE id=?", (json.dumps(logs, ensure_ascii=False), job_id))


def _finish_job(job_id: str, status: str, result: dict | None = None):
    with db_conn() as con:
        con.execute(
            "UPDATE jobs SET status=?, result=? WHERE id=?",
            (status, json.dumps(result or {}, ensure_ascii=False), job_id),
        )


# ── 라우트 ───────────────────────────────────────────────────────────────────

@bp.route("/article/<int:article_id>")
def detail(article_id: int):
    with db_conn() as con:
        article = con.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not article:
            return "기사를 찾을 수 없습니다.", 404
        content = con.execute(
            "SELECT * FROM generated_content WHERE article_id=?", (article_id,)
        ).fetchone()
        post = con.execute(
            "SELECT * FROM posts WHERE article_id=? ORDER BY published_at DESC LIMIT 1",
            (article_id,),
        ).fetchone()

    return render_template(
        "article_detail.html",
        article=dict(article),
        content=dict(content) if content else None,
        post=dict(post) if post else None,
    )


@bp.route("/article/<int:article_id>/generate", methods=["POST"])
def generate(article_id: int):
    with db_conn() as con:
        article = con.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not article:
            return jsonify({"ok": False, "error": "기사 없음"}), 404
        con.execute("UPDATE articles SET status='generating' WHERE id=?", (article_id,))

    job_id = _new_job(article_id, "generate")

    def run():
        try:
            _append_log(job_id, "기사 본문 가져오는 중...")
            art = fetch_article(article["url"])
            _append_log(job_id, f"본문 {len(art.text)}자 추출 완료")

            result = generate_pair(
                article_url=article["url"],
                article_title=art.title or article["title"],
                article_text=art.text,
                log_fn=lambda msg: _append_log(job_id, msg),
            )

            with db_conn() as con:
                con.execute(
                    """INSERT INTO generated_content
                       (article_id, blogspot_title, blogspot_html, threads_text, seo_keyword, image_urls)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(article_id) DO UPDATE SET
                         blogspot_title=excluded.blogspot_title,
                         blogspot_html=excluded.blogspot_html,
                         threads_text=excluded.threads_text,
                         seo_keyword=excluded.seo_keyword,
                         image_urls=excluded.image_urls,
                         generated_at=datetime('now')""",
                    (
                        article_id,
                        result["blogspot"]["title"],
                        result["blogspot"]["body_html"],
                        result["threads"]["threads_text"],
                        result["seo_keyword"],
                        json.dumps(result["image_urls"], ensure_ascii=False),
                    ),
                )
                con.execute("UPDATE articles SET status='generated' WHERE id=?", (article_id,))
                notion_pid = con.execute(
                    "SELECT notion_page_id FROM articles WHERE id=?", (article_id,)
                ).fetchone()

            # Notion 동기화
            npid = notion_pid["notion_page_id"] if notion_pid else None
            if npid:
                image_url = result["image_urls"][0] if result["image_urls"] else None
                notion_store.save_content(
                    page_id=npid,
                    blogspot_title=result["blogspot"]["title"],
                    blogspot_html=result["blogspot"]["body_html"],
                    threads_text=result["threads"]["threads_text"],
                    seo_keyword=result["seo_keyword"],
                    image_url=image_url,
                )

            _finish_job(job_id, "done", {"seo_keyword": result["seo_keyword"]})
        except Exception as e:
            with db_conn() as con:
                con.execute(
                    "UPDATE articles SET status='failed', error_msg=? WHERE id=?",
                    (str(e), article_id),
                )
            _append_log(job_id, f"오류: {e}")
            _finish_job(job_id, "error")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/article/<int:article_id>/save-content", methods=["POST"])
def save_content(article_id: int):
    """편집된 콘텐츠 저장."""
    data = request.get_json(force=True)
    with db_conn() as con:
        con.execute(
            """INSERT INTO generated_content
               (article_id, blogspot_title, blogspot_html, threads_text)
               VALUES (?,?,?,?)
               ON CONFLICT(article_id) DO UPDATE SET
                 blogspot_title=excluded.blogspot_title,
                 blogspot_html=excluded.blogspot_html,
                 threads_text=excluded.threads_text,
                 generated_at=datetime('now'),
                 edited=1""",
            (article_id, data.get("blogspot_title"), data.get("blogspot_html"), data.get("threads_text")),
        )
    return jsonify({"ok": True})


@bp.route("/article/<int:article_id>/publish", methods=["POST"])
def publish(article_id: int):
    with db_conn() as con:
        article = con.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        content = con.execute(
            "SELECT * FROM generated_content WHERE article_id=?", (article_id,)
        ).fetchone()

    if not article or not content:
        return jsonify({"ok": False, "error": "기사 또는 콘텐츠 없음"}), 404

    notion_page_id = article["notion_page_id"] if "notion_page_id" in article.keys() else None
    job_id = _new_job(article_id, "publish")
    with db_conn() as con:
        con.execute("UPDATE articles SET status='publishing' WHERE id=?", (article_id,))

    def run():
        blogspot_ok = False
        blogspot_url = ""
        blogspot_post_id = ""
        threads_ok = False
        threads_post_id = ""
        error_msg = ""

        try:
            _append_log(job_id, "Blogspot 발행 중...")
            b_result = blogger_client.publish(
                title=content["blogspot_title"],
                body_html=content["blogspot_html"],
                labels=["AI", "인공지능", content["seo_keyword"] or ""],
            )
            blogspot_ok = b_result["ok"]
            blogspot_url = b_result.get("post_url", "")
            blogspot_post_id = b_result.get("post_id", "")

            if blogspot_ok:
                _append_log(job_id, f"Blogspot 발행 완료: {blogspot_url}")
            else:
                _append_log(job_id, f"Blogspot 발행 실패: {b_result.get('error')}")
                error_msg = b_result.get("error", "")

            # Threads: Blogspot URL 치환 후 발행
            threads_text = content["threads_text"]
            if blogspot_url and "BLOGSPOT_URL" in threads_text:
                threads_text = threads_text.replace("BLOGSPOT_URL", blogspot_url)
            elif blogspot_url and "BLOGSPOT_URL" not in threads_text:
                threads_text = threads_text.rstrip() + f"\n\n자세한 분석 → {blogspot_url}"

            _append_log(job_id, "Threads 발행 중...")
            try:
                threads_post_id = threads_client.post_text(
                    text=threads_text[:490],
                    link_url=blogspot_url or None,
                )
                threads_ok = True
                _append_log(job_id, f"Threads 발행 완료: {threads_post_id}")
            except Exception as e:
                _append_log(job_id, f"Threads 발행 실패: {e}")
                if not error_msg:
                    error_msg = str(e)

            # SQLite 기록
            with db_conn() as con:
                con.execute(
                    """INSERT INTO posts
                       (article_id, blogspot_post_id, blogspot_url, threads_post_id,
                        blogspot_ok, threads_ok, error_msg)
                       VALUES (?,?,?,?,?,?,?)""",
                    (article_id, blogspot_post_id, blogspot_url, threads_post_id,
                     int(blogspot_ok), int(threads_ok), error_msg),
                )
                new_status = "published" if (blogspot_ok or threads_ok) else "failed"
                con.execute(
                    "UPDATE articles SET status=?, error_msg=? WHERE id=?",
                    (new_status, error_msg or None, article_id),
                )

            # Notion 동기화
            if notion_page_id:
                notion_store.save_publish_result(
                    page_id=notion_page_id,
                    blogspot_url=blogspot_url,
                    threads_post_id=threads_post_id,
                    blogspot_ok=blogspot_ok,
                    threads_ok=threads_ok,
                    error_msg=error_msg or None,
                )

            _finish_job(job_id, "done", {
                "blogspot_ok": blogspot_ok,
                "blogspot_url": blogspot_url,
                "threads_ok": threads_ok,
                "threads_post_id": threads_post_id,
            })
        except Exception as e:
            with db_conn() as con:
                con.execute(
                    "UPDATE articles SET status='failed', error_msg=? WHERE id=?",
                    (str(e), article_id),
                )
            _append_log(job_id, f"오류: {e}")
            _finish_job(job_id, "error")

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/job/<job_id>")
def job_status(job_id: str):
    with db_conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return jsonify({"status": "not_found"}), 404
    return jsonify({
        "status": row["status"],
        "logs": json.loads(row["logs"] or "[]"),
        "result": json.loads(row["result"] or "{}"),
    })
