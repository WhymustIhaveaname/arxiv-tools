"""Shadow library full-text fallback (Anna's Archive + Sci-Hub).

Last resort for paywalled DOIs that no OA mirror, OA aggregator or preprint
twin could deliver. Disabled by setting ``SHADOW_LIBRARIES=`` to empty.

Mirror URLs change frequently as registries seize domains:

  - Anna's Archive: https://annas-archive.li / .gl / .se (the .org and .se
    domains were suspended after the April 2026 US court ruling)
  - Sci-Hub: https://sci-hub.se / .st / .ru / .ee

Override via environment if a mirror stops responding:

    ANNAS_MIRROR=https://annas-archive.gl
    SCIHUB_MIRROR=https://sci-hub.ru
    SHADOW_LIBRARIES=annas,scihub        # comma-separated; order = try priority

Coverage notes:
  - Anna's Archive (SciDB) keeps indexing past 2021, ~95M papers.
  - Sci-Hub stopped indexing in 2021 (still has ~88M older papers).
  - LibGen scimag overlaps Sci-Hub 99.5% — not added here, would not
    increase recall meaningfully.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urljoin

import requests

from lit.config import HTTP_HEADERS
from lit.pdf import is_pdf_bytes
from lit.ratelimit import _brief_error, _request_with_retry


ANNAS_MIRROR = os.environ.get("ANNAS_MIRROR", "https://annas-archive.li")
SCIHUB_MIRROR = os.environ.get("SCIHUB_MIRROR", "https://sci-hub.se")
SHADOW_LIBRARIES: tuple[str, ...] = tuple(
    s.strip().lower()
    for s in os.environ.get("SHADOW_LIBRARIES", "annas,scihub").split(",")
    if s.strip()
)


_BROWSERY_HEADERS = {
    **HTTP_HEADERS,
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) "
        "Gecko/20100101 Firefox/120.0"
    ),
}

# Both Sci-Hub and Anna's Archive embed the PDF in the landing HTML one of
# these ways. Try in order:
#   - citation_pdf_url meta tag (Sci-Hub canonical 2026 form)
#   - <embed> (older Sci-Hub form)
#   - <iframe> (Anna's mirrors)
#   - any href ending in .pdf (last-resort)
_CITATION_PDF_RE = re.compile(
    r"""<meta\s+name=["']citation_pdf_url["']\s+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_EMBED_SRC_RE = re.compile(r"""<embed[^>]*src=["']([^"']+)["']""", re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(r"""<iframe[^>]*src=["']([^"']+)["']""", re.IGNORECASE)
_PDF_LINK_RE = re.compile(r"""href=["']([^"']+\.pdf[^"']*)["']""", re.IGNORECASE)


def _resolve(url: str, base_url: str) -> str:
    """Turn protocol-relative or root-relative shadow-library URLs absolute."""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return urljoin(base_url, url)
    if not url.startswith(("http://", "https://")):
        return urljoin(base_url, url)
    return url


def _extract_pdf_url(html: str, base_url: str) -> str | None:
    """Heuristically pull a PDF URL out of a shadow-library landing HTML."""
    for pat in (_CITATION_PDF_RE, _EMBED_SRC_RE, _IFRAME_SRC_RE, _PDF_LINK_RE):
        m = pat.search(html)
        if m:
            return _resolve(m.group(1), base_url)
    return None


def _fetch_pdf_from_landing(landing_url: str, *, service: str) -> bytes | None:
    """Fetch landing HTML, parse out PDF URL, fetch the PDF, validate magic bytes."""
    headers = {
        **_BROWSERY_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = _request_with_retry(
            requests.get,
            landing_url,
            service=service,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(
            f"  shadow {service}: landing failed ({_brief_error(e)})",
            file=sys.stderr,
        )
        return None

    # A few mirrors stream the PDF directly when given a DOI in the URL.
    if is_pdf_bytes(resp.content):
        return resp.content

    pdf_url = _extract_pdf_url(resp.text or "", str(resp.url))
    if not pdf_url:
        return None

    pdf_headers = {
        **_BROWSERY_HEADERS,
        "Accept": "application/pdf,*/*;q=0.9",
        "Referer": landing_url,
    }
    try:
        pdf_resp = _request_with_retry(
            requests.get,
            pdf_url,
            service=service,
            headers=pdf_headers,
            timeout=60,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(
            f"  shadow {service}: PDF fetch failed ({_brief_error(e)})",
            file=sys.stderr,
        )
        return None

    if not is_pdf_bytes(pdf_resp.content):
        return None
    return pdf_resp.content


def fetch_annas_archive(doi: str) -> bytes | None:
    """Anna's Archive SciDB. ~95M papers, indexed through 2026."""
    landing = f"{ANNAS_MIRROR.rstrip('/')}/scidb/{doi}"
    return _fetch_pdf_from_landing(landing, service="annas")


def fetch_scihub(doi: str) -> bytes | None:
    """Sci-Hub. ~88M papers; indexing frozen at 2021 — best for older work."""
    landing = f"{SCIHUB_MIRROR.rstrip('/')}/{doi}"
    return _fetch_pdf_from_landing(landing, service="scihub")


_FETCHERS = {
    "annas": fetch_annas_archive,
    "scihub": fetch_scihub,
}


def try_shadow_libraries(doi: str | None) -> bytes | None:
    """Walk ``SHADOW_LIBRARIES`` in order; return first valid PDF."""
    if not doi:
        return None
    for name in SHADOW_LIBRARIES:
        fn = _FETCHERS.get(name)
        if fn is None:
            continue
        print(f"  trying shadow library: {name}", file=sys.stderr)
        pdf = fn(doi)
        if pdf:
            return pdf
        print(f"  shadow {name}: no PDF", file=sys.stderr)
    return None
