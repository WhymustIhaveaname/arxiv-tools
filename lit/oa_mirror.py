"""Open-access mirror discovery + download.

When a paper's primary full-text path fails (e.g. a PubMed paper with no
PMC copy, or a preprint whose publisher page is Cloudflared), there's
often another OA host that has the same PDF — the publisher's own OA
page, an institutional repository, or a preprint server.

Three sources that know about these mirrors:

1. **Unpaywall** (``api.unpaywall.org/v2/{doi}``) — a dedicated OA index
   with ~50M records. Free with a ``mailto`` parameter. Returns the
   ``best_oa_location`` (the highest-quality OA copy) plus all other
   known OA locations.
2. **OpenAlex** — same data as Unpaywall (OpenAlex consumes Unpaywall
   upstream) exposed via ``best_oa_location.pdf_url`` / ``oa_locations``.
   We already query OpenAlex for metadata; the OA URL comes along for free.
3. **Crossref ``link`` array** — publisher-registered text-mining URLs.
   Less curated than Unpaywall but occasionally catches cases Unpaywall
   missed.

The downloader validates the response is an actual PDF (``%PDF`` magic
bytes) — many "OA URL" links redirect to HTML landing pages or require
JS, neither of which we want to save as a .pdf file.
"""

from __future__ import annotations

import sys

import requests

from lit.config import CONTACT_EMAIL, HTTP_HEADERS
from lit.crossref import fetch_crossref_work
from lit.ratelimit import _brief_error, _request_with_retry


UNPAYWALL_API_BASE = "https://api.unpaywall.org/v2"


def _unpaywall_urls(doi: str) -> list[str]:
    """Return all OA PDF URLs Unpaywall knows for a DOI, best first."""
    if not CONTACT_EMAIL:
        # Unpaywall requires mailto; skip silently if we don't have one.
        return []
    try:
        resp = _request_with_retry(
            requests.get,
            f"{UNPAYWALL_API_BASE}/{doi}",
            service="unpaywall",
            params={"email": CONTACT_EMAIL},
            headers=HTTP_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Unpaywall lookup failed: {_brief_error(e)}", file=sys.stderr)
        return []

    urls: list[str] = []
    best = data.get("best_oa_location") or {}
    if best.get("url_for_pdf"):
        urls.append(best["url_for_pdf"])
    for loc in data.get("oa_locations") or []:
        u = loc.get("url_for_pdf")
        if u and u not in urls:
            urls.append(u)
    return urls


def _crossref_tdm_pdf_urls(doi: str) -> list[str]:
    """Crossref's ``link`` array often lists publisher TDM PDF URLs."""
    msg = fetch_crossref_work(doi)
    if not msg:
        return []
    urls: list[str] = []
    for link in msg.get("link") or []:
        url = link.get("URL") or ""
        ctype = link.get("content-type") or ""
        if not url:
            continue
        if ctype == "application/pdf" or url.lower().endswith(".pdf"):
            if url not in urls:
                urls.append(url)
    return urls


def find_oa_pdf_urls(
    *,
    doi: str | None = None,
    openalex_pdf_url: str | None = None,
) -> list[str]:
    """Merge OA PDF URLs from every available indexer, best-first.

    Dedup-preserving: the same URL won't show up twice even if both
    Unpaywall and Crossref list it.
    """
    urls: list[str] = []

    def _add(u: str | None) -> None:
        if u and u not in urls:
            urls.append(u)

    _add(openalex_pdf_url)

    if doi:
        for u in _unpaywall_urls(doi):
            _add(u)
        for u in _crossref_tdm_pdf_urls(doi):
            _add(u)

    return urls


def try_download_pdf(url: str, *, timeout: int = 60) -> bytes | None:
    """Download ``url`` and return bytes iff the body starts with ``%PDF``.

    Uses a browser-ish UA so publisher servers that sniff for bots are
    less hostile. Returns ``None`` for HTML landing pages, 403s,
    connection errors, or any non-PDF body.
    """
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="oa_mirror",
            headers={
                **HTTP_HEADERS,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept": "application/pdf,*/*;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"OA mirror download failed ({url[:60]}...): {_brief_error(e)}", file=sys.stderr)
        return None

    if not resp.content or resp.content[:4] != b"%PDF":
        return None
    return resp.content
