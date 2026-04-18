"""Environment, paths, and HTTP constants shared across the package."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("ARXIV_CACHE_DIR", SCRIPT_DIR / ".arxiv"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Scratch space for ephemeral batch artefacts: failure manifests,
# human-friendly download guides, and the staging dir where the user drops
# manually-downloaded PDFs. Lives inside the repo so it's self-contained
# and easy to gitignore; never mixes with the shared cache at CACHE_DIR.
WORK_DIR = Path(os.environ.get("ARXIV_WORK_DIR", SCRIPT_DIR / ".work"))
MANUAL_PDF_DIR = WORK_DIR / "manual-pdfs"

# Optional per-user SCP hints the download_me.txt generator uses to render a
# ready-to-paste upload command. When unset it emits placeholders.
#   ARXIV_SCP_HOST    — the SSH alias / host the user connects from Windows
#   ARXIV_SCP_SOURCE  — the PowerShell-style glob for the PDF folder
SCP_HOST: str | None = os.environ.get("ARXIV_SCP_HOST")
SCP_SOURCE: str | None = os.environ.get("ARXIV_SCP_SOURCE")

load_dotenv(CACHE_DIR / ".env", override=False)

S2_API_KEY: str | None = os.environ.get("S2_API_KEY")
OPENALEX_API_KEY: str | None = os.environ.get("OPENALEX_API_KEY")
PUBMED_API_KEY: str | None = os.environ.get("PUBMED_API_KEY")
CORE_API_KEY: str | None = os.environ.get("CORE_API_KEY")
CONTACT_EMAIL: str = os.environ.get("CONTACT_EMAIL", "")

S2_API_BASE = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API_BASE = "https://api.openalex.org"
PUBMED_API_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC_API_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
CORE_API_BASE = "https://api.core.ac.uk/v3"

_mailto = f" (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else ""
HTTP_HEADERS = {
    "User-Agent": f"arxiv-tool/1.0{_mailto}",
}

_MIN_PDF_BYTES = 10_240
