# 已有工具调研

调研与 arxiv_tools 功能相关的已有工具，分析各自能力边界和互补关系。

两不要: star 太少的 (<10) 的不要, 年久失修(一年内不更新)的不要

## 对比总览

| 工具 | 搜索新论文 | 下载源文件 | 被引反查 | BibTeX | Agent/CLI |
|------|:---:|:---:|:---:|:---:|:---:|
| **arxiv_tools** | Y | Y | Y | Y | Y |
| Zotero (+ MCP) | - | - | ~ | Y | Y (MCP) |
| arxiv-mcp-server | Y | - | - | - | Y (MCP) |
| paper-search-mcp | Y | - | - | - | Y (MCP) |
| papis | - | - | - | Y | Y |
| arxiv2bib | - | - | - | Y | Y |

## Zotero 生态

桌面端文献管理工具，开源，插件生态丰富。本体是 GUI 应用，没有 CLI，但通过 MCP server 可以被 agent 调用。

**Zotero 本体**能做的：浏览器插件一键导入论文元数据和 PDF、本地文献库管理（分类、标签、全文搜索）、多设备同步（WebDAV / Zotero Storage）、与 Word/LibreOffice 集成生成参考文献。**Better BibTeX 插件**可以自动生成和维护 citation key、实时导出 `.bib` 文件，比我们的 `bib` 命令更强大（支持自定义 key 格式、pin key、去重等）。

**Zotero MCP Server** 已有多个实现，将 Zotero 库通过 MCP 协议暴露给 AI 助手：

| 项目 | 特点 |
|------|------|
| [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) | ~1600 stars，功能最全：语义搜索、标注提取、被引统计 |
| [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp) | ~360 stars，Zotero 插件形式，内置 MCP server（Streamable HTTP），无需单独部署 |
| [kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp) | ~140 stars，轻量 Python 实现，支持本地 API 和 Web API |
| [kaliaboi/mcp-zotero](https://github.com/kaliaboi/mcp-zotero) | ~120 stars，TypeScript 实现，面向 Claude Desktop |

**不能做的：** 依赖 Zotero 库中已有的论文，不能直接从 arXiv 搜索/下载新论文；不能下载 LaTeX 源文件；不能独立做被引反查。

## MCP Server

### [arxiv-mcp-server](https://github.com/blazickjp/arxiv-mcp-server)（~2.4k stars）

最热门的 arXiv MCP server，PyPI 可装。四个工具：`search_papers`（关键词+日期+分类过滤）、`download_paper`、`read_paper`（提取为 Markdown）、`list_papers`。

### [paper-search-mcp](https://github.com/openags/paper-search-mcp)（~170 stars）

多源论文搜索 MCP server，支持 20+ 数据源（arXiv、PubMed、bioRxiv、Google Scholar、S2、Crossref、OpenAlex、dblp 等）。两层架构：统一工具层（多源并发搜索+去重）+ 平台连接器层。

## CLI 工具

### [papis](https://github.com/papis/papis)（~1.6k stars）

命令行文献管理工具，功能最接近"终端版 Zotero"。从 DOI/Crossref 自动获取元数据、导出 BibTeX/YAML、用 YAML 存储元数据、支持 git/Dropbox 同步。

### [arxiv2bib](https://github.com/nathangrigg/arxiv2bib)（~57 stars）

给 arXiv ID 生成 BibTeX 条目。但实测表明我们的更好。

## 结论

可以借鉴的：
- **bibsearch 的本地文献库搜索** — 搜索已缓存的论文

Sources:
- [bibsearch](https://github.com/mjpost/bibsearch)
- [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp)
- [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp)
- [kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp)
- [kaliaboi/mcp-zotero](https://github.com/kaliaboi/mcp-zotero)
- [arxiv-mcp-server](https://github.com/blazickjp/arxiv-mcp-server)
- [paper-search-mcp](https://github.com/openags/paper-search-mcp)
- [papis](https://github.com/papis/papis)
- [arxiv2bib](https://github.com/nathangrigg/arxiv2bib)
