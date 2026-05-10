# -*- coding: utf-8 -*-
"""
배치 인메모리 캐시.
SQLite가 Render 무료 플랜 ephemeral 환경에서 신뢰할 수 없을 때 fallback으로 사용.
같은 Gunicorn 프로세스 내에서는 항상 유효.
"""
from __future__ import annotations

_store: dict[str, list[dict]] = {}


def save(batch_id: str, articles: list[dict]) -> None:
    _store[batch_id] = articles


def load(batch_id: str) -> list[dict] | None:
    return _store.get(batch_id)
