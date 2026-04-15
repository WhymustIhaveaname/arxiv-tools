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
    pmcid      TEXT,
    native_arxiv_id TEXT,
    year       INTEGER
)"""

# Columns added after the initial schema; checked on every connection and
# ALTER TABLE'd in if missing. Keep in sync with _CREATE_TABLE_SQL.
_NULLABLE_COLUMNS = [
    ("source", "TEXT"),
    ("doi", "TEXT"),
    ("pmid", "TEXT"),
    ("pmcid", "TEXT"),
    # native_arxiv_id: the bare arXiv ID (no "arxiv:" prefix). Kept separate from
    # the cache PK so cross-reference rows (pmid:/doi:/...) can still record the
    # arXiv identity of the same paper.
    ("native_arxiv_id", "TEXT"),
    ("year", "INTEGER"),
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
    arxiv_id: str | None = None
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
    for name, coltype in _NULLABLE_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {coltype}")
    # Cross-source PK: prefix any legacy bare IDs (e.g. "1706.03762") with "arxiv:"
    # so they don't collide with future "pmid:39876543" / "doi:10.x/y" entries.
    rows = conn.execute(
        "SELECT arxiv_id FROM papers WHERE arxiv_id NOT LIKE '%:%'"
    ).fetchall()
    for (old_id,) in rows:
        new_id = f"arxiv:{old_id}"
        try:
            conn.execute(
                "UPDATE papers SET arxiv_id = ? WHERE arxiv_id = ?",
                (new_id, old_id),
            )
        except sqlite3.IntegrityError:
            # The prefixed key already exists (rare); drop the legacy duplicate.
            conn.execute("DELETE FROM papers WHERE arxiv_id = ?", (old_id,))


def _normalize_paper_id(raw: str) -> str:
    """Canonicalise a paper key.

    Accepts either a bare arXiv ID (legacy) or a prefixed cross-source ID
    (``arxiv:1706.03762`` / ``pmid:39876543`` / ``doi:10.x/y``). Bare values
    get an ``arxiv:`` prefix so all rows live in one keyspace.
    """
    return raw if ":" in raw else f"arxiv:{raw}"


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
            "SELECT title, authors, abstract, categories, pdf_url, source, "
            "doi, pmid, pmcid, native_arxiv_id, year "
            "FROM papers WHERE arxiv_id = ?",
            (_normalize_paper_id(arxiv_id),),
        ).fetchone()
        if row is None:
            return None
        title, authors_json, abstract, categories_json, pdf_url, source, \
            doi, pmid, pmcid, arxiv_id_col, year = row
        return CachedPaper(
            title=title,
            authors=[CachedAuthor(name) for name in json.loads(authors_json)],
            abstract=abstract,
            categories=json.loads(categories_json),
            pdf_url=pdf_url,
            year=year,
            source=source,
            arxiv_id=arxiv_id_col,
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
                    cached_at, source, doi, pmid, pmcid, native_arxiv_id, year)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _normalize_paper_id(arxiv_id),
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
                    paper.arxiv_id,
                    paper.year,
                ),
            )
    finally:
        conn.close()


def cache_paper_with_crossrefs(primary_id: str, paper: CachedPaper, bibtex: str) -> None:
    """Write the primary row + one cross-reference row per known ID.

    So a paper that has arxiv_id="2401.12345", doi="10.x/y", pmid="39876543"
    ends up with four identical cache rows — one per ID form the user might
    look it up by. Each row carries the full metadata + the same bibtex, so
    future lookups hit cache regardless of which identifier the user supplied.

    Duplicate data, but each row is only a few KB and the DB stays trivially
    small; reading is O(1) on the primary key either way.
    """
    primary_key = _normalize_paper_id(primary_id)
    cache_paper(primary_key, paper, bibtex)

    aliases: list[str] = []
    if paper.arxiv_id:
        aliases.append(f"arxiv:{paper.arxiv_id}")
    if paper.doi:
        aliases.append(f"doi:{paper.doi.lower()}")
    if paper.pmid:
        aliases.append(f"pmid:{paper.pmid}")
    if paper.pmcid:
        aliases.append(f"pmcid:{paper.pmcid.upper()}")

    for alias in aliases:
        if alias == primary_key:
            continue
        cache_paper(alias, paper, bibtex)


def get_cached_bibtex(arxiv_id: str) -> str | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT bibtex FROM papers WHERE arxiv_id = ?",
            (_normalize_paper_id(arxiv_id),),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
