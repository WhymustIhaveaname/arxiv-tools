---
name: literature
description: >
  Use this skill whenever the user needs to work with academic papers across
  ANY field — computer science, AI/ML, physics, math, chemistry, biology,
  biomedicine, medicine, materials science, etc. Triggers include: "search
  for papers", "find papers on <topic>", "look up this paper", "get the
  abstract/metadata", "who cited this", "generate BibTeX", "download the
  paper", "download LaTeX source", or when the user provides any paper
  identifier — arXiv ID (e.g. 2401.12345), DOI (10.xxxx/...), PMID (PubMed
  numeric ID), PMC ID (PMC1234567), or a bioRxiv/medRxiv/ChemRxiv URL.
  Also triggers on domain-specific terms like CRISPR, protein folding,
  catalysis, MeSH terms, clinical trials, drug discovery, etc. when the
  user is clearly doing literature work.
---

# Academic Literature Tool (cross-domain)

Search, fetch metadata, generate citations, download full text, and look up citations across multiple scholarly databases: Semantic Scholar, OpenAlex, arXiv (and — under active development — PubMed, PubMed Central, bioRxiv, medRxiv, ChemRxiv, Europe PMC, Crossref).

Five subcommands: `search`, `info`, `tex`, `bib`, `cited`.

## How to run

Script dependencies are declared in inline metadata at the top of the file — you are responsible for installing them yourself.

## Subcommands

### search — find papers

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "keywords"
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "keywords" --max 10
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "keywords" --source s2|openalex|arxiv
```

Works across all fields — S2 and OpenAlex cover chemistry, biology, medicine, physics, etc.

S2-specific filter parameters:

```bash
--year 2024                    # single year
--year 2020-2024               # year range
--fields-of-study "Computer Science,Physics,Biology,Chemistry,Medicine"
--pub-types "JournalArticle,Conference"
--min-citations 50
--venue "NeurIPS"
--open-access                  # open-access only
```

S2 bulk search (up to 1000 results, supports sorting and pagination):

```bash
--bulk --sort "citationCount:desc"
--bulk --token <token from previous page>   # pagination
```

### info — get paper metadata (does not download tex)

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <arXiv ID>
```

Currently accepts arXiv IDs. DOI / PMID / PMC ID support is being added.

### tex — download full paper source

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID>
```

Downloads the tex source and returns the directory path and structure, preserving full LaTeX formatting and figures. If the source is unavailable, falls back to PDF download and text extraction.

LaTeX source is only available for arXiv papers. For non-arXiv papers (bioRxiv, PubMed, etc.), a future `fulltext` subcommand will fetch PDF / JATS XML.

### bib — generate BibTeX citation

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID> -o references.bib  # append to file
```

### cited — reverse citation lookup

Find which papers cite a given paper.

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --max 50
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --offset 20         # pagination
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --source s2|openalex
```

## Data source fallback

| Subcommand | Fallback order |
|------------|---------------|
| search | S2 → OpenAlex → arXiv |
| cited | S2 → OpenAlex |
| info / bib | local cache → OpenAlex → S2 → arXiv |
| tex | local cache → arXiv |

- search/cited prefer S2: more complete citation data
- info/bib prefer OpenAlex: most lenient rate limit (0.1s/req vs S2 2s vs arXiv 5s)
