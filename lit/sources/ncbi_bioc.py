"""NCBI BioC (biomedical text-mining) format fetcher for PMC papers.

BioC is NCBI's text-mining-oriented representation: the paper is split into
``passage`` objects (title / paragraph / caption / …), each with a byte
offset into the source, plus annotations when available. Coverage (~3M PMC
OA papers) is slightly broader than Europe PMC's JATS XML endpoint (~2.5M),
and the JSON shape is easier for downstream code than JATS.

Endpoint docs: https://www.ncbi.nlm.nih.gov/research/bionlp/APIs/BioC-PMC/
"""

from __future__ import annotations

import sys

import requests

from lit.config import HTTP_HEADERS
from lit.ratelimit import _brief_error, _request_with_retry


BIOC_API_BASE = "https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi"


def fetch_pmc_bioc_json(pmcid: str) -> str | None:
    """Fetch the BioC JSON full text of a PMC paper.

    Returns the raw JSON body (as str) or ``None`` if the paper is not in
    the OA subset. The same rate-limit bucket as PubMed proper is used
    because BioC-PMC is NCBI-hosted.
    """
    bare = pmcid.upper()
    if not bare.startswith("PMC"):
        bare = f"PMC{bare}"
    url = f"{BIOC_API_BASE}/BioC_json/{bare}/unicode"
    try:
        resp = _request_with_retry(
            requests.get,
            url,
            service="pubmed",
            headers=HTTP_HEADERS,
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"BioC-PMC fetch failed: {_brief_error(e)}", file=sys.stderr)
        return None

    body = resp.text or ""
    if not body.strip().startswith("["):
        return None
    return body
