"""PubMed adapter (NCBI E-utilities).

Search uses ESearch → ESummary for light listings (no abstract).
Full metadata (with abstract) comes from EFetch (XML).

API key is optional: without one we get 3 req/s; with one, 10 req/s.
Key is read from the PUBMED_API_KEY environment variable (see config.py).
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET

import requests

from lit.config import HTTP_HEADERS, PUBMED_API_BASE, PUBMED_API_KEY
from lit.ids import _truncate_authors
from lit.ratelimit import _brief_error, _request_with_retry
from paper_cache import CachedAuthor, CachedPaper


def _pubmed_params(**extra) -> dict[str, str]:
    if PUBMED_API_KEY:
        extra["api_key"] = PUBMED_API_KEY
    return extra


def pmid_to_pmcid(pmid: str) -> str | None:
    """Resolve a PMID to its PMC ID (PMCxxxxx) via NCBI ELink.

    Returns ``None`` when the PubMed paper has no OA full-text in PMC.
    """
    url = f"{PUBMED_API_BASE}/elink.fcgi"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            params=_pubmed_params(
                dbfrom="pubmed",
                db="pmc",
                id=pmid,
                retmode="json",
            ),
            headers=HTTP_HEADERS,
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"PubMed ELink failed: {_brief_error(e)}", file=sys.stderr)
        return None

    for ls in data.get("linksets") or []:
        for ldb in ls.get("linksetdbs") or []:
            if ldb.get("dbto") == "pmc":
                links = ldb.get("links") or []
                if links:
                    return f"PMC{links[0]}"
    return None


def pmcid_to_pmid(pmcid: str) -> str | None:
    """Resolve a PMC ID (e.g. ``PMC1234567``) to its PMID via NCBI ELink.

    Returns ``None`` when the PMC paper has no linked PubMed record.
    """
    bare = pmcid[3:] if pmcid.upper().startswith("PMC") else pmcid
    url = f"{PUBMED_API_BASE}/elink.fcgi"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            params=_pubmed_params(
                dbfrom="pmc",
                db="pubmed",
                id=bare,
                retmode="json",
            ),
            headers=HTTP_HEADERS,
            timeout=15,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"PubMed ELink failed: {_brief_error(e)}", file=sys.stderr)
        return None

    for ls in data.get("linksets") or []:
        for ldb in ls.get("linksetdbs") or []:
            if ldb.get("dbto") == "pubmed":
                links = ldb.get("links") or []
                if links:
                    return str(links[0])
    return None


def _esearch_pmids(query: str, max_results: int) -> list[str] | None:
    url = f"{PUBMED_API_BASE}/esearch.fcgi"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            params=_pubmed_params(
                db="pubmed",
                term=query,
                retmax=str(min(max_results, 1000)),
                retmode="json",
            ),
            headers=HTTP_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except requests.RequestException as e:
        print(f"PubMed ESearch failed: {_brief_error(e)}", file=sys.stderr)
        return None
    return data.get("esearchresult", {}).get("idlist") or None


def _esummary(pmids: list[str]) -> dict | None:
    """Fetch ESummary JSON for a batch of PMIDs."""
    url = f"{PUBMED_API_BASE}/esummary.fcgi"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            params=_pubmed_params(
                db="pubmed",
                id=",".join(pmids),
                retmode="json",
            ),
            headers=HTTP_HEADERS,
            timeout=30,
        )
        return resp.json()
    except requests.RequestException as e:
        print(f"PubMed ESummary failed: {_brief_error(e)}", file=sys.stderr)
        return None


def _search_pubmed(query: str, max_results: int = 20) -> list[dict] | None:
    """Search PubMed and return ESummary records.

    Two round trips: ESearch (get PMIDs) → ESummary (batch metadata).
    Abstracts are not returned by ESummary — use _fetch_paper_pubmed for those.
    """
    pmids = _esearch_pmids(query, max_results)
    if not pmids:
        return None

    data = _esummary(pmids)
    if not data:
        return None

    result_map = data.get("result") or {}
    uids = result_map.get("uids") or pmids
    records = [result_map[uid] for uid in uids if uid in result_map]
    return records[:max_results] or None


def _normalize_pubmed_search(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        pmid = r.get("uid") or ""
        authors = [a["name"] for a in (r.get("authors") or []) if a.get("authtype") == "Author"]
        if not authors:
            authors = [a["name"] for a in (r.get("authors") or [])]

        year = ""
        pubdate = r.get("pubdate") or r.get("epubdate") or ""
        if pubdate:
            year = pubdate.split(" ")[0].split("-")[0]

        out.append(
            {
                "id": f"PMID:{pmid}" if pmid else "",
                "title": r.get("title") or "",
                "authors": _truncate_authors(authors),
                "year": year or "?",
                "cited_by": None,
                "abstract": None,
            }
        )
    return out


def _fetch_paper_pubmed(pmid: str) -> CachedPaper | None:
    """Fetch full metadata for a PMID via EFetch (XML).

    Returns a CachedPaper with source="pubmed" and any cross-reference IDs
    (doi, pmcid) populated from the ArticleIdList.
    """
    url = f"{PUBMED_API_BASE}/efetch.fcgi"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            params=_pubmed_params(
                db="pubmed",
                id=pmid,
                rettype="abstract",
                retmode="xml",
            ),
            headers=HTTP_HEADERS,
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"PubMed EFetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        print(f"PubMed EFetch returned invalid XML: {e}", file=sys.stderr)
        return None

    article = root.find(".//PubmedArticle")
    if article is None:
        return None

    title_el = article.find(".//Article/ArticleTitle")
    title = "".join(title_el.itertext()).strip() if title_el is not None else ""
    if not title:
        return None

    authors: list[CachedAuthor] = []
    for au in article.findall(".//Article/AuthorList/Author"):
        last = (au.findtext("LastName") or "").strip()
        fore = (au.findtext("ForeName") or au.findtext("Initials") or "").strip()
        collective = (au.findtext("CollectiveName") or "").strip()
        if last or fore:
            name = f"{fore} {last}".strip()
            authors.append(CachedAuthor(name))
        elif collective:
            authors.append(CachedAuthor(collective))
    if not authors:
        return None

    abstract_parts: list[str] = []
    for ab in article.findall(".//Article/Abstract/AbstractText"):
        label = ab.get("Label")
        text = "".join(ab.itertext()).strip()
        if not text:
            continue
        abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = "\n\n".join(abstract_parts)

    doi = ""
    pmcid = ""
    # Pin the XPath to PubmedData/ArticleIdList — nested ArticleIdLists inside
    # ReferenceList entries carry the *cited papers'* IDs, which would otherwise
    # silently overwrite the main paper's metadata.
    for aid in article.findall("./PubmedData/ArticleIdList/ArticleId"):
        id_type = (aid.get("IdType") or "").lower()
        value = (aid.text or "").strip()
        if id_type == "doi":
            doi = value
        elif id_type == "pmc":
            pmcid = value.upper() if value.upper().startswith("PMC") else f"PMC{value}"

    journal = article.findtext(".//Article/Journal/Title") or ""
    categories = [journal] if journal else []

    year: int | None = None
    year_text = article.findtext(".//Article/Journal/JournalIssue/PubDate/Year")
    if year_text and year_text.isdigit():
        year = int(year_text)
    else:
        # MedlineDate covers e.g. "2024 Jan-Feb" or seasonal issues.
        medline_date = article.findtext(".//Article/Journal/JournalIssue/PubDate/MedlineDate") or ""
        m = re.search(r"\b(19|20)\d{2}\b", medline_date)
        if m:
            year = int(m.group(0))

    pdf_url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    if pmcid:
        pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/"

    return CachedPaper(
        title=title,
        authors=authors,
        abstract=abstract,
        categories=categories,
        pdf_url=pdf_url,
        year=year,
        source="pubmed",
        pmid=pmid,
        doi=doi or None,
        pmcid=pmcid or None,
    )


def print_pubmed_info(pmid: str, paper: CachedPaper) -> None:
    """CLI output for `info <PMID>`."""
    print(f"PMID: {pmid}")
    print(f"Title: {paper.title}")
    print(f"Authors: {', '.join(a.name for a in paper.authors)}")
    if paper.categories:
        print(f"Journal: {paper.categories[0]}")
    if paper.doi:
        print(f"DOI: {paper.doi}")
    if paper.pmcid:
        print(f"PMC: {paper.pmcid}")
    print(f"URL: {paper.pdf_url}")
    if paper.abstract:
        print(f"\nAbstract:\n{paper.abstract}")
