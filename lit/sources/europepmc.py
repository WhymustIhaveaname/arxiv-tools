"""Europe PMC adapter.

Europe PMC (EMBL-EBI's biomedical literature service) covers PubMed + PMC
+ preprints (bioRxiv / medRxiv / Research Square) + patents + clinical
guidelines — one API, no key, 10 req/s.

Two APIs live here:

1. **REST webservices** (``/europepmc/webservices/rest/``) — search, paper
   metadata by source+id, full-text XML, references, citations.
2. **Annotations API** (``/europepmc/annotations_api/``) — **text-mined
   entity annotations**: genes / diseases / chemicals / organisms / GO
   terms / accession numbers. No other platform gives us this. Core value
   of this adapter for biomedical + chemistry AI work.

Source IDs in Europe PMC's own namespace:
- ``MED`` — PubMed article (``id`` = PMID)
- ``PMC`` — PubMed Central full-text (``id`` = PMCID, e.g. ``"PMC7610144"``)
- ``PPR`` — preprint (bioRxiv / medRxiv / Research Square / …)
- ``PAT`` — patent
- ``HIR`` — heterogeneous imported records
"""

from __future__ import annotations

import re
import sys

import requests

from lit.config import EUROPEPMC_API_BASE, HTTP_HEADERS
from lit.ids import _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


EUROPEPMC_ANNOTATIONS_BASE = "https://www.ebi.ac.uk/europepmc/annotations_api"

# Annotation types the API exposes, grouped into user-friendly short names.
# Keys = CLI-facing names; values = the exact type strings Europe PMC uses.
ANNOTATION_TYPE_MAP = {
    "genes": "Gene_Proteins",
    "diseases": "Diseases",
    "chemicals": "Chemicals",
    "organisms": "Organisms",
    "go": "Gene Ontology",
    "methods": "Experimental Methods",
    "accessions": "Accession Numbers",
    "resources": "Resources",
}


def fetch_pmc_fulltext_xml(pmcid: str) -> str | None:
    """Fetch the JATS XML full-text for a PMC paper.

    Returns the raw XML body, or ``None`` if the paper is not in the OA
    subset (Europe PMC only serves OA full-text).
    """
    bare = pmcid.upper()
    if not bare.startswith("PMC"):
        bare = f"PMC{bare}"
    url = f"{EUROPEPMC_API_BASE}/{bare}/fullTextXML"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="europepmc",
            headers=HTTP_HEADERS,
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Europe PMC full-text fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    body = resp.text
    if not body or "<article" not in body:
        return None
    return body


# --------------------------------------------------------------------- search

def _build_europepmc_query(
    query: str,
    *,
    year: str | None = None,
    open_access: bool = False,
    src: str | None = None,
) -> str:
    """Assemble Europe PMC's search DSL from CLI filters.

    The REST endpoint accepts field-tagged clauses joined by AND:
      (foo) AND FIRST_PDATE:[2020-01-01 TO 2024-12-31] AND OPEN_ACCESS:y AND SRC:PPR
    """
    parts: list[str] = [f"({query})"] if query.strip() else []
    if year:
        if "-" in year:
            lo, hi = year.split("-", 1)
            lo = lo.strip() or "1800"
            hi = hi.strip() or "3000"
            parts.append(f"FIRST_PDATE:[{lo}-01-01 TO {hi}-12-31]")
        else:
            parts.append(f"FIRST_PDATE:[{year}-01-01 TO {year}-12-31]")
    if open_access:
        parts.append("OPEN_ACCESS:y")
    if src:
        parts.append(f"SRC:{src.upper()}")
    return " AND ".join(parts)


