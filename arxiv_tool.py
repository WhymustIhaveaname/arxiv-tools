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
    cited  - reverse citation lookup (S2 → OpenAlex)

Usage (via uv run):
    uv run arxiv_tool.py search "PINN" --max 5
    uv run arxiv_tool.py info 2401.12345
    uv run arxiv_tool.py bib 2401.12345 -o refs.bib
    uv run arxiv_tool.py tex 2401.12345
    uv run arxiv_tool.py cited 1711.10561 --max 20
    uv run arxiv_tool.py cited 1711.10561 --offset 20
    uv run arxiv_tool.py cited 1711.10561 --source openalex

Implementation is split across the `lit/` package; this module only owns
CLI orchestration (cmd_*, get_paper_info, main). The re-exports below keep
existing tests that patch `arxiv_tool.X` working.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import arxiv
import requests

from lit.config import (
    CACHE_DIR,
    CONTACT_EMAIL,
    HTTP_HEADERS,
    OPENALEX_API_BASE,
    OPENALEX_API_KEY,
    S2_API_BASE,
    S2_API_KEY,
)
from lit.ids import (
    _arxiv_date,
    _arxiv_year,
    _truncate_authors,
    extract_arxiv_id,
    extract_paper_id,
    sanitize_filename,
)
from lit.bibtex import (
    STOPWORDS,
    generate_bibtex,
    generate_bibtex_pubmed,
    generate_citation_key,
)
from lit.browser import (
    browser_download_pdf,
    browser_download_via_click,
    is_playwright_available,
)
from lit.crossref import fetch_bibtex_crossref
from lit.enrich import enrich_paper_ids
from lit.oa_mirror import find_oa_pdf_urls, try_download_pdf
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
    """Write ``{basename}.pdf`` + ``{basename}.txt`` to OUTPUT_DIR.

    The .txt is PyMuPDF's text extraction — readable by the LLM directly.
    Skips text if extraction fails; the raw PDF remains usable.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUTPUT_DIR / f"{out_basename}.pdf"
    txt_path = OUTPUT_DIR / f"{out_basename}.txt"
    pdf_path.write_bytes(pdf_bytes)
    print(f"Saved PDF: {pdf_path} ({len(pdf_bytes):,} bytes)")

    text = _extract_pdf_text(pdf_bytes)
    if text:
        txt_path.write_text(
            f"# {out_basename}\n\n## Full Text\n\n{text}", encoding="utf-8",
        )
        print(f"Saved text: {txt_path} ({len(text):,} chars)")
    else:
        print("PDF text extraction failed; raw PDF is still usable.", file=sys.stderr)


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


def _print_doi_info(doi: str, paper) -> None:
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


def _print_search_results(results: list[dict]) -> None:
    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['id']}")
        print(f"    Title: {r['title']}")
        print(f"    Authors: {r['authors']}")
        cited = f"  Cited: {r['cited_by']}" if r["cited_by"] is not None else ""
        print(f"    Year: {r['year']}{cited}")
        if r["abstract"]:
            print(f"    Abstract: {r['abstract'].replace(chr(10), ' ')}")
        print()


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


def cmd_search(args):
    source = args.source
    filters = _s2_filters_from_args(args)

    results = None

    if source == "pubmed":
        print("Searching PubMed...", file=sys.stderr)
        raw = _search_pubmed(
            args.query,
            args.max,
            offset=getattr(args, "offset", 0) or 0,
            year=getattr(args, "year", None),
            open_access=getattr(args, "open_access", False),
        )
        if raw:
            results = ("PubMed", _normalize_pubmed_search(raw))
        if not results:
            print("No results from PubMed")
            return
        source_name, normalized = results
        print(f"\nFound {len(normalized)} papers ({source_name}):\n")
        _print_search_results(normalized)
        return

    if source == "europepmc":
        print("Searching Europe PMC...", file=sys.stderr)
        raw = _search_europepmc(
            args.query,
            args.max,
            offset=getattr(args, "offset", 0) or 0,
            year=getattr(args, "year", None),
            open_access=getattr(args, "open_access", False),
        )
        if raw:
            results = ("Europe PMC", _normalize_europepmc_search(raw))
        if not results:
            print("No results from Europe PMC")
            return
        source_name, normalized = results
        print(f"\nFound {len(normalized)} papers ({source_name}):\n")
        _print_search_results(normalized)
        return

    if source == "chemrxiv":
        print("Searching ChemRxiv (via Crossref)...", file=sys.stderr)
        raw = _search_chemrxiv(
            args.query,
            args.max,
            offset=getattr(args, "offset", 0) or 0,
            year=getattr(args, "year", None),
        )
        if raw:
            results = ("ChemRxiv", _normalize_chemrxiv_search(raw))
        if not results:
            print("No results from ChemRxiv")
            return
        source_name, normalized = results
        print(f"\nFound {len(normalized)} papers ({source_name}):\n")
        _print_search_results(normalized)
        return

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

    if not results and source in ("openalex", "auto"):
        print("Searching OpenAlex...", file=sys.stderr)
        raw = _search_openalex(args.query, args.max)
        if raw:
            results = ("OpenAlex", _normalize_openalex_search(raw))

    if not results and source in ("arxiv", "auto"):
        if source == "auto":
            print(
                "⚠ S2 and OpenAlex both failed, falling back to arXiv API. "
                "If this keeps happening, check API keys and network.",
                file=sys.stderr,
            )
        print("Searching arXiv...", file=sys.stderr)
        raw = search_papers(args.query, args.max)
        if raw:
            results = ("arXiv", _normalize_arxiv_search(raw))

    if not results:
        print("No results from any source")
        return

    source_name, normalized = results
    print(f"\nFound {len(normalized)} papers ({source_name}):\n")
    _print_search_results(normalized)


def _resolve_pmcid_or_die(pmcid: str) -> str:
    pmid = pmcid_to_pmid(pmcid)
    if not pmid:
        print(f"Could not resolve {pmcid} to a PMID via NCBI ELink.", file=sys.stderr)
        sys.exit(1)
    print(f"Resolved {pmcid} → PMID:{pmid}", file=sys.stderr)
    return pmid


def cmd_info(args):
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"

    if id_type == "pmid":
        paper = get_paper_info_pubmed(clean_id)
        if not paper:
            print(f"Paper not found: PMID:{clean_id}", file=sys.stderr)
            return
        print_pubmed_info(clean_id, paper)
        return

    if id_type == "doi":
        paper = get_paper_info_doi(clean_id)
        if not paper:
            print(f"Paper not found: DOI:{clean_id}", file=sys.stderr)
            return
        _print_doi_info(clean_id, paper)
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
        return

    arxiv_date = _arxiv_date(clean_id)
    date_str = arxiv_date.strftime("%Y-%m") if arxiv_date else "?"

    print(f"arXiv ID: {clean_id}")
    print(f"Title: {paper.title}")
    print(f"Authors: {', '.join(a.name for a in paper.authors)}")
    print(f"Published: {date_str}")
    if paper.categories:
        print(f"Categories: {', '.join(paper.categories)}")
    print(f"PDF: {paper.pdf_url}")
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
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"

    if id_type == "arxiv":
        paper_spec = f"ArXiv:{clean_id}"
        display_id = f"arXiv:{clean_id}"
    elif id_type == "pmid":
        paper_spec = f"PMID:{clean_id}"
        display_id = f"PMID:{clean_id}"
    elif id_type == "doi":
        paper_spec = f"DOI:{clean_id}"
        display_id = f"DOI:{clean_id}"
    else:
        print(
            f"Unrecognised identifier '{args.arxiv_id}' — supported: arXiv ID, "
            f"PMID, PMC ID, DOI.",
            file=sys.stderr,
        )
        sys.exit(1)

    source = args.source
    offset = args.offset
    results = None
    used_source = ""

    if source in ("s2", "auto"):
        print(f"Querying Semantic Scholar: {paper_spec}")
        ret = _fetch_citations_s2_spec(paper_spec, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "Semantic Scholar"

    if results is None and source in ("openalex", "auto"):
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
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if id_type == "pmcid":
        clean_id = _resolve_pmcid_or_die(clean_id)
        id_type = "pmid"

    if id_type == "arxiv":
        paper_spec, display_id = f"ArXiv:{clean_id}", f"arXiv:{clean_id}"
    elif id_type == "pmid":
        paper_spec, display_id = f"PMID:{clean_id}", f"PMID:{clean_id}"
    elif id_type == "doi":
        paper_spec, display_id = f"DOI:{clean_id}", f"DOI:{clean_id}"
    else:
        print(
            f"Unrecognised identifier '{args.arxiv_id}' — supported: arXiv ID, "
            f"PMID, PMC ID, DOI.",
            file=sys.stderr,
        )
        sys.exit(1)

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


def cmd_tex(args):
    result = fetch_tex_source(args.arxiv_id, OUTPUT_DIR)
    if result:
        print("\nDirectory structure:")
        print(result.name)
        tree_lines = print_tree(result)
        for line in tree_lines:
            print(line)
    else:
        print("\ntex download failed, falling back to PDF...", file=sys.stderr)
        _fetch_pdf_fallback(args.arxiv_id, OUTPUT_DIR)


def _extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """Pull plain text out of a PDF using PyMuPDF (fitz). Returns None on failure."""
    import fitz
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None
    try:
        return "\n".join(page.get_text().strip() for page in doc)
    finally:
        doc.close()


def _fetch_pmc_to_disk(pmcid: str) -> None:
    """Download PMC full text using a fallback chain of formats.

    Order (best-for-LLM first):
      1. JATS XML from Europe PMC — structured paragraphs, section/figure/table tags
      2. BioC JSON from NCBI — passage sequence, slightly broader coverage (~3M OA)
      3. PDF from pmc.ncbi.nlm.nih.gov + PyMuPDF text extraction — last resort

    Each successful step writes a file under OUTPUT_DIR and stops. The PDF
    path writes both the PDF itself and an adjacent .txt of extracted text so
    Claude can read either. If every format fails, exits non-zero.
    """
    pmc_up = pmcid.upper()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    xml_path = OUTPUT_DIR / f"{pmc_up}.xml"
    bioc_path = OUTPUT_DIR / f"{pmc_up}.bioc.json"
    pdf_path = OUTPUT_DIR / f"{pmc_up}.pdf"
    txt_path = OUTPUT_DIR / f"{pmc_up}.txt"
    for existing in (xml_path, bioc_path, txt_path):
        if existing.exists():
            print(f"Already exists: {existing}")
            return

    print(f"[1/3] Trying Europe PMC JATS XML for {pmc_up}...", file=sys.stderr)
    xml = fetch_pmc_fulltext_xml(pmc_up)
    if xml:
        xml_path.write_text(xml, encoding="utf-8")
        print(f"Saved JATS XML: {xml_path} ({len(xml):,} bytes)")
        return

    print(f"[2/3] JATS unavailable — trying NCBI BioC JSON...", file=sys.stderr)
    bioc = fetch_pmc_bioc_json(pmc_up)
    if bioc:
        bioc_path.write_text(bioc, encoding="utf-8")
        print(f"Saved BioC JSON: {bioc_path} ({len(bioc):,} bytes)")
        return

    print(f"[3/3] Structured formats unavailable — trying PMC PDF...", file=sys.stderr)
    pdf = fetch_pmc_pdf(pmc_up)
    if pdf:
        pdf_path.write_bytes(pdf)
        print(f"Saved PDF: {pdf_path} ({len(pdf):,} bytes)")
        text = _extract_pdf_text(pdf)
        if text:
            txt_path.write_text(
                f"# {pmc_up}\n\nURL: https://pmc.ncbi.nlm.nih.gov/articles/{pmc_up}/\n\n## Full Text\n\n{text}",
                encoding="utf-8",
            )
            print(f"Saved text: {txt_path} ({len(text):,} chars)")
        else:
            print("PDF text extraction failed; raw PDF is still usable.", file=sys.stderr)
        return

    print(
        f"\nNo open-access full text available for {pmc_up} "
        f"(JATS, BioC, and PDF all failed). Paper may be closed-access or withdrawn.",
        file=sys.stderr,
    )
    sys.exit(1)


def _fetch_chemrxiv_to_disk(doi: str) -> None:
    """ChemRxiv full-text. Layered chain:
       1. OA mirror (Unpaywall / OpenAlex / Crossref) — catches the ~60-80%
          that also live on a non-Cloudflared host (published version,
          institutional repo, arXiv cross-post).
       2. Direct chemrxiv.org download (usually 403 but free to try).
       3. Playwright headless browser — passes Cloudflare Turnstile.
       4. Print URL for manual download.
    """
    safe = doi.lower().replace("/", "_")
    pdf_path = OUTPUT_DIR / f"{safe}.pdf"
    txt_path = OUTPUT_DIR / f"{safe}.txt"
    if txt_path.exists() or pdf_path.exists():
        print(f"Already exists: {txt_path if txt_path.exists() else pdf_path}")
        return

    # Resolve OpenAlex OA URL in case cache has one already.
    openalex_pdf = None
    cached = get_cached_paper(f"doi:{doi.lower()}")
    if cached and cached.pdf_url and "chemrxiv.org" not in cached.pdf_url:
        openalex_pdf = cached.pdf_url

    print(f"[1/3] Trying OA mirrors for {doi}...", file=sys.stderr)
    pdf = _try_oa_mirror_for_pdf(doi=doi, openalex_pdf_url=openalex_pdf)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    print(f"[2/3] Trying direct ChemRxiv PDF (likely Cloudflared)...", file=sys.stderr)
    pdf = fetch_chemrxiv_pdf(doi)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    # Layer 2a: click-path — open the HTML article page and hit its
    # "Download PDF" link like a real user would. Works because
    # Cloudflare's cookie gets applied to same-origin link clicks.
    article_url = f"https://chemrxiv.org/doi/full/{doi}"
    print(f"[3a/3] Trying browser click-path via {article_url[:80]}...",
          file=sys.stderr)
    pdf = browser_download_via_click(article_url)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    # Layer 2b: direct PDF URL fetch with warmup cookie. Stricter CF
    # zones on the asset gateway often reject this path too, but worth a try.
    landing_url = chemrxiv_pdf_url(doi) or article_url
    print(f"[3b/3] Trying direct browser fetch of {landing_url[:80]}...",
          file=sys.stderr)
    pdf = browser_download_pdf(landing_url, warmup_url=article_url)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    print(
        f"\nAll automatic full-text paths failed for {doi}.\n"
        f"Open this URL in a real browser to download:\n  {landing_url}\n",
        file=sys.stderr,
    )
    sys.exit(1)


def _fetch_biorxiv_to_disk(doi: str) -> None:
    """bioRxiv / medRxiv full text. Layered chain:
       1. If Europe PMC has a PMC full-text copy (some preprints get one),
          use the existing PMC chain (JATS XML → BioC JSON → PMC PDF).
       2. OA mirror discovery (Unpaywall / OpenAlex / Crossref).
       3. Playwright on the biorxiv.org landing page.
    """
    safe = doi.lower().replace("/", "_")

    # 1. Europe PMC might have a PMC copy; re-use PMC chain.
    from lit.sources.europepmc import _fetch_paper_europepmc_by_doi
    epmc = _fetch_paper_europepmc_by_doi(doi)
    if epmc and epmc.pmcid:
        print(f"[1/3] Europe PMC has {epmc.pmcid} for this preprint; using PMC chain.",
              file=sys.stderr)
        _fetch_pmc_to_disk(epmc.pmcid)
        return

    # 2. OA mirrors.
    print(f"[1/3] Trying OA mirrors for {doi}...", file=sys.stderr)
    openalex_pdf = None
    cached = get_cached_paper(f"doi:{doi.lower()}")
    if cached and cached.pdf_url and "biorxiv" not in cached.pdf_url.lower() \
                                  and "medrxiv" not in cached.pdf_url.lower():
        openalex_pdf = cached.pdf_url
    pdf = _try_oa_mirror_for_pdf(doi=doi, openalex_pdf_url=openalex_pdf)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    # 3. Playwright on the canonical PDF URL.
    # bioRxiv / medRxiv PDF URL template: {site}/content/{DOI}v{n}.full.pdf
    # We don't always know the version, so try the landing page; the browser
    # will follow links and pick up any PDF response.
    is_medrxiv = "medrxiv" in ((cached.pdf_url if cached else "") or "").lower()
    site = "medrxiv" if is_medrxiv else "biorxiv"
    landing = f"https://www.{site}.org/content/{doi}.full.pdf"
    warmup = f"https://www.{site}.org/content/{doi}"
    print(f"[2/3] Trying headless browser on {landing}...", file=sys.stderr)
    pdf = browser_download_pdf(landing, warmup_url=warmup)
    if pdf:
        _save_pdf_and_text(pdf, safe)
        return

    print(
        f"\nAll automatic full-text paths failed for {doi}.\n"
        f"Open this URL in a real browser to download:\n  {landing}\n",
        file=sys.stderr,
    )
    sys.exit(1)


def _ingest_local_pdf(path_str: str, out_basename: str) -> None:
    """Manual-download escape hatch: take a user-supplied PDF file and run
    it through the normal save-PDF-and-extract-text pipeline.

    Useful when every automatic path has failed (Cloudflare / WAF blocks,
    paywall, whatever) — the user downloads once via a real browser, then
    points the tool at the file so caching + text extraction proceed."""
    p = Path(path_str).expanduser().resolve()
    if not p.exists():
        print(f"Local file not found: {p}", file=sys.stderr)
        sys.exit(1)
    data = p.read_bytes()
    if data[:4] != b"%PDF":
        print(f"File is not a PDF (missing %PDF magic header): {p}", file=sys.stderr)
        sys.exit(1)
    _save_pdf_and_text(data, out_basename)


def cmd_fulltext(args):
    """Dispatch full-text fetch by ID type:

    - arxiv → existing tex source download (LaTeX, with PDF/text fallback)
    - pmcid → PMC fallback chain (JATS XML → BioC JSON → PDF + text)
    - pmid  → PMC chain; fallback to OA-mirror lookup when no PMC copy
    - doi (ChemRxiv 10.26434/*) → OA mirror → Cloudflared direct → headless browser
    - doi (bioRxiv/medRxiv 10.1101/*) → Europe PMC PMC → OA mirror → headless browser

    ``--from-file PATH`` is a universal escape hatch: if every automatic
    path gets blocked (IP reputation / JS challenge / paywall), download
    the PDF manually via a real browser and pass its path here. We'll
    save + extract text like any other successful download.
    """
    id_type, clean_id = extract_paper_id(args.arxiv_id)

    if args.from_file:
        # Derive a stable basename from whichever ID form we were given.
        if id_type == "pmid":
            basename = f"PMID{clean_id}"
        elif id_type == "pmcid":
            basename = clean_id.upper()
        elif id_type == "doi":
            basename = clean_id.lower().replace("/", "_")
        elif id_type == "arxiv":
            basename = clean_id.replace("/", "_")
        else:
            basename = clean_id
        _ingest_local_pdf(args.from_file, basename)
        return

    if id_type == "arxiv":
        cmd_tex(argparse.Namespace(arxiv_id=clean_id))
        return

    if id_type == "pmcid":
        _fetch_pmc_to_disk(clean_id)
        return

    if id_type == "pmid":
        # First try the PMC chain — structured XML / BioC / PDF from NCBI.
        paper = get_paper_info_pubmed(clean_id)
        pmcid = paper.pmcid if paper and paper.pmcid else pmid_to_pmcid(clean_id)
        if pmcid:
            _fetch_pmc_to_disk(pmcid)
            return

        # No PMC copy → try OA mirrors (publisher OA, institutional repos).
        # Layer-1 covers many papers marked "closed access" in PubMed.
        doi_for_pmid = paper.doi if paper else None
        print(
            f"PMID:{clean_id} has no PMC copy — trying OA mirrors...",
            file=sys.stderr,
        )
        if doi_for_pmid:
            pdf = _try_oa_mirror_for_pdf(doi=doi_for_pmid)
            if pdf:
                _save_pdf_and_text(pdf, f"PMID{clean_id}")
                return

        print(
            f"\nNo open-access full-text found for PMID:{clean_id} "
            f"(no PMC copy, no OA mirror).",
            file=sys.stderr,
        )
        sys.exit(1)

    if id_type == "doi" and is_chemrxiv_doi(clean_id):
        _fetch_chemrxiv_to_disk(clean_id)
        return

    if id_type == "doi" and _is_biorxiv_doi(clean_id):
        _fetch_biorxiv_to_disk(clean_id)
        return

    print(
        f"Unrecognised or unsupported identifier '{args.arxiv_id}' — `fulltext` "
        f"supports arXiv ID, PMID, PMC ID, ChemRxiv DOI, bioRxiv/medRxiv DOI.",
        file=sys.stderr,
    )
    sys.exit(1)


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
    %(prog)s cited 1711.10561
    %(prog)s cited 1711.10561 --max 50
    %(prog)s cited 1711.10561 --offset 20          # 第 21-40 条
    %(prog)s cited 1711.10561 --source openalex
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="搜索论文 (S2→OpenAlex→arXiv)")
    search_parser.add_argument("query", help="搜索关键词")
    search_parser.add_argument("--max", type=int, default=20, help="最大结果数 (默认 20)")
    search_parser.add_argument(
        "--source",
        choices=["auto", "s2", "openalex", "arxiv", "pubmed", "chemrxiv", "europepmc"],
        default="auto",
        help="数据源: auto=自动(S2→OpenAlex→arXiv), s2, openalex, arxiv, pubmed, chemrxiv, europepmc (默认 auto)",
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

    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except arxiv.HTTPError as e:
        print(f"Error: arXiv HTTP {e.status}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
