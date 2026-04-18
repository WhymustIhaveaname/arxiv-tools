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

# Academic literature workflow

<goal>
When the user asks for a paper's full text, your job is to land the text onto
disk at the canonical cache path, in the most LLM-readable format available.
Finish when the bytes are saved, not when the first automatic path fails. The
automatic pipeline already walks a layered fallback (OA mirrors, PMC cross-
references, preprint reverse-lookup) and will hand off paywalled /
Cloudflare-protected cases to you with a Playwright MCP recipe; complete the
handoff yourself rather than reporting failure back to the user.
</goal>

## How to invoke

Every subcommand runs via:

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" <subcommand> <args>
```

`uv` reads the PEP 723 inline metadata at the top of `arxiv_tool.py` and
installs dependencies automatically. No manual setup is needed.

Any ID form works wherever a paper is referenced: arXiv ID (`2401.12345`),
DOI (bare `10.xxxx/...` or `https://doi.org/...`), PMID (numeric),
PMC ID (`PMC1234567`), bioRxiv / medRxiv / ChemRxiv URL. The tool
autodetects.

## Commands — decision table

| The user wants... | Run |
|---|---|
| Search papers (default: all sources parallel, dedup) | `search "<query>"` |
| Paper metadata (abstract, authors, IDs, year) | `info <id>` |
| BibTeX entry | `bib <id>` |
| Who cited this paper | `cited <id>` |
| What this paper cites | `references <id>` |
| Biomedical similar papers | `similar <pmid>` |
| Text-mined genes / diseases / chemicals in a paper | `annotations <id>` |
| arXiv LaTeX source only | `tex <arxiv-id>` |
| Paper full text onto disk (any ID type) | `fulltext <id>` |
| Bulk fulltext over a list of IDs | `fulltext-batch <ids.txt>` |
| Ingest PDFs you already have | `fulltext-import <dir>` |

## Canonical cache layout

<cache>
Output files land under `$ARXIV_CACHE_DIR` (default: a `.arxiv/` directory
inside the plugin). Every successful `fulltext` run writes files named after
the ID. Basename rules:

| ID type | Basename |
|---|---|
| arXiv  | `2401.12345` (slashes replaced with `_`, handles old format `cs_0401001`) |
| PMID   | `PMID39876543` |
| PMC ID | `PMC7610144` (uppercased) |
| DOI    | `10.26434_chemrxiv-2024-abc` (lowercase, `/` → `_`) |

Files written by format, preference-sorted (best for LLM first):

1. `<basename>.xml` — JATS XML with structured section / figure / table tags (PMC)
2. `<basename>.bioc.json` — passage-level structured text (PMC fallback)
3. `<id>_<title-slug>/` — directory of `.tex` source files (arXiv)
4. `<basename>.pdf` + `<basename>.txt` — PDF with PyMuPDF text extraction

After every `fulltext` call, read the highest-priority file that exists for
that basename. When only `.txt` exists the paper is still fully readable;
structured formats are preferred because they preserve section boundaries.
</cache>

## Full-text retrieval — the main workflow

<algorithm>
For every paper the user asks you to fetch, in order:

1. Run `uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fulltext <id>`.

2. Inspect stderr + stdout. Three outcomes:

   - Output contains `Saved JATS XML:` / `Saved BioC JSON:` / `Saved PDF:` /
     `Saved text:` / `Directory structure:`. The file is on disk. Move on.

   - Output contains `Already exists:`. The cache already has this paper.
     Move on.

   - Output starts with `All automatic full-text paths failed for <id>.`
     and contains a `Landing URL:` line. Proceed to the Playwright MCP
     recipe below.

3. Verify the canonical file landed. Read the expected path from the cache
   layout section. If the file is missing after a "Saved ..." line, surface
   that as a bug — do not silently ignore.

4. Read the file (prefer `.xml` > `.bioc.json` > `.tex` dir > `.txt`) and
   continue with the user's task.
</algorithm>

## Playwright MCP recipe — run this when automatic fetch fails

<recipe>
The tool has already tried open-access mirrors (Unpaywall / OpenAlex
`best_oa_location` / CORE / Crossref TDM), Europe PMC cross-references,
preprint reverse-lookup, and direct publisher fetch. Remaining failure modes
are Cloudflare challenges, publisher paywalls, and JS-required download
buttons. Playwright MCP bypasses all three.

Flow: navigate to the landing URL → find the PDF → save bytes to a temp path
→ re-run `fulltext <id> --from-file <tmp-path>` so the tool ingests the PDF
into the canonical cache with text extraction.

Concrete steps:

