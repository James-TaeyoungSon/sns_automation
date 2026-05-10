# -*- coding: utf-8 -*-
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent


class Config:
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    OPENAI_IMAGE_MODEL: str = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")

    THREADS_ACCESS_TOKEN: str = os.getenv("THREADS_ACCESS_TOKEN", "")
    THREADS_USER_ID: str = os.getenv("THREADS_USER_ID", "")

    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    BLOGGER_BLOG_URL: str = os.getenv("BLOGGER_BLOG_URL", "")

    PEXELS_API_KEY: str = os.getenv("PEXELS_API_KEY", "")
    PIXABAY_API_KEY: str = os.getenv("PIXABAY_API_KEY", "")
    IMGBB_API_KEY: str = os.getenv("IMGBB_API_KEY", "")

    # 웹앱 설정
    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:5000").rstrip("/")
    FLASK_SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    ADMIN_TOKEN: str = os.getenv("ADMIN_TOKEN", "")

    # DB 경로
    DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "data" / "blog.db"))

        # Notion 연동
    NOTION_API_KEY: str = os.getenv("NOTION_API_KEY", "")
    NOTION_ARTICLES_DB_ID: str = os.getenv("NOTION_ARTICLES_DB_ID", "323795a7282b802dac16e3a15ceb57f6")

    # Telegram 봇
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # 스케줄 설정
    CRAWL_INTERVAL_HOURS: int = int(os.getenv("CRAWL_INTERVAL_HOURS", "6"))
    AUTO_POST_HOUR: int = int(os.getenv("AUTO_POST_HOUR", "9"))  # KST 기준
    AUTO_POST_ENABLED: bool = os.getenv("AUTO_POST_ENABLED", "false").lower() == "true"
    DIGEST_ENABLED: bool = os.getenv("DIGEST_ENABLED", "true").lower() == "true"

    @property
    def OAUTH_REDIRECT_URI(self) -> str:
        return f"{self.APP_BASE_URL}/auth/google/callback"

    @property
    def TOKEN_FILE(self) -> Path:
        return Path(self.DB_PATH).parent / "blogger_token.json"


cfg = Config()
