"""论文元数据 SQLite 缓存"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / ".paper_cache.db"


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
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id   TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            authors    TEXT NOT NULL,
            summary    TEXT NOT NULL,
            published  TEXT NOT NULL,
            updated    TEXT NOT NULL,
            categories TEXT NOT NULL,
            bibtex     TEXT NOT NULL,
            cached_at  TEXT NOT NULL
        )
    """)
    return conn


def get_cached_paper(arxiv_id: str) -> CachedPaper | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT title, authors, summary, published, updated, categories FROM papers WHERE arxiv_id = ?",
        (arxiv_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    title, authors_json, summary, published, updated, categories_json = row
    return CachedPaper(
        title=title,
        authors=[CachedAuthor(name) for name in json.loads(authors_json)],
        summary=summary,
        published=datetime.fromisoformat(published),
        updated=datetime.fromisoformat(updated),
        categories=json.loads(categories_json),
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def cache_paper(arxiv_id: str, paper: CachedPaper, bibtex: str) -> None:
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO papers
           (arxiv_id, title, authors, summary, published, updated, categories, bibtex, cached_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            arxiv_id,
            paper.title,
            json.dumps([a.name for a in paper.authors]),
            paper.summary,
            paper.published.isoformat(),
            paper.updated.isoformat(),
            json.dumps(paper.categories),
            bibtex,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_cached_bibtex(arxiv_id: str) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT bibtex FROM papers WHERE arxiv_id = ?",
        (arxiv_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None
