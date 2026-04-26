#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "arxiv",
#     "json5",
#     "pymupdf",
#     "python-dotenv",
#     "requests",
# ]
# ///
"""arXiv paper search and full-text fetch tool.

Subcommands:
    search - search papers (keyword / title / abstract)
    info   - fetch paper metadata (no download)
    bib    - generate a BibTeX entry
    tex    - download LaTeX source (with PDF-text fallback)
    infotex - print info, then download/show LaTeX source
    cited  - reverse citation lookup (S2 → OpenAlex)

Usage (via uv run):
    uv run arxiv_tool.py search "PINN" --max 5
    uv run arxiv_tool.py info 2401.12345
    uv run arxiv_tool.py bib 2401.12345 -o refs.bib
    uv run arxiv_tool.py tex 2401.12345
    uv run arxiv_tool.py infotex 2401.12345
    uv run arxiv_tool.py cited 1711.10561 --max 20
    uv run arxiv_tool.py cited 1711.10561 --offset 20
    uv run arxiv_tool.py cited 1711.10561 --source openalex

Implementation is split across the `lit/` package; this module only owns
CLI orchestration (cmd_*, get_paper_info, main). The re-exports below keep
existing tests that patch `arxiv_tool.X` working.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime as _datetime
from pathlib import Path

import arxiv
import requests

from lit.config import (
    AUDIT_LOG,
    CACHE_DIR,
    CONTACT_EMAIL,
    HTTP_HEADERS,
    MANUAL_PDF_DIR,
    OPENALEX_API_BASE,
    OPENALEX_API_KEY,
    OPENALEX_ENABLED,
    S2_API_BASE,
    S2_API_KEY,
    WORK_DIR,
)
from lit.ids import (
    _arxiv_date,
    _arxiv_year,
    _truncate_authors,
    basename_for_id,
    extract_arxiv_id,
    extract_paper_id,
    sanitize_filename,
)
from lit.bibtex import (
    generate_bibtex,
    generate_bibtex_pubmed,
    generate_citation_key,
)
from lit.aggregator import (
    DEFAULT_SOURCES as AGG_DEFAULT_SOURCES,
    DOMAIN_PRESETS as AGG_DOMAIN_PRESETS,
    aggregate_lookup,
    aggregate_search,
)
from lit.crossref import fetch_bibtex_crossref
from lit.display import (
    print_aggregated_results as _print_aggregated_results,
    print_doi_info as _print_doi_info,
    print_search_results as _print_search_results,
)
from lit.enrich import enrich_paper_ids
from lit.batch import (
    record_single_failure,
    record_single_success,
    run_batch,
    run_import,
)
from lit.fetch import Layer, walk_layers
from lit.oa_mirror import find_oa_pdf_urls, try_download_pdf
from lit.pdf import (
    extract_pdf_text as _pdf_extract_text,
    ingest_local_pdf as _pdf_ingest,
    save_pdf_and_text as _pdf_save,
)
from lit.preprint_lookup import PreprintVersion, find_preprint_versions
from lit.sources.chemrxiv import (
    _fetch_paper_chemrxiv,
    _normalize_chemrxiv_search,
    _search_chemrxiv,
    chemrxiv_pdf_url,
    fetch_chemrxiv_pdf,
    is_chemrxiv_doi,
)
from lit.ratelimit import RateLimiter, _brief_error, _request_with_retry
from lit.fulltext import (
    _extract_braced_arg,
    _extract_source,
    _fetch_pdf_fallback,
    _strip_tex_comments,
    _try_rename_with_title,
    fetch_tex_source,
    print_tree,
)
from lit.sources.arxiv_api import (
    _fetch_paper_arxiv,
    _normalize_arxiv_search,
    search_papers,
)
from lit.sources.europepmc import (
    ANNOTATION_TYPE_MAP,
    _fetch_paper_europepmc_by_doi,
    pmc_full_text_locator,
    _normalize_europepmc_search,
    _search_europepmc,
    fetch_annotations,
    fetch_pmc_fulltext_xml,
    group_annotations_by_type,
)
from lit.sources.ncbi_bioc import fetch_pmc_bioc_json
from lit.sources.pubmed import (
    _fetch_paper_pubmed,
    _normalize_pubmed_search,
    _search_pubmed,
    fetch_esummary_batch,
    fetch_pmc_pdf,
    fetch_pubmed_references,
    fetch_similar_pmids,
    pmcid_to_pmid,
    pmid_to_pmcid,
    print_pubmed_info,
)
from lit.sources.openalex import (
    _fetch_citations_openalex,
    _fetch_citations_openalex_spec,
    _fetch_paper_openalex,
    _fetch_paper_openalex_spec,
    _normalize_openalex_search,
    _openalex_params,
    _print_citations_openalex,
    _reconstruct_abstract,
    _resolve_openalex_id,
    _resolve_openalex_id_spec,
    _search_openalex,
)
from lit.sources.s2 import (
    _fetch_citations_s2,
    _fetch_citations_s2_spec,
    _fetch_paper_s2,
    _fetch_references_s2_spec,
    _normalize_s2_search,
    _print_citations_s2,
    _s2_headers,
    _s2_search_params,
    _search_s2,
    _search_s2_bulk,
)
from paper_cache import (
    CachedAuthor,
    CachedPaper,
    cache_paper,
    cache_paper_with_crossrefs,
    get_cached_bibtex,
    get_cached_paper,
)

OUTPUT_DIR = CACHE_DIR

_LAST_SOURCE: str = "unknown"


def _set_source(src: str) -> None:
    """Record the main source used by the current CLI invocation for audit."""
    global _LAST_SOURCE
    _LAST_SOURCE = src


def _write_audit_entry(entry: dict) -> None:
    """Append a CLI audit row. Failures are deliberately non-fatal."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def get_paper_info(arxiv_id: str):
    clean_id = extract_arxiv_id(arxiv_id)

    cached = get_cached_paper(clean_id)
    if cached:
        return cached

    paper = (
        _fetch_paper_openalex(clean_id)
        or _fetch_paper_s2(clean_id)
        or _fetch_paper_arxiv(clean_id)
    )

    if not paper:
        print(f"Paper not found: {clean_id}", file=sys.stderr)
        return None

    enrich_paper_ids(paper)
    bibtex = generate_bibtex(paper, clean_id)
    cache_paper_with_crossrefs(f"arxiv:{clean_id}", paper, bibtex)
    return paper


def get_paper_info_pubmed(pmid: str):
    """Cache-aware PubMed metadata fetch with cross-ref rows written on miss.

    Looks up ``pmid:<pmid>`` (or any alias) in the shared cache before hitting
    EFetch. On miss, fetches + enriches IDs via OpenAlex + writes one cache row
    per known ID so subsequent DOI/arXiv/PMC lookups of the same paper hit.
    Cached entries carry an empty bibtex until cmd_bib fills it in.
    """
    cache_key = f"pmid:{pmid}"
    cached = get_cached_paper(cache_key)
    if cached:
        return cached

    paper = _fetch_paper_pubmed(pmid)
    if not paper:
        return None

    enrich_paper_ids(paper)
    cache_paper_with_crossrefs(cache_key, paper, "")
    return paper


def _merge_europepmc_into(base, extra):
    """Overlay Europe PMC metadata onto an existing CachedPaper without losing
    anything the primary source already populated."""
    if not base.abstract and extra.abstract:
        base.abstract = extra.abstract
    if not base.pdf_url and extra.pdf_url:
        base.pdf_url = extra.pdf_url
    if not base.categories and extra.categories:
        base.categories = extra.categories
    if not base.pmid and extra.pmid:
        base.pmid = extra.pmid
    if not base.pmcid and extra.pmcid:
        base.pmcid = extra.pmcid
    if not base.year and extra.year:
        base.year = extra.year
    return base


def _is_biomed_preprint_doi(doi: str) -> bool:
    """bioRxiv / medRxiv / Research Square DOI prefixes — papers likely to
    be in Europe PMC but missing from OpenAlex's preprint index."""
    low = doi.lower()
    return low.startswith("10.1101/") or low.startswith("10.21203/")


def _is_biorxiv_doi(doi: str) -> bool:
    """bioRxiv & medRxiv share the 10.1101 prefix."""
    return doi.lower().startswith("10.1101/")


