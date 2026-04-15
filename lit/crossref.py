"""Crossref content-negotiation helpers.

Crossref doesn't require an API key; including `mailto` in the request lands us
in the polite pool (higher rate limit, better treatment).
"""

from __future__ import annotations

import sys

import requests

from lit.config import CONTACT_EMAIL, HTTP_HEADERS
from lit.ratelimit import _brief_error, _request_with_retry


CROSSREF_API_BASE = "https://api.crossref.org"


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


def search_crossref(
    query: str,
    *,
    max_results: int = 20,
    offset: int = 0,
    prefix: str | None = None,
    work_type: str | None = None,
    year: str | None = None,
) -> list[dict] | None:
    """Query Crossref's ``/works`` endpoint and return raw items.

    Parameters
    ----------
    query
        Free-text query (matched against title / authors / abstract).
    prefix
        DOI prefix filter (e.g. ``"10.26434"`` for ChemRxiv).
    work_type
        Crossref work-type filter (e.g. ``"posted-content"`` for preprints).
    year
        ``"YYYY"`` or ``"YYYY-YYYY"`` (maps to ``from-pub-date``/``until-pub-date``).

    Returns the list of ``message.items`` dicts, or ``None`` on error.
    """
    filters: list[str] = []
    if prefix:
        filters.append(f"prefix:{prefix}")
    if work_type:
        filters.append(f"type:{work_type}")
    if year:
        if "-" in year:
            lo, hi = year.split("-", 1)
            lo = lo.strip()
            hi = hi.strip()
            if lo:
                filters.append(f"from-pub-date:{lo}")
            if hi:
                filters.append(f"until-pub-date:{hi}")
        else:
            filters.append(f"from-pub-date:{year}")
            filters.append(f"until-pub-date:{year}")

    params: dict[str, str] = {
        "query": query,
        "rows": str(min(max_results, 1000)),
        "offset": str(offset),
    }
    if filters:
        params["filter"] = ",".join(filters)
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL

    try:
        resp = _request_with_retry(
            requests.get,
            f"{CROSSREF_API_BASE}/works",
            service="crossref",
            params=params,
            headers={**HTTP_HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Crossref search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    items = (data.get("message") or {}).get("items") or []
    return items[:max_results] or None


def fetch_crossref_work(doi: str) -> dict | None:
    """Fetch the full Crossref ``message`` for a DOI, or ``None`` on error."""
    params: dict[str, str] = {}
    if CONTACT_EMAIL:
        params["mailto"] = CONTACT_EMAIL
    try:
        resp = _request_with_retry(
            requests.get,
            f"{CROSSREF_API_BASE}/works/{doi}",
            service="crossref",
            params=params,
            headers={**HTTP_HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        return (resp.json() or {}).get("message") or None
    except requests.RequestException as e:
        print(f"Crossref DOI fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None
