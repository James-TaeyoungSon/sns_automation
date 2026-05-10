# -*- coding: utf-8 -*-
"""APScheduler 작업 정의 + Telegram 확인 콜백 핸들러."""
from __future__ import annotations

import json
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import cfg
from database import db_conn
from services.news_crawler import crawl_and_rank, crawl_all_sources
from services.article_reader import fetch_article
from services.llm_generator import generate_pair
from services import blogger_client, threads_client
from services import telegram_service, notion_store


# ── Telegram 확인 콜백 ────────────────────────────────────────────────────────

def _process_selected_articles(articles: list[dict]) -> None:
    """
    Telegram에서 선택 확정된 기사 처리:
    1. 각 기사 → SQLite + Notion에 저장
    2. 각 기사 → 콘텐츠 생성 (article_reader + llm_generator)
    3. Notion 페이지 업데이트 + SQLite generated_content 저장
    """
    saved_ids = []
    for art in articles:
        url = art["url"]
        title = art["title"]
        source = art.get("source", "")
        url_hash = art["url_hash"]

        # SQLite 저장
        article_id: int | None = None
        notion_page_id: str | None = None

        try:
            with db_conn() as con:
                con.execute(
                    """INSERT OR IGNORE INTO articles
                       (url, url_hash, title, source, published_at, status, score)
                       VALUES (?,?,?,?,?,'new',?)""",
                    (url, url_hash, title, source,
                     art.get("published_at"), art.get("score", 0)),
                )
                row = con.execute(
                    "SELECT id, notion_page_id FROM articles WHERE url_hash=?",
                    (url_hash,),
                ).fetchone()
                if row:
                    article_id = row["id"]
                    notion_page_id = row["notion_page_id"]
        except Exception as e:
            print(f"[scheduler] SQLite 저장 실패 ({title[:30]}): {e}")
            continue

        # Notion 저장 (notion_page_id 미설정 시에만)
        if not notion_page_id:
            notion_page_id = notion_store.create_article(
                url=url, title=title, source=source,
                published_at=art.get("published_at"),
            )
            if notion_page_id and article_id:
                try:
                    with db_conn() as con:
                        con.execute(
                            "UPDATE articles SET notion_page_id=? WHERE id=?",
                            (notion_page_id, article_id),
                        )
                except Exception:
                    pass

        saved_ids.append((article_id, notion_page_id, url, title))

    count = len(saved_ids)
    telegram_service.send_message(
        f"✅ <b>{count}개 기사 저장 완료</b>\n콘텐츠 생성을 시작합니다 (기사당 약 1~2분)..."
    )

    # 각 기사 콘텐츠 생성
    for article_id, notion_page_id, url, title in saved_ids:
        _generate_for_article(article_id, notion_page_id, url, title)


def _generate_for_article(
    article_id: int | None,
    notion_page_id: str | None,
    url: str,
    title: str,
) -> None:
    """단일 기사 콘텐츠 생성 → SQLite + Notion 업데이트."""
    print(f"[scheduler] 콘텐츠 생성 시작: {title[:50]}")

    if article_id:
        with db_conn() as con:
            con.execute("UPDATE articles SET status='generating' WHERE id=?", (article_id,))
    if notion_page_id:
        notion_store.update_status(notion_page_id, "생성중")

    try:
        art = fetch_article(url)
        result = generate_pair(
            article_url=url,
            article_title=art.title or title,
            article_text=art.text,
            log_fn=lambda msg: print(f"  [{title[:20]}] {msg}"),
        )

        # SQLite 저장
        if article_id:
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
                con.execute(
                    "UPDATE articles SET status='generated' WHERE id=?", (article_id,)
                )

        # Notion 업데이트
        if notion_page_id:
            image_url = result["image_urls"][0] if result["image_urls"] else None
            notion_store.save_content(
                page_id=notion_page_id,
                blogspot_title=result["blogspot"]["title"],
                blogspot_html=result["blogspot"]["body_html"],
                threads_text=result["threads"]["threads_text"],
                seo_keyword=result["seo_keyword"],
                image_url=image_url,
            )

        telegram_service.send_message(
            f"✅ <b>생성 완료:</b> {result['blogspot']['title'][:60]}\n"
            f"웹 UI에서 확인 후 발행하세요."
        )
        print(f"[scheduler] 생성 완료: {title[:50]}")

    except Exception as e:
        print(f"[scheduler] 생성 실패 ({title[:30]}): {e}")
        if article_id:
            with db_conn() as con:
                con.execute(
                    "UPDATE articles SET status='failed', error_msg=? WHERE id=?",
                    (str(e), article_id),
                )
        if notion_page_id:
            notion_store.update_status(notion_page_id, "실패", str(e))
        telegram_service.send_message(f"❌ <b>생성 실패:</b> {title[:40]}\n{e}")


# ── APScheduler 잡 ────────────────────────────────────────────────────────────

def send_digest_job():
    """뉴스 크롤 → 스코어링 → Top10 → Telegram 다이제스트 발송."""
    if not cfg.DIGEST_ENABLED:
        return
    print(f"[scheduler] 다이제스트 발송 시작 ({datetime.now().isoformat()})")
    try:
        articles = crawl_and_rank(limit_per_source=8, top_n=10)
        if articles:
            telegram_service.send_digest(articles)
        else:
            print("[scheduler] 크롤된 기사 없음")
    except Exception as e:
        print(f"[scheduler] 다이제스트 잡 오류: {e}")


