"""OpenAlex adapter: search, paper lookup, citations, abstract reconstruction."""

from __future__ import annotations

import sys

import requests

import re

from lit.config import (
    CONTACT_EMAIL,
    OPENALEX_API_BASE,
    OPENALEX_API_KEY,
    OPENALEX_ENABLED,
)
from lit.ids import _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


_ARXIV_FROM_DOI_RE = re.compile(r"10\.48550/arxiv\.(.+)$", re.IGNORECASE)


def _openalex_params(**extra) -> dict[str, str]:
    if OPENALEX_API_KEY:
        extra["api_key"] = OPENALEX_API_KEY
    else:
        extra["mailto"] = CONTACT_EMAIL
    return extra


def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Rebuild an abstract from OpenAlex's abstract_inverted_index format."""
    if not inverted_index:
        return None
    words: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            words.append((pos, word))
    words.sort()
    return " ".join(w for _, w in words)


def _search_openalex(query: str, max_results: int = 10) -> list[dict] | None:
    if not OPENALEX_ENABLED:
        return None

    url = f"{OPENALEX_API_BASE}/works"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="openalex",
            params=_openalex_params(
                search=query,
                select="id,title,authorships,publication_year,cited_by_count,ids,abstract_inverted_index",
                per_page=str(min(max_results, 200)),
                sort="relevance_score:desc",
            ),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"OpenAlex search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("results"):
        return None
    return data["results"][:max_results]


def _openalex_url_for_spec(paper_spec: str) -> str | None:
    """Map a generic paper_spec to the OpenAlex /works/{id} URL form."""
    if paper_spec.startswith("ArXiv:"):
        return f"{OPENALEX_API_BASE}/works/doi:10.48550/arXiv.{paper_spec[len('ArXiv:'):]}"
    if paper_spec.startswith("PMID:"):
        return f"{OPENALEX_API_BASE}/works/pmid:{paper_spec[len('PMID:'):]}"
    if paper_spec.startswith("DOI:"):
        return f"{OPENALEX_API_BASE}/works/doi:{paper_spec[len('DOI:'):]}"
    return None


