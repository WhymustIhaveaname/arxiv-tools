"""Tests for lit/shadow.py — Anna's Archive + Sci-Hub fallback."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from lit import shadow
from lit.shadow import (
    _extract_pdf_url,
    _resolve,
    fetch_annas_archive,
    fetch_scihub,
    try_shadow_libraries,
)


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------


class TestResolveUrl:
    def test_protocol_relative(self):
        assert _resolve("//example.com/x.pdf", "https://base/") == "https://example.com/x.pdf"

    def test_root_relative(self):
        assert _resolve("/x.pdf", "https://base/foo/") == "https://base/x.pdf"

    def test_relative(self):
        assert _resolve("x.pdf", "https://base/foo/") == "https://base/foo/x.pdf"

    def test_absolute_unchanged(self):
        u = "https://other.com/x.pdf"
        assert _resolve(u, "https://base/") == u


class TestExtractPdfUrl:
    def test_embed_src(self):
        html = '<html><embed type="application/pdf" src="//sci-hub.se/dl/1234.pdf"></html>'
        assert _extract_pdf_url(html, "https://sci-hub.se/10.1/x") == "https://sci-hub.se/dl/1234.pdf"

    def test_iframe_src(self):
        html = '<html><iframe src="https://annas.li/cdn/x.pdf" /></html>'
        assert _extract_pdf_url(html, "https://annas.li/scidb/10.1/x") == "https://annas.li/cdn/x.pdf"

    def test_pdf_link_fallback(self):
        html = '<html><a href="/cdn/abc.pdf">download</a></html>'
        assert (
            _extract_pdf_url(html, "https://annas.li/scidb/10.1/x")
            == "https://annas.li/cdn/abc.pdf"
        )

    def test_no_match_returns_none(self):
        assert _extract_pdf_url("<html><body>no pdf here</body></html>", "https://x/") is None

    def test_citation_pdf_url_meta_tag(self):
        """Sci-Hub canonical 2026 form: <meta name="citation_pdf_url" ...>"""
        html = (
            '<html><head>'
            '<meta name="citation_pdf_url" '
            'content="/storage/twin/6400/abc/boyle2017.pdf">'
            '</head></html>'
        )
        assert (
            _extract_pdf_url(html, "https://sci-hub.ru/10.1016/j.cell.2017.05.038")
            == "https://sci-hub.ru/storage/twin/6400/abc/boyle2017.pdf"
        )

    def test_citation_meta_takes_priority_over_embed(self):
        html = (
            '<meta name="citation_pdf_url" content="/real.pdf">'
            '<embed src="/wrong.pdf">'
        )
        assert _extract_pdf_url(html, "https://h/") == "https://h/real.pdf"


# --------------------------------------------------------------------------
# fetch_annas_archive / fetch_scihub
# --------------------------------------------------------------------------


def _resp(content: bytes = b"", text: str = "", url: str = "https://x/") -> MagicMock:
    r = MagicMock()
    r.content = content
    r.text = text
    r.url = url
    r.raise_for_status.return_value = None
    return r


class TestFetchScihub:
    def test_streamed_pdf_returned_directly(self):
        """If the landing URL itself responds with a PDF body, no need to parse."""
        with patch(
            "lit.shadow._request_with_retry",
            return_value=_resp(content=b"%PDF-1.4 binary stuff"),
        ):
            out = fetch_scihub("10.1038/foo")
        assert out == b"%PDF-1.4 binary stuff"

    def test_landing_html_with_embed(self):
        landing = _resp(
            content=b"<html>...",
            text='<embed src="//sci-hub.se/downloads/2024/01/foo.pdf">',
            url="https://sci-hub.se/10.1038/foo",
        )
        pdf = _resp(content=b"%PDF-1.5 stuff")
        with patch(
            "lit.shadow._request_with_retry", side_effect=[landing, pdf],
        ):
            out = fetch_scihub("10.1038/foo")
        assert out == b"%PDF-1.5 stuff"

    def test_returns_none_when_no_pdf_link_in_html(self):
        landing = _resp(text="<html>article not found</html>")
        with patch("lit.shadow._request_with_retry", return_value=landing):
            assert fetch_scihub("10.1/missing") is None

    def test_returns_none_when_pdf_response_is_not_pdf(self):
        """Embedded URL pointed somewhere, but it returned HTML (CF challenge etc.)."""
        landing = _resp(
            text='<embed src="https://sci-hub.se/dl/foo.pdf">',
            url="https://sci-hub.se/10.1/x",
        )
        not_a_pdf = _resp(content=b"<!DOCTYPE html><html>blocked</html>")
        with patch(
            "lit.shadow._request_with_retry", side_effect=[landing, not_a_pdf],
        ):
            assert fetch_scihub("10.1/x") is None

    def test_landing_request_failure_returns_none(self):
        with patch(
            "lit.shadow._request_with_retry",
            side_effect=requests.RequestException("DNS"),
        ):
            assert fetch_scihub("10.1/x") is None

    def test_pdf_request_failure_returns_none(self):
        landing = _resp(
            text='<embed src="https://sci-hub.se/dl/foo.pdf">',
            url="https://sci-hub.se/10.1/x",
        )
        with patch(
            "lit.shadow._request_with_retry",
            side_effect=[landing, requests.RequestException("connection reset")],
        ):
            assert fetch_scihub("10.1/x") is None


class TestFetchAnnasArchive:
    def test_iframe_landing(self):
        landing = _resp(
            text='<iframe src="/cdn/scidb/10.1038/foo.pdf"></iframe>',
            url="https://annas-archive.li/scidb/10.1038/foo",
        )
        pdf = _resp(content=b"%PDF-1.7 binary")
        with patch(
            "lit.shadow._request_with_retry", side_effect=[landing, pdf],
        ):
            out = fetch_annas_archive("10.1038/foo")
        assert out == b"%PDF-1.7 binary"


# --------------------------------------------------------------------------
# try_shadow_libraries — ordering + skip behaviour
# --------------------------------------------------------------------------


class TestTryShadowLibraries:
    def test_returns_none_for_empty_doi(self):
        assert try_shadow_libraries(None) is None
        assert try_shadow_libraries("") is None

    def test_walks_in_configured_order_returns_first_hit(self):
        """SHADOW_LIBRARIES = annas,scihub → annas tried first; if it
        returns bytes we never call scihub."""
        with patch.object(shadow, "SHADOW_LIBRARIES", ("annas", "scihub")), \
             patch("lit.shadow.fetch_annas_archive", return_value=b"%PDF-from-annas") as mock_a, \
             patch("lit.shadow.fetch_scihub") as mock_s, \
             patch("lit.shadow._FETCHERS", {
                 "annas": shadow.fetch_annas_archive,
                 "scihub": shadow.fetch_scihub,
             }):
            out = try_shadow_libraries("10.1/x")
        assert out == b"%PDF-from-annas"
        mock_a.assert_called_once()
        mock_s.assert_not_called()

    def test_falls_through_to_next_when_first_fails(self):
        with patch.object(shadow, "SHADOW_LIBRARIES", ("annas", "scihub")), \
             patch("lit.shadow.fetch_annas_archive", return_value=None), \
             patch("lit.shadow.fetch_scihub", return_value=b"%PDF-from-scihub"), \
             patch("lit.shadow._FETCHERS", {
                 "annas": shadow.fetch_annas_archive,
                 "scihub": shadow.fetch_scihub,
             }):
            out = try_shadow_libraries("10.1/x")
        assert out == b"%PDF-from-scihub"

    def test_unknown_library_name_skipped(self):
        with patch.object(shadow, "SHADOW_LIBRARIES", ("nonexistent", "scihub")), \
             patch("lit.shadow.fetch_scihub", return_value=b"%PDF-ok"), \
             patch("lit.shadow._FETCHERS", {"scihub": shadow.fetch_scihub}):
            out = try_shadow_libraries("10.1/x")
        assert out == b"%PDF-ok"

    def test_all_fail_returns_none(self):
        with patch.object(shadow, "SHADOW_LIBRARIES", ("annas", "scihub")), \
             patch("lit.shadow.fetch_annas_archive", return_value=None), \
             patch("lit.shadow.fetch_scihub", return_value=None), \
             patch("lit.shadow._FETCHERS", {
                 "annas": shadow.fetch_annas_archive,
                 "scihub": shadow.fetch_scihub,
             }):
            assert try_shadow_libraries("10.1/x") is None

    def test_default_libraries_include_both(self):
        """The default config (no env override) should enable both."""
        assert "annas" in shadow.SHADOW_LIBRARIES
        assert "scihub" in shadow.SHADOW_LIBRARIES
