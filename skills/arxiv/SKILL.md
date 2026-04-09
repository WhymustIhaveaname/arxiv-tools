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

### info — 获取论文信息（不下载 tex）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <arXiv ID>
```

### tex — 下载论文全文

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID>
```

下载 tex 源文件并返回下载目录和目录结构，保留完整 LaTeX 格式和图片。若源文件不可用，自动 fallback 到 PDF 下载并提取文本.

### bib — 生成 BibTeX 引用

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" bib <arXiv ID> -o references.bib  # 追加到文件
```

### cited — 被引反查

查看哪些论文引用了某篇论文。

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --max 50
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --offset 20         # 翻页
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" cited <arXiv ID> --source s2|openalex
```

## 数据源 fallback

| 子命令 | fallback 顺序 |
|--------|--------------|
| search | S2 → OpenAlex → arXiv |
| cited | S2 → OpenAlex |
| info / bib | 本地缓存 → OpenAlex → S2 → arXiv |
| tex | 本地缓存 → arXiv |

- search/cited 优先 S2：引用数据更全
- info/bib 优先 OpenAlex：限流最宽松（0.1s/req vs S2 2s vs arXiv 5s）
