"""Fill in missing cross-reference IDs on a CachedPaper.

Every source adapter returns whatever IDs *it* happened to see: arXiv's
library gives ``arxiv_id``, Semantic Scholar's ``externalIds`` carries DOI
and PMID, PubMed's EFetch has DOI and PMC, and so on. For the cache and
for BibTeX to work across sources, we want every paper to carry the
fullest possible set — so after the primary fetch, ask OpenAlex (the
source with the most complete cross-reference graph) for whatever is
still missing.

This costs one extra OpenAlex request per *first* fetch of a paper.
Subsequent cache hits pay nothing.
"""

from __future__ import annotations

from lit.sources.openalex import _fetch_paper_openalex_spec
from paper_cache import CachedPaper


def _first_usable_spec(paper: CachedPaper) -> str | None:
    """Pick the most reliable existing ID to query OpenAlex with."""
    if paper.doi:
        return f"DOI:{paper.doi}"
    if paper.pmid:
        return f"PMID:{paper.pmid}"
    if paper.arxiv_id:
        return f"ArXiv:{paper.arxiv_id}"
    # pmcid alone is not an OpenAlex key format; caller should resolve first.
    return None


def enrich_paper_ids(paper: CachedPaper) -> CachedPaper:
    """Fill missing PMID / PMCID on ``paper`` via OpenAlex.

    Intentionally conservative: only biomedical cross-references (PMID/PMCID)
    are filled from OpenAlex. DOI / arxiv_id / year come from the primary
    source, which is more trustworthy — OpenAlex is known to assign synthetic
    ``10.65215/…`` DOIs to arXiv works and occasionally report their
    re-indexing year instead of the publication year, so we don't let it
    overwrite those fields.

    Mutates + returns the same object for convenience. Safe to call
    unconditionally: short-circuits when both PMID and PMCID are already
    populated, fails silently if OpenAlex has no matching record.
    """
    if paper.pmid and paper.pmcid:
        return paper

    spec = _first_usable_spec(paper)
    if not spec:
        return paper

    enriched = _fetch_paper_openalex_spec(spec)
    if enriched is None:
        return paper

    if not paper.pmid and enriched.pmid:
        paper.pmid = enriched.pmid
    if not paper.pmcid and enriched.pmcid:
        paper.pmcid = enriched.pmcid
    return paper
