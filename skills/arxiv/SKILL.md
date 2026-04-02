---
name: arxiv
description: >
  This skill should be used when the user asks to "search for papers",
  "get paper info", "download LaTeX source", "generate BibTeX citation",
  "find citing papers", or provides an arXiv ID or arXiv URL.
---

# arXiv 论文搜索与分析

Five subcommands available: search (find papers), info (metadata), tex (download full text), bib (BibTeX), cited (reverse citation lookup). When user provides only keywords, default to search. When user provides only an arXiv ID, default to info.

## 运行方式

脚本依赖声明在文件头部的 inline metadata 中，请你自行负责依赖安装。

## 子命令

### search — 搜索论文

搜索默认走 Semantic Scholar → OpenAlex → arXiv 三级 fallback。

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词"
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词" --max 10
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词" --source s2|openalex|arxiv
```

S2 专用过滤参数：

```bash
--year 2024                    # 单年
--year 2020-2024               # 年份范围
--fields-of-study "Computer Science,Physics"
--pub-types "JournalArticle,Conference"
--min-citations 50
--venue "NeurIPS"
--open-access                  # 仅开放获取
```

S2 bulk 搜索（最多 1000 条，支持排序和翻页）：

```bash
--bulk --sort "citationCount:desc"
--bulk --token <上次返回的 token>   # 翻页
```

### info — 获取论文信息（不下载）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <arXiv ID>
```

### tex — 下载论文全文（LaTeX 源文件优先，PDF 自动 fallback）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID> -o ./my_papers
```

下载源文件并解压到 `arxiv/{arxiv_id}_{title}/`，保留完整 LaTeX 格式和图片。若源文件不可用，自动 fallback 到 PDF 下载并提取文本。

### bib — 生成 BibTeX 引用

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID> -o references.bib  # 追加到文件
```

### cited — 被引反查

查看哪些论文引用了某篇论文（S2 首选，OpenAlex 备选）。

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --max 50
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --offset 20         # 翻页
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --source s2|openalex
```

## 重要规则

### 获取全文只用 tex

`tex` 命令会自动处理 fallback：先尝试下载 LaTeX 源文件（格式完整），失败时自动 fallback 到 PDF 下载并提取文本。无需手动选择。

### 有 tex 就不用 bib

如果已经通过 `tex` 下载了源文件，论文的参考文献就在本地 `references.bib`（或 `.bbl` 文件）里，直接用 Read 工具读取，**不要再调 `bib` 命令**。同时需要 tex 和 bib 时，先执行 tex，然后从下载的源文件中读取引用信息即可。`bib` 命令走 arXiv API 且容易被限流，仅在不需要 tex 且没有已下载源文件时才用。

### 限流注意

- **arXiv API**（info、bib 依赖）限流严格。连续调用多个依赖 arXiv API 的命令时，每次间隔至少 5 秒。不要在 search 后立刻跑 info/bib。
- **Semantic Scholar**（search、cited 依赖）有 key 时 1 req/s，脚本设了 2s 间隔。连续查多篇 cited 时注意间隔。
- **tex** 直接下载文件，不走 arXiv API，限流较宽松。
