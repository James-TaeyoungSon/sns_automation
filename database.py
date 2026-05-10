# -*- coding: utf-8 -*-
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from config import cfg

_DB_PATH = Path(cfg.DB_PATH)

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT    NOT NULL,
    url_hash       TEXT    NOT NULL UNIQUE,
    title          TEXT    NOT NULL,
    source         TEXT,
    published_at   TEXT,
    fetched_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    status         TEXT    NOT NULL DEFAULT 'new',
    error_msg      TEXT,
    notion_page_id TEXT,
    score          REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS generated_content (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id     INTEGER NOT NULL UNIQUE REFERENCES articles(id),
    blogspot_title TEXT,
    blogspot_html  TEXT,
    threads_text   TEXT,
    seo_keyword    TEXT,
    image_urls     TEXT,
    generated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    edited         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id       INTEGER NOT NULL REFERENCES articles(id),
    blogspot_post_id TEXT,
    blogspot_url     TEXT,
    threads_post_id  TEXT,
    published_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    blogspot_ok      INTEGER NOT NULL DEFAULT 0,
    threads_ok       INTEGER NOT NULL DEFAULT 0,
    error_msg        TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT    PRIMARY KEY,
    article_id  INTEGER REFERENCES articles(id),
    job_type    TEXT,
    status      TEXT    NOT NULL DEFAULT 'running',
    logs        TEXT    DEFAULT '[]',
    result      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH))
    con.executescript(_SCHEMA)
    # 기존 DB 마이그레이션: 컬럼 없으면 추가
    for col, definition in [
        ("notion_page_id", "TEXT"),
        ("score", "REAL DEFAULT 0"),
        ("batch_id", "TEXT"),
    ]:
        try:
            con.execute(f"ALTER TABLE articles ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # 이미 존재하면 무시
    try:
        con.execute("ALTER TABLE generated_content ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db_conn():
    con = get_db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
