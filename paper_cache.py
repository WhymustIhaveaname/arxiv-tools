"""论文元数据 SQLite 缓存"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import os

CACHE_DIR = Path(os.environ.get("ARXIV_CACHE_DIR", Path(__file__).parent / ".arxiv"))
DB_PATH = CACHE_DIR / "paper_cache.db"

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS papers (
    arxiv_id   TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    authors    TEXT NOT NULL,
    summary    TEXT NOT NULL,
    published  TEXT NOT NULL,
    updated    TEXT NOT NULL,
    categories TEXT NOT NULL,
    pdf_url    TEXT NOT NULL,
    bibtex     TEXT NOT NULL,
    cached_at  TEXT NOT NULL
)"""


@dataclass
class CachedAuthor:
    name: str


@dataclass
class CachedPaper:
    title: str
    authors: list[CachedAuthor]
    summary: str
    published: datetime
    updated: datetime
    categories: list[str]
    pdf_url: str


def _get_conn() -> sqlite3.Connection:
    CACHE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CREATE_TABLE_SQL)
    return conn


def get_cached_paper(arxiv_id: str) -> CachedPaper | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT title, authors, summary, published, updated, categories, pdf_url FROM papers WHERE arxiv_id = ?",
            (arxiv_id,),
        ).fetchone()
        if row is None:
            return None
        title, authors_json, summary, published, updated, categories_json, pdf_url = row
        return CachedPaper(
            title=title,
            authors=[CachedAuthor(name) for name in json.loads(authors_json)],
            summary=summary,
            published=datetime.fromisoformat(published),
            updated=datetime.fromisoformat(updated),
            categories=json.loads(categories_json),
            pdf_url=pdf_url,
        )
    finally:
        conn.close()


def cache_paper(arxiv_id: str, paper: CachedPaper, bibtex: str) -> None:
    conn = _get_conn()
    try:
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO papers
                   (arxiv_id, title, authors, summary, published, updated, categories, pdf_url, bibtex, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    arxiv_id,
                    paper.title,
                    json.dumps([a.name for a in paper.authors]),
                    paper.summary,
                    paper.published.isoformat(),
                    paper.updated.isoformat(),
                    json.dumps(paper.categories),
                    paper.pdf_url,
                    bibtex,
                    datetime.now().isoformat(),
                ),
            )
    finally:
        conn.close()


def get_cached_bibtex(arxiv_id: str) -> str | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT bibtex FROM papers WHERE arxiv_id = ?",
            (arxiv_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
