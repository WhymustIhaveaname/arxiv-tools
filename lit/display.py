"""Cross-source human-readable formatters.

Source-specific printers (S2 / OpenAlex citation lists, PubMed paper info)
live in their respective ``lit/sources/*.py`` modules because they consume
raw API shapes. The functions here operate on neutral data:

  - ``print_search_results`` — generic search-result dict shape produced by
    every source's ``_normalize_*_search`` (id/title/authors/year/cited_by/abstract).
  - ``print_aggregated_results`` — :class:`lit.aggregator.AggregatedHit` dataclass,
    with a ``[OA+S2+PM]`` source-tag suffix per hit.
  - ``print_doi_info`` — single-paper detail block for the ``info`` command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lit.aggregator import SOURCE_SHORT
from lit.ids import _truncate_authors

if TYPE_CHECKING:  # avoid runtime import cycle
    from lit.aggregator import AggregatedHit


def print_search_results(results: list[dict]) -> None:
    """Render a normalized single-source search-result list."""
    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['id']}")
        print(f"    Title: {r['title']}")
        print(f"    Authors: {r['authors']}")
        cited = f"  Cited: {r['cited_by']}" if r["cited_by"] is not None else ""
        print(f"    Year: {r['year']}{cited}")
        if r["abstract"]:
            print(f"    Abstract: {r['abstract'].replace(chr(10), ' ')}")
        print()


def print_aggregated_results(hits: list["AggregatedHit"]) -> None:
    """Render an aggregator output list with per-hit source tags."""
    for i, h in enumerate(hits, 1):
        if h.arxiv_id:
            id_str = f"arXiv:{h.arxiv_id}"
        elif h.doi:
            id_str = f"DOI:{h.doi}"
        elif h.pmid:
            id_str = f"PMID:{h.pmid}"
        elif h.pmcid:
            id_str = f"PMCID:{h.pmcid}"
        else:
            id_str = ""
        src_tag = "+".join(SOURCE_SHORT.get(s, s) for s in h.sources)
        print(f"[{i}] {id_str}  [{src_tag}]")
        print(f"    Title: {h.title}")
        if h.authors:
            print(f"    Authors: {_truncate_authors(h.authors)}")
        cited = f"  Cited: {h.cited_by}" if h.cited_by is not None else ""
        print(f"    Year: {h.year or '?'}{cited}")
        if h.abstract:
            print(f"    Abstract: {h.abstract.replace(chr(10), ' ')}")
        print()


def print_doi_info(doi: str, paper) -> None:
    """Render a single-paper info block for a DOI lookup."""
    print(f"DOI: {doi}")
    print(f"Title: {paper.title}")
    print(f"Authors: {', '.join(a.name for a in paper.authors)}")
    if paper.year:
        print(f"Year: {paper.year}")
    if paper.pmid:
        print(f"PMID: {paper.pmid}")
    if paper.pmcid:
        print(f"PMC: {paper.pmcid}")
    if paper.pdf_url:
        print(f"PDF: {paper.pdf_url}")
    if paper.abstract:
        print(f"\nAbstract:\n{paper.abstract}")
