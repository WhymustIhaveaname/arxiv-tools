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
