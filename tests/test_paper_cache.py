"""paper_cache.py 单元测试"""

from __future__ import annotations

from datetime import datetime
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
    summary="The dominant sequence...",
    published=datetime(2017, 6, 12),
    updated=datetime(2017, 6, 12),
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
        assert result.published == datetime(2017, 6, 12)
        assert result.updated == datetime(2017, 6, 12)
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
            summary="New summary",
            published=datetime(2017, 6, 12),
            updated=datetime(2024, 1, 1),
            categories=["cs.AI"],
            pdf_url="https://arxiv.org/pdf/1706.03762",
        )
        cache_paper("1706.03762", updated, "@misc{new}")

        result = get_cached_paper("1706.03762")
        assert result.title == "Updated Title"
        assert get_cached_bibtex("1706.03762") == "@misc{new}"


class TestGetPaperInfoCache:

    def test_cache_hit_skips_api(self, cache_db):
        """缓存命中时不请求 arXiv API"""
        cache_paper("1706.03762", MOCK_CACHED_PAPER, MOCK_BIBTEX)

        import arxiv_tool

        with patch("paper_cache.DB_PATH", cache_db), \
             patch("arxiv_tool.arxiv.Client") as mock_client:
            result = arxiv_tool.get_paper_info("1706.03762")

            mock_client.assert_not_called()
            assert result.title == "Attention Is All You Need"
