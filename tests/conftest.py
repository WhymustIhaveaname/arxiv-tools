"""Allow tests to import arxiv_tool from project root + shared fixtures."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def cache_db(tmp_path):
    """Per-test isolated SQLite cache. Without this fixture tests would write
    to the real shared $ARXIV_CACHE_DIR/paper_cache.db."""
    db_path = tmp_path / "test_cache.db"
    with patch("paper_cache.DB_PATH", db_path):
        yield db_path


@pytest.fixture(autouse=True)
def _no_enrich():
    """Disable OpenAlex ID enrichment during tests by default.

    enrich_paper_ids fires an extra OpenAlex request after every fetch, which
    (a) hits the real network in unit tests that only meant to mock a single
    source, and (b) slows the suite down. Tests that specifically need to
    exercise enrichment should patch it themselves.
    """
    with patch("arxiv_tool.enrich_paper_ids", side_effect=lambda p: p):
        yield
