# -*- coding: utf-8 -*-
"""
Notion DB 연동 — 기사/생성 콘텐츠/발행 결과를 Notion 페이지로 저장.
DB ID: config.cfg.NOTION_ARTICLES_DB_ID
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import requests

from config import cfg

_BASE = "https://api.notion.com/v1"
_RATE_DELAY = 0.4  # 초 (Notion API 3req/s 제한 여유)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {cfg.NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _is_configured() -> bool:
    return bool(cfg.NOTION_API_KEY and cfg.NOTION_ARTICLES_DB_ID)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _rich_text(content: str) -> list[dict]:
    """긴 텍스트를 2000자 단위로 분할해 rich_text 배열 생성."""
    blocks = []
    while content:
        chunk = content[:2000]
        content = content[2000:]
        blocks.append({"type": "text", "text": {"content": chunk}})
    return blocks or [{"type": "text", "text": {"content": ""}}]


def _page_request(method: str, path: str, body: dict | None = None) -> dict | None:
    fn = getattr(requests, method)
    kwargs: dict[str, Any] = {"headers": _headers(), "timeout": 30}
    if body is not None:
        kwargs["data"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        resp = fn(f"{_BASE}{path}", **kwargs)
        resp.raise_for_status()
        time.sleep(_RATE_DELAY)
        return resp.json()
    except Exception as e:
        print(f"[notion_store] API 오류 ({method.upper()} {path}): {e}")
        return None


def _append_page_blocks(page_id: str, blocks: list[dict]) -> None:
    """페이지에 블록 추가 (기존 블록은 유지)."""
    _page_request("patch", f"/blocks/{page_id}/children", {"children": blocks})


# ── 기사 생성 ─────────────────────────────────────────────────────────────────

def create_article(
    url: str,
    title: str,
    source: str,
    published_at: str | None = None,
) -> str | None:
    """
    Notion DB에 기사 페이지 생성 → page_id 반환.
    실패 시 None 반환.
    """
    if not _is_configured():
        return None

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    props: dict[str, Any] = {
        "이름": {"title": _rich_text(title[:2000])},
        "URL": {"url": url},
        "Status": {"select": {"name": "신규"}},
        "소스": {"select": {"name": source}},
        "수집일시": {"date": {"start": now_iso}},
    }
    if published_at:
        try:
            # ISO 포맷 정규화
            props["Published At"] = {"date": {"start": published_at[:19] + "+00:00"}}
        except Exception:
            pass

    body = {
        "parent": {"database_id": cfg.NOTION_ARTICLES_DB_ID},
        "properties": props,
    }
    result = _page_request("post", "/pages", body)
    if result:
        page_id = result.get("id", "")
        print(f"[notion_store] 페이지 생성: {page_id[:8]}... | {title[:40]}")
        return page_id
    return None


# ── 상태 업데이트 ─────────────────────────────────────────────────────────────

def update_status(page_id: str, status: str, error_msg: str | None = None) -> bool:
    """기사 Status 업데이트."""
    if not _is_configured() or not page_id:
        return False
    props: dict[str, Any] = {"Status": {"select": {"name": status}}}
    if error_msg:
        props["Last Error"] = {"rich_text": _rich_text(error_msg[:2000])}
    result = _page_request("patch", f"/pages/{page_id}", {"properties": props})
    return result is not None


# ── 생성 콘텐츠 저장 ──────────────────────────────────────────────────────────

def save_content(
    page_id: str,
    blogspot_title: str,
    blogspot_html: str,
    threads_text: str,
    seo_keyword: str,
    image_url: str | None = None,
) -> bool:
    """
    생성된 블로그/Threads 콘텐츠를 Notion 페이지에 저장.
    - 메타데이터: 프로퍼티
    - 블로그 HTML: 페이지 본문 코드 블록 (길이 제한 없음)
    """
    if not _is_configured() or not page_id:
        return False

    props: dict[str, Any] = {
        "Status": {"select": {"name": "생성완료"}},
        "블로그 제목": {"rich_text": _rich_text(blogspot_title[:2000])},
        "SEO 키워드": {"rich_text": _rich_text(seo_keyword[:500])},
        "Threads Post": {"rich_text": _rich_text(threads_text[:2000])},
    }
    if image_url:
        props["이미지 URL"] = {"url": image_url}

    result = _page_request("patch", f"/pages/{page_id}", {"properties": props})
    if not result:
        return False

    # 블로그 HTML을 페이지 본문에 저장 (2000자씩 코드 블록 분할)
    blocks = [
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📝 Blogspot HTML"}}]}},
    ]
    html = blogspot_html
    while html:
        chunk = html[:2000]
        html = html[2000:]
        blocks.append({
            "object": "block",
            "type": "code",
            "code": {
                "language": "html",
                "rich_text": [{"type": "text", "text": {"content": chunk}}],
            },
        })
    _append_page_blocks(page_id, blocks)
    return True


# ── 발행 결과 저장 ────────────────────────────────────────────────────────────

def save_publish_result(
    page_id: str,
    blogspot_url: str,
    threads_post_id: str,
    blogspot_ok: bool,
    threads_ok: bool,
    error_msg: str | None = None,
) -> bool:
    """발행 결과(URL, post ID, 상태)를 Notion 페이지에 업데이트."""
    if not _is_configured() or not page_id:
        return False

    status = "발행완료" if (blogspot_ok or threads_ok) else "실패"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    props: dict[str, Any] = {
        "Status": {"select": {"name": status}},
        "Published At": {"date": {"start": now_iso}},
    }
    if blogspot_url:
        props["블로그 URL"] = {"url": blogspot_url}
    if threads_post_id:
        props["Thread Post ID"] = {"rich_text": _rich_text(threads_post_id)}
    if error_msg:
        props["Last Error"] = {"rich_text": _rich_text(error_msg[:2000])}

    result = _page_request("patch", f"/pages/{page_id}", {"properties": props})
    return result is not None


# ── 조회 ─────────────────────────────────────────────────────────────────────

def get_page_html(page_id: str) -> str:
    """페이지 본문 코드 블록에서 Blogspot HTML 복원."""
    if not _is_configured() or not page_id:
        return ""
    result = _page_request("get", f"/blocks/{page_id}/children")
    if not result:
        return ""
    parts = []
    for block in result.get("results", []):
        if block.get("type") == "code":
            for rt in block["code"].get("rich_text", []):
                parts.append(rt.get("plain_text", ""))
    return "".join(parts)


def get_article_props(page_id: str) -> dict:
    """Notion 페이지 프로퍼티를 딕셔너리로 반환."""
    if not _is_configured() or not page_id:
        return {}
    result = _page_request("get", f"/pages/{page_id}")
    if not result:
        return {}
    props = result.get("properties", {})
    out: dict = {"page_id": page_id}

    def _get_rt(key: str) -> str:
        items = props.get(key, {}).get("rich_text", [])
        return "".join(i.get("plain_text", "") for i in items)

    def _get_title(key: str = "이름") -> str:
        items = props.get(key, {}).get("title", [])
        return "".join(i.get("plain_text", "") for i in items)

    out["title"] = _get_title()
    out["url"] = props.get("URL", {}).get("url", "")
    out["status"] = props.get("Status", {}).get("select", {}).get("name", "")
    out["source"] = props.get("소스", {}).get("select", {}).get("name", "")
    out["seo_keyword"] = _get_rt("SEO 키워드")
    out["blogspot_title"] = _get_rt("블로그 제목")
    out["blogspot_url"] = props.get("블로그 URL", {}).get("url", "")
    out["image_url"] = props.get("이미지 URL", {}).get("url", "")
    out["threads_text"] = _get_rt("Threads Post")
    out["threads_post_id"] = _get_rt("Thread Post ID")
    out["last_error"] = _get_rt("Last Error")
    return out
