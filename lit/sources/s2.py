"""Semantic Scholar adapter: search, bulk search, paper lookup, citations."""

from __future__ import annotations

import sys

import requests

from lit.config import HTTP_HEADERS, S2_API_BASE, S2_API_KEY
from lit.ids import _arxiv_date, _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


def _s2_headers() -> dict[str, str]:
    if S2_API_KEY:
        return {**HTTP_HEADERS, "x-api-key": S2_API_KEY}
    return HTTP_HEADERS


def _s2_search_params(
    query: str,
    max_results: int,
    *,
    year: str | None = None,
    fields_of_study: str | None = None,
    publication_types: str | None = None,
    min_citations: int | None = None,
    venue: str | None = None,
    open_access: bool = False,
) -> dict:
    """Build S2 search params (shared between /paper/search and /paper/search/bulk)."""
    params: dict = {
        "query": query,
        "limit": min(max_results, 100),
        "fields": "title,year,authors,externalIds,citationCount,abstract",
    }
    if year:
        params["year"] = year
    if fields_of_study:
        params["fieldsOfStudy"] = fields_of_study
    if publication_types:
        params["publicationTypes"] = publication_types
    if min_citations is not None:
        params["minCitationCount"] = str(min_citations)
    if venue:
        params["venue"] = venue
    if open_access:
        params["openAccessPdf"] = ""
    return params


