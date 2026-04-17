"""ChemRxiv adapter — routed through Crossref because the official ChemRxiv
API (``chemrxiv.org/engage/...``) is behind Cloudflare Turnstile and blocks
any non-browser HTTP client.

Luckily ChemRxiv DOIs (``10.26434/chemrxiv-*``) are Crossref-registered,
which gives us title / authors / year / journal ("ChemRxiv") / PDF link.
Abstracts come from OpenAlex (its DOI index covers ChemRxiv well) and
BibTeX comes from Crossref content negotiation (same as any other DOI).

What we LOSE from not talking to ChemRxiv directly:
- Chemistry-category filters (``categoryIds`` in their API)
- License metadata (CC-BY / CC-BY-NC-ND per-paper)
- Direct PDF download (their asset URLs are also Cloudflared)
"""

from __future__ import annotations

import re
import sys

import requests

from lit.config import HTTP_HEADERS
from lit.crossref import fetch_crossref_work, search_crossref
from lit.ids import _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


CHEMRXIV_DOI_PREFIX = "10.26434"

# Known degenerate Crossref/OpenAlex "abstract" strings for ChemRxiv records —
# these appear when the publisher only registered admin metadata and not the
# real abstract. Matched case-insensitively against whitespace-collapsed text.
_CHEMRXIV_ABSTRACT_SENTINELS = (
    "publication status: published",
    "publication status: submitted",
    "publication status: pending",
)


def is_chemrxiv_doi(doi: str) -> bool:
    return doi.lower().startswith(f"{CHEMRXIV_DOI_PREFIX}/")


def _search_chemrxiv(
    query: str,
    max_results: int = 20,
    *,
    offset: int = 0,
    year: str | None = None,
) -> list[dict] | None:
    """Crossref-scoped search restricted to ChemRxiv (prefix 10.26434)."""
    return search_crossref(
        query,
        max_results=max_results,
        offset=offset,
        prefix=CHEMRXIV_DOI_PREFIX,
        work_type="posted-content",
        year=year,
    )


def _crossref_authors(item: dict) -> list[str]:
    names: list[str] = []
    for a in item.get("author") or []:
        given = (a.get("given") or "").strip()
        family = (a.get("family") or "").strip()
        if given or family:
            names.append(f"{given} {family}".strip())
        else:
            names.append(a.get("name") or "?")
    return names


