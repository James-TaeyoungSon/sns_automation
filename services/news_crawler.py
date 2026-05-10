# -*- coding: utf-8 -*-
"""
AI 뉴스 크롤러 — 5개 소스에서 최신 기사를 수집하고 SQLite에 저장.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from datetime import datetime

import feedparser
import requests

from database import db_conn

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ko,en;q=0.8",
}

# 뉴스 소스 — Top6 선별용
_NEWS_SOURCES = [
    # 공식 AI 기업 블로그
    {"name": "anthropic_news",  "url": "https://www.anthropic.com/rss.xml"},
    {"name": "openai_news",     "url": "https://openai.com/news/rss.xml"},
    {"name": "google_deepmind", "url": "https://deepmind.google/discover/blog/rss.xml"},
    {"name": "google_ai_blog",  "url": "https://blog.google/technology/ai/rss/"},
    {"name": "meta_ai",         "url": "https://ai.meta.com/blog/rss/"},
    # 테크 미디어
    {"name": "techcrunch_ai",   "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "mit_review",      "url": "https://www.technologyreview.com/feed/"},
    {"name": "hackernews",      "url": None},  # 별도 API
    {"name": "google_news_ko",  "url": "https://news.google.com/rss/search?q=AI+인공지능+LLM&hl=ko&gl=KR&ceid=KR:ko"},
    {"name": "google_news_en",  "url": "https://news.google.com/rss/search?q=generative+AI+LLM+ChatGPT&hl=en&gl=US&ceid=US:en"},
]

# 팁/노하우 소스 — Top4 선별용
_TIPS_SOURCES = [
    {"name": "reddit_chatgpt",       "url": "https://www.reddit.com/r/ChatGPT/.rss"},
    {"name": "reddit_claude",        "url": "https://www.reddit.com/r/ClaudeAI/.rss"},
    {"name": "reddit_prompteng",     "url": "https://www.reddit.com/r/PromptEngineering/.rss"},
    {"name": "reddit_localllama",    "url": "https://www.reddit.com/r/LocalLLaMA/.rss"},
    {"name": "towards_datascience",  "url": "https://towardsdatascience.com/feed"},
    {"name": "huggingface_blog",     "url": "https://huggingface.co/blog/feed.xml"},
]

# 하위 호환용 (crawl_all_sources에서 사용)
_RSS_SOURCES = [s for s in _NEWS_SOURCES if s["url"]] + _TIPS_SOURCES


def _url_hash(url: str) -> str:
    normalized = url.strip().lower().split("?")[0]
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def _parse_date(entry) -> str | None:
    for attr in ("published", "updated"):
        val = getattr(entry, attr, None)
        if val:
            try:
                t = time.strptime(val, "%a, %d %b %Y %H:%M:%S %z")
                return datetime(*t[:6]).isoformat()
            except Exception:
                return val[:50]
    return None


def _crawl_rss(source: dict, limit: int = 8) -> list[dict]:
    items = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries[:limit]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue
            items.append(
                {
                    "url": link,
                    "url_hash": _url_hash(link),
                    "title": title,
                    "source": source["name"],
                    "published_at": _parse_date(entry),
                }
            )
    except Exception as e:
        print(f"[news_crawler] RSS 오류 ({source['name']}): {e}")
    return items


def _crawl_hackernews(limit: int = 10) -> list[dict]:
    items = []
    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": "AI LLM machine learning", "tags": "story", "hitsPerPage": limit},
            headers=_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            title = hit.get("title", "").strip()
            if not title or not url:
                continue
            items.append(
                {
                    "url": url,
                    "url_hash": _url_hash(url),
                    "title": title,
                    "source": "hackernews",
                    "published_at": hit.get("created_at"),
                }
            )
    except Exception as e:
        print(f"[news_crawler] HackerNews 오류: {e}")
    return items


# ── 스코어링 ────────────────────────────────────────────────────────────────

_NEWS_SOURCE_SCORE = {
    "anthropic_news": 8, "openai_news": 8,
    "google_deepmind": 7, "google_ai_blog": 7, "meta_ai": 7,
    "techcrunch_ai": 5, "mit_review": 5,
    "hackernews": 4,
    "google_news_en": 3, "google_news_ko": 3,
}

_TIPS_SOURCE_SCORE = {
    "reddit_chatgpt": 5, "reddit_claude": 5,
    "reddit_prompteng": 4, "reddit_localllama": 4,
    "towards_datascience": 5, "huggingface_blog": 4,
}

_SOURCE_SCORE = {**_NEWS_SOURCE_SCORE, **_TIPS_SOURCE_SCORE}  # crawl_all_sources용

_HIGH_VALUE_KEYWORDS = [
    "gpt", "claude", "gemini", "llm", "openai", "anthropic", "mistral",
    "deepmind", "llama", "agent", "agi", "reasoning", "multimodal",
    "fine-tun", "rag", "benchmark", "트랜스포머", "생성형", "파운데이션",
]


def _score_item_with(item: dict, score_map: dict) -> float:
    score = float(score_map.get(item.get("source", ""), 1))
    title_lower = item.get("title", "").lower()
    for kw in _HIGH_VALUE_KEYWORDS:
        if kw in title_lower:
            score += 2
    pub = item.get("published_at", "") or ""
    if pub[:10] == datetime.now().strftime("%Y-%m-%d"):
        score += 1
    return score


def _score_item(item: dict) -> float:
    return _score_item_with(item, _SOURCE_SCORE)


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for item in items:
        h = item["url_hash"]
        if h not in seen:
            seen.add(h)
            result.append(item)
    return result


def crawl_and_rank(
    limit_per_source: int = 8,
    news_n: int = 6,
    tips_n: int = 4,
) -> list[dict]:
    """
    뉴스 소스 Top{news_n} + 팁/노하우 소스 Top{tips_n} 혼합 반환.
    DB 저장 안 함 — Telegram 다이제스트 발송 전용.
    """
    # 뉴스 크롤
    news_items: list[dict] = []
    for source in _NEWS_SOURCES:
        if source["url"]:
            news_items.extend(_crawl_rss(source, limit_per_source))
    news_items.extend(_crawl_hackernews(limit_per_source))
    news_items = _dedup(news_items)
    for item in news_items:
        item["score"] = _score_item_with(item, _NEWS_SOURCE_SCORE)
    news_items.sort(key=lambda x: x["score"], reverse=True)
    top_news = news_items[:news_n]

    # 팁/노하우 크롤
    tips_items: list[dict] = []
    for source in _TIPS_SOURCES:
        tips_items.extend(_crawl_rss(source, limit_per_source))
    tips_items = _dedup(tips_items)
    for item in tips_items:
        item["score"] = _score_item_with(item, _TIPS_SOURCE_SCORE)
    tips_items.sort(key=lambda x: x["score"], reverse=True)
    top_tips = tips_items[:tips_n]

    result = top_news + top_tips
    print(
        f"[news_crawler] 크롤 완료: 뉴스 {len(top_news)}개 + 팁 {len(top_tips)}개 = {len(result)}개"
    )
    return result


def crawl_all_sources(limit_per_source: int = 8) -> int:
    """모든 소스 크롤링 후 SQLite에 저장 → 신규 기사 수 반환 (수동 크롤용)."""
    all_items: list[dict] = []

    for source in _RSS_SOURCES:
        all_items.extend(_crawl_rss(source, limit_per_source))
    all_items.extend(_crawl_hackernews(limit_per_source))

    inserted = 0
    with db_conn() as con:
        for item in all_items:
            try:
                item["score"] = _score_item(item)
                con.execute(
                    """INSERT OR IGNORE INTO articles
                       (url, url_hash, title, source, published_at, score)
                       VALUES (:url, :url_hash, :title, :source, :published_at, :score)""",
                    item,
                )
                if con.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception as e:
                print(f"[news_crawler] DB 저장 실패 ({item.get('url')}): {e}")

    print(f"[news_crawler] 크롤 완료: {len(all_items)}개 처리, {inserted}개 신규 저장")
    return inserted