1. `browser_navigate` to the `Landing URL:` from the failure message. Allow
   60 s timeout; Cloudflare Turnstile (a JavaScript challenge that auto-solves
   in the background) typically clears in 5–15 s.

2. `browser_snapshot` to see the loaded page. If the title contains
   `Just a moment...` or `Verifying`, wait a few seconds and snapshot again
   until the real article page appears.

3. Find the PDF download control and fetch the bytes. The best path depends
   on the site:

   - `<meta name="citation_pdf_url">` in the page head — most journals set
     this. `browser_evaluate` to read `document.querySelector('meta[name=
     "citation_pdf_url"]').content`, then fetch that URL with the browser's
     cookies via another `browser_evaluate("async () => await (await
     fetch(url)).arrayBuffer()")`.

   - A visible download button or link. Snapshot, identify the ref, click
     it, capture the resulting download via `browser_network_requests` or
     a download event.

   - Direct `.pdf` href (common on bioRxiv `/content/<doi>.full.pdf`, PMC
     `/pdf/` endpoints).

4. Write the bytes to a temp path (e.g. `/tmp/<basename>.pdf`). Confirm the
   first four bytes are `%PDF` before re-invoking the tool — anything else
   is an error page, a login wall, or a CF challenge that didn't clear.

5. Run:

   ```bash
   uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fulltext <id> --from-file /tmp/<basename>.pdf
   ```

   The tool validates the magic bytes, extracts text with PyMuPDF, saves
   `<basename>.pdf` + `<basename>.txt` to the canonical cache.

6. Read the resulting `.txt` and continue.

