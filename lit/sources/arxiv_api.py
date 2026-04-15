"""arXiv adapter: uses the official `arxiv` Python library for metadata + search."""

from __future__ import annotations

import sys
import time

import arxiv

from lit.ids import _truncate_authors
from lit.ratelimit import RateLimiter
from paper_cache import CachedAuthor, CachedPaper


def _fetch_paper_arxiv(arxiv_id: str) -> CachedPaper | None:
    """Fetch metadata via the arxiv library (last-resort, slowest)."""
    client = arxiv.Client(num_retries=0)
    search = arxiv.Search(id_list=[arxiv_id])
    results = None
    for attempt in range(RateLimiter.RETRIES + 1):
        RateLimiter.acquire("arxiv")
        try:
            results = list(client.results(search))
            break
        except arxiv.HTTPError:
            if attempt < RateLimiter.RETRIES:
                wait = RateLimiter.backoff("arxiv", attempt)
                print(f"arXiv 429, {wait:.0f}s后重试...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if not results:
        return None

    paper = results[0]
    # arxiv.Result.doi is set when the author provided a journal DOI; the
    # synthetic arXiv DOI (10.48550/arXiv.X) is not exposed here.
    journal_doi = getattr(paper, "doi", None) or None
    return CachedPaper(
        title=paper.title,
        authors=[CachedAuthor(a.name) for a in paper.authors],
        abstract=paper.summary,
        categories=list(paper.categories),
        pdf_url=paper.pdf_url,
        year=paper.published.year if paper.published else None,
        source="arxiv",
        arxiv_id=arxiv_id,
        doi=journal_doi,
    )


def search_papers(query: str, max_results: int = 20) -> list:
    client = arxiv.Client()
    search = arxiv.Search(query=query, max_results=max_results)
    return list(client.results(search))


def _normalize_arxiv_search(results: list) -> list[dict]:
    out = []
    for paper in results:
        arxiv_id = paper.entry_id.split("/abs/")[-1]
        author_str = _truncate_authors([a.name for a in paper.authors])

        out.append(
            {
                "id": f"arXiv:{arxiv_id}",
                "title": paper.title,
                "authors": author_str,
                "year": paper.published.strftime("%Y-%m-%d"),
                "cited_by": None,
                "abstract": paper.summary,
            }
        )
    return out
