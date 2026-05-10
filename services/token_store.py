# -*- coding: utf-8 -*-
"""OAuth 토큰 영속성: 파일 우선, base64 환경변수 fallback."""
import base64
import os
from pathlib import Path

from config import cfg


def save(creds_json: str) -> None:
    path = cfg.TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds_json, encoding="utf-8")


def load() -> str | None:
    path = cfg.TOKEN_FILE
    if path.exists():
        return path.read_text(encoding="utf-8")
    # 환경변수 fallback (Render 퍼시스턴트 디스크 없는 환경)
    b64 = os.getenv("BLOGGER_TOKEN_B64", "")
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8")
        except Exception:
            pass
    return None


def exists() -> bool:
    return load() is not None


def restore_from_env() -> bool:
    """앱 시작 시 BLOGGER_TOKEN_B64 env var → 토큰 파일 복원. 이미 파일 있으면 스킵."""
    path = cfg.TOKEN_FILE
    if path.exists():
        return True
    b64 = os.getenv("BLOGGER_TOKEN_B64", "")
    if not b64:
        return False
    try:
        json_str = base64.b64decode(b64).decode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_str, encoding="utf-8")
        print("[token_store] BLOGGER_TOKEN_B64에서 토큰 복원 완료")
        return True
    except Exception as e:
        print(f"[token_store] 토큰 복원 실패: {e}")
        return False
