# -*- coding: utf-8 -*-
"""
Threads API 클라이언트 — auto_mysns_posting/sns_publisher.py에서 포팅.
두 단계: draft 생성 → 3초 대기 → publish.
"""
import time
import requests

from config import cfg


def post_text(text: str, link_url: str | None = None) -> str:
    """
    텍스트 전용 Threads 포스트 발행. 발행된 post_id 반환.
    link_url이 있으면 링크 첨부 (미리보기 카드 생성).
    """
    token = cfg.THREADS_ACCESS_TOKEN
    if not token:
        raise RuntimeError("THREADS_ACCESS_TOKEN이 설정되지 않음.")
    if "|" in token:
        raise RuntimeError("THREADS_ACCESS_TOKEN이 앱/클라이언트 토큰입니다. 유저 액세스 토큰을 사용하세요.")

    user_id = cfg.THREADS_USER_ID
    threads_user_id = user_id if (user_id and user_id.isdigit()) else "me"

    # 1단계: draft 생성
    url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads"
    payload: dict = {
        "media_type": "TEXT",
        "text": text,
        "access_token": token,
    }
    if link_url:
        payload["link_attachment"] = link_url

    resp = requests.post(url, data=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Threads draft 생성 실패: {_safe(resp)}")

    creation_id = resp.json().get("id")
    if not creation_id:
        raise RuntimeError("Threads가 creation_id를 반환하지 않음.")

    time.sleep(3)

    # 2단계: publish
    publish_url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads_publish"
    publish_resp = requests.post(
        publish_url,
        data={"creation_id": creation_id, "access_token": token},
        timeout=30,
    )
    if publish_resp.status_code != 200:
        raise RuntimeError(f"Threads 발행 실패: {_safe(publish_resp)}")

    post_id = publish_resp.json().get("id")
    if not post_id:
        raise RuntimeError("Threads 발행 응답에 post_id 없음.")
    return post_id


def _safe(response: requests.Response) -> str:
    try:
        return str(response.json())
    except ValueError:
        return response.text[:500]
