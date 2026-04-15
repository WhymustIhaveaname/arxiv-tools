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
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "keywords" --source s2|openalex|arxiv|pubmed
```

Works across all fields — S2 and OpenAlex cover chemistry, biology, medicine, physics, etc. For dedicated biomedical search (MeSH terms, clinical results), use `--source pubmed`.

PubMed-specific filters (work with `--source pubmed`):

```bash
--year 2020              # single year
--year 2020-2024         # year range (open-ended "2020-" / "-2015" ok)
--offset 20              # skip first 20 results (pagination)
--open-access            # restrict to free-full-text subset
```

MeSH and field tags pass through the query directly, e.g.
`search 'CRISPR-Cas Systems[MeSH]' --source pubmed`.

Europe PMC-specific notes (`--source europepmc`):

```bash
--year 2020 / 2020-2024
--offset 20
--open-access
```

Europe PMC's query DSL passes through, so you can write e.g.
`search 'SRC:PPR AND CRISPR' --source europepmc` to restrict to preprints,
or `TITLE:"foo" AND AUTH:"Smith"` for field-tagged searches. Covers
PubMed + PMC + preprints (bioRxiv/medRxiv/Research Square) + patents in one
API.

ChemRxiv-specific filters (`--source chemrxiv`):

```bash
--year 2024              # filter posting year (also accepts ranges)
--offset 20              # pagination
```

ChemRxiv DOIs (`10.26434/chemrxiv-*`) are handled automatically for
`info`/`bib`/`cited`/`references`. Search routes through Crossref because
chemrxiv.org's native API is Cloudflare-blocked for non-browser clients.

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
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <PMID>             # PubMed
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <PMC ID>           # e.g. PMC7610144 — auto-resolved to PMID via ELink
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <DOI>              # e.g. 10.1038/s41586-020-2649-2 — resolved via OpenAlex
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info https://pubmed.ncbi.nlm.nih.gov/39876543/
```

ID type is auto-detected: arXiv ID / URL, PMID, PMC ID, DOI (bare or
https://doi.org/...), or ncbi URLs.

### references — forward citation lookup (this paper's bibliography)

Inverse of `cited`. Returns the papers that this paper cites.

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" references <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" references <PMID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" references <PMC ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" references <DOI>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" references <ID> --max 50 --offset 20
```

Order: S2 `/paper/{id}/references` first (rich metadata in one call); when
S2 has no data for a PubMed paper, falls back to NCBI ELink
`pubmed_pubmed_refs` + ESummary.

### annotations — text-mined entities (Europe PMC)

Pull text-mined biomedical / chemical entities from Europe PMC's
Annotations API — genes, diseases, chemicals, organisms, Gene Ontology
terms, experimental methods, and dataset accession numbers, each with
a canonical ontology URI (UniProt / UMLS / CHEBI / GO / etc.).

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" annotations <PMID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" annotations <PMC ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" annotations <DOI>                    # must map to PubMed/PMC
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" annotations <ID> --type genes,diseases
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" annotations <ID> --max-per-type 10
```

Supported `--type` values: `genes`, `diseases`, `chemicals`, `organisms`,
`go`, `methods`, `accessions`, `resources`, `all` (default).

Entities are deduplicated per type (same surface string + URI collapse to
one line with a `[N×]` mention count). PMC full-text gives the richest
annotations; PubMed-only records fall back to title+abstract mining.

### tex — download LaTeX source (arXiv only)

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID>
```

Downloads the tex source and returns the directory path and structure, preserving full LaTeX formatting and figures. If the source is unavailable, falls back to PDF download and text extraction.

### fulltext — download full text (any supported source)

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fulltext <arXiv ID>  # LaTeX source + PDF fallback (same as `tex`)
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fulltext <PMID>      # ELink → PMC → JATS XML
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fulltext <PMC ID>    # Direct Europe PMC JATS XML
```

Dispatches by ID type:
- arXiv → LaTeX source from arxiv.org/e-print + PDF text-extraction fallback
- PMC ID → fallback chain: JATS XML (Europe PMC) → BioC JSON (NCBI) → PDF+text (PyMuPDF). Every PMC OA paper gets at least one readable format.
- PMID → ELink resolves to PMC, then the PMC chain above; exits if the paper has no OA full text anywhere
- ChemRxiv DOI → attempt PDF download (likely blocked by Cloudflare); on failure, prints the publisher URL so the user can open it in a real browser

### bib — generate BibTeX citation

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <PMID>                  # PubMed
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <PMC ID>                # auto-resolved to PMID
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <DOI>                   # Crossref direct
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <ID> -o refs.bib        # append to file
```

For PMIDs / DOIs the tool first asks Crossref via DOI content negotiation
(authoritative `@article` with journal/volume/issue/pages); on failure or
when a PubMed paper has no DOI, it builds a minimal `@article` from EFetch
metadata. Rendered BibTeX is cached per-ID so repeat calls are local.

### cited — reverse citation lookup

Find which papers cite a given paper.

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <PMID>               # PubMed
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <PMC ID>             # auto-resolved to PMID
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <DOI>                # via S2 DOI: accessor
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <ID> --max 50
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <ID> --offset 20     # pagination
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <ID> --source s2|openalex
```

S2 and OpenAlex both accept any of ArXiv:, PMID:, DOI: paper specs, so all
ID types work the same way.

## Data source fallback

| Subcommand | Fallback order |
|------------|---------------|
| search | S2 → OpenAlex → arXiv |
| cited | S2 → OpenAlex |
| info / bib | local cache → OpenAlex → S2 → arXiv |
| tex | local cache → arXiv |

- search/cited prefer S2: more complete citation data
- info/bib prefer OpenAlex: most lenient rate limit (0.1s/req vs S2 2s vs arXiv 5s)
