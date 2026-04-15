"""Crossref content-negotiation helpers.

Crossref doesn't require an API key; including `mailto` in the request lands us
in the polite pool (higher rate limit, better treatment).
"""

from __future__ import annotations

import sys

import requests

from lit.config import CONTACT_EMAIL, HTTP_HEADERS
from lit.ratelimit import _brief_error, _request_with_retry


def fetch_bibtex_crossref(doi: str) -> str | None:
    """Ask Crossref (via DOI content negotiation) for a BibTeX entry.

    This returns the publisher-authoritative record — journal, volume, issue,
    pages, all properly formatted — which is strictly better than anything we
    can synthesise from a search API. Returns None on any failure so callers
    can fall back to a locally generated entry.
    """
    url = f"https://doi.org/{doi}"
    headers = {
        **HTTP_HEADERS,
        "Accept": "application/x-bibtex",
    }
    params = {"mailto": CONTACT_EMAIL} if CONTACT_EMAIL else {}
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="crossref",
            headers=headers,
            params=params,
            allow_redirects=True,
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Crossref lookup failed: {_brief_error(e)}", file=sys.stderr)
        return None

    text = (resp.text or "").strip()
    if not text.startswith("@"):
        return None
    return text
