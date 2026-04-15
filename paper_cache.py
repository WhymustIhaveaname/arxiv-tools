"""Paper metadata SQLite cache."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
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
    abstract   TEXT NOT NULL DEFAULT '',
    categories TEXT NOT NULL DEFAULT '[]',
    pdf_url    TEXT NOT NULL DEFAULT '',
    bibtex     TEXT NOT NULL,
    cached_at  TEXT NOT NULL,
    source     TEXT,
    doi        TEXT,
    pmid       TEXT,
    pmcid      TEXT
)"""

# Columns added after the initial schema; checked on every connection and
# ALTER TABLE'd in if missing. Keep in sync with _CREATE_TABLE_SQL.
_NULLABLE_COLUMNS = [
    ("source", "TEXT"),
    ("doi", "TEXT"),
    ("pmid", "TEXT"),
    ("pmcid", "TEXT"),
]


@dataclass
class CachedAuthor:
    name: str


@dataclass
class CachedPaper:
    title: str
    authors: list[CachedAuthor]
    abstract: str = ""
    categories: list[str] = field(default_factory=list)
    pdf_url: str = ""
    year: int | None = None
    source: str | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None


def _migrate(conn: sqlite3.Connection) -> None:
    """Migrate legacy schemas in place."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    if not cols:
        return
    if "summary" in cols and "abstract" not in cols:
        conn.execute("ALTER TABLE papers RENAME COLUMN summary TO abstract")
    if "published" in cols:
        conn.execute("ALTER TABLE papers DROP COLUMN published")
    if "updated" in cols:
        conn.execute("ALTER TABLE papers DROP COLUMN updated")
    # Add nullable cross-reference columns on older databases.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for name, coltype in _NULLABLE_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {coltype}")


def _get_conn() -> sqlite3.Connection:
    CACHE_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CREATE_TABLE_SQL)
    _migrate(conn)
    return conn


def get_cached_paper(arxiv_id: str) -> CachedPaper | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT title, authors, abstract, categories, pdf_url, source, doi, pmid, pmcid "
            "FROM papers WHERE arxiv_id = ?",
            (arxiv_id,),
        ).fetchone()
        if row is None:
            return None
        title, authors_json, abstract, categories_json, pdf_url, source, doi, pmid, pmcid = row
        return CachedPaper(
            title=title,
            authors=[CachedAuthor(name) for name in json.loads(authors_json)],
            abstract=abstract,
            categories=json.loads(categories_json),
            pdf_url=pdf_url,
            source=source,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
        )
    finally:
        conn.close()


def cache_paper(arxiv_id: str, paper: CachedPaper, bibtex: str) -> None:
    conn = _get_conn()
    try:
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO papers
                   (arxiv_id, title, authors, abstract, categories, pdf_url, bibtex,
                    cached_at, source, doi, pmid, pmcid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    arxiv_id,
                    paper.title,
                    json.dumps([a.name for a in paper.authors]),
                    paper.abstract,
                    json.dumps(paper.categories),
                    paper.pdf_url,
                    bibtex,
                    datetime.now().isoformat(),
                    paper.source,
                    paper.doi,
                    paper.pmid,
                    paper.pmcid,
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