def _save_pdf_and_text(pdf_bytes: bytes, out_basename: str) -> None:
    """Thin wrapper around :func:`lit.pdf.save_pdf_and_text` using OUTPUT_DIR.

    OUTPUT_DIR is looked up dynamically at call time so tests that reassign
    ``arxiv_tool.OUTPUT_DIR = tmp_path`` keep working.
    """
    _pdf_save(pdf_bytes, out_basename, OUTPUT_DIR)


def _try_oa_mirror_for_pdf(
    *, doi: str | None = None, openalex_pdf_url: str | None = None,
) -> bytes | None:
    """Run the OA-mirror chain: Unpaywall → OpenAlex → Crossref TDM.

    Returns the first valid PDF body found, or ``None`` if every mirror
    either failed or returned non-PDF content.
    """
    urls = find_oa_pdf_urls(doi=doi, openalex_pdf_url=openalex_pdf_url)
    if not urls:
        return None
    for u in urls:
        print(f"  trying OA mirror: {u[:80]}...", file=sys.stderr)
        pdf = try_download_pdf(u)
        if pdf:
            return pdf
    return None


def get_paper_info_doi(doi: str):
    """Cache-aware DOI metadata fetch.

    Dispatch rules:
    - ChemRxiv DOIs (10.26434/…) → OpenAlex for abstract + Crossref for
      categories / PDF URL.
    - bioRxiv / medRxiv / Research Square DOIs → Europe PMC first (its
      preprint coverage is better), fall back to OpenAlex.
    - Everything else → OpenAlex.
    """
    cache_key = f"doi:{doi.lower()}"
    cached = get_cached_paper(cache_key)
    if cached:
        return cached

    paper = _fetch_paper_openalex_spec(f"DOI:{doi}")

    if is_chemrxiv_doi(doi):
        chem = _fetch_paper_chemrxiv(doi)
        if chem is not None:
            if paper is None:
                paper = chem
            else:
                # Keep OpenAlex's richer abstract, but overlay ChemRxiv-specific
                # fields so downstream display / caching looks correct.
                paper.source = "chemrxiv"
                if chem.pdf_url:
                    paper.pdf_url = chem.pdf_url
                if chem.categories and not paper.categories:
                    paper.categories = chem.categories

    if _is_biomed_preprint_doi(doi) and (paper is None or not paper.abstract):
        epmc = _fetch_paper_europepmc_by_doi(doi)
        if epmc is not None:
            paper = epmc if paper is None else _merge_europepmc_into(paper, epmc)

    if not paper:
        return None

    if not paper.doi:
        paper.doi = doi
    enrich_paper_ids(paper)
    cache_paper_with_crossrefs(cache_key, paper, "")
    return paper


def _s2_filters_from_args(args) -> dict:
    filters = {}
    if getattr(args, "year", None):
        filters["year"] = args.year
    if getattr(args, "fields_of_study", None):
        filters["fields_of_study"] = args.fields_of_study
    if getattr(args, "pub_types", None):
        filters["publication_types"] = args.pub_types
    if getattr(args, "min_citations", None) is not None:
        filters["min_citations"] = args.min_citations
    if getattr(args, "venue", None):
        filters["venue"] = args.venue
    if getattr(args, "open_access", False):
        filters["open_access"] = True
    return filters


_SINGLE_SOURCE_DISPATCH: dict[str, tuple[str, Callable, Callable, tuple[str, ...]]] = {
    "pubmed":    ("PubMed",                _search_pubmed,    _normalize_pubmed_search,
                  ("offset", "year", "open_access")),
    "europepmc": ("Europe PMC",            _search_europepmc, _normalize_europepmc_search,
                  ("offset", "year", "open_access")),
    "chemrxiv":  ("ChemRxiv (via Crossref)", _search_chemrxiv, _normalize_chemrxiv_search,
                  ("offset", "year")),
}


def _run_single_source(args, source: str) -> None:
    """One-source search dispatcher.

    Each entry in :data:`_SINGLE_SOURCE_DISPATCH` declares which kwargs the
    source's ``_search_*`` accepts; we forward only those to avoid
    ``TypeError`` from sources that don't take e.g. ``open_access``.
    """
    label, search_fn, normalize_fn, accepted = _SINGLE_SOURCE_DISPATCH[source]
    print(f"Searching {label}...", file=sys.stderr)
    kwargs: dict = {}
    if "offset" in accepted:
        kwargs["offset"] = int(getattr(args, "offset", 0) or 0)
    if "year" in accepted:
        kwargs["year"] = getattr(args, "year", None)
    if "open_access" in accepted:
        kwargs["open_access"] = bool(getattr(args, "open_access", False))
    raw = search_fn(args.query, args.max, **kwargs)
    if not raw:
        print(f"No results from {label}")
        return
    normalized = normalize_fn(raw)
    _set_source(source)
    print(f"\nFound {len(normalized)} papers ({label}):\n")
    _print_search_results(normalized)


def cmd_search(args):
    source = args.source
    if source == "openalex" and not OPENALEX_ENABLED:
        print(
            "OpenAlex 源已被禁用 (上游 metadata 污染). "
            "请改用 --source s2 / --source arxiv, 或省略 --source 走默认多源搜索.",
            file=sys.stderr,
        )
        sys.exit(2)

    filters = _s2_filters_from_args(args)

    if source == "all":
        agg_filters = dict(filters)
        agg_filters["offset"] = getattr(args, "offset", 0) or 0
        if getattr(args, "snippet", False):
            agg_filters["snippet"] = True
            print(
                "Using S2 /snippet/search (full-text snippet ranking) instead of /paper/search",
                file=sys.stderr,
            )
        sources = list(AGG_DEFAULT_SOURCES)
        domain = getattr(args, "domain", None)
        if domain:
            preset = AGG_DOMAIN_PRESETS[domain]  # argparse choices guards this
            sources = list(preset["sources"])
            # Domain field_of_study filter wins over user-supplied --fields-of-study
            # only when the user didn't provide one.
            if "fields_of_study" not in agg_filters and preset.get("fields_of_study"):
                agg_filters["fields_of_study"] = preset["fields_of_study"]
            print(
                f"Domain {domain!r}: restricting to {', '.join(sources)}"
                f" + S2 fields_of_study={agg_filters.get('fields_of_study')!r}",
                file=sys.stderr,
            )
        if not OPENALEX_ENABLED and "openalex" in sources:
            sources = [s for s in sources if s != "openalex"]
        print(
            f"Searching {', '.join(sources)} in parallel...",
            file=sys.stderr,
        )
        hits = aggregate_search(args.query, args.max, sources=sources, **agg_filters)
        if not hits:
            print("No results from any source")
            return
        _set_source("all")
        print(f"\nFound {len(hits)} unique papers:\n")
        _print_aggregated_results(hits)
        return

    if source in _SINGLE_SOURCE_DISPATCH:
        _run_single_source(args, source)
        return

    results = None

    if source in ("s2", "auto"):
        if getattr(args, "bulk", False):
            print("Searching Semantic Scholar (bulk)...", file=sys.stderr)
            sort = getattr(args, "sort", None)
            token = getattr(args, "token", None)
            ret = _search_s2_bulk(args.query, args.max, token=token, sort=sort, **filters)
            if ret:
                raw, next_token = ret
                results = ("Semantic Scholar (bulk)", _normalize_s2_search(raw))
                if next_token:
                    print(f"\nNext page token: {next_token}", file=sys.stderr)
        else:
            print("Searching Semantic Scholar...", file=sys.stderr)
            raw = _search_s2(args.query, args.max, **filters)
            if raw:
                results = ("Semantic Scholar", _normalize_s2_search(raw))

    if not results and source in ("openalex", "auto") and OPENALEX_ENABLED:
        print("Searching OpenAlex...", file=sys.stderr)
        raw = _search_openalex(args.query, args.max)
        if raw:
            results = ("OpenAlex", _normalize_openalex_search(raw))

    if not results and source in ("arxiv", "auto"):
        if source == "auto":
            if OPENALEX_ENABLED:
                msg = (
                    "⚠ S2 and OpenAlex both failed, falling back to arXiv API. "
                    "If this keeps happening, check API keys and network."
                )
            else:
                msg = "⚠ S2 failed, falling back to arXiv API (OpenAlex disabled)."
            print(msg, file=sys.stderr)
        print("Searching arXiv...", file=sys.stderr)
        raw = search_papers(args.query, args.max)
        if raw:
            results = ("arXiv", _normalize_arxiv_search(raw))

    if not results:
        print("No results from any source")
        return

    source_name, normalized = results
    _set_source(source_name.split()[0].lower())
    print(f"\nFound {len(normalized)} papers ({source_name}):\n")
    _print_search_results(normalized)


