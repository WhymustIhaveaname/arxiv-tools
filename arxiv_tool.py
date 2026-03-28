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
"""
arXiv 论文搜索与全文获取工具

功能：
1. search - 搜索论文（关键词、标题、摘要）
2. info - 获取论文信息（标题、作者、摘要等，不下载）
3. bib - 生成 BibTeX 引用（自动生成 citation key）
4. tex - 下载 LaTeX 源文件并解压（失败时自动 fallback 到 PDF 下载）
5. cited - 被引反查（Semantic Scholar 首选，OpenAlex 备选）

使用方法（通过 uv run）：
    uv run arxiv_tool.py search "PINN" --max 5
    uv run arxiv_tool.py info 2401.12345
    uv run arxiv_tool.py bib 2401.12345 -o refs.bib
    uv run arxiv_tool.py tex 2401.12345
    uv run arxiv_tool.py cited 1711.10561 --max 20
    uv run arxiv_tool.py cited 1711.10561 --offset 20  # 翻页
    uv run arxiv_tool.py cited 1711.10561 --source openalex
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import io
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import arxiv
import fitz  # PyMuPDF
import json5
import requests
from dotenv import load_dotenv

from paper_cache import CachedAuthor, CachedPaper, cache_paper, get_cached_bibtex, get_cached_paper

if TYPE_CHECKING:
    from arxiv import Result

# 加载 .env（与脚本同目录），已有环境变量不覆盖
SCRIPT_DIR = Path(__file__).parent
load_dotenv(SCRIPT_DIR / ".env", override=False)

# API Keys（有就用，没有也不影响基本功能）
S2_API_KEY: str | None = os.environ.get("S2_API_KEY")
OPENALEX_API_KEY: str | None = os.environ.get("OPENALEX_API_KEY")
CONTACT_EMAIL: str = os.environ.get("CONTACT_EMAIL", "")

# API 基础 URL
S2_API_BASE = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API_BASE = "https://api.openalex.org"

# HTTP 请求头（arXiv 推荐设置 User-Agent）
_mailto = f" (mailto:{CONTACT_EMAIL})" if CONTACT_EMAIL else ""
HTTP_HEADERS = {
    "User-Agent": f"arxiv-tool/1.0{_mailto}",
}

OUTPUT_DIR = SCRIPT_DIR / "arxiv"


class RateLimiter:
    """跨进程 rate limit 管理，用 json5 lock 文件实现"""

    LOCK_FILE = SCRIPT_DIR / ".ratelimit.lock"
    INTERVALS = {
        "s2": 2.0,  # Semantic Scholar: 1 req/s，用 2s 间隔留余量
        "arxiv": 3.0,  # arXiv 官方 API，限流严格
        "ut": 0.3,  # 测试用
    }

    @classmethod
    def _read(cls) -> dict:
        if not cls.LOCK_FILE.exists():
            return {}
        # try-catch approved: lock 文件可能损坏，自动删除并恢复，不应阻断功能
        try:
            return json5.loads(cls.LOCK_FILE.read_text())
        except ValueError:
            cls.LOCK_FILE.unlink(missing_ok=True)
            return {}

    @classmethod
    def _write(cls, lock: dict) -> None:
        with open(cls.LOCK_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json5.dumps(lock))
            f.flush()
            os.fsync(f.fileno())

    @classmethod
    def available(cls, service: str) -> bool:
        lock = cls._read()
        if service not in lock:
            return True
        return time.time() - lock[service] >= cls.INTERVALS[service]

    @classmethod
    def wait(cls, service: str) -> None:
        interval = cls.INTERVALS[service]
        for _ in range(5):
            lock = cls._read()
            if service not in lock:
                break
            remaining = interval - (time.time() - lock[service])
            if remaining <= 0:
                break
            time.sleep(remaining)
        else:
            raise RuntimeError(
                f"RateLimiter: {service} 连续 5 次未能获得请求窗口，请检查"
            )

    @classmethod
    def record(cls, service: str) -> None:
        lock = cls._read()
        lock[service] = time.time()
        cls._write(lock)


def get_paper_info(arxiv_id: str) -> Result | CachedPaper | None:
    clean_id = extract_arxiv_id(arxiv_id)

    cached = get_cached_paper(clean_id)
    if cached:
        return cached

    RateLimiter.wait("arxiv")
    RateLimiter.record("arxiv")
    client = arxiv.Client()
    search = arxiv.Search(id_list=[clean_id])
    results = list(client.results(search))
    if not results:
        print(f"未找到论文: {clean_id}", file=sys.stderr)
        return None

    paper = results[0]
    cached_paper = CachedPaper(
        title=paper.title,
        authors=[CachedAuthor(a.name) for a in paper.authors],
        summary=paper.summary,
        published=paper.published,
        updated=paper.updated,
        categories=list(paper.categories),
        pdf_url=paper.pdf_url,
    )
    bibtex = generate_bibtex(paper, clean_id)
    cache_paper(clean_id, cached_paper, bibtex)
    return paper


def sanitize_filename(name: str, max_length: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", "_", name)
    if len(name) > max_length:
        name = name[:max_length]
    return name.strip("._")


def extract_arxiv_id(input_str: str) -> str:
    """从输入中提取 arXiv ID

    支持格式：
    - 2401.12345
    - arXiv:2401.12345
    - https://arxiv.org/abs/2401.12345
    - https://arxiv.org/pdf/2401.12345.pdf
    """
    patterns = [
        r"(\d{4}\.\d{4,5}(?:v\d+)?)",  # 新格式: 2401.12345 或 2401.12345v1
        r"([a-z-]+/\d{7})",  # 旧格式: cs/0401001
    ]
    for pattern in patterns:
        match = re.search(pattern, input_str)
        if match:
            return match.group(1)
    return input_str


def search_papers(query: str, max_results: int = 20) -> list[Result]:
    client = arxiv.Client()
    search = arxiv.Search(query=query, max_results=max_results)
    return list(client.results(search))


def _truncate_authors(names: list[str], limit: int = 3) -> str:
    """将作者名列表截断为 'A, B, C...' 格式"""
    result = ", ".join(names[:limit])
    if len(names) > limit:
        result += "..."
    return result


def _normalize_s2_search(results: list[dict]) -> list[dict]:
    out = []
    for paper in results:
        ext_ids = paper["externalIds"] or {}
        arxiv_id = ext_ids.get("ArXiv", "")
        doi = ext_ids.get("DOI", "")
        if arxiv_id:
            id_str = f"arXiv:{arxiv_id}"
        elif doi:
            id_str = f"DOI:{doi}"
        else:
            id_str = ""

        authors = paper["authors"] or []
        author_str = _truncate_authors([a["name"] for a in authors])

        out.append(
            {
                "id": id_str,
                "title": paper["title"],
                "authors": author_str,
                "year": str(paper["year"] or "?"),
                "cited_by": paper["citationCount"],
                "abstract": paper["abstract"],
            }
        )
    return out


def _normalize_openalex_search(results: list[dict]) -> list[dict]:
    out = []
    for work in results:
        authorships = work["authorships"] or []
        author_str = _truncate_authors(
            [a["author"]["display_name"] for a in authorships]
        )

        ids = work["ids"] or {}
        arxiv_str = ""
        for key, val in ids.items():
            if "arxiv" in key.lower() and val:
                arxiv_str = val.replace("https://arxiv.org/abs/", "arXiv:")
                break
        id_str = arxiv_str or ids.get("doi", "") or ids.get("openalex", "")

        out.append(
            {
                "id": id_str,
                "title": work["title"],
                "authors": author_str,
                "year": str(work["publication_year"] or "?"),
                "cited_by": work["cited_by_count"],
                "abstract": None,
            }
        )
    return out


def _normalize_arxiv_search(results: list) -> list[dict]:
    out = []
    for paper in results:
        arxiv_id = paper.entry_id.split("/abs/")[-1]
        author_str = _truncate_authors([a.name for a in paper.authors])

        out.append(
            {
                "id": f"arXiv:{arxiv_id}",
                "title": paper.title,
                "authors": author_str,
                "year": paper.published.strftime("%Y-%m-%d"),
                "cited_by": None,
                "abstract": paper.summary,
            }
        )
    return out


def _print_search_results(results: list[dict]) -> None:
    for i, r in enumerate(results, 1):
        print(f"[{i}] {r['id']}")
        print(f"    标题: {r['title']}")
        print(f"    作者: {r['authors']}")
        cited = f"  被引: {r['cited_by']}" if r["cited_by"] is not None else ""
        print(f"    年份: {r['year']}{cited}")
        if r["abstract"]:
            print(f"    摘要: {r['abstract'].replace(chr(10), ' ')}")
        print()


def cmd_search(args):
    source = args.source

    results = None

    if source in ("s2", "auto"):
        print("搜索 Semantic Scholar...", file=sys.stderr)
        raw = _search_s2(args.query, args.max)
        if raw:
            results = ("Semantic Scholar", _normalize_s2_search(raw))

    if not results and source in ("openalex", "auto"):
        print("搜索 OpenAlex...", file=sys.stderr)
        raw = _search_openalex(args.query, args.max)
        if raw:
            results = ("OpenAlex", _normalize_openalex_search(raw))

    if not results and source in ("arxiv", "auto"):
        if source == "auto":
            print(
                "⚠ Semantic Scholar 和 OpenAlex 均失败，fallback 到 arXiv API。"
                "如果此消息持续出现，请检查 API key 和网络连接。",
                file=sys.stderr,
            )
        print("搜索 arXiv...", file=sys.stderr)
        raw = search_papers(args.query, args.max)
        if raw:
            results = ("arXiv", _normalize_arxiv_search(raw))

    if not results:
        print("所有搜索源均未返回结果")
        return

    source_name, normalized = results
    print(f"\n找到 {len(normalized)} 篇论文 ({source_name}):\n")
    _print_search_results(normalized)


def _fetch_pdf_fallback(arxiv_id: str, output_dir: Path) -> None:
    """tex 失败后的备选：下载 PDF 并提取文本"""
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = extract_arxiv_id(arxiv_id)
    file_id = clean_id.replace("/", "_")
    txt_file = output_dir / f"{file_id}.txt"
    pdf_file = output_dir / f"{file_id}.pdf"

    if txt_file.exists():
        print(f"文件已存在: {txt_file}")
        return

    pdf_url = f"https://arxiv.org/pdf/{clean_id}"
    print(f"下载 PDF: {pdf_url}")
    response = requests.get(pdf_url, headers=HTTP_HEADERS, timeout=60)
    response.raise_for_status()
    pdf_file.write_bytes(response.content)

    try:
        print("提取文本...")
        doc = fitz.open(pdf_file)
        text = "\n".join(page.get_text().strip() for page in doc)
        doc.close()
    except Exception:
        pdf_file.unlink(missing_ok=True)
        raise

    txt_file.write_text(
        f"# arXiv:{clean_id}\n\nURL: https://arxiv.org/abs/{clean_id}\n\n## Full Text\n\n{text}",
        encoding="utf-8",
    )
    print(f"已保存 PDF: {pdf_file}")
    print(f"已保存 TXT: {txt_file}")


def cmd_info(args):
    clean_id = extract_arxiv_id(args.arxiv_id)

    paper = get_paper_info(clean_id)
    if not paper:
        return

    print(f"arXiv ID: {clean_id}")
    print(f"标题: {paper.title}")
    print(f"作者: {', '.join(a.name for a in paper.authors)}")
    print(f"发布日期: {paper.published.strftime('%Y-%m-%d')}")
    print(f"更新日期: {paper.updated.strftime('%Y-%m-%d')}")
    print(f"分类: {', '.join(paper.categories)}")
    print(f"PDF: {paper.pdf_url}")
    print(f"\n摘要:\n{paper.summary}")


# 停用词列表，用于生成 citation key
STOPWORDS = {
    "a",
    "an",
    "the",
    "of",
    "for",
    "and",
    "or",
    "in",
    "on",
    "at",
    "to",
    "with",
    "by",
    "from",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "need",
    "dare",
    "ought",
    "used",
    "via",
    "using",
    "based",
    "towards",
    "toward",
}


def generate_citation_key(paper: Result) -> str:
    """生成 BibTeX citation key

    格式：{第一作者姓小写}{年份}{标题首个实词小写}
    示例：li2025codepde, raissi2017physics
    """
    last_name = re.sub(r"[^a-z]", "", paper.authors[0].name.split()[-1].lower())
    year = paper.published.year

    title_words = re.findall(r"[a-zA-Z]+", paper.title)
    first_word = ""
    for word in title_words:
        if word.lower() not in STOPWORDS:
            first_word = word.lower()
            break

    return f"{last_name}{year}{first_word}"


def generate_bibtex(paper: Result, arxiv_id: str) -> str:
    """生成 arXiv 标准格式的 BibTeX 条目"""
    citation_key = generate_citation_key(paper)
    authors = " and ".join(a.name for a in paper.authors)
    clean_id = re.sub(r"v\d+$", "", arxiv_id)

    bibtex = f"""@misc{{{citation_key},
      title={{{paper.title}}},
      author={{{authors}}},
      year={{{paper.published.year}}},
      eprint={{{clean_id}}},
      archivePrefix={{arXiv}},
      primaryClass={{{paper.categories[0]}}},
      url={{https://arxiv.org/abs/{clean_id}}},
}}"""
    return bibtex


def cmd_bib(args):
    clean_id = extract_arxiv_id(args.arxiv_id)

    # 确保缓存中有数据
    paper = get_paper_info(clean_id)
    if not paper:
        sys.exit(1)

    bibtex = get_cached_bibtex(clean_id) or generate_bibtex(paper, clean_id)

    if args.output:
        output_path = Path(args.output)
        mode = "a" if output_path.exists() else "w"
        with open(output_path, mode, encoding="utf-8") as f:
            if mode == "a" and output_path.stat().st_size > 0:
                f.write("\n\n")
            f.write(bibtex)
            f.write("\n")
        print(f"已{'追加' if mode == 'a' else '写入'}到: {output_path}")
    else:
        print(bibtex)


def print_tree(
    directory: Path, prefix: str = "", max_depth: int = 3, current_depth: int = 0
) -> list[str]:
    lines = []
    if current_depth >= max_depth:
        return lines

    items = sorted(directory.iterdir(), key=lambda x: (x.is_file(), x.name))
    for i, item in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{item.name}")

        if item.is_dir():
            extension = "    " if is_last else "│   "
            lines.extend(
                print_tree(item, prefix + extension, max_depth, current_depth + 1)
            )

    return lines


def fetch_tex_source(arxiv_id: str, output_dir: Path) -> Path | None:
    """下载 arXiv LaTeX 源文件并解压

    不调用 API，直接从 e-print 下载源文件。目录名使用 arXiv ID，
    下载后尝试从 tex 文件中提取标题来补充目录名。

    Args:
        arxiv_id: arXiv ID
        output_dir: 输出目录

    Returns:
        解压后的目录路径，失败返回 None
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = extract_arxiv_id(arxiv_id)
    # 移除版本号用于目录名
    dir_id = re.sub(r"v\d+$", "", clean_id).replace("/", "_")
    target_dir = output_dir / dir_id

    if target_dir.exists():
        print(f"目录已存在: {target_dir}")
        return target_dir
    existing = [p for p in output_dir.glob(f"{dir_id}_*") if p.is_dir()]
    if existing:
        print(f"目录已存在: {existing[0]}")
        return existing[0]

    source_url = f"https://arxiv.org/e-print/{clean_id}"
    print(f"下载源文件: {source_url}")

    try:
        response = requests.get(source_url, headers=HTTP_HEADERS, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"下载失败: {e}", file=sys.stderr)
        return None

    content = response.content

    target_dir.mkdir(parents=True, exist_ok=True)
    print("解压源文件...")
    try:
        _extract_source(content, target_dir)
    except Exception as e:
        print(f"解压失败: {e}", file=sys.stderr)
        shutil.rmtree(target_dir, ignore_errors=True)
        return None

    new_dir = _try_rename_with_title(target_dir, dir_id, output_dir)
    if new_dir:
        target_dir = new_dir

    print(f"已保存到: {target_dir}")
    return target_dir


def _extract_source(content: bytes, target_dir: Path) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tar:
            tar.extractall(target_dir, filter="data")
            print("解压为 tar.gz 格式")
            return
    except tarfile.ReadError:
        pass

    try:
        decompressed = gzip.decompress(content)
        # 解压后可能是 tar
        try:
            with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r") as tar:
                tar.extractall(target_dir, filter="data")
                print("解压为 gzip+tar 格式")
                return
        except tarfile.ReadError:
            # 纯 gzip 压缩的单个文件
            tex_file = target_dir / "main.tex"
            tex_file.write_bytes(decompressed)
            print("解压为单个 tex 文件")
            return
    except gzip.BadGzipFile:
        pass

    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r") as tar:
            tar.extractall(target_dir, filter="data")
            print("解压为 tar 格式")
            return
    except tarfile.ReadError:
        pass

    tex_file = target_dir / "main.tex"
    tex_file.write_bytes(content)
    print("保存为单个 tex 文件（无压缩）")


def _extract_braced_arg(text: str, start: int) -> str | None:
    """提取 text[start] 处 '{' 对应的完整花括号内容，支持嵌套"""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
    return None


def _strip_tex_comments(content: str) -> str:
    """移除 TeX 注释（% 开头的整行，以及行内未转义的 %）"""
    result = []
    for line in content.split("\n"):
        if line.lstrip().startswith("%"):
            continue
        result.append(re.sub(r"(?<!\\)%.*$", "", line))
    return "\n".join(result)


def _try_rename_with_title(
    target_dir: Path, dir_id: str, output_dir: Path
) -> Path | None:
    tex_files = list(target_dir.glob("*.tex"))
    if not tex_files:
        return None

    main_tex = next((f for f in tex_files if f.name == "main.tex"), tex_files[0])

    content = main_tex.read_text(encoding="utf-8", errors="ignore")
    content = _strip_tex_comments(content)
    match = re.search(r"\\title\s*\{", content)
    if not match:
        return None

    raw_title = _extract_braced_arg(content, match.end() - 1)
    if not raw_title:
        return None

    raw_title = re.sub(r"\\\\", " ", raw_title)
    raw_title = re.sub(r"\\[a-zA-Z]+\s*(\{[^}]*\})?", " ", raw_title)
    raw_title = re.sub(r"[{}]", "", raw_title)
    raw_title = re.sub(r"\s+", " ", raw_title).strip()
    if not raw_title:
        return None

    safe_title = sanitize_filename(raw_title, max_length=40)
    new_dir = output_dir / f"{dir_id}_{safe_title}"
    if new_dir.exists():
        return None

    target_dir.rename(new_dir)
    print(f"从 tex 提取标题，目录重命名为: {new_dir.name}")
    return new_dir


def _s2_headers() -> dict[str, str]:
    if S2_API_KEY:
        return {**HTTP_HEADERS, "x-api-key": S2_API_KEY}
    return HTTP_HEADERS


def _search_s2(query: str, max_results: int = 10) -> list[dict] | None:
    """通过 Semantic Scholar 搜索论文

    Returns:
        论文列表 [{"title", "year", "authors", "externalIds", "citationCount", "abstract"}]，
        失败返回 None
    """
    RateLimiter.wait("s2")
    url = f"{S2_API_BASE}/paper/search"
    try:
        resp = requests.get(
            url,
            params={
                "query": query,
                "limit": min(max_results, 100),
                "fields": "title,year,authors,externalIds,citationCount,abstract",
            },
            headers=_s2_headers(),
            timeout=30,
        )
        RateLimiter.record("s2")
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar 搜索失败: {e}", file=sys.stderr)
        return None

    if not data.get("data"):
        return None
    return data["data"][:max_results]


def _search_openalex(query: str, max_results: int = 10) -> list[dict] | None:
    """通过 OpenAlex 搜索论文

    Returns:
        论文列表 [{"title", "publication_year", "authorships", "cited_by_count", "ids", ...}]，
        失败返回 None
    """
    url = f"{OPENALEX_API_BASE}/works"
    try:
        resp = requests.get(
            url,
            params=_openalex_params(
                search=query,
                select="id,title,authorships,publication_year,cited_by_count,ids",
                per_page=str(min(max_results, 200)),
                sort="relevance_score:desc",
            ),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"OpenAlex 搜索失败: {e}", file=sys.stderr)
        return None

    if not data.get("results"):
        return None
    return data["results"][:max_results]


def _fetch_citations_s2(
    arxiv_id: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """从 Semantic Scholar 获取引用该论文的论文列表

    Returns:
        (引用论文列表, 总被引次数)，失败返回 None
    """
    RateLimiter.wait("s2")
    info_url = f"{S2_API_BASE}/paper/ArXiv:{arxiv_id}"
    try:
        resp = requests.get(
            info_url,
            params={"fields": "title,citationCount"},
            headers=_s2_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        paper_info = resp.json()
        print(f"论文: {paper_info['title']}")
        print(f"总被引次数: {paper_info['citationCount']}")
    except requests.RequestException as e:
        print(f"Semantic Scholar 查询失败: {e}", file=sys.stderr)
        return None

    RateLimiter.wait("s2")
    citations_url = f"{S2_API_BASE}/paper/ArXiv:{arxiv_id}/citations"
    try:
        resp = requests.get(
            citations_url,
            params={
                "fields": "title,year,externalIds,citationCount,authors",
                "offset": offset,
                "limit": min(max_results, 1000),
            },
            headers=_s2_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"Semantic Scholar 引用列表获取失败: {e}", file=sys.stderr)
        return None

    results = [
        item["citingPaper"] for item in data["data"] if item["citingPaper"]["title"]
    ]
    return results[:max_results], paper_info["citationCount"]


def _openalex_params(**extra) -> dict[str, str]:
    if OPENALEX_API_KEY:
        extra["api_key"] = OPENALEX_API_KEY
    else:
        extra["mailto"] = CONTACT_EMAIL
    return extra


def _resolve_openalex_id(arxiv_id: str) -> tuple[str, str, int] | None:
    """通过 arXiv DOI 查找 OpenAlex work ID

    Returns:
        (openalex_work_id, 论文标题, 被引次数)，失败返回 None
    """
    doi = f"10.48550/arXiv.{arxiv_id}"
    url = f"{OPENALEX_API_BASE}/works/doi:{doi}"
    try:
        resp = requests.get(url, params=_openalex_params(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        openalex_id = data["id"].split("/")[-1]  # "https://openalex.org/W123" -> "W123"
        return openalex_id, data["title"], data["cited_by_count"]
    except requests.RequestException:
        return None


def _fetch_citations_openalex(
    arxiv_id: str, max_results: int, offset: int = 0
) -> tuple[list[dict], int] | None:
    """从 OpenAlex 获取引用该论文的论文列表

    Returns:
        (引用论文列表, 总被引次数)，失败返回 None
    """
    resolved = _resolve_openalex_id(arxiv_id)
    if not resolved:
        print("OpenAlex: 未找到该论文", file=sys.stderr)
        return None

    work_id, title, total_citations = resolved
    print(f"论文: {title}")
    print(f"总被引次数: {total_citations}")

    # OpenAlex 用 page 分页，page 从 1 开始
    per_page = min(max_results, 200)
    page = (offset // per_page) + 1

    url = f"{OPENALEX_API_BASE}/works"
    try:
        resp = requests.get(
            url,
            params=_openalex_params(
                filter=f"cites:{work_id}",
                select="id,title,authorships,publication_year,cited_by_count",
                per_page=str(per_page),
                page=str(page),
                sort="cited_by_count:desc",
            ),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"OpenAlex 引用列表获取失败: {e}", file=sys.stderr)
        return None

    return data["results"][:max_results], total_citations


def _print_citations_s2(results: list[dict], start: int = 1) -> None:
    for i, paper in enumerate(results, start):
        ext_ids = paper["externalIds"] or {}
        arxiv_ext = ext_ids.get("ArXiv")
        arxiv_str = f"  arXiv:{arxiv_ext}" if arxiv_ext else ""

        authors = paper["authors"] or []
        author_str = _truncate_authors([a["name"] for a in authors])

        print(f"[{i}] {paper['title']}")
        print(f"    作者: {author_str}")
        print(
            f"    年份: {paper['year'] or '?'}  被引: {paper['citationCount']}{arxiv_str}"
        )
        print()


def _print_citations_openalex(results: list[dict], start: int = 1) -> None:
    for i, work in enumerate(results, start):
        authorships = work["authorships"] or []
        author_str = _truncate_authors(
            [a["author"]["display_name"] for a in authorships]
        )

        print(f"[{i}] {work['title']}")
        print(f"    作者: {author_str}")
        print(
            f"    年份: {work['publication_year'] or '?'}  被引: {work['cited_by_count']}"
        )
        print()


def cmd_cited(args):
    clean_id = extract_arxiv_id(args.arxiv_id)
    source = args.source
    offset = args.offset
    results = None
    used_source = ""

    if source in ("s2", "auto"):
        print(f"查询 Semantic Scholar: ArXiv:{clean_id}")
        ret = _fetch_citations_s2(clean_id, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "Semantic Scholar"

    if results is None and source in ("openalex", "auto"):
        if source == "auto":
            print("\nSemantic Scholar 失败，切换到 OpenAlex...")
        else:
            print(f"查询 OpenAlex: ArXiv:{clean_id}")
        ret = _fetch_citations_openalex(clean_id, args.max, offset)
        if ret is not None:
            results, _total = ret
            used_source = "OpenAlex"

    if not results:
        print(f"\n未找到引用 arXiv:{clean_id} 的论文")
        return

    start_num = offset + 1
    end_num = offset + len(results)
    print(f"\n数据源: {used_source}")
    print(f"显示第 {start_num}-{end_num} 篇引用论文:\n")

    if used_source == "Semantic Scholar":
        _print_citations_s2(results, start_num)
    else:
        _print_citations_openalex(results, start_num)


def cmd_tex(args):
    output_dir = Path(args.output) if args.output else OUTPUT_DIR
    result = fetch_tex_source(args.arxiv_id, output_dir)
    if result:
        print("\n目录结构:")
        print(result.name)
        tree_lines = print_tree(result)
        for line in tree_lines:
            print(line)
    else:
        print("\ntex 下载失败，fallback 到 PDF 下载...", file=sys.stderr)
        _fetch_pdf_fallback(args.arxiv_id, output_dir)


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
    %(prog)s tex 2505.08783 --output ./papers
    %(prog)s cited 1711.10561
    %(prog)s cited 1711.10561 --max 50
    %(prog)s cited 1711.10561 --offset 20          # 第 21-40 条
    %(prog)s cited 1711.10561 --source openalex
""",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # search 子命令
    search_parser = subparsers.add_parser("search", help="搜索论文 (S2→OpenAlex→arXiv)")
    search_parser.add_argument("query", help="搜索关键词")
    search_parser.add_argument(
        "--max", type=int, default=20, help="最大结果数 (默认 20)"
    )
    search_parser.add_argument(
        "--source",
        choices=["auto", "s2", "openalex", "arxiv"],
        default="auto",
        help="数据源: auto=自动(S2→OpenAlex→arXiv), s2, openalex, arxiv (默认 auto)",
    )
    search_parser.set_defaults(func=cmd_search)

    # info 子命令
    info_parser = subparsers.add_parser("info", help="获取论文信息（不下载全文）")
    info_parser.add_argument("arxiv_id", help="arXiv ID")
    info_parser.set_defaults(func=cmd_info)

    # bib 子命令
    bib_parser = subparsers.add_parser("bib", help="生成 BibTeX 引用")
    bib_parser.add_argument("arxiv_id", help="arXiv ID")
    bib_parser.add_argument("--output", "-o", help="输出文件路径（追加写入）")
    bib_parser.set_defaults(func=cmd_bib)

    # cited 子命令
    cited_parser = subparsers.add_parser("cited", help="被引反查：查看哪些论文引用了它")
    cited_parser.add_argument("arxiv_id", help="arXiv ID")
    cited_parser.add_argument(
        "--max", type=int, default=20, help="最大显示条数 (默认 20)"
    )
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

    # tex 子命令
    tex_parser = subparsers.add_parser("tex", help="下载 LaTeX 源文件并解压")
    tex_parser.add_argument("arxiv_id", help="arXiv ID")
    tex_parser.add_argument("--output", "-o", help=f"输出目录 (默认 {OUTPUT_DIR})")
    tex_parser.set_defaults(func=cmd_tex)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
