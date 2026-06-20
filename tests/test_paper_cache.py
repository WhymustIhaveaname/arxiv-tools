"""paper_cache.py 单元测试"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import sqlite3
from datetime import datetime, timedelta

from paper_cache import (
    CACHE_TTL_DAYS,
    CachedAuthor,
    CachedPaper,
    cache_paper,
    get_cached_bibtex,
    get_cached_paper,
)

MOCK_CACHED_PAPER = CachedPaper(
    title="Attention Is All You Need",
    authors=[CachedAuthor("Ashish Vaswani"), CachedAuthor("Noam Shazeer")],
    abstract="The dominant sequence...",
    categories=["cs.CL", "cs.LG"],
    pdf_url="https://arxiv.org/pdf/1706.03762",
)

MOCK_BIBTEX = "@misc{vaswani2017attention,\n  title={Attention Is All You Need},\n}"


@pytest.fixture()
def cache_db(tmp_path):
    """每个测试用独立的临时数据库"""
    db_path = tmp_path / "test_cache.db"
    with patch("paper_cache.DB_PATH", db_path):
        yield db_path


class TestCacheRoundtrip:

    def test_store_and_retrieve(self, cache_db):
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)
        result = get_cached_paper("1706.03762")

        assert result is not None
        assert result.title == "Attention Is All You Need"
        assert result.authors[0].name == "Ashish Vaswani"
        assert len(result.authors) == 2
        assert result.abstract == "The dominant sequence..."
        assert result.categories == ["cs.CL", "cs.LG"]

    def test_miss_returns_none(self, cache_db):
        assert get_cached_paper("9999.99999") is None

    def test_get_cached_bibtex(self, cache_db):
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)
        assert get_cached_bibtex("1706.03762") == MOCK_BIBTEX

    def test_bibtex_miss_returns_none(self, cache_db):
        assert get_cached_bibtex("9999.99999") is None

    def test_replace_on_duplicate(self, cache_db):
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        updated = CachedPaper(
            title="Updated Title",
            authors=[CachedAuthor("Author")],
            abstract="New abstract",
            categories=["cs.AI"],
            pdf_url="https://arxiv.org/pdf/1706.03762",
        )
        cache_paper("1706.03762", updated, "@misc{new}")

        result = get_cached_paper("1706.03762")
        assert result.title == "Updated Title"
        assert get_cached_bibtex("1706.03762") == "@misc{new}"


class TestGetPaperInfoCache:

    def test_cache_hit_skips_api(self, cache_db):
        """缓存命中时不请求任何 API"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        import arxiv_tool

        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool._fetch_paper_s2") as mock_s2, \
             patch("arxiv_tool._fetch_paper_openalex") as mock_oa, \
             patch("arxiv_tool._fetch_paper_arxiv") as mock_arxiv:
            result = arxiv_tool.get_paper_info("1706.03762")

            mock_s2.assert_not_called()
            mock_oa.assert_not_called()
            mock_arxiv.assert_not_called()
            assert result.title == "Attention Is All You Need"


class TestGetPaperInfoFallback:

    def test_openalex_success_skips_others(self, cache_db):
        """OpenAlex 成功时不调 S2 和 arXiv"""
        import arxiv_tool

        fake = CachedPaper(
            title="OA Paper", authors=[CachedAuthor("A")],
        )
        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool._fetch_paper_openalex", return_value=fake) as mock_oa, \
             patch("arxiv_tool._fetch_paper_s2") as mock_s2, \
             patch("arxiv_tool._fetch_paper_arxiv") as mock_arxiv:
            result = arxiv_tool.get_paper_info("2401.00001")

            mock_oa.assert_called_once()
            mock_s2.assert_not_called()
            mock_arxiv.assert_not_called()
            assert result.title == "OA Paper"

    def test_openalex_fail_falls_back_to_s2(self, cache_db):
        """OpenAlex 失败后 fallback 到 S2"""
        import arxiv_tool

        fake = CachedPaper(
            title="S2 Paper", authors=[CachedAuthor("B")],
        )
        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool._fetch_paper_openalex", return_value=None), \
             patch("arxiv_tool._fetch_paper_s2", return_value=fake) as mock_s2, \
             patch("arxiv_tool._fetch_paper_arxiv") as mock_arxiv:
            result = arxiv_tool.get_paper_info("2401.00001")

            mock_s2.assert_called_once()
            mock_arxiv.assert_not_called()
            assert result.title == "S2 Paper"

    def test_all_fail_falls_back_to_arxiv(self, cache_db):
        """OpenAlex 和 S2 都失败后 fallback 到 arXiv"""
        import arxiv_tool

        fake = CachedPaper(
            title="arXiv Paper", authors=[CachedAuthor("C")],
            categories=["cs.LG"],
        )
        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool._fetch_paper_openalex", return_value=None), \
             patch("arxiv_tool._fetch_paper_s2", return_value=None), \
             patch("arxiv_tool._fetch_paper_arxiv", return_value=fake):
            result = arxiv_tool.get_paper_info("2401.00001")
            assert result.title == "arXiv Paper"

    def test_all_fail_returns_none(self, cache_db):
        """三个数据源都失败返回 None"""
        import arxiv_tool

        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool._fetch_paper_s2", return_value=None), \
             patch("arxiv_tool._fetch_paper_openalex", return_value=None), \
             patch("arxiv_tool._fetch_paper_arxiv", return_value=None):
            result = arxiv_tool.get_paper_info("9999.99999")
            assert result is None