def _fetch_paper_openalex_spec(paper_spec: str) -> CachedPaper | None:
    """Fetch full metadata from OpenAlex by any paper_spec.

    Returns a CachedPaper with title/authors/abstract/year and any
    cross-reference IDs OpenAlex returns. ``source`` is set to "openalex".
    """
    if not OPENALEX_ENABLED:
        return None

    url = _openalex_url_for_spec(paper_spec)
    if url is None:
        return None
    try:
        resp = _request_with_retry(
            requests.get, url, service="openalex",
            params=_openalex_params(
                select="title,authorships,abstract_inverted_index,publication_year,doi,ids,best_oa_location",
            ),
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"OpenAlex lookup failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not data.get("title"):
        return None

    authorships = data.get("authorships") or []
    authors = [CachedAuthor(a["author"]["display_name"]) for a in authorships]
    if not authors:
        return None

    abstract = _reconstruct_abstract(data.get("abstract_inverted_index")) or ""

    ids = data.get("ids") or {}
    doi_field = data.get("doi") or ids.get("doi") or ""
    if doi_field.startswith("https://doi.org/"):
        doi_field = doi_field[len("https://doi.org/"):]

    pmid_field = ids.get("pmid") or ""
    if pmid_field.startswith("https://pubmed.ncbi.nlm.nih.gov/"):
        pmid_field = pmid_field.rstrip("/").rsplit("/", 1)[-1]

    pmcid_field = ids.get("pmcid") or ""
    if "PMC" in pmcid_field:
        pmcid_field = "PMC" + pmcid_field.split("PMC", 1)[1].rstrip("/")

    pdf_url = ""
    oa = data.get("best_oa_location") or {}
    if oa.get("pdf_url"):
        pdf_url = oa["pdf_url"]

    # OpenAlex carries the arXiv ID either in ids.arxiv or as an arXiv-pattern DOI.
    arxiv_id_field = ""
    for key, val in ids.items():
        if "arxiv" in key.lower() and val:
            if "/abs/" in val:
                arxiv_id_field = val.rsplit("/abs/", 1)[-1]
            else:
                arxiv_id_field = val
            break
    if not arxiv_id_field and doi_field:
        m = _ARXIV_FROM_DOI_RE.match(doi_field)
        if m:
            arxiv_id_field = m.group(1)

    return CachedPaper(
        title=data["title"],
        authors=authors,
        abstract=abstract,
        pdf_url=pdf_url,
        year=data.get("publication_year"),
        source="openalex",
        arxiv_id=arxiv_id_field or None,
        doi=doi_field or None,
        pmid=pmid_field or None,
        pmcid=pmcid_field or None,
    )


def _fetch_paper_openalex(arxiv_id: str) -> CachedPaper | None:
    """Back-compat wrapper for arXiv IDs.

    OpenAlex has known-flaky metadata for arXiv papers (synthetic 10.65215/…
    DOIs and re-indexing years). Since we queried by arXiv ID, we know the
    canonical values; override anything OpenAlex reported that's obviously
    wrong for an arXiv paper.
    """
    from lit.ids import _arxiv_year  # local import to avoid an import cycle

    paper = _fetch_paper_openalex_spec(f"ArXiv:{arxiv_id}")
    if paper is None:
        return None
    paper.arxiv_id = arxiv_id
    paper.pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    if not paper.doi or paper.doi.lower().startswith("10.65215/"):
        paper.doi = f"10.48550/arXiv.{arxiv_id}"
    arxiv_y = _arxiv_year(arxiv_id)
    if arxiv_y:
        paper.year = arxiv_y
    return paper


def _resolve_openalex_id_spec(paper_spec: str) -> tuple[str, str, int] | None:
    """Resolve a generic paper_spec to (openalex_work_id, title, cited_by_count).

    Accepts ``"ArXiv:xxx"`` (mapped via 10.48550/arXiv.xxx DOI), ``"PMID:xxx"``
    (OpenAlex's native pmid: accessor), or ``"DOI:xxx"``.
    """
    if not OPENALEX_ENABLED:
        return None

    if paper_spec.startswith("ArXiv:"):
        doi = f"10.48550/arXiv.{paper_spec[len('ArXiv:'):]}"
        url = f"{OPENALEX_API_BASE}/works/doi:{doi}"
    elif paper_spec.startswith("PMID:"):
        url = f"{OPENALEX_API_BASE}/works/pmid:{paper_spec[len('PMID:'):]}"
    elif paper_spec.startswith("DOI:"):
        url = f"{OPENALEX_API_BASE}/works/doi:{paper_spec[len('DOI:'):]}"
    else:
        return None

    try:
        resp = _request_with_retry(requests.get, url, service="openalex", params=_openalex_params(), timeout=15)
        data = resp.json()
        openalex_id = data["id"].split("/")[-1]
        return openalex_id, data["title"], data["cited_by_count"]
    except requests.RequestException:
        return None


def _resolve_openalex_id(arxiv_id: str) -> tuple[str, str, int] | None:
    """Back-compat wrapper for arXiv IDs."""
    return _resolve_openalex_id_spec(f"ArXiv:{arxiv_id}")


def _fetch_citations_openalex_spec(
    paper_spec: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    if not OPENALEX_ENABLED:
        return None

    resolved = _resolve_openalex_id_spec(paper_spec)
    if not resolved:
        print("OpenAlex: paper not found", file=sys.stderr)
        return None

    work_id, title, total_citations = resolved
    print(f"Paper: {title}")
    print(f"Total citations: {total_citations}")

    per_page = min(max_results, 200)
    page = (offset // per_page) + 1

    url = f"{OPENALEX_API_BASE}/works"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="openalex",
            params=_openalex_params(
                filter=f"cites:{work_id}",
                select="id,title,authorships,publication_year,cited_by_count",
                per_page=str(per_page),
                page=str(page),
                sort="cited_by_count:desc",
            ),
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"OpenAlex citations fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    return data["results"][:max_results], total_citations


def _fetch_citations_openalex(
    arxiv_id: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """Back-compat wrapper for arXiv IDs — delegates to _fetch_citations_openalex_spec."""
    return _fetch_citations_openalex_spec(f"ArXiv:{arxiv_id}", max_results, offset)


def _normalize_openalex_search(results: list[dict]) -> list[dict]:
    out = []
    for work in results:
        authorships = work["authorships"] or []
        author_str = _truncate_authors(
            [a["author"]["display_name"] for a in authorships]
        )

        ids = work["ids"] or {}
        arxiv_str = ""
        for key, val in ids.items():
            if "arxiv" in key.lower() and val:
                arxiv_str = val.replace("https://arxiv.org/abs/", "arXiv:")
                break
        if not arxiv_str:
            doi = ids.get("doi", "")
            if "arxiv." in doi.lower():
                arxiv_str = "arXiv:" + doi.rsplit("arxiv.", 1)[-1]
        id_str = arxiv_str or ids.get("doi", "") or ids.get("openalex", "")

        out.append(
            {
                "id": id_str,
                "title": work["title"],
                "authors": author_str,
                "year": str(work["publication_year"] or "?"),
                "cited_by": work["cited_by_count"],
                "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
            }
        )
    return out


def _print_citations_openalex(results: list[dict], start: int = 1) -> None:
    for i, work in enumerate(results, start):
        authorships = work["authorships"] or []
        author_str = _truncate_authors(
            [a["author"]["display_name"] for a in authorships]
        )

        print(f"[{i}] {work['title']}")
        print(f"    Authors: {author_str}")
        print(
            f"    Year: {work['publication_year'] or '?'}  Cited: {work['cited_by_count']}"
        )
        print()
