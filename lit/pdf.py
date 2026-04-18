"""PDF utilities: magic-byte validation, text extraction, save-with-text, manual ingest.

Centralised here so every full-text path (arXiv, PMC, OA mirror, manual)
shares one ``%PDF`` check, one PyMuPDF wrapper, one save layout, and one
``--from-file`` ingest. Functions take ``output_dir`` explicitly so callers
control where files land — ``arxiv_tool.py`` passes its module-level
``OUTPUT_DIR`` (which tests reassign for sandboxing).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz  # PyMuPDF


# DOI pattern per Crossref guidance: 10. followed by a 4-9 digit registrant,
# slash, then any non-space characters. We trim trailing punctuation that
# commonly rides along in text (periods, commas, parens) after matching.
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)


def _normalize_doi(raw: str) -> str:
    """Strip surrounding noise from a candidate DOI string."""
    raw = raw.strip().rstrip(".,;)]}>")
    # Strip a trailing citation marker like "doi:10.xxx/foo." that occasionally
    # hangs off URL-encoded DOIs in PDF metadata.
    return raw.lower()


def extract_doi_from_pdf(pdf_bytes: bytes) -> str | None:
    """Best-effort DOI recovery from a PDF's own content.

    Source order (publisher-provided metadata is the most reliable):

    1. PyMuPDF document metadata (``/Subject``, ``/Keywords``, ``/Title``,
       and the rare ``/doi`` key). Many Elsevier / Nature / Springer /
       AAAS PDFs embed the DOI here.
    2. First-page plain text — typical "Cite this article" / "Available
       online at https://doi.org/..." strings sit near the top.

    Returns the lowercased DOI string (e.g. ``"10.1038/s41586-025-08800-x"``)
    or ``None`` when nothing parseable is found.
    """
    if not is_pdf_bytes(pdf_bytes):
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        meta = doc.metadata or {}
        for key in ("doi", "Doi", "DOI", "subject", "Subject", "keywords", "Keywords", "title", "Title"):
            val = meta.get(key) or ""
            m = _DOI_RE.search(val)
            if m:
                return _normalize_doi(m.group(0))

        # First page only — DOI is conventionally on the title page header,
        # and scanning the whole document is both slower and noisier.
        try:
            first_page = doc[0].get_text() if doc.page_count else ""
        except Exception:
            first_page = ""
        m = _DOI_RE.search(first_page)
        if m:
            return _normalize_doi(m.group(0))
    finally:
        doc.close()
    return None


def is_pdf_bytes(data: bytes | None) -> bool:
    """True iff bytes begin with the ``%PDF`` magic header.

    The tool's full-text chains often see HTML (Cloudflare challenges,
    paywall landing pages, 404 bodies) returned with ``Content-Type:
    application/pdf`` — magic-byte validation is the only reliable filter.
    """
    return bool(data) and data[:4] == b"%PDF"


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Return PyMuPDF's plain-text rendering of ``pdf_bytes``, or ``None``.

    Returns ``None`` for both unparseable PDFs and PyMuPDF crashes; the
    caller decides whether to keep the raw PDF anyway.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        return "\n".join(page.get_text().strip() for page in doc)
    finally:
        doc.close()


def save_pdf_and_text(
    pdf_bytes: bytes,
    out_basename: str,
    output_dir: Path,
    *,
    source_url: str | None = None,
) -> None:
    """Write ``{basename}.pdf`` + ``{basename}.txt`` under ``output_dir``.

    The ``.txt`` is PyMuPDF's text extraction wrapped with a markdown
    header — readable by an LLM directly. If extraction fails the raw PDF
    is still saved and a stderr warning is printed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"{out_basename}.pdf"
    txt_path = output_dir / f"{out_basename}.txt"
    pdf_path.write_bytes(pdf_bytes)
    print(f"Saved PDF: {pdf_path} ({len(pdf_bytes):,} bytes)")

    text = extract_pdf_text(pdf_bytes)
    if not text:
        print("PDF text extraction failed; raw PDF is still usable.", file=sys.stderr)
        return

    header = f"# {out_basename}\n"
    if source_url:
        header += f"\nURL: {source_url}\n"
    txt_path.write_text(f"{header}\n## Full Text\n\n{text}", encoding="utf-8")
    print(f"Saved text: {txt_path} ({len(text):,} chars)")


def ingest_local_pdf(path_str: str, out_basename: str, output_dir: Path) -> None:
    """Manual escape hatch: read a user-supplied file, validate, save.

    Exits with code 1 if the file is missing or fails the ``%PDF`` magic
    check — callers (``cmd_fulltext --from-file``) treat both as fatal.
    """
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        print(f"Local file not found: {p}", file=sys.stderr)
        sys.exit(1)
    data = p.read_bytes()
    if not is_pdf_bytes(data):
        print(
            f"File is not a PDF (missing %PDF magic header): {p}",
            file=sys.stderr,
        )
        sys.exit(1)
    save_pdf_and_text(data, out_basename, output_dir)