def _resolve_pmcid_or_die(pmcid: str) -> str:
    pmid = pmcid_to_pmid(pmcid)
    if not pmid:
        print(f"Could not resolve {pmcid} to a PMID via NCBI ELink.", file=sys.stderr)
        sys.exit(1)
    print(f"Resolved {pmcid} → PMID:{pmid}", file=sys.stderr)
    return pmid


# S2 / OpenAlex paper_spec prefix (capital-A ArXiv) and user-facing display prefix
# (lowercase-a arXiv) for each ID type. Keep these in lockstep with the fetchers
# in lit/sources/{s2,openalex}.py.
_XREF_ID_PREFIXES = {
    "arxiv": ("ArXiv:", "arXiv:"),
    "pmid":  ("PMID:",  "PMID:"),
    "doi":   ("DOI:",   "DOI:"),
}


def _resolve_xref_id(raw_id: str) -> tuple[str, str, str, str]:
    """Shared ID resolution for ``cited`` / ``references``.

    Parses ``raw_id``, resolves PMC IDs to their PMID via NCBI ELink, and
    returns ``(paper_spec, display_id, id_type, clean_id)``. Exits non-zero
    on unsupported identifiers (keywords, unknown formats).
    """
    id_type, clean_id = extract_paper_id(raw_id)
    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"
    if id_type not in _XREF_ID_PREFIXES:
        print(
            f"Unrecognised identifier '{raw_id}' — supported: arXiv ID, "
            f"PMID, PMC ID, DOI.",
            file=sys.stderr,
        )
        sys.exit(1)
    spec_pfx, disp_pfx = _XREF_ID_PREFIXES[id_type]
    return f"{spec_pfx}{clean_id}", f"{disp_pfx}{clean_id}", id_type, clean_id


def cmd_info(args):
    """Display paper metadata from a parallel multi-source lookup.

    Cache check first (so repeated calls are free). On cache miss, runs
    every source that supports the ID type concurrently and merges fields
    — broadest abstract, longest author list, union of cross-ref IDs. The
    legacy single-source ``get_paper_info_*`` helpers stay available to
    library callers for backward compatibility.
    """
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"

    cache_key = {
        "arxiv": f"arxiv:{clean_id}",
        "pmid":  f"pmid:{clean_id}",
        "doi":   f"doi:{clean_id.lower()}",
    }.get(id_type)
    if cache_key is None:
        print(
            f"Unrecognised identifier '{args.arxiv_id}' — supported: arXiv ID, "
            f"PMID, PMC ID, DOI.",
            file=sys.stderr,
        )
        sys.exit(1)

    paper = get_cached_paper(cache_key)
    was_cached = paper is not None
    if paper is None:
        kwargs = {id_type if id_type != "arxiv" else "arxiv_id": clean_id}
        paper = aggregate_lookup(**kwargs)
        if paper is None:
            label = {"arxiv": "arXiv", "pmid": "PMID", "doi": "DOI"}[id_type]
            print(f"Paper not found: {label}:{clean_id}", file=sys.stderr)
            return
        # Caching is best-effort: if enrich/cache crash on a partial record
        # (e.g. test fixtures missing a field), still print what we have.
        try:
            enrich_paper_ids(paper)
            cache_paper_with_crossrefs(cache_key, paper, "")
        except Exception as e:  # noqa: BLE001
            print(f"[cmd_info] cache write failed: {e}", file=sys.stderr)

    _set_source("cache" if was_cached else (getattr(paper, "source", None) or "lookup"))

    if id_type == "pmid":
        print_pubmed_info(clean_id, paper)
        return
    if id_type == "doi":
        _print_doi_info(clean_id, paper)
        return

    # arxiv path
    arxiv_date = _arxiv_date(clean_id)
    date_str = arxiv_date.strftime("%Y-%m") if arxiv_date else "?"
    print(f"arXiv ID: {clean_id}")
    print(f"Title: {paper.title}")
    print(f"Authors: {', '.join(a.name for a in paper.authors)}")
    print(f"Published: {date_str}")
    if paper.categories:
        print(f"Categories: {', '.join(paper.categories)}")
    print(f"PDF: {paper.pdf_url}")
    cached_tex = _find_cached_tex_dir(clean_id)
    if cached_tex:
        print(f"Tex (cached): {cached_tex}")
    print(f"\nAbstract:\n{paper.abstract}")


def _write_bibtex(bibtex: str, output: str | None) -> None:
    if not output:
        print(bibtex)
        return
    output_path = Path(output)
    mode = "a" if output_path.exists() else "w"
    with open(output_path, mode, encoding="utf-8") as f:
        if mode == "a" and output_path.stat().st_size > 0:
            f.write("\n\n")
        f.write(bibtex)
        f.write("\n")
    print(f"{'Appended' if mode == 'a' else 'Written'} to: {output_path}")


def _bib_for_pmid(pmid: str) -> str | None:
    """Fetch PubMed metadata then try Crossref (via DOI) before falling back
    to a locally-built @article entry. Bibtex is cached after first render.
    """
    cache_key = f"pmid:{pmid}"
    cached_bib = get_cached_bibtex(cache_key)
    if cached_bib:
        return cached_bib

    paper = get_paper_info_pubmed(pmid)
    if not paper:
        print(f"Paper not found: PMID:{pmid}", file=sys.stderr)
        return None

    bibtex = None
    if paper.doi:
        bibtex = fetch_bibtex_crossref(paper.doi)
    if not bibtex:
        bibtex = generate_bibtex_pubmed(paper, pmid)

    cache_paper_with_crossrefs(cache_key, paper, bibtex)
    return bibtex


def _bib_for_doi(doi: str) -> str | None:
    """Crossref content negotiation for DOIs. Cached on first success."""
    cache_key = f"doi:{doi.lower()}"
    cached_bib = get_cached_bibtex(cache_key)
    if cached_bib:
        return cached_bib

    bibtex = fetch_bibtex_crossref(doi)
    if not bibtex:
        return None

    paper = get_paper_info_doi(doi)
    if paper is None:
        # Cache key didn't get populated by get_paper_info_doi; fabricate a
        # minimal CachedPaper so the bibtex still survives the round trip.
        paper = CachedPaper(title="", authors=[], doi=doi, source="crossref")
    cache_paper_with_crossrefs(cache_key, paper, bibtex)
    return bibtex


def cmd_bib(args):
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"

    if id_type == "pmid":
        bibtex = _bib_for_pmid(clean_id)
        if not bibtex:
            sys.exit(1)
        _write_bibtex(bibtex, args.output)
        return

    if id_type == "doi":
        bibtex = _bib_for_doi(clean_id)
        if not bibtex:
            print(f"Could not fetch BibTeX for DOI:{clean_id}", file=sys.stderr)
            sys.exit(1)
        _write_bibtex(bibtex, args.output)
        return

    if id_type != "arxiv":
        print(
            f"Unrecognised identifier '{args.arxiv_id}' — supported: arXiv ID, "
            f"PMID, PMC ID, DOI.",
            file=sys.stderr,
        )
        sys.exit(1)

    paper = get_paper_info(clean_id)
    if not paper:
        sys.exit(1)

    bibtex = get_cached_bibtex(clean_id)
    if not bibtex:
        bibtex = generate_bibtex(paper, clean_id)

    _write_bibtex(bibtex, args.output)


