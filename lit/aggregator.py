"""Multi-source search aggregator.

Runs OpenAlex / S2 / PubMed / Europe PMC / ChemRxiv / arXiv in parallel,
deduplicates by canonical IDs (DOI, arXiv ID, PMID, PMC ID, with arXiv-DOI
equivalence) plus a (normalised-title, year) fallback, and merges fields
across sources to give the user the broadest, richest view of each paper.

The aggregator is the new default for ``cmd_search``; single-source paths
remain available via ``--source <name>`` for debugging or domain-narrow
queries.
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from lit.ids import _arxiv_year
from lit.sources.arxiv_api import _fetch_paper_arxiv, search_papers as _search_arxiv
from lit.sources.chemrxiv import (
    _fetch_paper_chemrxiv,
    _search_chemrxiv,
    is_chemrxiv_doi,
)
from lit.sources.europepmc import (
    _fetch_paper_europepmc_by_doi,
    _search_europepmc,
)
from lit.sources.openalex import (
    _fetch_paper_openalex,
    _fetch_paper_openalex_spec,
    _reconstruct_abstract,
    _search_openalex,
)
from lit.sources.pubmed import _fetch_paper_pubmed, _search_pubmed
from lit.sources.s2 import _fetch_paper_s2, _search_s2, _search_s2_snippet
from paper_cache import CachedPaper


ALL_SOURCES = ("openalex", "s2", "pubmed", "europepmc", "chemrxiv", "arxiv")
DEFAULT_SOURCES = ALL_SOURCES  # 用户要求"尽量强大": 默认全开

SOURCE_SHORT = {
    "openalex": "OA",
    "s2": "S2",
    "pubmed": "PM",
    "europepmc": "EPMC",
    "chemrxiv": "ChR",
    "arxiv": "AX",
}

# Domain shortcuts: limit the source set + apply an S2 fields_of_study filter.
# OpenAlex + S2 are kept in every domain because they're the broadest indexes;
# the rest are added/dropped based on relevance.
DOMAIN_PRESETS: dict[str, dict] = {
    "bio": {
        "sources": ("openalex", "s2", "pubmed", "europepmc"),
        "fields_of_study": "Biology,Medicine",
    },
    "med": {
        "sources": ("openalex", "s2", "pubmed", "europepmc"),
        "fields_of_study": "Medicine",
    },
    "chem": {
        "sources": ("openalex", "s2", "chemrxiv", "europepmc"),
        "fields_of_study": "Chemistry,Materials Science",
    },
    "cs": {
        "sources": ("openalex", "s2", "arxiv"),
        "fields_of_study": "Computer Science",
    },
    "phys": {
        "sources": ("openalex", "s2", "arxiv"),
        "fields_of_study": "Physics",
    },
}

_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_S2_KNOWN_FILTERS = {
    "year",
    "fields_of_study",
    "publication_types",
    "min_citations",
    "venue",
    "open_access",
}


@dataclass
class AggregatedHit:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    arxiv_id: str | None = None
    abstract: str | None = None
    cited_by: int | None = None
    sources: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# per-source extractors: raw API response → list[AggregatedHit]
# --------------------------------------------------------------------------


def _hits_from_openalex(query: str, max_results: int, **_filters) -> list[AggregatedHit]:
    raw = _search_openalex(query, max_results)
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for w in raw:
        ids = w.get("ids") or {}
        doi = (w.get("doi") or ids.get("doi") or "").lower()
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]
        pmid_raw = ids.get("pmid") or ""
        pmid = pmid_raw.rstrip("/").rsplit("/", 1)[-1] if pmid_raw else ""
        pmcid_raw = ids.get("pmcid") or ""
        pmcid = ""
        if "PMC" in pmcid_raw:
            pmcid = "PMC" + pmcid_raw.split("PMC", 1)[1].rstrip("/")
        arxiv_id = ""
        for k, v in ids.items():
            if "arxiv" in k.lower() and v:
                arxiv_id = v.replace("https://arxiv.org/abs/", "")
                break
        if not arxiv_id and doi:
            m = _ARXIV_DOI_RE.match(doi)
            if m:
                arxiv_id = m.group(1)
        # OpenAlex's publication_year is the re-indexing year for arXiv papers
        # (e.g. returns 2025 for a 1706.* ID). The arXiv ID itself encodes the
        # correct submission year — prefer that whenever we have an arXiv ID.
        year = w.get("publication_year")
        if arxiv_id:
            ax_year = _arxiv_year(arxiv_id)
            if ax_year:
                year = ax_year
        authorships = w.get("authorships") or []
        out.append(
            AggregatedHit(
                title=w.get("title") or "",
                authors=[a["author"]["display_name"] for a in authorships if a.get("author")],
                year=year,
                doi=doi or None,
                pmid=pmid or None,
                pmcid=pmcid or None,
                arxiv_id=arxiv_id or None,
                abstract=_reconstruct_abstract(w.get("abstract_inverted_index")),
                cited_by=w.get("cited_by_count"),
                sources=["openalex"],
            )
        )
    return out


def _hits_from_s2(query: str, max_results: int, **filters) -> list[AggregatedHit]:
    if filters.get("snippet"):
        # /snippet/search ignores filter kwargs (the endpoint doesn't take them).
        raw = _search_s2_snippet(query, max_results)
    else:
        s2_filters = {k: v for k, v in filters.items() if k in _S2_KNOWN_FILTERS}
        raw = _search_s2(query, max_results, **s2_filters)
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for p in raw:
        ext = p.get("externalIds") or {}
        doi = (ext.get("DOI") or "").lower()
        pmcid = ext.get("PubMedCentral") or ""
        if pmcid and not pmcid.upper().startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        out.append(
            AggregatedHit(
                title=p.get("title") or "",
                authors=[a["name"] for a in (p.get("authors") or []) if a.get("name")],
                year=p.get("year"),
                doi=doi or None,
                pmid=ext.get("PubMed") or None,
                pmcid=pmcid or None,
                arxiv_id=ext.get("ArXiv") or None,
                abstract=p.get("abstract"),
                cited_by=p.get("citationCount"),
                sources=["s2"],
            )
        )
    return out


def _hits_from_pubmed(query: str, max_results: int, **filters) -> list[AggregatedHit]:
    raw = _search_pubmed(
        query,
        max_results,
        offset=int(filters.get("offset", 0) or 0),
        year=filters.get("year"),
        open_access=bool(filters.get("open_access", False)),
    )
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for r in raw:
        pmid = r.get("uid") or ""
        doi = ""
        pmcid = ""
        for aid in r.get("articleids") or []:
            kind = (aid.get("idtype") or "").lower()
            val = aid.get("value") or ""
            if kind == "doi" and val and not doi:
                doi = val.lower()
            elif kind in ("pmc", "pmcid") and val and not pmcid:
                pmcid = val if val.upper().startswith("PMC") else f"PMC{val}"
        authors_list = r.get("authors") or []
        authors = [
            a["name"]
            for a in authors_list
            if a.get("name") and (a.get("authtype") in (None, "Author"))
        ] or [a["name"] for a in authors_list if a.get("name")]
        year_v: int | None = None
        pubdate = r.get("pubdate") or r.get("epubdate") or ""
        if pubdate:
            ystr = pubdate.split(" ")[0].split("-")[0]
            if ystr.isdigit():
                year_v = int(ystr)
        out.append(
            AggregatedHit(
                title=(r.get("title") or "").rstrip("."),
                authors=authors,
                year=year_v,
                doi=doi or None,
                pmid=pmid or None,
                pmcid=pmcid or None,
                sources=["pubmed"],
            )
        )
    return out


def _hits_from_europepmc(query: str, max_results: int, **filters) -> list[AggregatedHit]:
    raw = _search_europepmc(
        query,
        max_results,
        offset=int(filters.get("offset", 0) or 0),
        year=filters.get("year"),
        open_access=bool(filters.get("open_access", False)),
    )
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for r in raw:
        doi = (r.get("doi") or "").lower()
        author_str = r.get("authorString") or ""
        # authorString is "Smith J, Doe K, ..." — split, but keep full names.
        authors = [a.strip() for a in author_str.split(",") if a.strip()]
        year_v: int | None = None
        py = r.get("pubYear") or ""
        if isinstance(py, int):
            year_v = py
        elif py and str(py)[:4].isdigit():
            year_v = int(str(py)[:4])
        abstract = r.get("abstractText") or None
        if abstract:
            abstract = re.sub(r"</?[^>]+>", "", abstract).strip() or None
        out.append(
            AggregatedHit(
                title=(r.get("title") or "").rstrip("."),
                authors=authors,
                year=year_v,
                doi=doi or None,
                pmid=r.get("pmid") or None,
                pmcid=r.get("pmcid") or None,
                abstract=abstract,
                cited_by=r.get("citedByCount"),
                sources=["europepmc"],
            )
        )
    return out


def _hits_from_chemrxiv(query: str, max_results: int, **filters) -> list[AggregatedHit]:
    raw = _search_chemrxiv(
        query,
        max_results,
        offset=int(filters.get("offset", 0) or 0),
        year=filters.get("year"),
    )
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for it in raw:
        doi = (it.get("DOI") or "").lower()
        title = (it.get("title") or [""])[0]
        authors: list[str] = []
        for a in it.get("author") or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = f"{given} {family}".strip() or a.get("name")
            if name:
                authors.append(name)
        year_v: int | None = None
        for fld in ("posted", "published-online", "published-print", "issued"):
            parts = (it.get(fld) or {}).get("date-parts") or []
            if parts and parts[0] and parts[0][0]:
                try:
                    year_v = int(parts[0][0])
                except (TypeError, ValueError):
                    pass
                break
        abstract = it.get("abstract")
        if abstract:
            abstract = re.sub(r"</?[^>]+>", "", abstract).strip() or None
        out.append(
            AggregatedHit(
                title=title,
                authors=authors,
                year=year_v,
                doi=doi or None,
                abstract=abstract,
                cited_by=it.get("is-referenced-by-count"),
                sources=["chemrxiv"],
            )
        )
    return out


def _hits_from_arxiv(query: str, max_results: int, **_filters) -> list[AggregatedHit]:
    raw = _search_arxiv(query, max_results)
    if not raw:
        return []
    out: list[AggregatedHit] = []
    for paper in raw:
        ax_id = (paper.entry_id or "").split("/abs/")[-1]
        ax_id = re.sub(r"v\d+$", "", ax_id)
        out.append(
            AggregatedHit(
                title=paper.title or "",
                authors=[a.name for a in (paper.authors or [])],
                year=paper.published.year if paper.published else None,
                arxiv_id=ax_id or None,
                doi=getattr(paper, "doi", None) or None,
                abstract=paper.summary,
                sources=["arxiv"],
            )
        )
    return out


_FETCHERS = {
    "openalex": _hits_from_openalex,
    "s2": _hits_from_s2,
    "pubmed": _hits_from_pubmed,
    "europepmc": _hits_from_europepmc,
    "chemrxiv": _hits_from_chemrxiv,
    "arxiv": _hits_from_arxiv,
}


# --------------------------------------------------------------------------
# dedup / merge
# --------------------------------------------------------------------------


def _canonical_keys(hit: AggregatedHit) -> set[str]:
    """All ID-derived keys that should collapse to the same paper.

    arXiv ID and the corresponding 10.48550/arXiv.X DOI are treated as the
    same key — without this, OpenAlex (DOI form) and arXiv (raw ID) would
    look like two distinct papers.
    """
    keys: set[str] = set()
    if hit.arxiv_id:
        ax = hit.arxiv_id.lower()
        keys.add(f"arxiv:{ax}")
        keys.add(f"doi:10.48550/arxiv.{ax}")
    if hit.doi:
        d = hit.doi.lower()
        keys.add(f"doi:{d}")
        m = _ARXIV_DOI_RE.match(d)
        if m:
            keys.add(f"arxiv:{m.group(1).lower()}")
    if hit.pmid:
        keys.add(f"pmid:{hit.pmid}")
    if hit.pmcid:
        keys.add(f"pmcid:{hit.pmcid.upper()}")
    if not keys and hit.title:
        norm = re.sub(r"\W+", "", hit.title.lower())[:80]
        if norm:
            keys.add(f"title:{norm}|{hit.year or ''}")
    return keys


def _pick_longer(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if len(a) >= len(b) else b


def _merge(a: AggregatedHit, b: AggregatedHit) -> AggregatedHit:
    cited = max(a.cited_by or 0, b.cited_by or 0)
    return AggregatedHit(
        title=_pick_longer(a.title, b.title) or "",
        authors=a.authors if len(a.authors) >= len(b.authors) else b.authors,
        year=a.year or b.year,
        doi=a.doi or b.doi,
        pmid=a.pmid or b.pmid,
        pmcid=a.pmcid or b.pmcid,
        arxiv_id=a.arxiv_id or b.arxiv_id,
        abstract=_pick_longer(a.abstract, b.abstract),
        cited_by=cited if cited > 0 else None,
        sources=sorted(set(a.sources + b.sources)),
    )


def _dedup(hits: list[AggregatedHit]) -> list[AggregatedHit]:
    """Union-find dedup: a single hit can absorb multiple existing groups
    if its key set bridges them (e.g. has both an arXiv ID and a journal DOI).
    """
    by_canonical: dict[str, AggregatedHit] = {}
    key_to_canonical: dict[str, str] = {}

    for hit in hits:
        keys = _canonical_keys(hit)
        if not keys:
            anon = f"_anon_{id(hit)}"
            by_canonical[anon] = hit
            continue

        # Find every group this hit touches via any key.
        touched: list[str] = []
        for k in keys:
            c = key_to_canonical.get(k)
            if c is not None and c not in touched:
                touched.append(c)

        if not touched:
            canonical = sorted(keys)[0]
            by_canonical[canonical] = hit
            for k in keys:
                key_to_canonical[k] = canonical
            continue

        # Merge: collapse all touched groups + the new hit into the first.
        primary = touched[0]
        merged = by_canonical[primary]
        for c in touched[1:]:
            merged = _merge(merged, by_canonical.pop(c))
        merged = _merge(merged, hit)
        by_canonical[primary] = merged

        # Re-point every key (new hit's keys + absorbed groups' keys) to primary.
        for k in _canonical_keys(merged):
            key_to_canonical[k] = primary
        absorbed = set(touched[1:])
        if absorbed:
            for k, v in list(key_to_canonical.items()):
                if v in absorbed:
                    key_to_canonical[k] = primary

    return list(by_canonical.values())


def _rank(hits: list[AggregatedHit]) -> list[AggregatedHit]:
    """Sort by source-consensus first, then citation count, then year."""
    return sorted(
        hits,
        key=lambda h: (-len(h.sources), -(h.cited_by or 0), -(h.year or 0)),
    )


# Stopwords stripped before computing title↔query token overlap. Kept
# intentionally small — real content words like "learning" or "structure"
# are valid signal; only true filler words are dropped.
_RELEVANCE_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "the", "to", "with", "via", "using", "based", "toward",
    "towards", "new", "novel",
})

_RELEVANCE_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _significant_query_tokens(query: str) -> set[str]:
    """Return query tokens worth matching against titles.

    Drops stopwords and one-character tokens; case-insensitive.
    """
    return {
        t
        for t in _RELEVANCE_TOKEN_RE.findall((query or "").lower())
        if len(t) >= 2 and t not in _RELEVANCE_STOPWORDS
    }


def _drop_irrelevant_singletons(
    hits: list[AggregatedHit], query: str
) -> list[AggregatedHit]:
    """Remove single-source hits whose titles share zero significant query tokens.

    Multi-source hits (≥2 contributing APIs) are always kept — source consensus
    is evidence enough. Single-source hits must show at least one non-stopword
    query token in their title to survive. This catches the failure mode where
    OpenAlex/S2 relevance ranking leaks a popular paper that merely shares a
    generic word like "Advances" or "Model" with the query.
    """
    q_tokens = _significant_query_tokens(query)
    if not q_tokens:
        return hits  # Can't filter — user's query is all stopwords.
    kept: list[AggregatedHit] = []
    for h in hits:
        if len(h.sources) >= 2:
            kept.append(h)
            continue
        title_tokens = set(_RELEVANCE_TOKEN_RE.findall((h.title or "").lower()))
        if title_tokens & q_tokens:
            kept.append(h)
    return kept


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def aggregate_search(
    query: str,
    max_results: int = 20,
    *,
    sources: tuple[str, ...] | list[str] = DEFAULT_SOURCES,
    parallel: bool = True,
    **filters,
) -> list[AggregatedHit]:
    """Run the configured sources, dedup, merge, rank, and truncate to ``max_results``.

    Each source receives the same ``query`` and ``max_results``; in practice each
    returns up to ``max_results`` hits, so the aggregator may briefly hold up to
    ``len(sources) * max_results`` entries before dedup. ``filters`` are passed
    through to fetchers that understand them (each fetcher self-selects).
    """
    fetchers = [(name, _FETCHERS[name]) for name in sources if name in _FETCHERS]
    if not fetchers:
        return []

    def _safe(name: str, fn) -> list[AggregatedHit]:
        try:
            return fn(query, max_results, **filters)
        except Exception as e:  # noqa: BLE001 — never let one source kill the run
            print(f"[aggregator] {name} failed: {e}", file=sys.stderr)
            return []

    all_hits: list[AggregatedHit] = []
    if parallel and len(fetchers) > 1:
        with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
            futures = {ex.submit(_safe, n, fn): n for n, fn in fetchers}
            for fut in as_completed(futures):
                all_hits.extend(fut.result())
    else:
        for name, fn in fetchers:
            all_hits.extend(_safe(name, fn))

    deduped = _dedup(all_hits)
    filtered = _drop_irrelevant_singletons(deduped, query)
    return _rank(filtered)[:max_results]


# --------------------------------------------------------------------------
# parallel single-paper lookup (for cmd_info)
# --------------------------------------------------------------------------


def _pick_longer_str(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if len(a) >= len(b) else b


def _merge_papers(papers: list["CachedPaper"]) -> "CachedPaper":
    """Field-by-field merge of multiple CachedPaper records → one rich record.

    Preference rules (asymmetric on purpose, matching the per-source
    sanitization in single-source paths):
      - title: first non-empty (sources tend to agree; OpenAlex sometimes
        capitalises differently)
      - authors: longest list (most sources truncate)
      - abstract: longest non-empty (PubMed/EuropePMC abstracts are typically
        canonical; OpenAlex's reconstructed-from-inverted-index can be slightly
        different word order but still longer than nothing)
      - year: prefer non-empty; ties broken by first-encountered
      - doi/pmid/pmcid/arxiv_id/pdf_url/categories: union (first non-empty)
      - source: comma-separated tag of all contributing sources
    """
    base = papers[0]
    sources = [base.source] if base.source else []
    for p in papers[1:]:
        if not base.title and p.title:
            base.title = p.title
        if len(p.authors) > len(base.authors):
            base.authors = p.authors
        merged_abstract = _pick_longer_str(base.abstract or None, p.abstract or None)
        if merged_abstract:
            base.abstract = merged_abstract
        base.year = base.year or p.year
        base.doi = base.doi or p.doi
        base.pmid = base.pmid or p.pmid
        base.pmcid = base.pmcid or p.pmcid
        base.arxiv_id = base.arxiv_id or p.arxiv_id
        base.pdf_url = base.pdf_url or p.pdf_url
        if not base.categories and p.categories:
            base.categories = p.categories
        if p.source and p.source not in sources:
            sources.append(p.source)
    if sources:
        base.source = "+".join(sources)
    return base


def aggregate_lookup(
    *,
    arxiv_id: str | None = None,
    doi: str | None = None,
    pmid: str | None = None,
) -> "CachedPaper | None":
    """Parallel multi-source single-paper lookup.

    Routes the lookup to every source that supports the given ID type:
      - arxiv_id → OpenAlex + S2 + arXiv API
      - doi      → OpenAlex + Europe PMC; ChemRxiv too if it's a 10.26434 DOI
      - pmid     → PubMed + OpenAlex (via pmid: accessor)

    Sources run concurrently; any one going down doesn't block the others.
    Returns the merged :class:`paper_cache.CachedPaper` (richest single
    record) or ``None`` if every source comes back empty.
    """
    fetchers: list[tuple[str, callable]] = []
    if arxiv_id:
        fetchers.append(("openalex", lambda: _fetch_paper_openalex(arxiv_id)))
        fetchers.append(("s2",       lambda: _fetch_paper_s2(arxiv_id)))
        fetchers.append(("arxiv",    lambda: _fetch_paper_arxiv(arxiv_id)))
    if doi:
        fetchers.append(("openalex", lambda: _fetch_paper_openalex_spec(f"DOI:{doi}")))
        fetchers.append(("europepmc", lambda: _fetch_paper_europepmc_by_doi(doi)))
        if is_chemrxiv_doi(doi):
            fetchers.append(("chemrxiv", lambda: _fetch_paper_chemrxiv(doi)))
    if pmid:
        fetchers.append(("pubmed",   lambda: _fetch_paper_pubmed(pmid)))
        fetchers.append(("openalex", lambda: _fetch_paper_openalex_spec(f"PMID:{pmid}")))

    if not fetchers:
        return None

    papers: list[CachedPaper] = []
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        future_to_name = {ex.submit(fn): name for name, fn in fetchers}
        for fut in as_completed(future_to_name):
            name = future_to_name[fut]
            try:
                p = fut.result()
            except Exception as e:  # noqa: BLE001 — never let one source kill the lookup
                print(f"[aggregate_lookup] {name} failed: {e}", file=sys.stderr)
                continue
            if p is not None:
                papers.append(p)
    if not papers:
        return None
    return _merge_papers(papers)
