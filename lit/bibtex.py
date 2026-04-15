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
