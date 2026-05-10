# -*- coding: utf-8 -*-
"""
이미지 서비스:
- gpt-image-1로 블로그 전용 AI 이미지 생성 → ImgBB 업로드 (영구 공개 URL)
- Pexels/Pixabay 스톡 이미지 URL 조회 (gpt-image-1 실패 시 fallback)
- ImgBB 파일/바이트 업로드
"""
from __future__ import annotations

import base64

import requests
from openai import OpenAI

from config import cfg

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=cfg.OPENAI_API_KEY)
    return _client


# ── gpt-image-1 이미지 생성 ───────────────────────────────────────────────────

def generate_blog_image(article_title: str, seo_keyword: str) -> str | None:
    """
    gpt-image-1로 블로그 헤더 이미지 생성 → ImgBB 업로드 → 공개 URL 반환.
    gpt-image-1은 base64(b64_json)로 반환하므로 ImgBB에 직접 업로드.
    실패 시 None 반환.
    """
    if not cfg.OPENAI_API_KEY:
        print("[image_service] OPENAI_API_KEY 미설정, 이미지 생성 건너뜀")
        return None

    prompt = (
        f"A clean, modern, high-quality digital illustration for a Korean AI tech blog post about: "
        f"{article_title[:120]}. "
        f"Style: minimalist flat design with blue and purple gradient, abstract geometric shapes, "
        f"circuit board patterns, glowing nodes representing neural networks. "
        f"NO text, NO letters, NO watermark. Wide landscape format. Professional and elegant."
    )

    try:
        client = _get_client()
        print("[image_service] gpt-image-1 이미지 생성 중...")
        resp = client.images.generate(
            model=cfg.OPENAI_IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
            quality="low",
            n=1,
            timeout=120.0,
        )
        # gpt-image-1은 b64_json으로 반환
        img_b64 = resp.data[0].b64_json
        img_bytes = base64.b64decode(img_b64)

        public_url = upload_bytes_to_imgbb(img_bytes, filename="blog_image.png")
        if public_url:
            print(f"[image_service] 이미지 생성 완료: {public_url}")
            return public_url
        return None

    except Exception as e:
        print(f"[image_service] gpt-image-1 실패: {e}")
        return None


# ── ImgBB 업로드 ─────────────────────────────────────────────────────────────

def upload_to_imgbb(image_path: str) -> str | None:
    """이미지 파일 → ImgBB 업로드 → 공개 URL 반환."""
    imgbb_key = cfg.IMGBB_API_KEY
    if not imgbb_key:
        print("[image_service] IMGBB_API_KEY 미설정")
        return None
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(
                "https://api.imgbb.com/1/upload",
                data={"key": imgbb_key},
                files={"image": f},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json()["data"]["url"]
    except Exception as e:
        print(f"[image_service] ImgBB 업로드 실패: {e}")
        return None


def upload_bytes_to_imgbb(image_bytes: bytes, filename: str = "image.jpg") -> str | None:
    """이미지 바이트 → base64 → ImgBB 업로드 → 공개 URL 반환."""
    imgbb_key = cfg.IMGBB_API_KEY
    if not imgbb_key:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": imgbb_key, "image": b64, "name": filename},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"]["url"]
    except Exception as e:
        print(f"[image_service] ImgBB base64 업로드 실패: {e}")
        return None


# ── Pexels/Pixabay fallback ───────────────────────────────────────────────────

def fetch_stock_image_urls(query: str, count: int = 1) -> list[str]:
    """Pexels → Pixabay 순서로 스톡 이미지 URL 반환 (DALL-E fallback용)."""
    urls: list[str] = []

    if cfg.PEXELS_API_KEY:
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": cfg.PEXELS_API_KEY},
                params={"query": query, "per_page": count, "orientation": "landscape"},
                timeout=15,
            )
            r.raise_for_status()
            for p in r.json().get("photos", [])[:count]:
                url = p["src"].get("large") or p["src"].get("original", "")
                if url:
                    urls.append(url)
        except Exception as e:
            print(f"[image_service] Pexels 실패: {e}")

    if len(urls) < count and cfg.PIXABAY_API_KEY:
        try:
            r = requests.get(
                "https://pixabay.com/api/",
                params={"key": cfg.PIXABAY_API_KEY, "q": query, "image_type": "photo",
                        "orientation": "horizontal", "per_page": count, "safesearch": "true"},
                timeout=15,
            )
            r.raise_for_status()
            for h in r.json().get("hits", [])[: count - len(urls)]:
                url = h.get("webformatURL", "")
                if url:
                    urls.append(url)
        except Exception as e:
            print(f"[image_service] Pixabay 실패: {e}")

    return urls