def cmd_cited(args):
    paper_spec, display_id, _id_type, _clean_id = _resolve_xref_id(args.arxiv_id)
    source = args.source
    if source == "openalex" and not OPENALEX_ENABLED:
        print(
            "OpenAlex 源已被禁用 (上游 metadata 污染). "
            "请改用 --source s2, 或省略 --source 走自动选择.",
            file=sys.stderr,
        )
        sys.exit(2)

    offset = args.offset
    results = None
    used_source = ""

    if source in ("s2", "auto"):
        print(f"Querying Semantic Scholar: {paper_spec}")
        ret = _fetch_citations_s2_spec(paper_spec, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "Semantic Scholar"

    if results is None and source in ("openalex", "auto") and OPENALEX_ENABLED:
        if source == "auto":
            print("\nSemantic Scholar failed, switching to OpenAlex...")
        else:
            print(f"Querying OpenAlex: {paper_spec}")
        ret = _fetch_citations_openalex_spec(paper_spec, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "OpenAlex"

    if not results:
        print(f"\nNo citations found for {display_id}")
        return

    start_num = offset + 1
    end_num = offset + len(results)
    _set_source(used_source.split()[0].lower())
    print(f"\nSource: {used_source}")
    print(f"Showing citations #{start_num}-{end_num}:\n")

    if used_source == "Semantic Scholar":
        _print_citations_s2(results, start_num)
    else:
        _print_citations_openalex(results, start_num)


def cmd_annotations(args):
    """Text-mined entity annotations from Europe PMC.

    Shows each recognised gene / disease / chemical / organism / GO term
    / experimental method / accession number with its canonical ontology
    URI. PMC full-text papers have the richest annotations; PubMed-only
    records fall back to title+abstract mining.
    """
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    pmid = pmcid = None
    if id_type == "pmcid":
        pmcid = clean_id.upper()
    elif id_type == "pmid":
        pmid = clean_id
        # Try to resolve PMC too so we get full-text annotations when available.
        paper = get_paper_info_pubmed(clean_id)
        if paper and paper.pmcid:
            pmcid = paper.pmcid.upper()
    elif id_type == "doi":
        paper = get_paper_info_doi(clean_id)
        if paper is None:
            print(f"Paper not found for DOI:{clean_id}", file=sys.stderr)
            sys.exit(1)
        pmid = paper.pmid
        pmcid = paper.pmcid
        if not pmid and not pmcid:
            print(
                f"No PubMed/PMC mapping for DOI:{clean_id} — Europe PMC's "
                f"annotation API is PubMed/PMC-keyed only.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print(
            f"Unrecognised identifier '{args.arxiv_id}' — annotations accepts "
            f"PMID, PMC ID, or a DOI that Europe PMC can resolve to one.",
            file=sys.stderr,
        )
        sys.exit(1)

    types: list[str] | None = None
    if args.type and args.type != "all":
        types = [t.strip() for t in args.type.split(",") if t.strip()]

    annos = fetch_annotations(pmid=pmid, pmcid=pmcid, types=types)
    if annos is None:
        print("Annotations fetch failed (network or API error).", file=sys.stderr)
        sys.exit(1)
    if not annos:
        display = f"PMC:{pmcid}" if pmcid else f"PMID:{pmid}"
        print(f"No text-mined annotations for {display}.")
        return

    grouped = group_annotations_by_type(annos)
    display = f"PMC:{pmcid}" if pmcid else f"PMID:{pmid}"
    print(f"\n{len(annos)} annotations for {display}:\n")
    for type_name in sorted(grouped):
        bucket = grouped[type_name]
        # Deduplicate by surface string + URI so the same gene mentioned
        # 10 times shows up once with a count.
        seen: dict[tuple[str, str], int] = {}
        for a in bucket:
            surface = (a.get("exact") or "").strip()
            uri = ""
            for tag in a.get("tags") or []:
                if tag.get("uri"):
                    uri = tag["uri"]
                    break
            key = (surface.lower(), uri)
            seen[key] = seen.get(key, 0) + 1
        print(f"=== {type_name} ({len(bucket)} mentions, {len(seen)} unique) ===")
        ordered = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0][0]))
        for (surface, uri), count in ordered[: args.max_per_type]:
            tail = f"  [{count}×]" if count > 1 else ""
            extra = f"  {uri}" if uri else ""
            # find a representative tag name when the exact surface is cryptic
            print(f"  • {surface}{tail}{extra}")
        if len(ordered) > args.max_per_type:
            print(f"  … and {len(ordered) - args.max_per_type} more unique entities")
        print()


def _references_via_pubmed(pmid: str, max_results: int, offset: int) -> tuple[list[dict], int] | None:
    """ELink pubmed_pubmed_refs → ESummary batch. Fallback when S2 has no data."""
    ref_pmids = fetch_pubmed_references(pmid)
    if ref_pmids is None:
        return None
    total = len(ref_pmids)
    if not ref_pmids:
        return [], 0
    page = ref_pmids[offset : offset + max_results]
    records = fetch_esummary_batch(page)
    if records is None:
        return None
    return records, total


def _print_references_pubmed(records: list[dict], start: int = 1) -> None:
    """Print ESummary records using the same shape as _print_citations_*."""
    for i, r in enumerate(records, start):
        pmid = r.get("uid") or ""
        authors = [a["name"] for a in (r.get("authors") or []) if a.get("authtype") == "Author"]
        if not authors:
            authors = [a["name"] for a in (r.get("authors") or [])]
        year = (r.get("pubdate") or r.get("epubdate") or "").split(" ")[0].split("-")[0] or "?"
        tail = f"  PMID:{pmid}" if pmid else ""
        print(f"[{i}] {r.get('title') or '(no title)'}")
        print(f"    Authors: {_truncate_authors(authors)}")
        print(f"    Year: {year}{tail}")
        print()


def cmd_references(args):
    """Forward citations — the papers this paper cites.

    Tries S2 first (covers every ID type, returns rich metadata in one shot).
    Falls back to PubMed ELink for PMIDs when S2 has no reference data.
    """
    paper_spec, display_id, id_type, clean_id = _resolve_xref_id(args.arxiv_id)
    offset = args.offset
    print(f"Querying Semantic Scholar: {paper_spec}")
    ret = _fetch_references_s2_spec(paper_spec, args.max, offset)
    if ret is not None and ret[0]:
        refs, total = ret
        start = offset + 1
        end = offset + len(refs)
        print(f"\nSource: Semantic Scholar")
        print(f"Showing references #{start}-{end} of {total}:\n")
        _print_citations_s2(refs, start)
        return

    if id_type == "pmid":
        print("\nS2 returned no references; falling back to PubMed ELink...", file=sys.stderr)
        pm_ret = _references_via_pubmed(clean_id, args.max, offset)
        if pm_ret is not None and pm_ret[0]:
            refs, total = pm_ret
            start = offset + 1
            end = offset + len(refs)
            print(f"\nSource: PubMed ELink + ESummary")
            print(f"Showing references #{start}-{end} of {total}:\n")
            _print_references_pubmed(refs, start)
            return

    print(f"\nNo references found for {display_id}")


def cmd_similar(args):
    """Similar-articles list for a PubMed paper.

    Wraps NCBI's ``elink.fcgi?linkname=pubmed_pubmed`` — a co-citation /
    MeSH-overlap relatedness ranking. PMID-only: arXiv / DOI / PMC IDs are
    rejected (NCBI's similarity model is PubMed-internal).
    """
    id_type, clean_id = extract_paper_id(args.arxiv_id)
    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"
    if id_type != "pmid":
        print(
            f"`similar` only supports PMID — got {id_type or 'unknown'} "
            f"({args.arxiv_id}). Look up the PubMed PMID first.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Querying PubMed ELink (similar): PMID:{clean_id}")
    sim_pmids = fetch_similar_pmids(clean_id, max_results=args.max + args.offset)
    if not sim_pmids:
        print(f"No similar articles found for PMID:{clean_id}")
        return

    page = sim_pmids[args.offset : args.offset + args.max]
    records = fetch_esummary_batch(page)
    if not records:
        print("ESummary returned no records.", file=sys.stderr)
        return

    start = args.offset + 1
    end = args.offset + len(records)
    print(f"\nSource: PubMed ELink (pubmed_pubmed) + ESummary")
    print(f"Showing similar articles #{start}-{end} of {len(sim_pmids)}:\n")
    _print_references_pubmed(records, start)