Site-specific hints (domain knowledge you won't find in Playwright docs):

- ChemRxiv: the article page is Cloudflare-challenged on first visit. A single
  navigate + ~10 s wait clears the challenge. Known working download selectors,
  in priority order: `a[download][href*="asset"]`, `a[href*="/original/"]`,
  `button:has-text("Download")`. The asset URL is same-origin with the article
  page; clicking from the cleared article page succeeds where a direct hit
  from a fresh context fails.
- bioRxiv / medRxiv: after you've visited `https://www.biorxiv.org/content/<doi>`
  (or `www.medrxiv.org/...`) the session has the clearance cookie, and
  `/content/<doi>.full.pdf` serves the PDF directly.
- Nature / Cell / Springer / Wiley: the "Download PDF" button usually calls
  a signed asset URL via JS. Click-then-capture works; direct hrefs often
  don't.
- Institutional paywalls (NEJM, Science full text, many Wiley journals):
  a login wall will stop you. Do not claim success. Instead, surface back to
  the user: the landing URL you tried, what the page showed (login wall
  vs CF challenge vs 404), and any OA alternatives the page links to
  (preprint, institutional repo, author homepage).
- WAF hard-blocks (fast-fail, don't retry): if the page shows
  "There was a problem providing the content you requested" (Elsevier /
  ScienceDirect), a persistent Cloudflare "Just a moment..." that does
  not clear after ~30 s, or an outright HTTP 403 on a plain GET of the
  landing URL, the server's egress IP is WAF-blacklisted. Playwright MCP
  cannot solve this. Stop after one attempt per site — do not retry from
  a fresh tab or with a different UA, it will not help. Surface the
  failure with a one-line "IP-level block by <publisher>; manual
  download from a different network required" and move on to the next
  paper; the user will use `fulltext-import` to ingest manual copies
  later.

<example>
Handoff for a ChemRxiv DOI:

  $ uv run ".../arxiv_tool.py" fulltext 10.26434/chemrxiv-2024-abc
  [1/2] Trying OA mirrors for 10.26434/chemrxiv-2024-abc...
    OA mirror download failed (...): 403
  [2/2] Trying direct ChemRxiv PDF (likely Cloudflared)...
    ChemRxiv PDF fetch failed: Cloudflare challenge
  All automatic full-text paths failed for 10.26434/chemrxiv-2024-abc.
  Landing URL: https://chemrxiv.org/doi/full/10.26434/chemrxiv-2024-abc
  If you are an agent with Playwright MCP ... re-run `fulltext ... --from-file <path>` ...

Agent actions:
  1. browser_navigate("https://chemrxiv.org/engage/chemrxiv/article-details/abc", timeout=60000)
  2. browser_snapshot — title is "Just a moment..."; wait 10 s
  3. browser_snapshot — title is now the paper title; the page has a
     "Download" button mapped to an asset URL
  4. browser_click on the Download button; capture the downloaded bytes
  5. Save bytes to /tmp/10.26434_chemrxiv-2024-abc.pdf; verify first 4 bytes are b"%PDF"
  6. uv run ".../arxiv_tool.py" fulltext 10.26434/chemrxiv-2024-abc \
        --from-file /tmp/10.26434_chemrxiv-2024-abc.pdf
       → Saved PDF: /home/arxiv/10.26434_chemrxiv-2024-abc.pdf (2,145,823 bytes)
       → Saved text: /home/arxiv/10.26434_chemrxiv-2024-abc.txt (48,221 chars)
  7. Read /home/arxiv/10.26434_chemrxiv-2024-abc.txt — continue with user's task.
</example>

Do not open a browser when the automatic chain already succeeded. The recipe
is only for the handoff case.
</recipe>

## Piggyback metadata while the browser is open

<piggyback>
The automatic metadata pipeline (OpenAlex / S2 / PubMed / Crossref) covers
title, authors, abstract, year, DOI / PMID, citation counts, OA status. It
does not always have:

- author affiliations (institution per author)
- funding / grant IDs
- figure and table captions
- supplementary material URLs
- preprint-to-version-of-record link
- keywords beyond what the API exposed

When the user's task benefits from any of these and you have a landing page
open anyway for a Playwright download, grab them in the same session.
Do not open a browser solely for metadata enrichment — `info <id>` is the
right tool for that, and it hits the API aggregator.
</piggyback>

## Search filters — consolidated reference

| Flag | Applies to | Meaning |
|---|---|---|
| `--max N` | all | max results (default 20) |
| `--source <name>` | all | `all` (default: parallel + dedup across all sources) or one of `s2`, `openalex`, `arxiv`, `pubmed`, `chemrxiv`, `europepmc`, `auto` |
| `--domain <name>` | with `--source all` | `bio` / `med` / `chem` / `cs` / `phys` — restricts to relevant sources and sets S2 fields-of-study |
| `--snippet` | with `--source all` | S2 snippet search (rank by full-text phrase match — good for technical terms) |
| `--year 2020` / `--year 2020-2024` | most | single year or range |
| `--offset N` | PubMed, Europe PMC | pagination skip |
| `--open-access` | S2, PubMed, Europe PMC | OA subset only |
| `--fields-of-study A,B` | S2 | e.g. `Computer Science,Physics` |
| `--pub-types A,B` | S2 | e.g. `JournalArticle,Conference` |
| `--min-citations N` | S2 | citation floor |
| `--venue NAME` | S2 | specific conference/journal |
| `--bulk` | S2 | up to 1000 results with pagination |
| `--sort field:dir` | S2 bulk | e.g. `citationCount:desc` |
| `--token <t>` | S2 bulk | pagination continuation |

Source-native query DSL passes through verbatim:
- `--source pubmed` accepts MeSH and field tags: `'CRISPR-Cas Systems[MeSH]'`,
  `'Smith J[Author]'`.
- `--source europepmc` accepts the Europe PMC DSL: `'SRC:PPR AND CRISPR'`
  (PPR = preprint sources), `'TITLE:"foo" AND AUTH:"Smith"'`.

## Other flags

| Command | Flag | Meaning |
|---|---|---|
| `info` / `bib` / `tex` | (ID only) | no extra flags |
| `cited` | `--max`, `--offset`, `--source {auto,s2,openalex}` | pagination + source override |
| `references` | `--max`, `--offset` | pagination (S2 + PubMed ELink fallback; no `--source`) |
| `similar` | `--max`, `--offset` | pagination (NCBI ELink only) |
| `annotations` | `--type` | comma-sep subset of `genes,diseases,chemicals,organisms,go,methods,accessions,resources,all` |
| `annotations` | `--max-per-type N` | dedup cap per entity type (default 30) |
| `fulltext` | `--from-file PATH` | ingest a manually-downloaded PDF, skip all network paths |
| `fulltext-batch` | `--manifest PATH` | failure TSV output path |
| `fulltext-import` | `--manifest PATH` | manifest from a prior `fulltext-batch` for filename-to-ID matching |

## Data source fallback summary

| Command | Order |
|---|---|
| `search` | `--source all` parallel aggregator (default); single-source with `--source <name>` |
| `info` / `bib` | local cache → parallel aggregator → Crossref content negotiation (for bib) |
| `cited` | S2 → OpenAlex |
| `references` | S2 → PubMed ELink (biomed fallback) |
| `tex` | arXiv e-print |
| `fulltext` | per ID-type dispatch table (arxiv → LaTeX; PMC → JATS/BioC/PDF; PMID → PMC then DOI chain; DOI → OA mirrors → preprint reverse-lookup → Playwright handoff) |

S2 leads search and citation lookup because of richer graph coverage;
OpenAlex leads info/bib because of the most lenient rate limit
(0.1 s/request). PubMed is the authoritative biomedical source.