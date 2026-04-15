"""OpenAlex adapter: search, paper lookup, citations, abstract reconstruction."""

from __future__ import annotations

import sys

import requests

from lit.config import CONTACT_EMAIL, OPENALEX_API_BASE, OPENALEX_API_KEY
from lit.ids import _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


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


def _fetch_paper_openalex(arxiv_id: str) -> CachedPaper | None:
    doi = f"10.48550/arXiv.{arxiv_id}"
    url = f"{OPENALEX_API_BASE}/works/doi:{doi}"
    try:
        resp = _request_with_retry(
            requests.get, url, service="openalex",
            params=_openalex_params(
                select="title,authorships,abstract_inverted_index",
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

    return CachedPaper(
        title=data["title"],
        authors=authors,
        abstract=abstract,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )


def _resolve_openalex_id_spec(paper_spec: str) -> tuple[str, str, int] | None:
    """Resolve a generic paper_spec to (openalex_work_id, title, cited_by_count).

    Accepts ``"ArXiv:xxx"`` (mapped via 10.48550/arXiv.xxx DOI), ``"PMID:xxx"``
    (OpenAlex's native pmid: accessor), or ``"DOI:xxx"``.
    """
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
