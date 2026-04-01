# 已有工具调研

调研与 arxiv_tools 功能相关的已有工具，分析各自能力边界和互补关系。

## Zotero

桌面端文献管理工具，开源，插件生态丰富。

**能做的：**
- 浏览器插件一键导入论文元数据和 PDF
- 本地文献库管理、分类、标签、全文搜索
- 多设备同步（WebDAV / Zotero Storage）
- 与 Word/LibreOffice 集成，插入引用和生成参考文献

**不能做的：**
- 没有 CLI，无法被脚本或 agent 调用
- 不能下载 LaTeX 源文件
- 被引反查需要额外插件，能力有限
- 需要安装 GUI 应用

### Better BibTeX 插件

Zotero 的 BibTeX 管理插件。

- 自动生成和维护 citation key
- 实时导出 `.bib` 文件，与 LaTeX 工作流集成
- 比我们的 `bib` 命令更强大（支持自定义 key 格式、pin key、去重等）

### Zotero MCP Server

已有多个实现，将 Zotero 库通过 MCP 协议暴露给 AI 助手：

| 项目 | 特点 |
|------|------|
| [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) | 最热门（759+ stars），功能最全：语义搜索、标注提取、被引统计、撤稿提醒 |
| [cookjohn/zotero-mcp](https://github.com/cookjohn/zotero-mcp) | Zotero 插件形式，内置 MCP server（Streamable HTTP），无需单独部署 |
| [kujenga/zotero-mcp](https://github.com/kujenga/zotero-mcp) | 轻量 Python 实现，支持本地 API 和 Web API |
| [kaliaboi/mcp-zotero](https://github.com/kaliaboi/mcp-zotero) | Node.js 实现，面向 Claude Desktop |

**能做的：**
- 在 Claude/ChatGPT 中搜索、浏览 Zotero 库
- 提取 PDF 标注和笔记
- 语义搜索（基于嵌入向量）
- 生成引用、管理文献

**不能做的：**
- 依赖 Zotero 库中已有的论文，不能直接从 arXiv 搜索/下载新论文
- 不能下载 LaTeX 源文件
- 不能独立做被引反查（依赖 Zotero 已收录的数据）

## Semantic Scholar API

学术搜索引擎 API，本项目的 `search` 和 `cited` 命令已集成。

**能做的：**
- 论文搜索（语义理解，不只是关键词匹配）
- 被引/引用关系查询
- 批量查询论文元数据
- 提供影响力指标（citation count、influential citations）

**不能做的：**
- 不提供全文或源文件下载
- 不生成 BibTeX（需要自行拼装）
- 有速率限制（无 API key 时 100 req/5min）
- 元数据偶尔不完整或滞后

## OpenAlex API

开放学术数据 API，本项目作为 Semantic Scholar 的备选已集成。

**能做的：**
- 完全免费开放，无需 API key（有 key 进 polite pool）
- 论文、作者、机构、期刊等实体的结构化查询
- 被引/引用关系
- 覆盖面广（约 2.5 亿篇）

**不能做的：**
- 搜索质量不如 Semantic Scholar（关键词匹配为主）
- 不提供全文或源文件
- 不生成 BibTeX
- 数据更新有延迟

## arxiv API

arXiv 官方 API，本项目通过 `arxiv` Python 库调用。

**能做的：**
- 按关键词、作者、分类搜索论文
- 获取元数据（标题、摘要、作者、分类等）
- 下载 PDF 和 LaTeX 源文件
- 数据权威，与 arXiv 网站同步

**不能做的：**
- 速率限制严格（建议 3 秒间隔）
- 无被引/引用关系数据
- 搜索质量一般（不支持语义搜索）
- 仅覆盖 arXiv 上的论文

## 与 arxiv_tools 的关系

```
                    搜索新论文    下载源文件    被引反查    文献管理    Agent/CLI 集成
arxiv_tools            Y            Y           Y          -            Y
Zotero + MCP           -            -           ~          Y            Y (via MCP)
Semantic Scholar        Y            -           Y          -            -
OpenAlex               Y            -           Y          -            -
arXiv API              Y            Y           -          -            -
```

**结论：** arxiv_tools 的核心价值在于将 arXiv 搜索/下载、被引反查、BibTeX 生成整合为一个轻量 CLI，可直接被 LLM agent 调用。Zotero + MCP 更擅长已有文献的管理和交互，两者互补而非替代。
