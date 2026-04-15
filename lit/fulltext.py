"""arXiv LaTeX source + PDF fallback download."""

from __future__ import annotations

import gzip
import io
import re
import shutil
import sys
import tarfile
from pathlib import Path

import fitz  # PyMuPDF
import requests

from lit.config import HTTP_HEADERS, _MIN_PDF_BYTES
from lit.ids import extract_arxiv_id, sanitize_filename
from lit.ratelimit import _request_with_retry


def fetch_tex_source(arxiv_id: str, output_dir: Path) -> Path | None:
    """Download and extract arXiv LaTeX source.

    Does not call any API — fetches e-print directly. Directory is named after
    the arXiv ID, then renamed with the extracted \\title{} when available.

    Returns the extracted directory, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = extract_arxiv_id(arxiv_id)
    dir_id = re.sub(r"v\d+$", "", clean_id).replace("/", "_")
    target_dir = output_dir / dir_id

    if target_dir.exists():
        print(f"Already exists: {target_dir}")
        return target_dir
    existing = [p for p in output_dir.glob(f"{dir_id}_*") if p.is_dir()]
    if existing:
        print(f"Already exists: {existing[0]}")
        return existing[0]

    source_url = f"https://arxiv.org/e-print/{clean_id}"
    print(f"Downloading source: {source_url}")

    try:
        response = _request_with_retry(requests.get, source_url, service="arxiv", headers=HTTP_HEADERS, timeout=60)
    except requests.RequestException as e:
        print(f"Download failed: {e}", file=sys.stderr)
        return None

    content = response.content

    target_dir.mkdir(parents=True, exist_ok=True)
    print("Extracting source...")
    try:
        _extract_source(content, target_dir)
    except Exception as e:
        print(f"Extraction failed: {e}", file=sys.stderr)
        shutil.rmtree(target_dir, ignore_errors=True)
        return None

    new_dir = _try_rename_with_title(target_dir, dir_id, output_dir)
    if new_dir:
        target_dir = new_dir

    print(f"Saved to: {target_dir}")
    return target_dir


def _extract_source(content: bytes, target_dir: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            tar.extractall(target_dir, filter="data")
            print("Extracted as tar.gz")
            return
    except tarfile.ReadError:
        pass

    try:
        decompressed = gzip.decompress(content)
        try:
            with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r") as tar:
                tar.extractall(target_dir, filter="data")
                print("Extracted as gzip+tar")
                return
        except tarfile.ReadError:
            tex_file = target_dir / "main.tex"
            tex_file.write_bytes(decompressed)
            print("Extracted as single tex file")
            return
    except gzip.BadGzipFile:
        pass

    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r") as tar:
            tar.extractall(target_dir, filter="data")
            print("Extracted as tar")
            return
    except tarfile.ReadError:
        pass

    tex_file = target_dir / "main.tex"
    tex_file.write_bytes(content)
    print("Saved as single tex file (uncompressed)")


def _extract_braced_arg(text: str, start: int) -> str | None:
    """Return the text inside text[start]'s '{...}', supporting nesting."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None


def _strip_tex_comments(content: str) -> str:
    """Drop TeX % comments (full-line and inline, ignoring \\%)."""
    result = []
    for line in content.split("\n"):
        if line.lstrip().startswith("%"):
            continue
        result.append(re.sub(r"(?<!\\)%.*$", "", line))
    return "\n".join(result)


def _try_rename_with_title(
    target_dir: Path, dir_id: str, output_dir: Path
) -> Path | None:
    tex_files = list(target_dir.glob("*.tex"))
    if not tex_files:
        return None

    main_tex = next((f for f in tex_files if f.name == "main.tex"), tex_files[0])

    content = main_tex.read_text(encoding="utf-8", errors="ignore")
    content = _strip_tex_comments(content)
    match = re.search(r"\\title\s*\{", content)
    if not match:
        return None

    raw_title = _extract_braced_arg(content, match.end() - 1)
    if not raw_title:
        return None

    raw_title = re.sub(r"\\\\", " ", raw_title)
    raw_title = re.sub(r"\\[a-zA-Z]+\s*(\{[^}]*\})?", " ", raw_title)
    raw_title = re.sub(r"[{}]", "", raw_title)
    raw_title = re.sub(r"\s+", " ", raw_title).strip()
    if not raw_title:
        return None

    safe_title = sanitize_filename(raw_title, max_length=40)
    new_dir = output_dir / f"{dir_id}_{safe_title}"
    if new_dir.exists():
        return None

    target_dir.rename(new_dir)
    print(f"Renamed to: {new_dir.name}")
    return new_dir


def _fetch_pdf_fallback(arxiv_id: str, output_dir: Path) -> None:
    """Fallback when tex download fails: fetch PDF and extract text."""
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = extract_arxiv_id(arxiv_id)
    file_id = clean_id.replace("/", "_")
    txt_file = output_dir / f"{file_id}.txt"
    pdf_file = output_dir / f"{file_id}.pdf"

    if txt_file.exists():
        print(f"Already exists: {txt_file}")
        return

    pdf_url = f"https://arxiv.org/pdf/{clean_id}"
    print(f"Downloading PDF: {pdf_url}")
    response = _request_with_retry(requests.get, pdf_url, service="arxiv", headers=HTTP_HEADERS, timeout=60)
    if len(response.content) < _MIN_PDF_BYTES:
        print(f"Downloaded file only {len(response.content)} bytes, likely not a valid PDF", file=sys.stderr)
        return
    pdf_file.write_bytes(response.content)

    try:
        print("Extracting text...")
        doc = fitz.open(pdf_file)
        text = "\n".join(page.get_text().strip() for page in doc)
        doc.close()
    except Exception:
        pdf_file.unlink(missing_ok=True)
        raise

    txt_file.write_text(
        f"# arXiv:{clean_id}\n\nURL: https://arxiv.org/abs/{clean_id}\n\n## Full Text\n\n{text}",
        encoding="utf-8",
    )
    print(f"Saved PDF: {pdf_file}")
    print(f"Saved TXT: {txt_file}")


def print_tree(
    directory: Path, prefix: str = "", max_depth: int = 3, current_depth: int = 0
) -> list[str]:
    lines = []
    if current_depth >= max_depth:
        return lines

    items = sorted(directory.iterdir(), key=lambda x: (x.is_file(), x.name))
    for i, item in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{item.name}")

        if item.is_dir():
            extension = "    " if is_last else "│   "
            lines.extend(
                print_tree(item, prefix + extension, max_depth, current_depth + 1)
            )

    return lines
