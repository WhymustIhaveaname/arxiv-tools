"""Europe PMC adapter — currently used for PMC full-text JATS XML.

Europe PMC's ``/PMC/{pmcid}/fullTextXML`` is the cleanest path to OA
full-text in standard JATS XML. PubMed's own EFetch can return PMC
full-text too, but the XML is fiddlier and Europe PMC requires no key.
"""

from __future__ import annotations

import sys

import requests

from lit.config import EUROPEPMC_API_BASE, HTTP_HEADERS
from lit.ratelimit import _brief_error, _request_with_retry


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
