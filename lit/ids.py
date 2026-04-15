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


def extract_paper_id(raw: str) -> tuple[str, str]:
    """Classify an arbitrary paper identifier.

    Returns (id_type, id_value) where id_type is one of:
    - "arxiv"   — clean arXiv ID, version suffix stripped
    - "pmid"    — PubMed numeric ID
    - "pmcid"   — PMC ID (e.g. "PMC1234567")
    - "doi"     — DOI string (no scheme/host prefix)
    - "unknown" — free text, caller should treat as a search query

    Recognises bare IDs, arxiv: / arXiv: / PMC prefixes, and URLs for
    arxiv.org, doi.org, pubmed.ncbi.nlm.nih.gov, pmc.ncbi.nlm.nih.gov.
    """
    s = raw.strip()

    if "arxiv.org" in s:
        m = re.search(r"(\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+/\d{7})", s)
        if m:
            return ("arxiv", re.sub(r"v\d+$", "", m.group(1)))

    m = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", s)
    if m:
        return ("pmid", m.group(1))

    m = re.search(r"pmc\.ncbi\.nlm\.nih\.gov/articles?/(PMC\d+)", s, re.IGNORECASE)
    if m:
        return ("pmcid", m.group(1).upper())

    if s.lower().startswith("arxiv:"):
        return ("arxiv", re.sub(r"v\d+$", "", s[6:]))

    if re.match(r"^PMC\d+$", s, re.IGNORECASE):
        return ("pmcid", s.upper())

    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
        if s.lower().startswith(prefix):
            return ("doi", s[len(prefix):])

    if re.match(r"^10\.\d+/", s):
        return ("doi", s)

    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", s):
        return ("arxiv", re.sub(r"v\d+$", "", s))

    if re.match(r"^[a-z-]+/\d{7}$", s):
        return ("arxiv", s)

    if re.match(r"^\d{7,9}$", s):
        return ("pmid", s)

    return ("unknown", s)