def cmd_tex(args):
    was_cached = _find_cached_tex_dir(args.arxiv_id) is not None
    result = fetch_tex_source(args.arxiv_id, OUTPUT_DIR)
    if result:
        _set_source("cache" if was_cached else "arxiv")
        print("\nDirectory structure:")
        print(result.name)
        tree_lines = print_tree(result)
        for line in tree_lines:
            print(line)
    else:
        _set_source("pdf_fallback")
        print("\ntex download failed, falling back to PDF...", file=sys.stderr)
        _fetch_pdf_fallback(args.arxiv_id, OUTPUT_DIR)


def _find_cached_tex_dir(arxiv_id: str) -> Path | None:
    """Return an existing extracted tex directory without any network request."""
    clean_id = re.sub(r"v\d+$", "", extract_arxiv_id(arxiv_id))
    dir_id = basename_for_id("arxiv", clean_id)
    exact = OUTPUT_DIR / dir_id
    if exact.is_dir():
        return exact
    for path in OUTPUT_DIR.glob(f"{dir_id}_*"):
        if path.is_dir():
            return path
    return None


def cmd_infotex(args):
    """Print metadata, then fetch/show the LaTeX source tree."""
    cmd_info(args)
    print()
    cmd_tex(args)


# Re-exported for back-compat with code that imports `arxiv_tool._extract_pdf_text`.
_extract_pdf_text = _pdf_extract_text


def _already_saved(basename: str, *exts: str) -> bool:
    """If any ``{basename}.{ext}`` already exists under OUTPUT_DIR, print and return True."""
    for ext in exts:
        path = OUTPUT_DIR / f"{basename}.{ext}"
        if path.exists():
            print(f"Already exists: {path}")
            return True
    return False


def _fail_fulltext(message: str) -> None:
    """Print a terminal-failure message to stderr and exit non-zero."""
    print(f"\n{message}", file=sys.stderr)
    sys.exit(1)


def _try_pmc_to_disk(pmcid: str) -> bool:
    """PMC layered chain. Returns True on first success, False if all layers fail."""
    pmc_up = pmcid.upper()
    if _already_saved(pmc_up, "xml", "bioc.json", "txt"):
        return True
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _save_xml() -> bool:
        xml = fetch_pmc_fulltext_xml(pmc_up)
        if not xml:
            return False
        path = OUTPUT_DIR / f"{pmc_up}.xml"
        path.write_text(xml, encoding="utf-8")
        print(f"Saved JATS XML: {path} ({len(xml):,} bytes)")
        return True

    def _save_bioc() -> bool:
        bioc = fetch_pmc_bioc_json(pmc_up)
        if not bioc:
            return False
        path = OUTPUT_DIR / f"{pmc_up}.bioc.json"
        path.write_text(bioc, encoding="utf-8")
        print(f"Saved BioC JSON: {path} ({len(bioc):,} bytes)")
        return True

    layers = [
        Layer(f"[1/3] Trying Europe PMC JATS XML for {pmc_up}...", _save_xml),
        Layer(f"[2/3] JATS unavailable — trying NCBI BioC JSON...", _save_bioc),
        Layer(f"[3/3] Structured formats unavailable — trying PMC PDF...",
              lambda: fetch_pmc_pdf(pmc_up)),
    ]
    return walk_layers(
        layers, basename=pmc_up, output_dir=OUTPUT_DIR,
        source_url=f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_up}/",
    )


def _try_chemrxiv_to_disk(doi: str) -> bool:
    """ChemRxiv layered chain. Returns True on first success.

    Order: OA mirror → direct chemrxiv.org. When both fail the publisher
    landing page is typically Cloudflare-protected; the caller prints a
    handoff message so an agent with Playwright MCP can take over.
    """
    safe = basename_for_id("doi", doi)
    if _already_saved(safe, "pdf", "txt"):
        return True

    cached = get_cached_paper(f"doi:{doi.lower()}")
    openalex_pdf = (
        cached.pdf_url
        if cached and cached.pdf_url and "chemrxiv.org" not in cached.pdf_url
        else None
    )

    layers = [
        Layer(f"[1/2] Trying OA mirrors for {doi}...",
              lambda: _try_oa_mirror_for_pdf(doi=doi, openalex_pdf_url=openalex_pdf)),
        Layer("[2/2] Trying direct ChemRxiv PDF (likely Cloudflared)...",
              lambda: fetch_chemrxiv_pdf(doi)),
    ]
    return walk_layers(layers, basename=safe, output_dir=OUTPUT_DIR)


def _biorxiv_site_and_landing(doi: str) -> tuple[str, str]:
    """Guess the ``biorxiv`` vs ``medrxiv`` subdomain for a 10.1101 DOI.

    They share the DOI prefix but live on different subdomains; we infer
    from any cached OpenAlex ``pdf_url`` and default to bioRxiv. Returns
    ``(site, landing_pdf_url)``.
    """
    cached = get_cached_paper(f"doi:{doi.lower()}")
    is_medrxiv = "medrxiv" in ((cached.pdf_url if cached else "") or "").lower()
    site = "medrxiv" if is_medrxiv else "biorxiv"
    return site, f"https://www.{site}.org/content/{doi}.full.pdf"


def _try_biorxiv_to_disk(doi: str) -> bool:
    """bioRxiv / medRxiv layered chain. Returns True on first success.

    Order: Europe PMC PMC copy (some preprints have one) → OA mirror.
    First layer is special-cased: if a PMC copy exists, delegate to the
    PMC chain (JATS / BioC / PDF) entirely, since structured XML beats
    a scraped PDF. When both layers fail the caller prints a handoff so
    an agent with Playwright MCP can take over.
    """
    safe = basename_for_id("doi", doi)
    if _already_saved(safe, "pdf", "txt"):
        return True

    cached = get_cached_paper(f"doi:{doi.lower()}")
    openalex_pdf = (
        cached.pdf_url
        if cached and cached.pdf_url
        and "biorxiv" not in cached.pdf_url.lower()
        and "medrxiv" not in cached.pdf_url.lower()
        else None
    )

    def _try_europepmc_pmc() -> bool:
        epmc = _fetch_paper_europepmc_by_doi(doi)
        if not (epmc and epmc.pmcid):
            return False
        print(
            f"  Europe PMC has {epmc.pmcid} for this preprint; using PMC chain.",
            file=sys.stderr,
        )
        return _try_pmc_to_disk(epmc.pmcid)

    layers = [
        Layer(f"[1/2] Checking Europe PMC for a PMC copy of {doi}...",
              _try_europepmc_pmc),
        Layer(f"[2/2] Trying OA mirrors for {doi}...",
              lambda: _try_oa_mirror_for_pdf(doi=doi, openalex_pdf_url=openalex_pdf)),
    ]
    return walk_layers(layers, basename=safe, output_dir=OUTPUT_DIR)


def _ingest_local_pdf(path_str: str, out_basename: str) -> None:
    """Thin wrapper around :func:`lit.pdf.ingest_local_pdf` using OUTPUT_DIR."""
    _pdf_ingest(path_str, out_basename, OUTPUT_DIR)


def _try_arxiv_to_disk(arxiv_id: str) -> bool:
    """arXiv layered chain: LaTeX source → PDF fallback. Returns True on success.

    ``_fetch_pdf_fallback`` returns silently when the response body is too
    small to be a real PDF, so we verify a file actually exists before
    declaring success.
    """
    result = fetch_tex_source(arxiv_id, OUTPUT_DIR)
    if result:
        # cmd_tex's success path also prints a directory tree; preserve that.
        print("\nDirectory structure:")
        print(result.name)
        for line in print_tree(result):
            print(line)
        return True
    print("\ntex download failed, falling back to PDF...", file=sys.stderr)
    try:
        _fetch_pdf_fallback(arxiv_id, OUTPUT_DIR)
    except Exception as e:  # noqa: BLE001 — fall through to next preprint version
        print(f"PDF fallback failed: {e}", file=sys.stderr)
        return False
    file_id = arxiv_id.replace("/", "_")
    return any((OUTPUT_DIR / f"{file_id}.{ext}").exists() for ext in ("txt", "pdf"))


