"""Tests for lit/pdf.py — PDF utilities and DOI extraction."""

from __future__ import annotations

import fitz

from lit.pdf import extract_doi_from_pdf, is_pdf_bytes


def _make_pdf(*, metadata: dict | None = None, page_text: str = "") -> bytes:
    """Build a real one-page PDF in memory for extraction tests."""
    doc = fitz.open()
    page = doc.new_page()
    if page_text:
        page.insert_text((72, 72), page_text, fontsize=10)
    if metadata:
        # PyMuPDF requires a full metadata dict; fill unset keys with empty strings.
        full = {
            "title": "", "author": "", "subject": "", "keywords": "",
            "creator": "", "producer": "", "creationDate": "", "modDate": "",
            "trapped": "", "encryption": None, "format": "PDF-1.7",
        }
        full.update(metadata)
        doc.set_metadata(full)
    data = doc.tobytes()
    doc.close()
    return data


class TestIsPdfBytes:
    def test_valid_pdf(self):
        assert is_pdf_bytes(b"%PDF-1.4\n...")

    def test_html_rejected(self):
        assert not is_pdf_bytes(b"<!DOCTYPE html>")

    def test_empty_rejected(self):
        assert not is_pdf_bytes(b"")
        assert not is_pdf_bytes(None)


class TestExtractDoiFromPdf:
    def test_doi_in_metadata_subject(self):
        pdf = _make_pdf(metadata={"subject": "doi:10.1038/s41586-025-08800-x"})
        assert extract_doi_from_pdf(pdf) == "10.1038/s41586-025-08800-x"

    def test_doi_in_first_page_text(self):
        pdf = _make_pdf(
            page_text="Available online at https://doi.org/10.1016/j.cell.2022.09.002",
        )
        assert extract_doi_from_pdf(pdf) == "10.1016/j.cell.2022.09.002"

    def test_metadata_beats_page_text(self):
        """When both have a DOI the metadata value wins."""
        pdf = _make_pdf(
            metadata={"keywords": "10.1038/nature12345"},
            page_text="Cite as: 10.9999/wrong.999",
        )
        assert extract_doi_from_pdf(pdf) == "10.1038/nature12345"

    def test_trailing_punct_stripped(self):
        pdf = _make_pdf(page_text="See 10.1038/nature24265, also ...")
        assert extract_doi_from_pdf(pdf) == "10.1038/nature24265"

    def test_no_doi_returns_none(self):
        pdf = _make_pdf(page_text="A paper without any DOI reference anywhere.")
        assert extract_doi_from_pdf(pdf) is None

    def test_non_pdf_bytes_returns_none(self):
        assert extract_doi_from_pdf(b"<html>not a PDF</html>") is None

    def test_empty_bytes_returns_none(self):
        assert extract_doi_from_pdf(b"") is None

    def test_lowercased(self):
        pdf = _make_pdf(metadata={"subject": "doi:10.1038/ABC.2023.DEF"})
        assert extract_doi_from_pdf(pdf) == "10.1038/abc.2023.def"
