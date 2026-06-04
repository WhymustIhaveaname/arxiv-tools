"""OCR fallback for scanned/image-only PDFs.

Hybrid pipeline: PyMuPDF direct text → Tesseract OCR → Claude Vision.
Only invoked when ``extract_pdf_text`` returns empty/near-empty text.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Per-page OCR
# ---------------------------------------------------------------------------


def _ocr_with_tesseract(image_bytes: bytes) -> str | None:
    """Run Tesseract OCR on a single page image.  Returns extracted text or *None*."""
    try:
        import pytesseract
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="eng")
        return text.strip() or None
    except ImportError:
        print(
            "  pytesseract not installed; install with `pip install pytesseract`.",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"  Tesseract OCR failed: {exc}", file=sys.stderr)
        return None


def _ocr_quality_is_low(text: str) -> bool:
    """Heuristic to flag garbled OCR (diagram noise, column-interleave artefacts).

    Returns *True* when the OCR output is likely too poor to use directly.
    """
    if not text or len(text) < 100:
        return True

    lines = text.split("\n")
    if not lines:
        return True

    noise_lines = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Isolated single non-alpha character — hallmark of logo / diagram noise
        if len(stripped) == 1 and not stripped.isalpha():
            noise_lines += 1
            continue
        # Lines that are mostly non-alpha and non-space → diagram / table noise
        if len(stripped) > 3:
            alpha = sum(1 for c in stripped if c.isalpha() or c.isspace())
            if alpha / len(stripped) < 0.3:
                noise_lines += 1

    return (noise_lines / len(lines)) > 0.15


def _call_claude_vision(image_path: Path, page_num: int) -> str | None:
    """Transcribe a page image with Claude Opus 4.8 via the local ``claude`` CLI.

    Returns the cleaned transcription, or *None* on any failure (CLI missing,
    timeout, non-zero exit, …).
    """
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    prompt = (
        f"Read {image_path}. This is page {page_num} of a scanned academic paper. "
        "Transcribe ALL visible text precisely.  IMPORTANT rules: "
        "(1) Skip any download watermark (e.g. 'Downloaded by …'). "
        "(2) For chemical structure diagrams, describe key features in [square brackets]. "
        "(3) For tables, preserve column alignment. "
        "(4) Transcribe spectral/NMR data exactly as printed. "
        "(5) Read multi-column layouts in correct reading order. "
        "(6) Output ONLY the raw transcription — zero preamble, zero commentary."
    )

    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", "opus", "--add-dir", str(image_path.parent)],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        print(
            "  claude CLI not found; set CLAUDE_BIN env var or install Claude Code.",
            file=sys.stderr,
        )
        return None
    except subprocess.TimeoutExpired:
        print("  Claude Vision call timed out (>180 s).", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  Claude Vision call failed: {exc}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"  Claude CLI exited {result.returncode}: {result.stderr[:200]}",
            file=sys.stderr,
        )
        return None

    text = result.stdout.strip()

    # ── strip common Claude preambles ──────────────────────────────
    preambles = [
        "Here is my transcription",
        "Here is the transcription",
        "Here's the transcription",
        "Here is the complete transcription",
        "Here is the transcribed text",
        "Here is the raw transcription",
    ]
    for preamble in preambles:
        if preamble in text[:300]:
            idx = text.find(preamble)
            after = text[idx + len(preamble):]
            # Remove colon / "of the page:" / "of page X:"
            after = re.sub(r"^[:\s]+", "", after)
            after = re.sub(r"^of\s+(the\s+)?page\s*\d*[:\s]*\n*", "", after, flags=re.IGNORECASE)
            # Remove leading --- fenced block
            after = re.sub(r"^---+\s*\n?", "", after)
            text = after
            break

    # Remove trailing "---" fences that Claude sometimes wraps with
    text = re.sub(r"\n---+\s*$", "", text)

    return text.strip() or None


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------


def ocr_scanned_pdf(
    pdf_bytes: bytes,
    *,
    dpi: int = 300,
    use_claude: bool = True,
    max_claude_pages: int = 20,
) -> str:
    """OCR every page of an image-only PDF with a hybrid fallback per page.

    1. PyMuPDF ``get_text()``  — keep the page as-is when it already has text.
    2. Tesseract OCR            — fast, local, free.
    3. Claude Opus 4.8 Vision   — only when Tesseract quality looks low.

    Parameters
    ----------
    pdf_bytes:
        Raw PDF content.
    dpi:
        Render resolution passed to ``Page.get_pixmap(dpi=...)``.
    use_claude:
        Set to *False* to skip Claude Vision entirely (offline / air-gapped).
    max_claude_pages:
        Hard cap on Claude API calls per paper (avoids runaway cost for
        huge documents).

    Returns
    -------
    str
        The combined text of all pages, separated by blank lines.
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        print(f"Cannot open PDF for OCR: {exc}", file=sys.stderr)
        return ""

    total = doc.page_count
    pages_text: list[str] = []
    claude_calls = 0
    tess_ok = 0
    claude_ok = 0
    empty = 0

    for i, page in enumerate(doc):
        pg_num = i + 1

        # 1.  Direct text layer → keep it
        direct = page.get_text().strip()
        if direct:
            pages_text.append(direct)
            continue

        # 2.  Render page to image (300 DPI)
        try:
            pix = page.get_pixmap(dpi=dpi)
        except Exception:
            pages_text.append("")
            empty += 1
            continue
        img_bytes = pix.tobytes("png")

        # 3.  Tesseract
        tess_text = _ocr_with_tesseract(img_bytes)

        if tess_text and not _ocr_quality_is_low(tess_text):
            pages_text.append(tess_text)
            tess_ok += 1
            continue

        # 4.  Claude Vision (when Tesseract quality is low)
        if use_claude and claude_calls < max_claude_pages:
            with tempfile.NamedTemporaryFile(
                suffix=".png", prefix=f"ocr_pg{pg_num:02d}_", delete=False
            ) as tmp:
                tmp.write(img_bytes)
                tmp_path = Path(tmp.name)

            try:
                claude_text = _call_claude_vision(tmp_path, pg_num)
                if claude_text:
                    pages_text.append(claude_text)
                    claude_calls += 1
                    claude_ok += 1
                else:
                    # Claude failed — use Tesseract even if noisy
                    pages_text.append(tess_text or "")
                    if tess_text:
                        tess_ok += 1
                    else:
                        empty += 1
            finally:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
        elif tess_text:
            pages_text.append(tess_text)
            tess_ok += 1
        else:
            pages_text.append("")
            empty += 1

    doc.close()

    # ── summary to stderr ─────────────────────────────────────────
    parts = []
    if tess_ok:
        parts.append(f"{tess_ok} Tesseract")
    if claude_ok:
        parts.append(f"{claude_ok} Claude Vision")
    if empty:
        parts.append(f"{empty} empty")
    if parts:
        print(
            f"  OCR complete ({total} pages): {', '.join(parts)}",
            file=sys.stderr,
        )

    return "\n\n".join(pages_text)
