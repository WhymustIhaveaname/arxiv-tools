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
    sanitize_filename,
)
from lit.bibtex import STOPWORDS, generate_bibtex, generate_citation_key
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
from lit.sources.openalex import (
    _fetch_citations_openalex,
    _fetch_paper_openalex,
    _normalize_openalex_search,
    _openalex_params,
    _print_citations_openalex,
    _reconstruct_abstract,
    _resolve_openalex_id,
    _search_openalex,
)
from lit.sources.s2 import (
    _fetch_citations_s2,
    _fetch_paper_s2,
    _normalize_s2_search,
    _print_citations_s2,
    _s2_headers,
    _s2_search_params,
    _search_s2,
    _search_s2_bulk,
)
from paper_cache import cache_paper, get_cached_bibtex, get_cached_paper

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

    bibtex = generate_bibtex(paper, clean_id)
    cache_paper(clean_id, paper, bibtex)
    return paper


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


def cmd_info(args):
    clean_id = extract_arxiv_id(args.arxiv_id)

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


def cmd_bib(args):
    clean_id = extract_arxiv_id(args.arxiv_id)

    paper = get_paper_info(clean_id)
    if not paper:
        sys.exit(1)

    bibtex = get_cached_bibtex(clean_id)
    if not bibtex:
        bibtex = generate_bibtex(paper, clean_id)

    if args.output:
        output_path = Path(args.output)
        mode = "a" if output_path.exists() else "w"
        with open(output_path, mode, encoding="utf-8") as f:
            if mode == "a" and output_path.stat().st_size > 0:
                f.write("\n\n")
            f.write(bibtex)
            f.write("\n")
        print(f"{'Appended' if mode == 'a' else 'Written'} to: {output_path}")
    else:
        print(bibtex)


def cmd_cited(args):
    clean_id = extract_arxiv_id(args.arxiv_id)
    source = args.source
    offset = args.offset
    results = None
    used_source = ""

    if source in ("s2", "auto"):
        print(f"Querying Semantic Scholar: ArXiv:{clean_id}")
        ret = _fetch_citations_s2(clean_id, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "Semantic Scholar"

    if results is None and source in ("openalex", "auto"):
        if source == "auto":
            print("\nSemantic Scholar failed, switching to OpenAlex...")
        else:
            print(f"Querying OpenAlex: ArXiv:{clean_id}")
        ret = _fetch_citations_openalex(clean_id, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "OpenAlex"

    if not results:
        print(f"\nNo citations found for arXiv:{clean_id}")
        return

    start_num = offset + 1
    end_num = offset + len(results)
    print(f"\nSource: {used_source}")
    print(f"Showing citations #{start_num}-{end_num}:\n")

    if used_source == "Semantic Scholar":
        _print_citations_s2(results, start_num)
    else:
        _print_citations_openalex(results, start_num)


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
        choices=["auto", "s2", "openalex", "arxiv"],
        default="auto",
        help="数据源: auto=自动(S2→OpenAlex→arXiv), s2, openalex, arxiv (默认 auto)",
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
    search_parser.set_defaults(func=cmd_search)

    info_parser = subparsers.add_parser("info", help="获取论文信息（不下载全文）")
    info_parser.add_argument("arxiv_id", help="arXiv ID")
    info_parser.set_defaults(func=cmd_info)

    bib_parser = subparsers.add_parser("bib", help="生成 BibTeX 引用")
    bib_parser.add_argument("arxiv_id", help="arXiv ID")
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

    tex_parser = subparsers.add_parser("tex", help="下载 LaTeX 源文件并解压")
    tex_parser.add_argument("arxiv_id", help="arXiv ID")
    tex_parser.set_defaults(func=cmd_tex)

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
