"""BibTeX entry + citation-key generation."""

from __future__ import annotations

import re

from lit.ids import _arxiv_year

STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "in", "on", "at", "to", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare", "ought",
    "used", "via", "using", "based", "towards", "toward",
}


def generate_citation_key(paper, arxiv_id: str) -> str:
    """Generate a BibTeX citation key.

    Format: {first_author_last_name_lower}{year}{first_meaningful_title_word_lower}
    Example: vaswani2017attention, raissi2017physics

    Year comes from the arXiv ID to avoid OpenAlex returning a journal year.
    """
    last_name = re.sub(r"[^a-z]", "", paper.authors[0].name.split()[-1].lower())
    year = _arxiv_year(arxiv_id)

    title_words = re.findall(r"[a-zA-Z]+", paper.title)
    first_word = ""
    for word in title_words:
        if word.lower() not in STOPWORDS:
            first_word = word.lower()
            break

    return f"{last_name}{year}{first_word}"


def _pubmed_citation_key(paper, pmid: str) -> str:
    """Citation key for a PubMed paper: {first_author_last}{year}{first_meaningful_title_word}.

    Falls back to ``pm{pmid}`` when neither author name nor year is available.
    """
    last_name = ""
    if paper.authors:
        last_name = re.sub(r"[^a-z]", "", paper.authors[0].name.split()[-1].lower())
    year_str = str(paper.year) if paper.year else ""

    first_word = ""
    for word in re.findall(r"[a-zA-Z]+", paper.title):
        if word.lower() not in STOPWORDS:
            first_word = word.lower()
            break

    # A title word alone makes a useless, collision-prone key. Require at least
    # one of (author, year) before assembling; otherwise fall back to pm<pmid>.
    if not last_name and not year_str:
        return f"pm{pmid}"
    key = f"{last_name}{year_str}{first_word}".strip()
    return key or f"pm{pmid}"


def generate_bibtex_pubmed(paper, pmid: str) -> str:
    """Build an ``@article`` entry from PubMed-side metadata.

    Used when Crossref content negotiation fails (or the paper has no DOI).
    The result is less complete than Crossref's — no volume/issue/pages —
    but captures what EFetch gave us.
    """
    citation_key = _pubmed_citation_key(paper, pmid)
    authors = " and ".join(a.name for a in paper.authors)
    journal = paper.categories[0] if paper.categories else ""

    fields = [f"title={{{paper.title}}}"]
    if authors:
        fields.append(f"author={{{authors}}}")
    if journal:
        fields.append(f"journal={{{journal}}}")
    if paper.year:
        fields.append(f"year={{{paper.year}}}")
    if paper.doi:
        fields.append(f"doi={{{paper.doi}}}")
    fields.append(f"pmid={{{pmid}}}")
    if paper.pmcid:
        fields.append(f"pmcid={{{paper.pmcid}}}")
    fields.append(f"url={{https://pubmed.ncbi.nlm.nih.gov/{pmid}/}}")

    body = ",\n      ".join(fields)
    return f"@article{{{citation_key},\n      {body},\n}}"


def generate_bibtex(paper, arxiv_id: str) -> str:
    """Generate an arXiv-style BibTeX entry."""
    citation_key = generate_citation_key(paper, arxiv_id)
    authors = " and ".join(a.name for a in paper.authors)
    clean_id = re.sub(r"v\d+$", "", arxiv_id)
    year = _arxiv_year(arxiv_id) or paper.published.year

    fields = [
        f"title={{{paper.title}}}",
        f"author={{{authors}}}",
        f"year={{{year}}}",
        f"eprint={{{clean_id}}}",
        "archivePrefix={arXiv}",
    ]
    if paper.categories:
        fields.append(f"primaryClass={{{paper.categories[0]}}}")
    fields.append(f"url={{https://arxiv.org/abs/{clean_id}}}")

    body = ",\n      ".join(fields)
    return f"@misc{{{citation_key},\n      {body},\n}}"
