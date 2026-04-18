"""Tests for lit/pdf.py — PDF utilities and DOI extraction."""

from __future__ import annotations

import fitz

from lit.pdf import extract_doi_from_pdf, is_pdf_bytes, save_pdf_and_text


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

    def test_chemrxiv_version_suffix_stripped(self):
        """ChemRxiv's versioned DOI collapses to the unversioned base so
        fulltext-batch and fulltext-import share a cache basename."""
        pdf = _make_pdf(metadata={"subject": "10.26434/chemrxiv-2024-zmmnw-v2"})
        assert extract_doi_from_pdf(pdf) == "10.26434/chemrxiv-2024-zmmnw"

    def test_chemrxiv_higher_version_stripped(self):
        pdf = _make_pdf(metadata={"subject": "10.26434/chemrxiv-2023-abcde-v11"})
        assert extract_doi_from_pdf(pdf) == "10.26434/chemrxiv-2023-abcde"

    def test_non_chemrxiv_v_suffix_untouched(self):
        """Real publisher DOIs sometimes legitimately end in -v2 or similar
        (arXiv crossref DOIs, some Zenodo records). Only 10.26434/ prefix
        collapses."""
        pdf = _make_pdf(metadata={"subject": "10.48550/arxiv.2401.12345"})
        assert extract_doi_from_pdf(pdf) == "10.48550/arxiv.2401.12345"


class TestSavePdfAndTextWarnsOnScanned:
    """save_pdf_and_text emits a stderr warning for image-only PDFs so the
    user knows the downstream .txt is unusable. Triggered by a large PDF
    (>100 KB) paired with near-zero extractable text."""

    def test_warns_when_large_pdf_yields_no_text(self, tmp_path, capsys):
        # An image-only PDF: valid %PDF bytes padded with noise to exceed
        # the size threshold, no text inserted so PyMuPDF returns empty.
        pdf_bytes = _make_pdf()  # empty doc, no text
        padded = pdf_bytes + b"%scan-noise-padding " * 10_000  # ~190 KB
        # Append after %%EOF is tolerated by fitz — it still parses the
        # leading valid PDF and extracts zero chars.
        save_pdf_and_text(padded, "scanned_basename", tmp_path)
        err = capsys.readouterr().err
        assert "scanned/image-only" in err
        assert "scanned_basename" in err

    def test_no_warning_on_normal_pdf(self, tmp_path, capsys):
        pdf = _make_pdf(page_text="lorem ipsum " * 200)  # plenty of text
        save_pdf_and_text(pdf, "normal_basename", tmp_path)
        assert "scanned/image-only" not in capsys.readouterr().err

    def test_no_warning_on_tiny_pdf(self, tmp_path, capsys):
        """Below the 100 KB threshold we don't warn; a tiny real PDF
        might legitimately be near-empty (a one-line memo, a cover sheet)."""
        pdf = _make_pdf()  # empty, small
        save_pdf_and_text(pdf, "tiny_basename", tmp_path)
        assert "scanned/image-only" not in capsys.readouterr().err
