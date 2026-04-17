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
from lit.sources.arxiv_api import search_papers as _search_arxiv
from lit.sources.chemrxiv import _search_chemrxiv
from lit.sources.europepmc import _search_europepmc
from lit.sources.openalex import _reconstruct_abstract, _search_openalex
from lit.sources.pubmed import _search_pubmed
from lit.sources.s2 import _search_s2


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

    return _rank(_dedup(all_hits))[:max_results]
