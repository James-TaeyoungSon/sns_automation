# -*- coding: utf-8 -*-
"""
Telegram Bot 서비스.
- 크롤된 Top10 기사 다이제스트를 인라인 버튼으로 발송
- 사용자가 버튼으로 기사 선택 → 확인 시 SQLite + Notion에 저장 + 콘텐츠 생성 시작
- 단일 사용자 봇 (TELEGRAM_CHAT_ID 설정된 계정만 응답)
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime

import requests

from config import cfg

_API = "https://api.telegram.org/bot"
_sessions: dict[int, dict] = {}  # msg_id → session
_offset: int = 0
_stop_event = threading.Event()
_poll_thread: threading.Thread | None = None

# 콜백 등록
_on_confirm_cb = None   # 다이제스트 선택 확정 시
_on_url_cb = None       # 수동 URL 전송 시
_on_recommend_cb = None # "추천" 키워드 → 즉시 크롤+다이제스트 발송


def set_confirm_callback(cb) -> None:
    global _on_confirm_cb
    _on_confirm_cb = cb


def set_url_callback(cb) -> None:
    """URL 수동 전송 콜백 등록. cb(url: str) 형태."""
    global _on_url_cb
    _on_url_cb = cb


def set_recommend_callback(cb) -> None:
    """'추천' 키워드 콜백 등록. cb() 형태."""
    global _on_recommend_cb
    _on_recommend_cb = cb


def _is_configured() -> bool:
    return bool(cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID)


def _api(method: str, payload: dict) -> dict | None:
    if not _is_configured():
        return None
    try:
        resp = requests.post(
            f"{_API}{cfg.TELEGRAM_BOT_TOKEN}/{method}",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[telegram] API 오류 ({method}): {e}")
        return None


# ── 메시지 전송 / 편집 ────────────────────────────────────────────────────────

def send_message(text: str, reply_markup: dict | None = None) -> int | None:
    """메시지 발송 → message_id 반환."""
    payload: dict = {
        "chat_id": cfg.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _api("sendMessage", payload)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def edit_message(msg_id: int, text: str, reply_markup: dict | None = None) -> bool:
    """기존 메시지 편집."""
    payload: dict = {
        "chat_id": cfg.TELEGRAM_CHAT_ID,
        "message_id": msg_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _api("editMessageText", payload)
    return bool(result and result.get("ok"))


def answer_callback(callback_id: str, text: str = "") -> None:
    _api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


# ── 인라인 키보드 빌드 ────────────────────────────────────────────────────────

def _build_keyboard(msg_id: int, articles: list[dict], selected: set[int]) -> dict:
    rows = []
    for i, art in enumerate(articles):
        mark = "✅" if i in selected else "⬜"
        label = f"{mark} {art['title'][:35]}{'…' if len(art['title']) > 35 else ''}"
        rows.append([{"text": label, "callback_data": f"t:{msg_id}:{i}"}])

    sel_count = len(selected)
    rows.append([{
        "text": f"📌 확인 ({sel_count}개 선택)" if sel_count else "📌 확인",
        "callback_data": f"c:{msg_id}",
    }])
    return {"inline_keyboard": rows}


def _build_digest_text(articles: list[dict], selected: set[int]) -> str:
    now = datetime.now().strftime("%m/%d %H:%M")
    lines = [f"<b>🤖 AI 뉴스 다이제스트 ({now})</b>", "발행할 기사를 선택하세요.\n"]
    for i, art in enumerate(articles):
        mark = "✅" if i in selected else "◻️"
        src = art.get("source", "")
        lines.append(f"{mark} <b>{i+1}.</b> {art['title'][:60]}")
        if src:
            lines.append(f"   <i>({src})</i>")
    lines.append("\n선택 후 📌 확인 버튼을 누르세요.")
    return "\n".join(lines)


# ── 다이제스트 발송 ───────────────────────────────────────────────────────────

def send_digest(articles: list[dict]) -> int | None:
    """
    Top 10 기사를 인라인 버튼으로 발송.
    articles: [{"url", "title", "source", "score", "published_at"}, ...]
    반환: message_id (폴링에서 세션 매핑에 사용)
    """
    if not _is_configured():
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정, 다이제스트 발송 생략")
        return None

    if not articles:
        print("[telegram] 발송할 기사 없음")
        return None

    text = _build_digest_text(articles, set())
    # 임시 msg_id 0으로 keyboard 생성 후 실제 msg_id로 교체
    msg_id = send_message(text, _build_keyboard(0, articles, set()))
    if msg_id is None:
        return None

    # 실제 msg_id로 세션 저장 + keyboard 재발송
    _sessions[msg_id] = {
        "chat_id": cfg.TELEGRAM_CHAT_ID,
        "articles": articles,
        "selected": set(),
    }
    # keyboard callback_data를 실제 msg_id로 업데이트
    edit_message(msg_id, text, _build_keyboard(msg_id, articles, set()))
    print(f"[telegram] 다이제스트 발송 완료 (msg_id={msg_id}, {len(articles)}개 기사)")
    return msg_id


# ── 콜백 처리 ────────────────────────────────────────────────────────────────

def _handle_callback(update: dict) -> None:
    cq = update.get("callback_query", {})
    if not cq:
        return

    callback_id = cq["id"]
    data = cq.get("data", "")
    from_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))

    # 인증된 채팅만 처리
    if from_id != str(cfg.TELEGRAM_CHAT_ID):
        answer_callback(callback_id, "접근 권한이 없습니다.")
        return

    parts = data.split(":")
    if len(parts) < 2:
        return

    action = parts[0]
    try:
        msg_id = int(parts[1])
    except ValueError:
        return

    session = _sessions.get(msg_id)
    if not session:
        answer_callback(callback_id, "세션이 만료되었습니다. 새 다이제스트를 요청하세요.")
        return

    articles = session["articles"]
    selected: set[int] = session["selected"]

    if action == "t" and len(parts) == 3:
        idx = int(parts[2])
        if idx in selected:
            selected.discard(idx)
        else:
            selected.add(idx)
        text = _build_digest_text(articles, selected)
        edit_message(msg_id, text, _build_keyboard(msg_id, articles, selected))
        answer_callback(callback_id)

    elif action == "c":
        if not selected:
            answer_callback(callback_id, "선택된 기사가 없습니다.")
            return

        chosen = [articles[i] for i in sorted(selected)]
        answer_callback(callback_id, f"{len(chosen)}개 기사 처리 시작...")
        send_message(f"✅ <b>{len(chosen)}개 기사</b>를 저장하고 콘텐츠 생성을 시작합니다.\n잠시 기다려 주세요...")

        # 세션 정리
        del _sessions[msg_id]

        # 확인 콜백 실행 (별도 스레드)
        if _on_confirm_cb:
            threading.Thread(
                target=_on_confirm_cb,
                args=(chosen,),
                daemon=True,
            ).start()


# ── 메시지 처리 ───────────────────────────────────────────────────────────────

import re as _re
_URL_RE = _re.compile(r"https?://\S+")
_RECOMMEND_KEYWORDS = {"추천", "기사추천", "뉴스", "뉴스추천", "크롤", "크롤링"}


def _handle_message(update: dict) -> None:
    """일반 텍스트 메시지 처리.
    - '추천' 등 키워드 → 즉시 크롤링 + 다이제스트 발송
    - URL 포함 → 수동 포스팅 파이프라인
    """
    msg = update.get("message", {})
    if not msg:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(cfg.TELEGRAM_CHAT_ID):
        return

    text = msg.get("text", "").strip()

    # 추천 키워드 감지 (공백 제거 후 비교)
    normalized = text.replace(" ", "").lower()
    if normalized in _RECOMMEND_KEYWORDS:
        send_message("🔍 지금 바로 AI 뉴스를 크롤링합니다...\n(약 30초 소요)")
        if _on_recommend_cb:
            threading.Thread(target=_on_recommend_cb, daemon=True).start()
        return

    # URL 감지
    match = _URL_RE.search(text)
    if not match:
        return

    url = match.group(0).rstrip(".,)")
    send_message(
        f"🔗 <b>URL 감지!</b>\n<code>{url}</code>\n\n"
        f"기사를 가져와서 Blogspot + Threads에 포스팅합니다...\n"
        f"(약 2~3분 소요)"
    )

    if _on_url_cb:
        threading.Thread(target=_on_url_cb, args=(url,), daemon=True).start()


# ── 업데이트 폴링 ─────────────────────────────────────────────────────────────

def _poll_loop() -> None:
    global _offset
    print("[telegram] 폴링 시작")
    while not _stop_event.is_set():
        try:
            resp = requests.get(
                f"{_API}{cfg.TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"timeout": 20, "offset": _offset},
                timeout=25,
            )
            if resp.ok:
                updates = resp.json().get("result", [])
                for upd in updates:
                    _offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        _handle_callback(upd)
                    elif "message" in upd:
                        _handle_message(upd)
        except Exception as e:
            if not _stop_event.is_set():
                print(f"[telegram] 폴링 오류: {e}")
                time.sleep(5)


def start_polling() -> None:
    global _poll_thread
    if not _is_configured():
        print("[telegram] 봇 미설정, 폴링 건너뜀")
        return
    if _poll_thread and _poll_thread.is_alive():
        return
    _stop_event.clear()
    _poll_thread = threading.Thread(target=_poll_loop, daemon=True, name="tg-poll")
    _poll_thread.start()


def stop_polling() -> None:
    _stop_event.set()
