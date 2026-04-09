"""paper_cache.py 单元测试"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from paper_cache import (
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
        import sqlite3

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
             '["cs.CL"]', "url", "bibtex", "2024-01-01T00:00:00"),
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
