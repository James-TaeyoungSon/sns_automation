# -*- coding: utf-8 -*-
"""APScheduler 작업 정의 + Telegram 확인 콜백 핸들러."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import cfg
from database import db_conn
from services.news_crawler import crawl_and_rank, crawl_all_sources
from services.article_reader import fetch_article
from services.llm_generator import generate_pair
from services import blogger_client, threads_client
from services import telegram_service, notion_store, batch_store


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────

def _save_article_to_db(art: dict) -> tuple[int | None, str | None]:
    """기사를 SQLite + Notion에 저장 → (article_id, notion_page_id) 반환."""
    url, title = art["url"], art["title"]
    source = art.get("source", "")
    url_hash = art["url_hash"]
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
                "SELECT id, notion_page_id FROM articles WHERE url_hash=?", (url_hash,)
            ).fetchone()
            if row:
                article_id = row["id"]
                notion_page_id = row["notion_page_id"]
    except Exception as e:
        print(f"[scheduler] SQLite 저장 실패 ({title[:30]}): {e}")
        return None, None

    if not notion_page_id:
        try:
            notion_page_id = notion_store.create_article(
                url=url, title=title, source=source,
                published_at=art.get("published_at"),
            )
        except Exception as e:
            print(f"[scheduler] Notion create_article 실패 ({title[:30]}): {e}")
            notion_page_id = None
        if notion_page_id and article_id:
            with db_conn() as con:
                con.execute(
                    "UPDATE articles SET notion_page_id=? WHERE id=?",
                    (notion_page_id, article_id),
                )
    return article_id, notion_page_id


def _generate_content(
    article_id: int | None,
    notion_page_id: str | None,
    url: str,
    title: str,
) -> dict | None:
    """콘텐츠 생성(fetch + LLM) → SQLite + Notion 저장 → result dict 반환. 실패 시 None."""
    print(f"[scheduler] 생성 시작: {title[:50]}")
    if article_id:
        with db_conn() as con:
            con.execute("UPDATE articles SET status='generating' WHERE id=?", (article_id,))
    if notion_page_id:
        notion_store.update_status(notion_page_id, "생성중")

    try:
        try:
            art = fetch_article(url)
            article_title = art.title or title
            article_text = art.text
        except Exception as fetch_err:
            print(f"[scheduler] fetch 실패 ({url[:60]}): {fetch_err} — 제목만으로 생성")
            article_title = title
            article_text = ""

        result = generate_pair(
            article_url=url,
            article_title=article_title,
            article_text=article_text,
            log_fn=lambda msg: print(f"  [{title[:20]}] {msg}"),
        )

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
                    (article_id, result["blogspot"]["title"],
                     result["blogspot"]["body_html"],
                     result["threads"]["threads_text"],
                     result["seo_keyword"],
                     json.dumps(result["image_urls"], ensure_ascii=False)),
                )
                con.execute("UPDATE articles SET status='generated' WHERE id=?", (article_id,))

        if notion_page_id:
            notion_store.save_content(
                page_id=notion_page_id,
                blogspot_title=result["blogspot"]["title"],
                blogspot_html=result["blogspot"]["body_html"],
                threads_text=result["threads"]["threads_text"],
                seo_keyword=result["seo_keyword"],
                image_url=result["image_urls"][0] if result["image_urls"] else None,
            )

        print(f"[scheduler] 생성 완료: {title[:50]}")
        return result

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
        telegram_service.send_message(
            f"❌ <b>생성 실패:</b> {title[:40]}\n<code>{str(e)[:200]}</code>"
        )
        return None


def _publish_generated(
    article_id: int | None,
    notion_page_id: str | None,
    title: str,
    result: dict,
    publish_index: int = 0,
) -> None:
    """생성된 콘텐츠를 Blogspot + Threads에 발행."""
    print(f"[scheduler] 발행 시작 (#{publish_index + 1}): {title[:50]}")
    if article_id:
        with db_conn() as con:
            con.execute("UPDATE articles SET status='publishing' WHERE id=?", (article_id,))

    b_result = blogger_client.publish(
        title=result["blogspot"]["title"],
        body_html=result["blogspot"]["body_html"],
        labels=["AI", "인공지능", result["seo_keyword"] or ""],
    )
    blogspot_url = b_result.get("post_url", "")
    blogspot_ok = b_result["ok"]

    threads_text = result["threads"]["threads_text"]
    if blogspot_url:
        threads_text = threads_text.replace("BLOGSPOT_URL", blogspot_url)
    elif "BLOGSPOT_URL" in threads_text:
        threads_text = threads_text.replace("BLOGSPOT_URL", result.get("article_url", ""))

    threads_post_id = ""
    threads_ok = False
    try:
        threads_post_id = threads_client.post_text(threads_text[:490], link_url=blogspot_url or None)
        threads_ok = True
    except Exception as e:
        print(f"[scheduler] Threads 발행 실패: {e}")

    new_status = "published" if (blogspot_ok or threads_ok) else "failed"
    if article_id:
        with db_conn() as con:
            con.execute(
                """INSERT INTO posts
                   (article_id, blogspot_post_id, blogspot_url, threads_post_id, blogspot_ok, threads_ok)
                   VALUES (?,?,?,?,?,?)""",
                (article_id, b_result.get("post_id", ""), blogspot_url,
                 threads_post_id, int(blogspot_ok), int(threads_ok)),
            )
            con.execute("UPDATE articles SET status=? WHERE id=?", (new_status, article_id))

    if notion_page_id:
        notion_store.save_publish_result(
            page_id=notion_page_id,
            blogspot_url=blogspot_url,
            threads_post_id=threads_post_id,
            blogspot_ok=blogspot_ok,
            threads_ok=threads_ok,
        )

    lines = [f"{'🚀' if publish_index == 0 else '⏰'} <b>발행 완료 (#{publish_index + 1}):</b> {result['blogspot']['title'][:50]}"]
    if blogspot_ok:
        lines.append(f"📝 Blogspot: {blogspot_url}")
    else:
        lines.append(f"❌ Blogspot 실패: <code>{str(b_result.get('error', '알 수 없는 오류'))[:150]}</code>")
    if threads_ok:
        lines.append(f"🧵 Threads 발행됨")
    else:
        lines.append(f"❌ Threads 실패")
    telegram_service.send_message("\n".join(lines))


# ── Telegram 확인 콜백 ────────────────────────────────────────────────────────

def _process_selected_articles(articles: list[dict]) -> None:
    """
    Telegram 선택 확정 → 전체 생성 → 웹 감수 링크 전송.
    실제 발행은 /batch/<batch_id> 페이지에서 사용자가 확인 후 실행.
    """
    batch_id = uuid.uuid4().hex[:12]
    count = len(articles)
    telegram_service.send_message(
        f"⚙️ <b>{count}개 기사 생성 시작</b>\n"
        f"콘텐츠 생성 중... (기사당 약 1~2분)"
    )

    generated: list[dict] = []  # 캐시용 결과 목록
    for art in articles:
        article_id, notion_page_id = _save_article_to_db(art)
        if article_id is None:
            continue
        with db_conn() as con:
            con.execute("UPDATE articles SET batch_id=? WHERE id=?", (batch_id, article_id))
        result = _generate_content(article_id, notion_page_id, art["url"], art["title"])
        if result:
            generated.append({
                "id": article_id,           # SQLite row와 키 이름 통일
                "notion_page_id": notion_page_id,
                "title": art["title"],
                "url": art["url"],
                "source": art.get("source", ""),
                "status": "generated",
                "blogspot_title": result["blogspot"]["title"],
                "blogspot_html": result["blogspot"]["body_html"],
                "threads_text": result["threads"]["threads_text"],
                "seo_keyword": result["seo_keyword"],
                "image_urls": result["image_urls"],
                "edited": 0,
            })

    if not generated:
        telegram_service.send_message("❌ 생성된 콘텐츠가 없습니다.")
        return

    # 메모리 캐시에 저장 (SQLite fallback용)
    batch_store.save(batch_id, generated)

    review_url = f"{cfg.APP_BASE_URL}/batch/{batch_id}"
    telegram_service.send_message(
        f"✅ <b>{len(generated)}개 생성 완료!</b>\n\n"
        f"아래 링크에서 내용을 확인·수정 후 발행하세요:\n"
        f"{review_url}"
    )


# ── 수동 URL 포스팅 ───────────────────────────────────────────────────────────

def _process_manual_url(url: str) -> None:
    """
    Telegram에서 URL 직접 전송 시 호출.
    기사 fetch → 생성 → Blogspot + Threads 발행 → Notion 저장까지 원스톱.
    """
    print(f"[scheduler] 수동 URL 처리: {url}")
    url_hash = hashlib.sha256(url.strip().lower().split("?")[0].encode()).hexdigest()[:32]

    # SQLite 저장
    article_id: int | None = None
    notion_page_id: str | None = None
    title = url  # 일단 URL로 초기화, fetch 후 교체

    try:
        try:
            art = fetch_article(url)
            title = art.title or url
            manual_text = art.text
        except Exception as fetch_err:
            print(f"[scheduler] 수동 fetch 실패 ({url[:60]}): {fetch_err} — 제목만으로 생성")
            title = url
            manual_text = ""

        with db_conn() as con:
            con.execute(
                """INSERT OR IGNORE INTO articles
                   (url, url_hash, title, source, status)
                   VALUES (?,?,?,'manual','generating')""",
                (url, url_hash, title),
            )
            row = con.execute(
                "SELECT id, notion_page_id FROM articles WHERE url_hash=?", (url_hash,)
            ).fetchone()
            if row:
                article_id = row["id"]
                notion_page_id = row["notion_page_id"]

        # Notion 저장
        if not notion_page_id:
            notion_page_id = notion_store.create_article(
                url=url, title=title, source="manual",
            )
            if notion_page_id and article_id:
                with db_conn() as con:
                    con.execute(
                        "UPDATE articles SET notion_page_id=?, title=? WHERE id=?",
                        (notion_page_id, title, article_id),
                    )

        telegram_service.send_message(f"📰 <b>{title[:60]}</b>\n\n콘텐츠 생성 중...")

        result = generate_pair(
            article_url=url,
            article_title=title,
            article_text=manual_text,
            log_fn=lambda msg: print(f"  [수동] {msg}"),
        )

        # SQLite generated_content 저장
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
                         image_urls=excluded.image_urls""",
                    (
                        article_id,
                        result["blogspot"]["title"],
                        result["blogspot"]["body_html"],
                        result["threads"]["threads_text"],
                        result["seo_keyword"],
                        json.dumps(result["image_urls"], ensure_ascii=False),
                    ),
                )

        # Blogspot 발행
        telegram_service.send_message("🚀 Blogspot 발행 중...")
        b_result = blogger_client.publish(
            title=result["blogspot"]["title"],
            body_html=result["blogspot"]["body_html"],
            labels=["AI", "인공지능", result["seo_keyword"] or ""],
        )
        blogspot_url = b_result.get("post_url", "")
        blogspot_ok = b_result["ok"]

        # Threads 발행
        threads_text = result["threads"]["threads_text"]
        if blogspot_url:
            threads_text = threads_text.replace("BLOGSPOT_URL", blogspot_url)
        if not blogspot_ok:
            threads_text = threads_text.replace("BLOGSPOT_URL", url)

        threads_post_id = ""
        threads_ok = False
        try:
            threads_post_id = threads_client.post_text(threads_text[:490], link_url=blogspot_url or None)
            threads_ok = True
        except Exception as e:
            print(f"[scheduler] 수동 Threads 발행 실패: {e}")

        # DB 업데이트
        new_status = "published" if (blogspot_ok or threads_ok) else "failed"
        if article_id:
            with db_conn() as con:
                con.execute(
                    """INSERT INTO posts
                       (article_id, blogspot_post_id, blogspot_url, threads_post_id, blogspot_ok, threads_ok)
                       VALUES (?,?,?,?,?,?)""",
                    (article_id, b_result.get("post_id",""), blogspot_url,
                     threads_post_id, int(blogspot_ok), int(threads_ok)),
                )
                con.execute("UPDATE articles SET status=? WHERE id=?", (new_status, article_id))

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
            notion_store.save_publish_result(
                page_id=notion_page_id,
                blogspot_url=blogspot_url,
                threads_post_id=threads_post_id,
                blogspot_ok=blogspot_ok,
                threads_ok=threads_ok,
            )

        # 결과 알림
        lines = ["✅ <b>포스팅 완료!</b>"]
        if blogspot_ok:
            lines.append(f"📝 Blogspot: {blogspot_url}")
        else:
            lines.append(f"❌ Blogspot 실패: {b_result.get('error','')}")
        if threads_ok:
            lines.append(f"🧵 Threads: 발행 완료 (ID: {threads_post_id})")
        else:
            lines.append("❌ Threads 실패")
        telegram_service.send_message("\n".join(lines))

    except Exception as e:
        print(f"[scheduler] 수동 URL 처리 오류: {e}")
        telegram_service.send_message(f"❌ <b>오류 발생:</b> {e}")
        if article_id:
            with db_conn() as con:
                con.execute(
                    "UPDATE articles SET status='failed', error_msg=? WHERE id=?",
                    (str(e), article_id),
                )