def _try_preprint_versions(
    versions: list[PreprintVersion], fallback_basename: str
) -> bool:
    """Walk preprint versions in priority order; return True at first success.

    Each version dispatches to its source's ``_try_*_to_disk`` (returning
    bool) so a failed version cleanly advances to the next without needing
    exception-based control flow.
    """
    for v in versions:
        label = f" ({v.version_label})" if v.version_label else ""
        print(
            f"  → trying {v.source} preprint version: {v.id}{label}",
            file=sys.stderr,
        )
        if v.source == "arxiv":
            if _try_arxiv_to_disk(v.id):
                return True
        elif v.source in ("biorxiv", "medrxiv"):
            if _try_biorxiv_to_disk(v.id):
                return True
        elif v.source == "chemrxiv":
            if _try_chemrxiv_to_disk(v.id):
                return True
        elif v.pdf_url:
            # Other preprint hosts: just try the OpenAlex-supplied PDF URL.
            pdf = try_download_pdf(v.pdf_url)
            if pdf:
                _save_pdf_and_text(pdf, fallback_basename)
                return True
    return False


def _try_preprint_layer(doi: str, basename: str) -> bool:
    """Layer wrapper: look up + walk preprint versions for ``doi``."""
    versions = find_preprint_versions(doi=doi)
    if not versions:
        print("  No preprint versions known to OpenAlex.", file=sys.stderr)
        return False
    print(
        f"  Found {len(versions)} preprint version(s): "
        f"{', '.join(v.source for v in versions)}",
        file=sys.stderr,
    )
    return _try_preprint_versions(versions, fallback_basename=basename)


def _try_europepmc_pmc_for_doi(doi: str) -> bool:
    """If this DOI has a PMC copy (per Europe PMC), walk the PMC chain.

    Many OA-published Wiley/Nature/Cell/JACS articles sit in PMC with full
    JATS XML; using that avoids the publisher paywall entirely.

    Skips the PMC chain when Europe PMC reports ``isOpenAccess=N``: in that
    case the PMCID is just an abstract-index entry and every JATS/BioC
    fetch 404s, so we let the caller fall through to OA mirrors instead.
    """
    pmcid, is_oa = pmc_full_text_locator(doi)
    if not pmcid:
        return False
    if not is_oa:
        print(
            f"  Europe PMC has {pmcid} for this DOI but isOpenAccess=N; "
            f"skipping PMC chain (full text not available).",
            file=sys.stderr,
        )
        return False
    print(
        f"  Europe PMC has {pmcid} for this DOI; using PMC chain.",
        file=sys.stderr,
    )
    return _try_pmc_to_disk(pmcid)


def _try_generic_doi_to_disk(doi: str) -> bool:
    """Generic-DOI layered chain for paywalled journal articles.

    Order: PMC cross-link (many OA journal articles have a PMC copy with
    clean JATS XML) → preprint reverse lookup (Nature/Cell/Science papers
    often have an arXiv/bioRxiv twin) → OA mirror (Unpaywall/OpenAlex/
    CORE/Crossref). Manual ``--from-file`` is the explicit escape hatch
    when every layer fails; a calling agent with Playwright MCP can use
    the printed landing URL to fetch paywalled content and re-ingest.
    """
    safe = basename_for_id("doi", doi)
    if _already_saved(safe, "pdf", "txt"):
        return True

    layers = [
        Layer(f"[1/3] Checking Europe PMC for a PMC copy of {doi}...",
              lambda: _try_europepmc_pmc_for_doi(doi)),
        Layer(f"[2/3] Looking for preprint versions of {doi}...",
              lambda: _try_preprint_layer(doi, safe)),
        Layer(f"[3/3] Trying OA mirrors for {doi}...",
              lambda: _try_oa_mirror_for_pdf(doi=doi)),
    ]
    return walk_layers(layers, basename=safe, output_dir=OUTPUT_DIR)


def _try_pmid_to_disk(pmid: str) -> bool:
    """PMID fallback chain: PMC copy → preprint reverse lookup → OA mirror."""
    paper = get_paper_info_pubmed(pmid)
    # Trust EFetch's ArticleIdList when it returned a record; only ELink-fall
    # back when EFetch itself failed (paper is None).
    pmcid = paper.pmcid if paper else pmid_to_pmcid(pmid)
    if pmcid and _try_pmc_to_disk(pmcid):
        return True
    doi = paper.doi if paper else None
    if not doi:
        return False
    basename = basename_for_id("pmid", pmid)
    print(
        f"PMID:{pmid} has no PMC copy — walking DOI fallback chain...",
        file=sys.stderr,
    )
    layers = [
        Layer(f"[1/2] Looking for preprint versions of {doi}...",
              lambda: _try_preprint_layer(doi, basename)),
        Layer(f"[2/2] Trying OA mirrors for {doi}...",
              lambda: _try_oa_mirror_for_pdf(doi=doi)),
    ]
    return walk_layers(layers, basename=basename, output_dir=OUTPUT_DIR)


def _try_doi_to_disk(doi: str) -> bool:
    """Route a DOI to chemrxiv / biorxiv / generic chain."""
    if is_chemrxiv_doi(doi):
        return _try_chemrxiv_to_disk(doi)
    if _is_biorxiv_doi(doi):
        return _try_biorxiv_to_disk(doi)
    return _try_generic_doi_to_disk(doi)


def _doi_landing_url(doi: str) -> str:
    """Return the user-facing article page URL for a DOI, suitable for an
    agent with Playwright MCP to navigate (not the direct PDF asset URL,
    which is typically Cloudflare-blocked without a session cookie from the
    article page first).
    """
    if is_chemrxiv_doi(doi):
        return f"https://chemrxiv.org/doi/full/{doi}"
    if _is_biorxiv_doi(doi):
        site, _ = _biorxiv_site_and_landing(doi)
        return f"https://www.{site}.org/content/{doi}"
    return f"https://doi.org/{doi}"


def _agent_handoff_hint(clean_id: str, landing: str) -> str:
    """Structured failure message inviting an agent with Playwright MCP to retake.

    Parseable by a calling Claude: `Landing URL:` and `--from-file` are the
    two affordances the agent needs to fetch the PDF with Playwright MCP
    and re-ingest it via this tool.
    """
    return (
        f"All automatic full-text paths failed for {clean_id}.\n"
        f"Landing URL: {landing}\n"
        f"If you are an agent with Playwright MCP available, fetch the PDF "
        f"from the landing URL above (may require clicking through a "
        f"Cloudflare challenge or publisher paywall), save it locally, then "
        f"re-run `fulltext {clean_id} --from-file <path>` to ingest it."
    )


# id_type → (try_fn returning bool, failure-hint builder).
# try_fn is shared between cmd_fulltext (interactive) and fulltext-batch; the
# hint is only consulted by cmd_fulltext on failure.
_FULLTEXT_DISPATCH = {
    "arxiv": (
        _try_arxiv_to_disk,
        lambda cid: (
            f"Could not fetch full text for arXiv:{cid} — tex source and "
            f"PDF fallback both failed."
        ),
    ),
    "pmcid": (
        _try_pmc_to_disk,
        lambda cid: (
            f"No open-access full text available for {cid.upper()} "
            f"(JATS, BioC, and PDF all failed). "
            f"Paper may be closed-access or withdrawn."
        ),
    ),
    "pmid": (
        _try_pmid_to_disk,
        lambda cid: (
            f"No full-text found for PMID:{cid} "
            f"(no PMC copy, no preprint, no OA mirror)."
        ),
    ),
    "doi": (
        _try_doi_to_disk,
        lambda cid: _agent_handoff_hint(cid, _doi_landing_url(cid)),
    ),
}


def _try_fulltext_for_id(id_type: str, clean_id: str) -> bool:
    """Try-dispatch used by ``fulltext-batch``; never exits."""
    spec = _FULLTEXT_DISPATCH.get(id_type)
    return spec[0](clean_id) if spec else False


def _fulltext_manifest_paths() -> tuple[Path, Path, Path]:
    """Default ``(manifest, download_me, manual_pdf_dir)`` triple for the
    work directory, used by every cmd_fulltext* path so all three stay in
    sync. MANUAL_PDF_DIR is created on demand because download_me.txt
    references it as the upload target."""
    return (
        WORK_DIR / "fulltext_failed.tsv",
        WORK_DIR / "download_me.txt",
        MANUAL_PDF_DIR,
    )


