---
name: arxiv
description: |
  arXiv 论文搜索与分析。当用户提到 arXiv ID（如 2401.12345）、需要搜索学术论文、
  获取论文全文/摘要、生成 BibTeX 引用、查看被引论文时使用。
allowed-tools:
  - Bash
  - Read
  - Write
  - Grep
  - Glob
---

# arXiv 论文搜索与分析

根据用户输入 `$ARGUMENTS` 判断需要执行哪个子命令。如果用户只给了关键词没说具体要干嘛，默认执行 search。如果用户给了 arXiv ID 没说具体要干嘛，默认执行 info。

## 运行方式

使用插件目录下的 venv 运行：

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" <子命令>
```

## 子命令

### search — 搜索论文

搜索默认走 Semantic Scholar → OpenAlex → arXiv 三级 fallback。

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词"
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词" --max 10
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" search "关键词" --source s2|openalex|arxiv
```

### info — 获取论文信息（不下载）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" info <arXiv ID>
```

### tex — 下载 LaTeX 源文件（获取全文首选）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" tex <arXiv ID>
```

下载源文件并解压到 `arxiv/{arxiv_id}_{title}/`，保留完整 LaTeX 格式和图片。

### fetch — 下载 PDF 并提取文本（tex 失败时的备选）

```bash
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fetch <arXiv ID>
uv run "${CLAUDE_PLUGIN_ROOT}/arxiv_tool.py" fetch <arXiv ID> -o ./my_papers
```

保存到插件目录下的 `arxiv/`，生成 `{id}.pdf` 和 `{id}.txt`。

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

### tex 优先，fetch 备选

获取论文全文时**必须优先用 tex**。tex 拿到原生 LaTeX 源码，格式完整；fetch 依赖 PDF 转文本，复杂排版容易丢信息或错位。仅当 tex 失败（如论文未提供源文件）时才用 fetch。

### 有 tex 就不用 bib

如果已经通过 `tex` 下载了源文件，论文的参考文献就在本地 `references.bib`（或 `.bbl` 文件）里，直接用 Read 工具读取，**不要再调 `bib` 命令**。同时需要 tex 和 bib 时，先执行 tex，然后从下载的源文件中读取引用信息即可。`bib` 命令走 arXiv API 且容易被限流，仅在不需要 tex 且没有已下载源文件时才用。

### 限流注意

- **arXiv API**（info、bib 依赖）限流严格。连续调用多个依赖 arXiv API 的命令时，每次间隔至少 5 秒。不要在 search 后立刻跑 info/bib。
- **Semantic Scholar**（search、cited 依赖）有 key 时 1 req/s，脚本设了 2s 间隔。连续查多篇 cited 时注意间隔。
- **fetch 和 tex** 直接下载文件，不走 arXiv API，限流较宽松。

### 常用 arXiv 分类

| 分类 | 领域 |
|------|------|
| cs.AI | 人工智能 |
| cs.LG | 机器学习 |
| cs.CV | 计算机视觉 |
| cs.CL | 计算语言学/NLP |
| physics.comp-ph | 计算物理 |
| math.NA | 数值分析 |
| stat.ML | 统计机器学习 |
