# -*- coding: utf-8 -*-
"""
Blogger API v3 클라이언트 — 서버사이드 OAuth2 (Flow, not InstalledAppFlow).
최초 1회 /auth/google 방문으로 인증, 이후 자동 갱신.
"""
from __future__ import annotations

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from config import cfg
from services import token_store

SCOPES = ["https://www.googleapis.com/auth/blogger"]

_CLIENT_CONFIG = {
    "web": {
        "client_id": cfg.GOOGLE_CLIENT_ID,
        "client_secret": cfg.GOOGLE_CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [cfg.OAUTH_REDIRECT_URI],
    }
}

_service_cache: object | None = None
_pending_flows: dict[str, Flow] = {}  # state → Flow (PKCE code_verifier 보존용)


def _build_flow(state: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        _CLIENT_CONFIG,
        scopes=SCOPES,
        state=state,
    )
    flow.redirect_uri = cfg.OAUTH_REDIRECT_URI
    return flow


def get_authorization_url() -> tuple[str, str]:
    """OAuth 인증 URL과 state 반환. Flow를 메모리에 보존해 PKCE 검증 통과."""
    flow = _build_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    _pending_flows[state] = flow  # code_verifier 포함 flow 저장
    return url, state


def exchange_code(code: str, state: str) -> Credentials:
    """인증 코드를 토큰으로 교환하고 저장."""
    global _service_cache
    _service_cache = None

    # 저장된 flow 재사용 (code_verifier 일치시켜야 PKCE 통과)
    flow = _pending_flows.pop(state, None) or _build_flow(state=state)
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_store.save(creds.to_json())
    return creds


def get_service():
    """인증된 Blogger API 서비스 객체 반환 (캐싱)."""
    global _service_cache
    if _service_cache:
        return _service_cache

    raw = token_store.load()
    if not raw:
        raise RuntimeError("Blogger OAuth 토큰 없음. /auth/google 에서 인증하세요.")

    creds = Credentials.from_authorized_user_json(raw, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_store.save(creds.to_json())

    if not creds.valid:
        raise RuntimeError("Blogger OAuth 토큰이 유효하지 않음. 재인증 필요.")

    _service_cache = build("blogger", "v3", credentials=creds)
    return _service_cache


def is_authenticated() -> bool:
    try:
        get_service()
        return True
    except Exception:
        return False


def get_blog_id(blog_url: str) -> str:
    service = get_service()
    url = blog_url if blog_url.startswith("http") else f"https://{blog_url}"
    resp = service.blogs().getByUrl(url=url).execute()
    return resp["id"]


def publish(
    title: str,
    body_html: str,
    labels: list[str] | None = None,
    is_draft: bool = False,
) -> dict:
    """Blogspot에 글 발행. 반환: {"ok", "post_url", "post_id", "error"}"""
    try:
        service = get_service()
        blog_id = get_blog_id(cfg.BLOGGER_BLOG_URL)

        post_body: dict = {
            "kind": "blogger#post",
            "title": title,
            "content": body_html,
        }
        if labels:
            post_body["labels"] = labels

        result = service.posts().insert(
            blogId=blog_id,
            body=post_body,
            isDraft=is_draft,
        ).execute()

        return {
            "ok": True,
            "post_url": result.get("url", ""),
            "post_id": result.get("id", ""),
        }
    except Exception as e:
        return {"ok": False, "post_url": "", "post_id": "", "error": str(e)}