def _crossref_year(item: dict) -> str:
    # Crossref exposes the date under several fields depending on work type;
    # for preprints ``posted`` is authoritative.
    for field in ("posted", "published-online", "published-print", "issued"):
        parts = (item.get(field) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return ""


def _normalize_chemrxiv_search(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        doi = it.get("DOI") or ""
        title = (it.get("title") or [""])[0]
        abstract_raw = it.get("abstract") or None
        abstract = None
        if abstract_raw:
            # Crossref abstracts are JATS XML fragments; strip tags crudely.
            abstract = re.sub(r"</?[^>]+>", "", abstract_raw).strip() or None
        out.append(
            {
                "id": f"DOI:{doi}" if doi else "",
                "title": title,
                "authors": _truncate_authors(_crossref_authors(it)),
                "year": _crossref_year(it) or "?",
                "cited_by": it.get("is-referenced-by-count"),
                "abstract": abstract,
            }
        )
    return out


def _pdf_url_from_crossref(item: dict) -> str:
    """Pull the best-guess PDF URL out of Crossref's ``link`` array."""
    for link in item.get("link") or []:
        url = link.get("URL") or ""
        if link.get("content-type") == "application/pdf" or url.endswith(".pdf"):
            return url
    # Fallback: Crossref's TDM/similarity-check link, which for ChemRxiv points
    # at the asset gateway (blocked by Cloudflare but still useful to display).
    for link in item.get("link") or []:
        if link.get("intended-application") in ("similarity-checking", "text-mining"):
            return link.get("URL") or ""
    return ""


def _abstract_is_degenerate(abstract: str | None) -> bool:
    """True when an abstract is empty or a known admin-metadata sentinel.

    ChemRxiv's Crossref record sometimes has ``<jats:p>Publication status:
    Published</jats:p>`` instead of the real abstract — treat those as if
    no abstract exists so callers can recover from the peer-reviewed twin.
    """
    if not abstract:
        return True
    collapsed = re.sub(r"\s+", " ", abstract).strip().lower()
    if len(collapsed) < 50:
        return True
    return any(s in collapsed for s in _CHEMRXIV_ABSTRACT_SENTINELS)


def _abstract_from_published_twin(msg: dict) -> str:
    """Recover the real abstract from the peer-reviewed DOI.

    ChemRxiv records expose ``relation.is-preprint-of`` pointing at the
    published-version DOI. That DOI typically has a proper Crossref
    abstract (JATS) or an OpenAlex inverted-index abstract — either beats
    the preprint's empty/sentinel abstract.
    """
    relation = msg.get("relation") or {}
    targets = relation.get("is-preprint-of") or []
    for t in targets:
        if (t.get("id-type") or "").lower() != "doi":
            continue
        twin_doi = (t.get("id") or "").strip()
        if not twin_doi:
            continue
        twin_msg = fetch_crossref_work(twin_doi)
        twin_abs = (twin_msg or {}).get("abstract") or ""
        if twin_abs:
            cleaned = re.sub(r"</?[^>]+>", "", twin_abs).strip()
            if cleaned and not _abstract_is_degenerate(cleaned):
                return cleaned
        # Crossref had nothing useful — try OpenAlex for the twin.
        oa_abs = _openalex_abstract_for_doi(twin_doi)
        if oa_abs and not _abstract_is_degenerate(oa_abs):
            return oa_abs
    return ""


def _openalex_abstract_for_doi(doi: str) -> str:
    """Reconstruct OpenAlex's inverted-index abstract for a DOI, or ''."""
    from lit.config import OPENALEX_API_BASE
    from lit.sources.openalex import _openalex_params, _reconstruct_abstract

    url = f"{OPENALEX_API_BASE}/works/doi:{doi.lower()}"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="openalex",
            params=_openalex_params(select="abstract_inverted_index"),
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException:
        return ""
    return _reconstruct_abstract(data.get("abstract_inverted_index")) or ""


def _fetch_paper_chemrxiv(doi: str) -> CachedPaper | None:
    """Full metadata for a ChemRxiv DOI via Crossref.

    Abstract fallback chain: Crossref preprint abstract → published-twin
    (``relation.is-preprint-of``) Crossref abstract → published-twin
    OpenAlex abstract. The twin lookup only runs when the preprint abstract
    is empty or matches a known ChemRxiv admin-metadata sentinel.
    """
    msg = fetch_crossref_work(doi)
    if not msg:
        return None

    title = (msg.get("title") or [""])[0]
    if not title:
        return None
    authors = [CachedAuthor(n) for n in _crossref_authors(msg)]
    if not authors:
        return None

    abstract = ""
    abs_raw = msg.get("abstract")
    if abs_raw:
        abstract = re.sub(r"</?[^>]+>", "", abs_raw).strip()
    if _abstract_is_degenerate(abstract):
        abstract = _abstract_from_published_twin(msg) or abstract

    year_str = _crossref_year(msg)
    year = int(year_str) if year_str.isdigit() else None

    pdf_url = _pdf_url_from_crossref(msg)

    categories = []
    for subj in msg.get("subject") or []:
        if isinstance(subj, str) and subj:
            categories.append(subj)

    return CachedPaper(
        title=title,
        authors=authors,
        abstract=abstract,
        categories=categories,
        pdf_url=pdf_url,
        year=year,
        source="chemrxiv",
        doi=doi,
    )


def fetch_chemrxiv_pdf(doi: str) -> bytes | None:
    """Best-effort PDF download.

    ChemRxiv asset URLs sit behind Cloudflare Turnstile, which rejects every
    non-browser client we've tried (requests, cloudscraper, the official
    ``chemrxiv`` pip package). We try anyway — if you're on a network that
    Cloudflare trusts, this may work; otherwise callers should fall back to
    printing the URL for the user to open in a real browser.
    """
    msg = fetch_crossref_work(doi)
    if not msg:
        return None
    url = _pdf_url_from_crossref(msg)
    if not url:
        return None
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="crossref",  # reuse its gentle rate limiter
            headers={
                **HTTP_HEADERS,
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
                "Accept": "application/pdf,*/*",
            },
            timeout=60,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"ChemRxiv PDF fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if resp.content[:4] != b"%PDF":
        return None
    return resp.content


def chemrxiv_pdf_url(doi: str) -> str:
    """Return the publisher PDF URL from Crossref metadata (no download)."""
    msg = fetch_crossref_work(doi)
    if not msg:
        return ""
    return _pdf_url_from_crossref(msg)