def cmd_fulltext(args):
    """Dispatch full-text fetch by ID type via ``_FULLTEXT_DISPATCH``.

    ``--from-file PATH`` bypasses all network paths: point it at a manually
    downloaded PDF and we extract text + save to cache with the ID's
    canonical basename. Agents with Playwright MCP should use this after
    fetching content the automatic chain couldn't reach.

    Failures and successes both touch the shared download_me manifest so a
    one-off ``fulltext`` from inside an agent loop contributes to the same
    queue ``fulltext-batch`` builds, and a successful retry quietly clears
    a stale entry.
    """
    id_type, clean_id = extract_paper_id(args.arxiv_id)
    manifest_path, download_me_path, manual_pdf_dir = _fulltext_manifest_paths()

    if args.from_file:
        _ingest_local_pdf(args.from_file, basename_for_id(id_type, clean_id))
        record_single_success(
            args.arxiv_id,
            manifest_path=manifest_path,
            download_me_path=download_me_path,
            manual_pdf_dir=manual_pdf_dir,
        )
        return

    spec = _FULLTEXT_DISPATCH.get(id_type)
    if spec is None:
        print(
            f"Unrecognised or unsupported identifier '{args.arxiv_id}' — "
            f"`fulltext` supports arXiv ID, PMID, PMC ID, DOI (any), "
            f"bioRxiv/medRxiv DOI.",
            file=sys.stderr,
        )
        sys.exit(1)

    try_fn, fail_hint = spec
    if try_fn(clean_id):
        record_single_success(
            args.arxiv_id,
            manifest_path=manifest_path,
            download_me_path=download_me_path,
            manual_pdf_dir=manual_pdf_dir,
        )
        return
    MANUAL_PDF_DIR.mkdir(parents=True, exist_ok=True)
    record_single_failure(
        args.arxiv_id,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )
    _fail_fulltext(fail_hint(clean_id))


def cmd_fulltext_batch(args):
    """Walk a file of paper IDs through the fulltext chain; manifest the failures."""
    ids_path = Path(args.ids_file).expanduser().resolve()
    if not ids_path.exists():
        print(f"IDs file not found: {ids_path}", file=sys.stderr)
        sys.exit(1)
    default_manifest, download_me_path, manual_pdf_dir = _fulltext_manifest_paths()
    manifest_path = (
        Path(args.manifest).expanduser().resolve() if args.manifest else default_manifest
    )
    manual_pdf_dir.mkdir(parents=True, exist_ok=True)
    run_batch(
        ids_path,
        try_fetch=_try_fulltext_for_id,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )


def cmd_fulltext_sweep(args):
    """Scan markdown / text files for paper IDs and batch-fetch each through fulltext.

    End-of-task audit: after writing a doc that cites N papers (idea, review, landscape,
    proposal), run `fulltext-sweep <doc> [<doc>...]` to ensure every cited paper has
    fulltext on disk. Already-cached IDs auto-skip, only new ones hit the network.
    Failures land in the usual `fulltext_failed.tsv` + `download_me.txt` for manual
    handoff, identical to `fulltext-batch`.
    """
    import re
    import tempfile

    files = [Path(p).expanduser().resolve() for p in args.files]
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print(f"File not found: {missing[0]}", file=sys.stderr)
        sys.exit(1)

    arxiv_pat = re.compile(r"(?:arXiv[:\s]*|arxiv[:\s]*)(\d{4}\.\d{4,5})", re.I)
    doi_pat = re.compile(r"\b(10\.\d{4,9}/[A-Za-z0-9._/\-]+[A-Za-z0-9])")
    pmc_pat = re.compile(r"\bPMC\d{5,}\b")

    ids: set[str] = set()
    for f in files:
        text = f.read_text(errors="replace")
        ids |= set(arxiv_pat.findall(text))
        for m in doi_pat.findall(text):
            m = m.rstrip(".,;:)]")
            if "/" in m:
                ids.add(m)
        ids |= set(pmc_pat.findall(text))

    if not ids:
        print(f"fulltext-sweep: no paper IDs in {len(files)} file(s).")
        return

    print(f"fulltext-sweep: {len(ids)} unique IDs across {len(files)} file(s).")
    with tempfile.NamedTemporaryFile("w", suffix="_ids.txt", delete=False) as fh:
        fh.write("\n".join(sorted(ids)) + "\n")
        ids_path = Path(fh.name)
    try:
        default_manifest, download_me_path, manual_pdf_dir = _fulltext_manifest_paths()
        manifest_path = (
            Path(args.manifest).expanduser().resolve() if args.manifest else default_manifest
        )
        manual_pdf_dir.mkdir(parents=True, exist_ok=True)
        run_batch(
            ids_path,
            try_fetch=_try_fulltext_for_id,
            manifest_path=manifest_path,
            download_me_path=download_me_path,
            manual_pdf_dir=manual_pdf_dir,
        )
    finally:
        ids_path.unlink(missing_ok=True)


def cmd_fulltext_import(args):
    """Scan a directory for manually-downloaded PDFs and ingest each."""
    default_manifest, download_me_path, manual_pdf_dir = _fulltext_manifest_paths()
    pdf_dir = (
        Path(args.pdf_dir).expanduser().resolve() if args.pdf_dir else manual_pdf_dir
    )
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else (default_manifest if default_manifest.exists() else None)
    )
    run_import(
        pdf_dir,
        OUTPUT_DIR,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )


