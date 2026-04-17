"""Tests for lit/preprint_lookup.py — OpenAlex-driven preprint reverse lookup."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from lit.preprint_lookup import (
    PreprintVersion,
    _canonical_source,
    _id_from_location,
    _strip_doi_suffix,
    find_preprint_versions,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class TestCanonicalSource:
    def test_arxiv_recognised(self):
        assert _canonical_source("arXiv (Cornell University)") == "arxiv"

    def test_biorxiv_recognised(self):
        assert _canonical_source("bioRxiv") == "biorxiv"

    def test_chemrxiv_recognised(self):
        assert _canonical_source("ChemRxiv") == "chemrxiv"

    def test_research_square(self):
        assert _canonical_source("Research Square (Research Square)") == "researchsquare"

    def test_unknown_returns_none(self):
        assert _canonical_source("Nature Communications") is None
        assert _canonical_source("") is None
        assert _canonical_source(None) is None  # type: ignore[arg-type]


class TestStripDoiSuffix:
    def test_strips_pdf(self):
        assert _strip_doi_suffix("10.1101/2023.01.01.123456.pdf") == "10.1101/2023.01.01.123456"

    def test_strips_full(self):
        assert _strip_doi_suffix("10.1101/foo.full") == "10.1101/foo"

    def test_strips_version(self):
        assert _strip_doi_suffix("10.1101/foo.v2") == "10.1101/foo"

    def test_chained_suffixes(self):
        assert _strip_doi_suffix("10.1101/foo.full.pdf") == "10.1101/foo"

    def test_clean_doi_unchanged(self):
        assert _strip_doi_suffix("10.1101/foo") == "10.1101/foo"


class TestIdFromLocation:
    def test_arxiv_from_landing_url(self):
        loc = {
            "landing_page_url": "https://arxiv.org/abs/2401.12345",
            "pdf_url": None,
        }
        assert _id_from_location("arxiv", loc) == "2401.12345"

    def test_arxiv_strips_version_suffix(self):
        loc = {"landing_page_url": "https://arxiv.org/abs/2401.12345v3", "pdf_url": ""}
        assert _id_from_location("arxiv", loc) == "2401.12345"

    def test_arxiv_old_format(self):
        loc = {"landing_page_url": "https://arxiv.org/abs/cs/0401001", "pdf_url": ""}
        assert _id_from_location("arxiv", loc) == "cs/0401001"

    def test_arxiv_from_pdf_url(self):
        loc = {"landing_page_url": "", "pdf_url": "https://arxiv.org/pdf/2401.99999"}
        assert _id_from_location("arxiv", loc) == "2401.99999"

    def test_biorxiv_doi_extracted(self):
        loc = {
            "landing_page_url": "https://www.biorxiv.org/content/10.1101/2023.05.01.539001v1",
            "pdf_url": None,
        }
        out = _id_from_location("biorxiv", loc)
        assert out == "10.1101/2023.05.01.539001v1" or out == "10.1101/2023.05.01.539001"

    def test_biorxiv_strips_full_pdf(self):
        loc = {
            "landing_page_url": "",
            "pdf_url": "https://www.biorxiv.org/content/10.1101/2023.05.01.539001.full.pdf",
        }
        assert _id_from_location("biorxiv", loc) == "10.1101/2023.05.01.539001"

    def test_chemrxiv_doi(self):
        loc = {
            "landing_page_url": "https://chemrxiv.org/engage/chemrxiv/article-details/10.26434/chemrxiv-2024-abcde",
            "pdf_url": None,
        }
        assert _id_from_location("chemrxiv", loc) == "10.26434/chemrxiv-2024-abcde"

    def test_research_square_doi(self):
        loc = {
            "landing_page_url": "https://www.researchsquare.com/article/rs-12345/v1",
            "pdf_url": "https://assets.researchsquare.com/files/rs-12345/v1/abc.pdf",
        }
        # No 10.21203 in URL — should be None unless we get the DOI elsewhere
        result = _id_from_location("researchsquare", loc)
        # acceptable: None (no 10.21203/* in URL)
        assert result is None or result.startswith("10.21203/")

    def test_research_square_doi_when_present(self):
        loc = {
            "landing_page_url": "https://doi.org/10.21203/rs.3.rs-12345/v1",
            "pdf_url": None,
        }
        assert _id_from_location("researchsquare", loc) == "10.21203/rs.3.rs-12345/v1"

    def test_ssrn_id(self):
        loc = {
            "landing_page_url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4567890",
            "pdf_url": None,
        }
        assert _id_from_location("ssrn", loc) == "4567890"


# --------------------------------------------------------------------------
# find_preprint_versions
# --------------------------------------------------------------------------


def _mock_resp(json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


class TestFindPreprintVersions:
    def test_returns_empty_on_no_doi(self):
        assert find_preprint_versions(doi=None) == []
        assert find_preprint_versions(doi="") == []

    def test_arxiv_version_extracted(self):
        data = {
            "locations": [
                {
                    "source": {"display_name": "Nature"},
                    "landing_page_url": "https://www.nature.com/articles/s41586-023-12345",
                    "pdf_url": None,
                    "version": "publishedVersion",
                },
                {
                    "source": {"display_name": "arXiv (Cornell University)"},
                    "landing_page_url": "https://arxiv.org/abs/2305.12345",
                    "pdf_url": None,
                    "version": "submittedVersion",
                },
            ],
            "best_oa_location": None,
        }
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1038/s41586-023-12345")
        assert len(versions) == 1
        assert versions[0].source == "arxiv"
        assert versions[0].id == "2305.12345"
        assert versions[0].version_label == "submittedVersion"

    def test_arxiv_takes_priority_over_biorxiv(self):
        data = {
            "locations": [
                {
                    "source": {"display_name": "bioRxiv"},
                    "landing_page_url": "https://www.biorxiv.org/content/10.1101/2023.05.01.539001v1",
                    "pdf_url": None,
                    "version": "submittedVersion",
                },
                {
                    "source": {"display_name": "arXiv"},
                    "landing_page_url": "https://arxiv.org/abs/2305.99999",
                    "pdf_url": None,
                    "version": "submittedVersion",
                },
            ],
            "best_oa_location": None,
        }
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert len(versions) == 2
        assert versions[0].source == "arxiv"
        assert versions[1].source == "biorxiv"

    def test_dedup_when_best_oa_duplicates_locations(self):
        loc = {
            "source": {"display_name": "arXiv"},
            "landing_page_url": "https://arxiv.org/abs/2401.00001",
            "pdf_url": None,
            "version": "submittedVersion",
        }
        data = {"locations": [loc], "best_oa_location": loc}
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert len(versions) == 1

    def test_no_preprint_returns_empty(self):
        data = {
            "locations": [
                {
                    "source": {"display_name": "Cell"},
                    "landing_page_url": "https://www.cell.com/cell/abstract/S0092-8674...",
                    "pdf_url": None,
                    "version": "publishedVersion",
                },
            ],
            "best_oa_location": None,
        }
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1016/j.cell.2024.123456")
        assert versions == []

    def test_unrecognised_id_skipped(self):
        """Preprint host recognised but ID can't be extracted from the URL."""
        data = {
            "locations": [
                {
                    "source": {"display_name": "arXiv"},
                    "landing_page_url": "https://example.com/no-arxiv-id-here",
                    "pdf_url": None,
                },
            ],
            "best_oa_location": None,
        }
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert versions == []

    def test_request_failure_returns_empty(self):
        with patch(
            "lit.preprint_lookup._request_with_retry",
            side_effect=requests.RequestException("network down"),
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert versions == []

    def test_s2_fallback_when_openalex_lacks_arxiv(self):
        """If OpenAlex doesn't know about the arXiv twin, ask S2."""
        oa_data = {
            "locations": [
                {
                    "source": {"display_name": "Nature"},
                    "landing_page_url": "https://www.nature.com/foo",
                    "pdf_url": None,
                },
            ],
            "best_oa_location": None,
        }
        s2_data = {"externalIds": {"DOI": "10.1/x", "ArXiv": "2401.99999"}}

        def _route(*args, **kwargs):
            url = args[1] if len(args) > 1 else kwargs.get("url")
            if "openalex" in url:
                return _mock_resp(oa_data)
            if "semanticscholar" in url:
                return _mock_resp(s2_data)
            raise AssertionError(f"unexpected URL: {url}")

        with patch(
            "lit.preprint_lookup._request_with_retry", side_effect=_route,
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert len(versions) == 1
        assert versions[0].source == "arxiv"
        assert versions[0].id == "2401.99999"

    def test_s2_skipped_when_openalex_already_has_arxiv(self):
        """Don't waste the S2 call if OpenAlex already gave us an arXiv version."""
        oa_data = {
            "locations": [{
                "source": {"display_name": "arXiv"},
                "landing_page_url": "https://arxiv.org/abs/2401.11111",
                "pdf_url": None,
            }],
            "best_oa_location": None,
        }

        s2_call_count = [0]
        def _route(*args, **kwargs):
            url = args[1] if len(args) > 1 else kwargs.get("url")
            if "semanticscholar" in url:
                s2_call_count[0] += 1
            return _mock_resp(oa_data)

        with patch(
            "lit.preprint_lookup._request_with_retry", side_effect=_route,
        ):
            versions = find_preprint_versions(doi="10.1/x")
        assert versions[0].id == "2401.11111"
        assert s2_call_count[0] == 0

    def test_multiple_distinct_preprints(self):
        data = {
            "locations": [
                {
                    "source": {"display_name": "arXiv"},
                    "landing_page_url": "https://arxiv.org/abs/2401.11111",
                    "pdf_url": None,
                },
                {
                    "source": {"display_name": "ChemRxiv"},
                    "landing_page_url": "https://chemrxiv.org/engage/chemrxiv/article-details/10.26434/chemrxiv-2024-xyz",
                    "pdf_url": None,
                },
                {
                    "source": {"display_name": "bioRxiv"},
                    "landing_page_url": "https://www.biorxiv.org/content/10.1101/2024.01.01.999999v1",
                    "pdf_url": None,
                },
            ],
            "best_oa_location": None,
        }
        with patch(
            "lit.preprint_lookup._request_with_retry",
            return_value=_mock_resp(data),
        ):
            versions = find_preprint_versions(doi="10.1/x")
        # Sorted by priority: arxiv → biorxiv → chemrxiv
        assert [v.source for v in versions] == ["arxiv", "biorxiv", "chemrxiv"]