class TestMigration:
    """旧 schema 自动迁移"""

    def test_migrates_old_schema(self, cache_db):
        """带 summary/published/updated 列的旧表应自动迁移"""
        # 构造旧 schema
        conn = sqlite3.connect(cache_db)
        conn.execute("""
            CREATE TABLE papers (
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
            )
        """)
        conn.execute(
            "INSERT INTO papers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("1706.03762", "Old Title", '["Old Author"]', "Old abstract",
             "2017-06-12T00:00:00", "2017-06-12T00:00:00",
             '["cs.CL"]', "url", "bibtex", datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # 触发迁移 + 读取
        paper = get_cached_paper("1706.03762")
        assert paper is not None
        assert paper.title == "Old Title"
        assert paper.abstract == "Old abstract"
        assert paper.categories == ["cs.CL"]

        # 验证列已变成新 schema
        conn = sqlite3.connect(cache_db)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)")}
        conn.close()
        assert "summary" not in cols
        assert "abstract" in cols
        assert "published" not in cols
        assert "updated" not in cols


class TestCacheTTL:
    """TTL 过期后 get_cached_paper 返回 None, 触发 re-fetch"""

    def test_fresh_cache_returns_paper(self, cache_db):
        """cached_at 在 TTL 内 → 正常返回"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)
        assert get_cached_paper("1706.03762") is not None

    def test_stale_cache_returns_none(self, cache_db):
        """cached_at 超过 TTL → 返回 None"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        # 把 cached_at 倒拨到 TTL 之前
        stale_time = (datetime.now() - timedelta(days=CACHE_TTL_DAYS + 1)).isoformat()
        conn = sqlite3.connect(cache_db)
        conn.execute("UPDATE papers SET cached_at = ? WHERE arxiv_id = ?",
                     (stale_time, "1706.03762"))
        conn.commit()
        conn.close()

        assert get_cached_paper("1706.03762") is None

    def test_archived_row_not_returned(self, cache_db):
        """归档行 (id 带 _YYMMDD 后缀) 不被原 id 查询命中"""
        cache_paper("1706.03762_250620", MOCK_CACHED_PAPER, MOCK_BIBTEX)
        assert get_cached_paper("1706.03762") is None

    def test_ttl_is_7_days(self):
        assert CACHE_TTL_DAYS == 7


class TestCacheArchiveOnRefresh:
    """cache_paper 对已有旧行做 rename+insert 原子操作"""

    def test_archive_preserves_old_row(self, cache_db):
        """刷新时旧行 rename 为 id_YYMMDD, 新行正常插入"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        # 把 cached_at 倒拨使其过期
        stale_time = (datetime.now() - timedelta(days=CACHE_TTL_DAYS + 1)).isoformat()
        conn = sqlite3.connect(cache_db)
        conn.execute("UPDATE papers SET cached_at = ? WHERE arxiv_id = ?",
                     (stale_time, "1706.03762"))
        conn.commit()
        conn.close()

        # 重新 cache (模拟 re-fetch)
        updated = CachedPaper(
            title="Updated Title v2",
            authors=[CachedAuthor("Author")],
            abstract="New abstract",
        )
        cache_paper("1706.03762", updated, "@misc{new}")

        # 新行存在
        result = get_cached_paper("1706.03762")
        assert result is not None
        assert result.title == "Updated Title v2"

        # 旧行以 _YYMMDD 归档
        today = datetime.now().strftime("%y%m%d")
        conn = sqlite3.connect(cache_db)
        row = conn.execute(
            "SELECT title, cached_at FROM papers WHERE arxiv_id = ?",
            (f"1706.03762_{today}",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "Attention Is All You Need"
        assert row[1] == stale_time  # cached_at 保留原值

    def test_archive_is_atomic(self, cache_db):
        """rename + insert 在同一事务, 总行数正确"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        updated = CachedPaper(
            title="V2", authors=[CachedAuthor("A")],
        )
        cache_paper("1706.03762", updated, "@misc{v2}")

        conn = sqlite3.connect(cache_db)
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
        assert count == 2  # 旧行(归档) + 新行

    def test_no_archive_when_first_insert(self, cache_db):
        """首次插入没有旧行, 不应产生归档"""
        cache_paper("2401.00001", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        conn = sqlite3.connect(cache_db)
        count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        conn.close()
        assert count == 1
