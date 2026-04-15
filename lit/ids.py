"""Paper ID parsing and small formatting helpers.

Currently arXiv-only; future commits will add extract_paper_id() to cover
DOI / PMID / PMC / etc.
"""

from __future__ import annotations

import re
from datetime import datetime as _datetime


def extract_arxiv_id(input_str: str) -> str:
    """Extract an arXiv ID from various input formats.

    Supports:
    - 2401.12345
    - arXiv:2401.12345
    - https://arxiv.org/abs/2401.12345
    - https://arxiv.org/pdf/2401.12345.pdf
    - cs/0401001 (old format)
    """
    patterns = [
        r"(\d{4}\.\d{4,5}(?:v\d+)?)",
        r"([a-z-]+/\d{7})",
    ]
    for pattern in patterns:
        match = re.search(pattern, input_str)
        if match:
            return match.group(1)
    return input_str


def sanitize_filename(name: str, max_length: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name)
    if len(name) > max_length:
        name = name[:max_length]
    return name.strip("._")


def _arxiv_date(arxiv_id: str) -> _datetime | None:
    """Extract submission year+month from an arXiv ID -> YYYY-MM-01."""
    m = re.match(r"(\d{2})(\d{2})\.\d+", arxiv_id)
    if not m:
        m = re.match(r"[a-z-]+/(\d{2})(\d{2})\d{3}", arxiv_id)
    if not m:
        return None
    yy, mm = int(m.group(1)), int(m.group(2))
    if not 1 <= mm <= 12:
        return None
    year = 1900 + yy if yy >= 91 else 2000 + yy
    return _datetime(year, mm, 1)


def _arxiv_year(arxiv_id: str) -> int | None:
    """Extract submission year from an arXiv ID.

    Authoritative source: the arXiv ID itself encodes the submission date
    (new format YYMM.XXXXX or old format subject/YYMMNNN).
    """
    d = _arxiv_date(arxiv_id)
    return d.year if d else None


def _truncate_authors(names: list[str], limit: int = 3) -> str:
    result = ", ".join(names[:limit])
    if len(names) > limit:
        result += "..."
    return result
