"""Environment, paths, and HTTP constants shared across the package."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("ARXIV_CACHE_DIR", SCRIPT_DIR / ".arxiv"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(CACHE_DIR / ".env", override=False)

S2_API_KEY: str | None = os.environ.get("S2_API_KEY")
OPENALEX_API_KEY: str | None = os.environ.get("OPENALEX_API_KEY")
PUBMED_API_KEY: str | None = os.environ.get("PUBMED_API_KEY")
CONTACT_EMAIL: str = os.environ.get("CONTACT_EMAIL", "")

S2_API_BASE = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API_BASE = "https://api.openalex.org"
PUBMED_API_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_mailto = f" (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else ""
HTTP_HEADERS = {
    "User-Agent": f"arxiv-tool/1.0{_mailto}",
}

_MIN_PDF_BYTES = 10_240