def _search_europepmc(
    query: str,
    max_results: int = 20,
    *,
    offset: int = 0,
    year: str | None = None,
    open_access: bool = False,
    src: str | None = None,
) -> list[dict] | None:
    """Search Europe PMC and return raw ``resultList.result`` records.

    Europe PMC uses ``cursorMark`` for deep pagination (>10k results), but
    for our shallow use case a simple page-based offset is enough: fetch
    ``offset + max_results`` records in one call, slice the tail.
    """
    assembled = _build_europepmc_query(
        query, year=year, open_access=open_access, src=src
    )
    url = f"{EUROPEPMC_API_BASE}/search"
    page_size = min(offset + max_results, 1000)
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="europepmc",
            params={
                "query": assembled,
                "format": "json",
                "resultType": "core",   # full records incl abstract + text-mined flags
                "pageSize": str(page_size),
                "cursorMark": "*",
            },
            headers=HTTP_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Europe PMC search failed: {_brief_error(e)}", file=sys.stderr)
        return None

    items = (data.get("resultList") or {}).get("result") or []
    return items[offset : offset + max_results] or None


def _normalize_europepmc_search(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        src = r.get("source") or ""
        ext_id = r.get("id") or ""
        pmid = r.get("pmid") or ""
        pmcid = r.get("pmcid") or ""
        doi = r.get("doi") or ""

        # Prefer the most recognizable ID form for display.
        if pmid:
            id_str = f"PMID:{pmid}"
        elif pmcid:
            id_str = f"PMCID:{pmcid}"
        elif doi:
            id_str = f"DOI:{doi}"
        elif src and ext_id:
            id_str = f"{src}:{ext_id}"
        else:
            id_str = ""

        abstract = r.get("abstractText") or None
        if abstract:
            abstract = re.sub(r"</?[^>]+>", "", abstract).strip() or None

        out.append(
            {
                "id": id_str,
                "title": (r.get("title") or "").rstrip("."),
                "authors": r.get("authorString") or "",
                "year": str(r.get("pubYear") or "?"),
                "cited_by": r.get("citedByCount"),
                "abstract": abstract,
            }
        )
    return out


def _fetch_paper_europepmc_by_source_id(source: str, ext_id: str) -> CachedPaper | None:
    """Fetch a single paper by Europe PMC's own (source, id) pair.

    Used when we already know the paper is in Europe PMC and want the
    richest metadata (categories, OA flag, PMC full-text URL).
    """
    url = f"{EUROPEPMC_API_BASE}/search"
    query = f"SRC:{source.upper()} AND EXT_ID:{ext_id}"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="europepmc",
            params={"query": query, "format": "json", "resultType": "core", "pageSize": "1"},
            headers=HTTP_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Europe PMC paper fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    items = (data.get("resultList") or {}).get("result") or []
    if not items:
        return None
    r = items[0]
    return _record_to_cached_paper(r)


def _fetch_paper_europepmc_by_doi(doi: str) -> CachedPaper | None:
    """Look up a paper in Europe PMC by DOI (any source — MED, PPR, etc.).

    Great for preprints whose DOI is the primary identifier (bioRxiv's
    10.1101/…, medRxiv's 10.1101/…, Research Square's 10.21203/…).
    """
    r = _fetch_raw_europepmc_by_doi(doi)
    return _record_to_cached_paper(r) if r else None


def _fetch_raw_europepmc_by_doi(doi: str) -> dict | None:
    """Internal: return the raw Europe PMC result record for a DOI.

    Used when callers need fields not in CachedPaper (e.g. ``isOpenAccess``,
    ``inPMC``) to make retrieval decisions.
    """
    url = f"{EUROPEPMC_API_BASE}/search"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="europepmc",
            params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": "1"},
            headers=HTTP_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Europe PMC DOI fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    items = (data.get("resultList") or {}).get("result") or []
    return items[0] if items else None


def pmc_full_text_locator(doi: str) -> tuple[str | None, bool]:
    """Return ``(pmcid, is_oa_full_text)`` for a DOI.

    Europe PMC happily returns a PMCID for papers that are only abstract-
    indexed (``inPMC=Y`` but ``isOpenAccess=N``); those PMCIDs then 404 on
    every JATS/BioC fetch. Callers use ``is_oa_full_text`` to skip the PMC
    chain and jump straight to OA mirrors.
    """
    r = _fetch_raw_europepmc_by_doi(doi)
    if not r:
        return None, False
    pmcid = r.get("pmcid") or None
    is_oa = (r.get("isOpenAccess") or "").upper() == "Y"
    return pmcid, is_oa


def _record_to_cached_paper(r: dict) -> CachedPaper:
    """Europe PMC ``resultList.result`` record → CachedPaper."""
    abstract = r.get("abstractText") or ""
    if abstract:
        abstract = re.sub(r"</?[^>]+>", "", abstract).strip()

    # Authors: authorList.author[].fullName is the cleanest; authorString is a
    # pre-joined string we can fall back to.
    authors: list[CachedAuthor] = []
    for a in (r.get("authorList") or {}).get("author") or []:
        name = a.get("fullName") or a.get("lastName") or ""
        if name:
            authors.append(CachedAuthor(name))
    if not authors and r.get("authorString"):
        authors = [CachedAuthor(n.strip()) for n in r["authorString"].split(",") if n.strip()]

    journal = (r.get("journalInfo") or {}).get("journal", {}).get("title") or ""
    categories = [journal] if journal else []

    pmid = r.get("pmid") or None
    pmcid = r.get("pmcid") or None
    doi = r.get("doi") or None

    year = None
    try:
        if r.get("pubYear"):
            year = int(r["pubYear"])
    except (ValueError, TypeError):
        pass

    # Pick the first available full-text PDF URL if Europe PMC exposes one.
    pdf_url = ""
    for u in (r.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
        if u.get("documentStyle") == "pdf":
            pdf_url = u.get("url") or ""
            break

    return CachedPaper(
        title=(r.get("title") or "").rstrip("."),
        authors=authors,
        abstract=abstract,
        categories=categories,
        pdf_url=pdf_url,
        year=year,
        source="europepmc",
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
    )


# ---------------------------------------------------------------- annotations

def _article_id_for_annotations(pmid: str | None, pmcid: str | None) -> str | None:
    """Build the ``articleIds`` parameter value for the annotations endpoint.

    Prefers PMC over PMID (better annotation coverage — PMC has full-text
    text mining; PubMed has title+abstract only).
    """
    if pmcid:
        pmc = pmcid.upper()
        if not pmc.startswith("PMC"):
            pmc = f"PMC{pmc}"
        return f"PMC:{pmc}"
    if pmid:
        return f"MED:{pmid}"
    return None


def fetch_annotations(
    *,
    pmid: str | None = None,
    pmcid: str | None = None,
    types: list[str] | None = None,
) -> list[dict] | None:
    """Fetch text-mined annotations for one paper.

    Returns the flat list of annotation dicts (each has ``type``, ``exact``
    (the surface string matched), ``tags`` with ontology URIs, ``section``,
    ``prefix``/``postfix`` context, and so on).

    Pass exactly one of ``pmid`` or ``pmcid``. ``types`` is the
    CLI-friendly list (``["genes", "diseases"]``), translated internally
    to Europe PMC's strings.
    """
    article_id = _article_id_for_annotations(pmid, pmcid)
    if not article_id:
        return None

    params: dict[str, str] = {"articleIds": article_id, "format": "JSON"}
    if types:
        mapped = [ANNOTATION_TYPE_MAP.get(t, t) for t in types]
        params["type"] = ",".join(mapped)

    try:
        resp = _request_with_retry(
            requests.get,
            f"{EUROPEPMC_ANNOTATIONS_BASE}/annotationsByArticleIds",
            service="europepmc",
            params=params,
            headers=HTTP_HEADERS,
            timeout=60,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"Europe PMC annotations fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    if not isinstance(data, list) or not data:
        return []
    # The API returns one entry per articleId; we only ever query one, so
    # flatten the annotation list out.
    return data[0].get("annotations") or []


def group_annotations_by_type(annotations: list[dict]) -> dict[str, list[dict]]:
    """Bucket annotations by their ``type`` field, preserving order."""
    grouped: dict[str, list[dict]] = {}
    for a in annotations:
        t = a.get("type") or "Uncategorised"
        grouped.setdefault(t, []).append(a)
    return grouped
