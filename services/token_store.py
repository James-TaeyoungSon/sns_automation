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
