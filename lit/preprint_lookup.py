"""Reverse-lookup of preprint versions for paywalled DOIs.

Many Nature/Cell/Science/JACS/Angew papers exist as arXiv, bioRxiv, medRxiv,
ChemRxiv, Research Square or SSRN preprints months before publication. The
preprint content differs from the published version mainly in reviewer-
requested edits — for literature search and most agentic workflows it is
"good enough" and freely downloadable, while the published PDF is locked.

OpenAlex's ``/works/{id}`` endpoint exposes a ``locations`` array listing
every known instance of a work, including preprint mirrors. We pull that
list, filter to recognised preprint hosts, and extract a usable native
identifier (arXiv ID, biorxiv DOI, etc.) so the caller can route through
our existing per-source full-text chains.

Returned versions are sorted by *fetch quality* (arXiv first because we
have LaTeX source support, then bioRxiv / medRxiv, then ChemRxiv, then
the rest), not by recency.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

import requests

from lit.config import OPENALEX_API_BASE, S2_API_BASE
from lit.crossref import fetch_crossref_work, search_crossref
from lit.ratelimit import _brief_error, _request_with_retry
from lit.sources.openalex import _openalex_params
from lit.sources.s2 import _s2_headers


@dataclass
class PreprintVersion:
    """One preprint instance of a paper, normalised for downstream dispatch."""

    source: str               # canonical host: arxiv | biorxiv | medrxiv | chemrxiv | researchsquare | ssrn
    id: str                   # native ID for that source (arXiv ID / DOI / etc.)
    pdf_url: str | None = None
    landing_url: str | None = None
    version_label: str | None = None  # OpenAlex's submittedVersion / acceptedVersion / publishedVersion


# Canonical name → substrings to match against OpenAlex source.display_name.
_PREPRINT_SOURCES: dict[str, tuple[str, ...]] = {
    "arxiv": ("arxiv",),
    "biorxiv": ("biorxiv",),
    "medrxiv": ("medrxiv",),
    "chemrxiv": ("chemrxiv",),
    "researchsquare": ("research square",),
    "ssrn": ("ssrn",),
}

# Priority for the returned list — arXiv first because the LaTeX source
# chain gives the cleanest text extraction; rxiv family next; SSRN last.
_PRIORITY: dict[str, int] = {
    "arxiv": 0,
    "biorxiv": 1,
    "medrxiv": 2,
    "chemrxiv": 3,
    "researchsquare": 4,
    "ssrn": 5,
}

_ARXIV_URL_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", re.IGNORECASE
)
_BIORXIV_DOI_RE = re.compile(r"(10\.1101/[^\s\?#]+)", re.IGNORECASE)
_CHEMRXIV_DOI_RE = re.compile(r"(10\.26434/[^\s\?#]+)", re.IGNORECASE)
_RSQ_DOI_RE = re.compile(r"(10\.21203/[^\s\?#]+)", re.IGNORECASE)
_SSRN_ID_RE = re.compile(r"abstract_id=(\d+)|/abstract/(\d+)", re.IGNORECASE)


def _canonical_source(display_name: str) -> str | None:
    low = (display_name or "").lower()
    for canonical, substrs in _PREPRINT_SOURCES.items():
        for s in substrs:
            if s in low:
                return canonical
    return None


def _strip_doi_suffix(doi: str) -> str:
    """Strip URL-style trailing fragments OpenAlex sometimes leaves on DOIs.

    Repeatedly peels off ``.pdf`` / ``.full`` / ``.abstract`` / ``.vN`` —
    these come from .full.pdf style links and can chain arbitrarily.
    """
    pat = re.compile(r"\.(pdf|full|abstract|v\d+)$", re.IGNORECASE)
    prev = None
    while prev != doi:
        prev = doi
        doi = pat.sub("", doi)
    return doi


def _id_from_location(canonical: str, loc: dict) -> str | None:
    """Pull a usable native ID for the chosen preprint source from the location dict."""
    landing = loc.get("landing_page_url") or ""
    pdf = loc.get("pdf_url") or ""
    haystack = f"{landing} {pdf}"

    if canonical == "arxiv":
        m = _ARXIV_URL_RE.search(haystack)
        if m:
            return re.sub(r"v\d+$", "", m.group(1))
    elif canonical in ("biorxiv", "medrxiv"):
        m = _BIORXIV_DOI_RE.search(haystack)
        if m:
            return _strip_doi_suffix(m.group(1))
    elif canonical == "chemrxiv":
        m = _CHEMRXIV_DOI_RE.search(haystack)
        if m:
            return _strip_doi_suffix(m.group(1))
    elif canonical == "researchsquare":
        m = _RSQ_DOI_RE.search(haystack)
        if m:
            return _strip_doi_suffix(m.group(1))
    elif canonical == "ssrn":
        m = _SSRN_ID_RE.search(haystack)
        if m:
            return m.group(1) or m.group(2)
    return None


def find_preprint_versions(doi: str | None = None) -> list[PreprintVersion]:
    """Return all preprint versions of a DOI, sorted by fetch quality.

    Empty list if OpenAlex doesn't know the DOI, or if no preprint mirror
    is among its known locations.
    """
    if not doi:
        return []

    url = f"{OPENALEX_API_BASE}/works/doi:{doi.lower()}"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="openalex",
            params=_openalex_params(select="locations,best_oa_location"),
            timeout=15,
        )
        data = resp.json()
    except requests.HTTPError as e:
        # 404 just means OpenAlex doesn't index this DOI — silent, not an error.
        if e.response is not None and e.response.status_code == 404:
            return []
        print(f"OpenAlex preprint lookup failed: {_brief_error(e)}", file=sys.stderr)
        return []
    except requests.RequestException as e:
        print(f"OpenAlex preprint lookup failed: {_brief_error(e)}", file=sys.stderr)
        return []

    locations = list(data.get("locations") or [])
    # best_oa_location is usually duplicated in `locations`, but some records
    # only carry it on the top level — include defensively.
    boa = data.get("best_oa_location")
    if boa:
        locations.append(boa)

    seen: set[tuple[str, str]] = set()
    versions: list[PreprintVersion] = []
    for loc in locations:
        if not loc:
            continue
        src = loc.get("source") or {}
        canonical = _canonical_source(src.get("display_name") or "")
        if not canonical:
            continue
        ext_id = _id_from_location(canonical, loc)
        if not ext_id:
            continue
        key = (canonical, ext_id.lower())
        if key in seen:
            continue
        seen.add(key)
        versions.append(
            PreprintVersion(
                source=canonical,
                id=ext_id,
                pdf_url=loc.get("pdf_url"),
                landing_url=loc.get("landing_page_url"),
                version_label=loc.get("version"),
            )
        )

    # Secondary: ask Semantic Scholar for an arXiv ID. S2 sometimes knows
    # about a preprint twin that OpenAlex's locations array doesn't list
    # (the link only appears once OpenAlex re-indexes the preprint as a
    # separate work, which can lag publication by months).
    if not any(v.source == "arxiv" for v in versions):
        ax_id = _arxiv_id_from_s2(doi)
        if ax_id:
            versions.append(
                PreprintVersion(
                    source="arxiv",
                    id=ax_id,
                    pdf_url=f"https://arxiv.org/pdf/{ax_id}",
                    landing_url=f"https://arxiv.org/abs/{ax_id}",
                    version_label="submittedVersion",
                )
            )

    # Tertiary: title-fuzzy search on preprint servers. When a publisher
    # doesn't file the preprint link with Crossref/OpenAlex (Wiley is a
    # frequent offender), a title+author query against each preprint DOI
    # prefix reliably surfaces the twin. Only runs when earlier layers
    # came up empty to avoid duplicate preprint servers chatter.
    if not versions:
        versions.extend(_title_fuzzy_preprint_lookup(doi))

    versions.sort(key=lambda v: _PRIORITY.get(v.source, 99))
    return versions


# DOI prefixes used by each preprint server that registers with Crossref.
# arXiv is absent by design — arXiv doesn't register most of its papers with
# Crossref, so this path can't find them (S2 handles that case above).
_PREPRINT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("chemrxiv", "10.26434"),
    ("biorxiv", "10.1101"),
    ("researchsquare", "10.21203"),
    ("ssrn", "10.2139"),
)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _title_tokens(title: str) -> set[str]:
    """Normalise a title to a token set for Jaccard similarity."""
    return set(_TOKEN_RE.findall((title or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _title_fuzzy_preprint_lookup(doi: str) -> list[PreprintVersion]:
    """Find preprint twin(s) of ``doi`` by title search on each preprint server.

    Approach: fetch the target DOI's title from Crossref, then query Crossref
    with title + DOI-prefix filter for each preprint server. Accept a hit
    only when its title's Jaccard similarity to the target is >= 0.85 — high
    enough to reject same-author follow-up papers, low enough to tolerate
    the small title edits that sometimes happen between preprint and press.
    """
    # Don't look up preprint versions of a preprint.
    for _, prefix in _PREPRINT_PREFIXES:
        if doi.lower().startswith(f"{prefix}/"):
            return []

    msg = fetch_crossref_work(doi)
    if not msg:
        return []
    target_title = (msg.get("title") or [""])[0]
    if len(target_title) < 20:
        # Too-short titles yield noisy token-set matches ("Corrigendum", etc.).
        return []
    target_tokens = _title_tokens(target_title)

    results: list[PreprintVersion] = []
    for source, prefix in _PREPRINT_PREFIXES:
        items = search_crossref(
            target_title,
            max_results=3,
            prefix=prefix,
            work_type="posted-content",
        ) or []
        for it in items:
            cand_title = (it.get("title") or [""])[0]
            if _jaccard(target_tokens, _title_tokens(cand_title)) < 0.85:
                continue
            cand_doi = (it.get("DOI") or "").lower()
            if not cand_doi:
                continue
            results.append(
                PreprintVersion(
                    source=source,
                    id=_strip_doi_suffix(cand_doi),
                    landing_url=f"https://doi.org/{cand_doi}",
                    version_label="submittedVersion",
                )
            )
            break  # one match per server is enough
    return results


def _arxiv_id_from_s2(doi: str) -> str | None:
    """Look up the arXiv ID of a DOI via Semantic Scholar's ``externalIds``."""
    url = f"{S2_API_BASE}/paper/DOI:{doi}"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="s2",
            params={"fields": "externalIds"},
            headers=_s2_headers(),
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"S2 preprint lookup failed: {_brief_error(e)}", file=sys.stderr)
        return None
    ext = data.get("externalIds") or {}
    return ext.get("ArXiv") or None