def main():
    parser = argparse.ArgumentParser(
        description="arXiv 论文搜索与全文获取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
    %(prog)s search "PINN" --max 5
    %(prog)s info 2401.12345
    %(prog)s bib 2505.08783
    %(prog)s bib 2505.08783 -o references.bib
    %(prog)s tex 2505.08783
    %(prog)s infotex 2505.08783
    %(prog)s cited 1711.10561
    %(prog)s cited 1711.10561 --max 50
    %(prog)s cited 1711.10561 --offset 20          # 第 21-40 条
    %(prog)s cited 1711.10561 --source openalex
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="搜索论文 (默认 all: 多源并行+去重)")
    search_parser.add_argument("query", help="搜索关键词")
    search_parser.add_argument("--max", type=int, default=20, help="最大结果数 (默认 20)")
    search_parser.add_argument(
        "--source",
        choices=["all", "auto", "s2", "openalex", "arxiv", "pubmed", "chemrxiv", "europepmc"],
        default="all",
        help=(
            "数据源 (默认 all: OpenAlex+S2+PubMed+EuropePMC+ChemRxiv+arXiv 并行搜索, "
            "DOI/PMID/arXiv-ID 去重, 字段合并). 单源: s2, openalex, arxiv, pubmed, "
            "chemrxiv, europepmc. auto=旧的 S2→OpenAlex→arXiv 串行 fallback."
        ),
    )
    search_parser.add_argument(
        "--domain",
        choices=sorted(AGG_DOMAIN_PRESETS.keys()),
        help="领域快捷方式 (仅与 --source all 配合): bio/med/chem/cs/phys, 限定相关源 + S2 fields_of_study",
    )
    search_parser.add_argument(
        "--snippet", action="store_true",
        help="改用 S2 /snippet/search (按全文片段命中排序), 适合查技术术语 (仅 --source all)",
    )
    search_parser.add_argument("--year", help="年份或范围 (如 2024, 2020-2024, 2020-)")
    search_parser.add_argument("--fields-of-study", help="研究领域，逗号分隔 (如 Computer Science,Physics)")
    search_parser.add_argument("--pub-types", help="发表类型，逗号分隔 (如 JournalArticle,Conference)")
    search_parser.add_argument("--min-citations", type=int, help="最低引用数")
    search_parser.add_argument("--venue", help="会议/期刊名称")
    search_parser.add_argument("--open-access", action="store_true", help="仅显示开放获取论文")
    search_parser.add_argument("--bulk", action="store_true", help="使用 S2 bulk 搜索（最多 1000 条）")
    search_parser.add_argument("--sort", help="排序字段 (如 citationCount:desc, publicationDate:desc)")
    search_parser.add_argument("--token", help="bulk 搜索翻页 token")
    # Common pagination offset (consumed by PubMed; S2/OpenAlex use their own).
    search_parser.add_argument(
        "--offset", type=int, default=0,
        help="跳过前 N 条结果（PubMed 分页；S2/OpenAlex 用各自的机制）",
    )
    search_parser.set_defaults(func=cmd_search)

    info_parser = subparsers.add_parser("info", help="获取论文信息（不下载全文）")
    info_parser.add_argument("arxiv_id", help="arXiv ID 或 PubMed PMID")
    info_parser.set_defaults(func=cmd_info)

    bib_parser = subparsers.add_parser("bib", help="生成 BibTeX 引用")
    bib_parser.add_argument("arxiv_id", help="arXiv ID 或 PubMed PMID")
    bib_parser.add_argument("--output", "-o", help="输出文件路径（追加写入）")
    bib_parser.set_defaults(func=cmd_bib)

    cited_parser = subparsers.add_parser("cited", help="被引反查：查看哪些论文引用了它")
    cited_parser.add_argument("arxiv_id", help="arXiv ID")
    cited_parser.add_argument("--max", type=int, default=20, help="最大显示条数 (默认 20)")
    cited_parser.add_argument(
        "--offset", type=int, default=0, help="跳过前 N 条结果，用于翻页 (默认 0)"
    )
    cited_parser.add_argument(
        "--source",
        choices=["auto", "s2", "openalex"],
        default="auto",
        help="数据源: auto=自动(S2优先), s2=Semantic Scholar, openalex=OpenAlex (默认 auto)",
    )
    cited_parser.set_defaults(func=cmd_cited)

    annotations_parser = subparsers.add_parser(
        "annotations",
        help="Europe PMC 文本挖掘实体 (基因/疾病/化学物质/生物/GO/方法/数据集 ID)",
    )
    annotations_parser.add_argument(
        "arxiv_id", help="PMID / PMC ID / DOI (DOI 需能映射到 PubMed/PMC)",
    )
    annotations_parser.add_argument(
        "--type", default="all",
        help=(
            "实体类型, 逗号分隔; 可选: "
            f"{', '.join(sorted(ANNOTATION_TYPE_MAP.keys()))}. 默认 all"
        ),
    )
    annotations_parser.add_argument(
        "--max-per-type", type=int, default=30,
        help="每个类型最多显示多少条不重复实体 (默认 30)",
    )
    annotations_parser.set_defaults(func=cmd_annotations)

    similar_parser = subparsers.add_parser(
        "similar",
        help="相似论文 (NCBI ELink pubmed_pubmed, 仅支持 PMID)",
    )
    similar_parser.add_argument("arxiv_id", help="PubMed PMID (或 PMC ID, 会先转 PMID)")
    similar_parser.add_argument("--max", type=int, default=20, help="最大显示条数 (默认 20)")
    similar_parser.add_argument(
        "--offset", type=int, default=0,
        help="跳过前 N 条 (默认 0). 注意 NCBI 按相关度排序, 越大越次要",
    )
    similar_parser.set_defaults(func=cmd_similar)

    references_parser = subparsers.add_parser(
        "references", help="正向引用: 这篇论文引用了哪些 (S2 优先, PMID 时 PubMed ELink 兜底)"
    )
    references_parser.add_argument("arxiv_id", help="arXiv ID / PMID / PMC ID / DOI")
    references_parser.add_argument("--max", type=int, default=20, help="最大显示条数 (默认 20)")
    references_parser.add_argument("--offset", type=int, default=0, help="跳过前 N 条，用于翻页 (默认 0)")
    references_parser.set_defaults(func=cmd_references)

    tex_parser = subparsers.add_parser("tex", help="下载 LaTeX 源文件并解压 (arXiv 专用)")
    tex_parser.add_argument("arxiv_id", help="arXiv ID")
    tex_parser.set_defaults(func=cmd_tex)

    infotex_parser = subparsers.add_parser(
        "infotex", help="info + tex 组合：先打印论文信息，再下载 LaTeX"
    )
    infotex_parser.add_argument("arxiv_id", help="arXiv ID")
    infotex_parser.set_defaults(func=cmd_infotex)

    fulltext_parser = subparsers.add_parser(
        "fulltext", help="下载全文: 分层 fallback (JATS→BioC→PDF; OA mirror; Playwright)"
    )
    fulltext_parser.add_argument(
        "arxiv_id", help="arXiv ID / PMID / PMC ID / ChemRxiv DOI / bioRxiv DOI",
    )
    fulltext_parser.add_argument(
        "--from-file", metavar="PATH",
        help="手动兜底: 已经通过浏览器下载好的 PDF 路径, 跳过全部在线下载直接提文本入库",
    )
    fulltext_parser.set_defaults(func=cmd_fulltext)

    batch_parser = subparsers.add_parser(
        "fulltext-batch",
        help="批量下载全文: 读 ID 列表逐个跑, 失败的写到 manifest TSV 等待手动下载",
    )
    batch_parser.add_argument(
        "ids_file",
        help="每行一个 ID 的文本文件 (# 注释 / 空行忽略). 支持 arXiv / PMID / PMC ID / DOI",
    )
    batch_parser.add_argument(
        "--manifest", metavar="PATH",
        help="失败 ID 的输出 TSV 路径 (默认 OUTPUT_DIR/fulltext_failed.tsv)",
    )
    batch_parser.set_defaults(func=cmd_fulltext_batch)

    sweep_parser = subparsers.add_parser(
        "fulltext-sweep",
        help="扫 markdown/text 文件抽出引用的 paper ID, 批量下全文 (end-of-task 审计)",
    )
    sweep_parser.add_argument(
        "files", nargs="+",
        help="一个或多个 markdown / 纯文本文件, 混抽 arxiv / DOI / PMC ID, 去重后批量 fulltext",
    )
    sweep_parser.add_argument(
        "--manifest", metavar="PATH",
        help="失败 ID 的输出 TSV 路径 (默认 OUTPUT_DIR/fulltext_failed.tsv)",
    )
    sweep_parser.set_defaults(func=cmd_fulltext_sweep)

    import_parser = subparsers.add_parser(
        "fulltext-import",
        help="批量导入手动下载的 PDF: 扫目录, 按 manifest / 文件名匹配 ID, 入缓存",
    )
    import_parser.add_argument(
        "pdf_dir", nargs="?", default=None,
        help=(
            "包含 *.pdf 的目录. 省略时默认用 $ARXIV_WORK_DIR/manual-pdfs (即 "
            "fulltext-batch 指引你上传的那个目录). 文件名不用改, 工具先从 PDF "
            "内容里认 DOI, 认不出才退回去看文件名"
        ),
    )
    import_parser.add_argument(
        "--manifest", metavar="PATH",
        help=(
            "fulltext-batch 产出的 TSV; 省略时自动用 $ARXIV_WORK_DIR/fulltext_failed.tsv "
            "(如果存在). manifest 只用于 basename 兜底匹配, 主匹配靠 PDF 内容"
        ),
    )
    import_parser.set_defaults(func=cmd_fulltext_import)

    args = parser.parse_args()
    started = time.time()
    exit_code = 0
    global _LAST_SOURCE
    _LAST_SOURCE = "unknown"
    arg = (
        getattr(args, "arxiv_id", None)
        or getattr(args, "query", None)
        or getattr(args, "ids_file", None)
        or getattr(args, "pdf_dir", None)
        or ""
    )
    if not arg and getattr(args, "files", None):
        arg = ",".join(str(p) for p in args.files)
    flags = {
        k: v for k, v in vars(args).items()
        if k not in ("command", "func", "arxiv_id", "query", "ids_file", "files", "pdf_dir")
        and v is not None and v is not False
    }

    try:
        args.func(args)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        exit_code = 130
    except arxiv.HTTPError as e:
        print(f"Error: arXiv HTTP {e.status}", file=sys.stderr)
        exit_code = 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        exit_code = 1
    finally:
        _write_audit_entry({
            "ts": _datetime.now().isoformat(timespec="seconds"),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
            "cmd": args.command,
            "arg": arg,
            "flags": flags,
            "source_hit": _LAST_SOURCE,
            "cached_before": _LAST_SOURCE == "cache",
            "elapsed_s": round(time.time() - started, 3),
            "exit_code": exit_code,
        })

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