def _search_s2(query: str, max_results: int = 10, **filters) -> list[dict] | None:
    params = _s2_search_params(query, max_results, **filters)
    try:
        resp = _request_with_retry(
            requests.get,
            f"{S2_API_BASE}/paper/search",
            service="s2",
            params=params,
            headers=_s2_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("data"):
        return None
    return data["data"][:max_results]


def _search_s2_snippet(query: str, max_results: int = 10) -> list[dict] | None:
    """S2 full-text snippet search → list of paper records (regular search shape).

    Hits the ``/snippet/search`` endpoint, which returns text fragments from
    indexed full-text where ``query`` appears, each tagged with the parent
    paper. We collapse to one record per paper (first/best snippet wins),
    fold the snippet text into ``abstract`` so it shows in default output,
    and return in the same shape as :func:`_search_s2` so the aggregator
    can swap endpoints transparently.

    Useful when ``--snippet`` is set: instead of S2's title/abstract keyword
    match, you get papers whose body text mentions the phrase. Great for
    technical terms that authors don't put in titles.
    """
    try:
        resp = _request_with_retry(
            requests.get,
            f"{S2_API_BASE}/snippet/search",
            service="s2",
            params={"query": query, "limit": min(max_results, 100)},
            headers=_s2_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar snippet search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("data"):
        return None

    out: list[dict] = []
    seen: set[str] = set()
    for item in data["data"]:
        paper = item.get("paper") or {}
        pid = str(paper.get("corpusId") or paper.get("paperId") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        snippet_text = (item.get("snippet") or {}).get("text") or ""
        # Reshape into the regular /paper/search response shape so callers
        # (incl. aggregator's _hits_from_s2) need no special-casing.
        out.append({
            "paperId": paper.get("paperId"),
            "title": paper.get("title") or "",
            "year": paper.get("year"),
            "authors": paper.get("authors") or [],
            "abstract": snippet_text or paper.get("abstract"),
            "citationCount": paper.get("citationCount"),
            "externalIds": paper.get("externalIds") or {},
        })
        if len(out) >= max_results:
            break
    return out or None


def _search_s2_bulk(
    query: str,
    max_results: int = 100,
    token: str | None = None,
    sort: str | None = None,
    **filters,
) -> tuple[list[dict], str | None] | None:
    """Bulk search (up to 10M results via token pagination, 1000/page)."""
    params = _s2_search_params(query, min(max_results, 1000), **filters)
    if token:
        params["token"] = token
    if sort:
        params["sort"] = sort
    try:
        resp = _request_with_retry(
            requests.get,
            f"{S2_API_BASE}/paper/search/bulk",
            service="s2",
            params=params,
            headers=_s2_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar bulk search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("data"):
        return None
    return data["data"][:max_results], data.get("token")


def _fetch_paper_s2(arxiv_id: str) -> CachedPaper | None:
    """Fetch paper metadata from Semantic Scholar by arXiv ID."""
    url = f"{S2_API_BASE}/paper/ArXiv:{arxiv_id}"
    try:
        resp = _request_with_retry(
            requests.get, url, service="s2",
            params={"fields": "title,authors,abstract,year,externalIds"},
            headers=_s2_headers(),
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"S2 lookup failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("title") or not data.get("authors"):
        return None

    published = _arxiv_date(arxiv_id)
    if not published:
        return None

    ext = data.get("externalIds") or {}
    pmcid = ext.get("PubMedCentral") or ""
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"

    return CachedPaper(
        title=data["title"],
        authors=[CachedAuthor(a["name"]) for a in data["authors"]],
        abstract=data.get("abstract") or "",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        year=data.get("year"),
        source="s2",
        arxiv_id=ext.get("ArXiv") or arxiv_id,
        doi=ext.get("DOI") or None,
        pmid=ext.get("PubMed") or None,
        pmcid=pmcid or None,
    )


def _fetch_citations_s2_spec(
    paper_spec: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """Fetch citations given a generic S2 paper_id spec.

    paper_spec is whatever S2 accepts in the URL path, e.g. ``"ArXiv:2401.12345"``,
    ``"PMID:39876543"``, ``"DOI:10.1038/xxx"``, ``"CorpusId:123"``. The caller is
    responsible for building the correct prefix.
    """
    info_url = f"{S2_API_BASE}/paper/{paper_spec}"
    try:
        resp = _request_with_retry(
            requests.get,
            info_url,
            service="s2",
            params={"fields": "title,citationCount"},
            headers=_s2_headers(),
            timeout=30,
        )
        paper_info = resp.json()
        print(f"Paper: {paper_info['title']}")
        print(f"Total citations: {paper_info['citationCount']}")
    except requests.RequestException as e:
        print(f"Semantic Scholar query failed: {_brief_error(e)}", file=sys.stderr)
        return None

    citations_url = f"{S2_API_BASE}/paper/{paper_spec}/citations"
    try:
        resp = _request_with_retry(
            requests.get,
            citations_url,
            service="s2",
            params={
                "fields": "title,year,externalIds,citationCount,authors",
                "offset": offset,
                "limit": min(max_results, 1000),
            },
            headers=_s2_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar citations fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    results = [
        item["citingPaper"] for item in data["data"] if item["citingPaper"]["title"]
    ]
    return results[:max_results], paper_info["citationCount"]


def _fetch_citations_s2(
    arxiv_id: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """Back-compat wrapper for arXiv IDs — delegates to _fetch_citations_s2_spec."""
    return _fetch_citations_s2_spec(f"ArXiv:{arxiv_id}", max_results, offset)


def _fetch_references_s2_spec(
    paper_spec: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """Fetch the references (forward citations) of a paper via S2.

    S2 exposes ``/paper/{paper_id}/references`` returning each cited paper.
    Shape mirrors ``_fetch_citations_s2_spec`` so callers can reuse printers.
    """
    info_url = f"{S2_API_BASE}/paper/{paper_spec}"
    try:
        resp = _request_with_retry(
            requests.get,
            info_url,
            service="s2",
            params={"fields": "title,referenceCount"},
            headers=_s2_headers(),
            timeout=30,
        )
        paper_info = resp.json()
        print(f"Paper: {paper_info['title']}")
        print(f"Total references: {paper_info.get('referenceCount') or 0}")
    except requests.RequestException as e:
        print(f"Semantic Scholar query failed: {_brief_error(e)}", file=sys.stderr)
        return None

    refs_url = f"{S2_API_BASE}/paper/{paper_spec}/references"
    try:
        resp = _request_with_retry(
            requests.get,
            refs_url,
            service="s2",
            params={
                "fields": "title,year,externalIds,citationCount,authors",
                "offset": offset,
                "limit": min(max_results, 1000),
            },
            headers=_s2_headers(),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar references fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    results = [
        item["citedPaper"] for item in data["data"]
        if item.get("citedPaper") and item["citedPaper"].get("title")
    ]
    return results[:max_results], paper_info.get("referenceCount") or 0


def _normalize_s2_search(results: list[dict]) -> list[dict]:
    out = []
    for paper in results:
        ext_ids = paper["externalIds"] or {}
        arxiv_id = ext_ids.get("ArXiv", "")
        doi = ext_ids.get("DOI", "")
        if arxiv_id:
            id_str = f"arXiv:{arxiv_id}"
        elif doi:
            id_str = f"DOI:{doi}"
        else:
            id_str = ""

        authors = paper["authors"] or []
        author_str = _truncate_authors([a["name"] for a in authors])

        out.append(
            {
                "id": id_str,
                "title": paper["title"],
                "authors": author_str,
                "year": str(paper["year"] or "?"),
                "cited_by": paper["citationCount"],
                "abstract": paper["abstract"],
            }
        )
    return out


def _is_stub_citation(paper: dict) -> bool:
    """Heuristic: S2's ``/references`` occasionally returns partial records
    matched from JATS body fragments — bullet text or figure captions that
    the upstream parser mistook for reference entries. They come through
    with no authors, no year, no external IDs, AND ``citationCount=None``
    (S2 fills the count when the candidate matched a real paper; ``None``
    signals an unresolved stub). Any legitimate S2 paper record will
    populate at least one of these four slots.
    """
    if paper.get("authors"):
        return False
    if paper.get("year"):
        return False
    if paper.get("externalIds") or {}:
        return False
    return paper.get("citationCount") is None


def _print_citations_s2(results: list[dict], start: int = 1) -> None:
    idx = start
    filtered = 0
    for paper in results:
        if _is_stub_citation(paper):
            filtered += 1
            continue
        ext_ids = paper["externalIds"] or {}
        arxiv_ext = ext_ids.get("ArXiv")
        arxiv_str = f"  arXiv:{arxiv_ext}" if arxiv_ext else ""

        authors = paper["authors"] or []
        author_str = _truncate_authors([a["name"] for a in authors])

        print(f"[{idx}] {paper['title']}")
        print(f"    Authors: {author_str}")
        print(
            f"    Year: {paper['year'] or '?'}  Cited: {paper['citationCount']}{arxiv_str}"
        )
        print()
        idx += 1

    if filtered:
        print(
            f"({filtered} stub entries filtered — "
            f"upstream JATS body fragments miscategorised as references)",
            file=sys.stderr,
        )