# ── APScheduler 잡 ────────────────────────────────────────────────────────────

def send_digest_job():
    """뉴스 크롤 → 스코어링 → Top10 → Telegram 다이제스트 발송."""
    if not cfg.DIGEST_ENABLED:
        return
    print(f"[scheduler] 다이제스트 발송 시작 ({datetime.now().isoformat()})")
    try:
        articles = crawl_and_rank(limit_per_source=8, news_n=6, tips_n=4)
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
    # Telegram 콜백 등록
    telegram_service.set_confirm_callback(
        lambda articles: threading.Thread(
            target=_process_selected_articles,
            args=(articles,),
            daemon=True,
        ).start()
    )
    telegram_service.set_url_callback(
        lambda url: threading.Thread(
            target=_process_manual_url,
            args=(url,),
            daemon=True,
        ).start()
    )
    telegram_service.set_recommend_callback(
        lambda: threading.Thread(
            target=send_digest_job,
            daemon=True,
        ).start()
    )
    # Telegram 폴링 시작
    telegram_service.start_polling()

    scheduler = BackgroundScheduler(timezone="Asia/Seoul", misfire_grace_time=3600)

    # 매일 08:00 KST 다이제스트 발송
    scheduler.add_job(
        send_digest_job,
        trigger=CronTrigger(hour=cfg.DIGEST_HOUR, minute=cfg.DIGEST_MINUTE, timezone="Asia/Seoul"),
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