def crawl_news_job():
    """수동 크롤 또는 Telegram 미사용 환경용 — SQLite에 직접 저장."""
    print(f"[scheduler] 뉴스 크롤 시작 ({datetime.now().isoformat()})")
    try:
        inserted = crawl_all_sources()
        print(f"[scheduler] 크롤 완료: {inserted}개 신규")
    except Exception as e:
        print(f"[scheduler] 크롤 오류: {e}")


def auto_post_job():
    """가장 오래된 'reviewed' 기사를 자동 발행."""
    if not cfg.AUTO_POST_ENABLED:
        return

    print(f"[scheduler] 자동 발행 시작 ({datetime.now().isoformat()})")

    with db_conn() as con:
        article = con.execute(
            "SELECT * FROM articles WHERE status='reviewed' ORDER BY fetched_at ASC LIMIT 1"
        ).fetchone()

    if not article:
        print("[scheduler] 자동 발행할 기사 없음 (reviewed 상태 없음)")
        return

    article_id = article["id"]
    notion_page_id = article["notion_page_id"] if "notion_page_id" in article.keys() else None
    print(f"[scheduler] 기사 발행 시도: [{article_id}] {article['title'][:50]}")

    try:
        with db_conn() as con:
            content = con.execute(
                "SELECT * FROM generated_content WHERE article_id=?", (article_id,)
            ).fetchone()

        if not content:
            art = fetch_article(article["url"])
            result = generate_pair(
                article_url=article["url"],
                article_title=art.title or article["title"],
                article_text=art.text,
            )
            with db_conn() as con:
                con.execute(
                    """INSERT INTO generated_content
                       (article_id, blogspot_title, blogspot_html, threads_text, seo_keyword, image_urls)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        article_id, result["blogspot"]["title"],
                        result["blogspot"]["body_html"],
                        result["threads"]["threads_text"],
                        result["seo_keyword"],
                        json.dumps(result["image_urls"], ensure_ascii=False),
                    ),
                )
            content_data = {
                "blogspot_title": result["blogspot"]["title"],
                "blogspot_html": result["blogspot"]["body_html"],
                "threads_text": result["threads"]["threads_text"],
                "seo_keyword": result["seo_keyword"],
                "image_urls": json.dumps(result["image_urls"], ensure_ascii=False),
            }
        else:
            content_data = dict(content)

        b_result = blogger_client.publish(
            title=content_data["blogspot_title"],
            body_html=content_data["blogspot_html"],
            labels=["AI", "인공지능", content_data.get("seo_keyword") or ""],
        )
        blogspot_url = b_result.get("post_url", "")
        blogspot_ok = b_result["ok"]

        threads_text = content_data["threads_text"]
        if blogspot_url:
            threads_text = threads_text.replace("BLOGSPOT_URL", blogspot_url)
            if "BLOGSPOT_URL" not in content_data["threads_text"]:
                threads_text = threads_text.rstrip() + f"\n\n자세한 분석 → {blogspot_url}"

        threads_post_id = ""
        threads_ok = False
        try:
            threads_post_id = threads_client.post_text(threads_text[:490], link_url=blogspot_url or None)
            threads_ok = True
        except Exception as e:
            print(f"[scheduler] Threads 발행 실패: {e}")

        with db_conn() as con:
            con.execute(
                """INSERT INTO posts
                   (article_id, blogspot_post_id, blogspot_url, threads_post_id, blogspot_ok, threads_ok)
                   VALUES (?,?,?,?,?,?)""",
                (article_id, b_result.get("post_id", ""), blogspot_url,
                 threads_post_id, int(blogspot_ok), int(threads_ok)),
            )
            new_status = "published" if (blogspot_ok or threads_ok) else "failed"
            con.execute("UPDATE articles SET status=? WHERE id=?", (new_status, article_id))

        if notion_page_id:
            notion_store.save_publish_result(
                page_id=notion_page_id,
                blogspot_url=blogspot_url,
                threads_post_id=threads_post_id,
                blogspot_ok=blogspot_ok,
                threads_ok=threads_ok,
            )

        print(f"[scheduler] 자동 발행 완료: Blogspot={blogspot_ok}, Threads={threads_ok}")
    except Exception as e:
        print(f"[scheduler] 자동 발행 오류: {e}")
        with db_conn() as con:
            con.execute(
                "UPDATE articles SET status='failed', error_msg=? WHERE id=?",
                (str(e), article_id),
            )


# ── 스케줄러 생성 ─────────────────────────────────────────────────────────────

def create_scheduler() -> BackgroundScheduler:
    # Telegram 확인 콜백 등록
    telegram_service.set_confirm_callback(
        lambda articles: threading.Thread(
            target=_process_selected_articles,
            args=(articles,),
            daemon=True,
        ).start()
    )
    # Telegram 폴링 시작
    telegram_service.start_polling()

    scheduler = BackgroundScheduler(timezone="Asia/Seoul", misfire_grace_time=3600)

    # 다이제스트 발송 (Telegram 사용 시)
    scheduler.add_job(
        send_digest_job,
        trigger=IntervalTrigger(hours=cfg.CRAWL_INTERVAL_HOURS),
        id="send_digest",
        replace_existing=True,
    )

    if cfg.AUTO_POST_ENABLED:
        scheduler.add_job(
            auto_post_job,
            trigger=CronTrigger(hour=cfg.AUTO_POST_HOUR, minute=0, timezone="Asia/Seoul"),
            id="auto_post",
            replace_existing=True,
        )

    return scheduler
