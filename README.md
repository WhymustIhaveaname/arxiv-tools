# Arxiv Tools

给 agent 用的跨学科文献工具。覆盖 arXiv / PubMed / PubMed Central / Europe PMC / ChemRxiv / Semantic Scholar / OpenAlex / Crossref / CORE。叫 arxiv_tools 只是因为我们从 arXiv 开始。

## 子命令

| 命令 | 功能 |
|------|------|
| `search <query>` | 默认 `--source all` **多源并行 + DOI/ID 去重 + 字段合并**, 单源用 `--source <name>` |
| `info <id>` | 论文元数据 (多源并行 lookup, 字段合并) |
| `bib <id>` | BibTeX (有 DOI 走 Crossref 内容协商, 拿权威 `@article`) |
| `cited <id>` | 反向引用: 谁引了它 (S2 优先 → OpenAlex) |
| `references <id>` | 正向引用: 它引了谁 (S2 优先 → PubMed ELink 兜底) |
| `similar <PMID>` | 相似论文 (NCBI ELink `pubmed_pubmed`, 仅 PMID) |
| `annotations <pmid\|pmcid>` | Europe PMC 文本挖掘实体 (基因/疾病/化学物质 + ontology URI) |
| `tex <arxiv_id>` | arXiv LaTeX 源码 |
| `infotex <arxiv_id>` | 先打印 `info`, 再跑 `tex`; `info` 若发现本地 tex cache 会显示路径 |
| `fulltext <id>` | 分层全文获取链 (见下) |
| `fulltext-batch <ids.txt>` | 批量跑 fulltext, 失败的写到 manifest TSV |
| `fulltext-sweep <file.md> [...]` | 扫文档里的 arXiv/DOI/PMC ID, 批量补齐全文 |
| `fulltext-import <dir>` | 扫目录导入手动下载的 PDF, 按 manifest/文件名匹配 |

`<id>` 都自动识别: 接受 arXiv ID / PMID / PMC ID / DOI 任一形式。

### Search 额外 flag

- `--domain {bio,med,chem,cs,phys}`: 领域快捷, 限定相关源 + S2 fields_of_study
- `--snippet`: S2 改用 `/snippet/search` (按全文片段命中排序), 适合查技术术语
- `--year` / `--open-access` / `--min-citations` / `--venue` / `--fields-of-study` / `--pub-types`
- `--max` / `--offset` (PubMed/EuropePMC 翻页)
- `--bulk` / `--sort` / `--token` (S2 bulk 搜索, 最多 1000 条)

### OpenAlex 状态

OpenAlex adapter 代码保留, 但默认通过 `OPENALEX_ENABLED=False` 禁用网络调用:
上游曾出现 arXiv 记录 metadata 污染。默认 `search --source all` 会跳过
OpenAlex; 显式 `--source openalex` 会退出并提示改用 S2/arXiv。将来确认
上游干净后只需在 `lit/config.py` 打开 flag。

### Audit log

每次 CLI 调用都会向 `$ARXIV_CACHE_DIR/.audit.jsonl` 追加一行 JSONL:
`ts/user/cmd/arg/flags/source_hit/cached_before/elapsed_s/exit_code`。
写 audit 失败会静默忽略, 不影响工具主流程。

### Fulltext 分层链

按 ID 类型分发:

- **arxiv**: LaTeX 源码 → PDF fallback
- **pmcid**: JATS XML → BioC JSON → PDF (LLM 友好度递降)
- **pmid 有 PMC 副本**: PMC 链
- **pmid 无 PMC**: preprint 反查 → OA mirror → 失败时打印 landing URL, 让带 Playwright MCP 的 agent 接管
- **doi (chemrxiv/biorxiv/通用)**: 各自 layered chain (PMC 副本 / preprint 反查 → OA mirror → 失败时打印 landing URL, agent 接管)
- **`--from-file <path>`**: 任何 ID 都能用; 浏览器手动下后传给我们入库

`fulltext-batch` 跑完会把失败的写到 TSV manifest, 用户浏览器下载后 `fulltext-import` 批量入库。

## 安装与配置 (多用户共享)

### 1. 安装插件

Claude Code 中:

```
/plugin marketplace add /path/to/arxiv-tools
/plugin install arxiv-tools
```

更新: `claude plugin update arxiv-tools@arxiv-tools` 或 `/plugin` 交互界面。

### 2. 共享缓存目录

默认缓存在脚本同目录下的 `.arxiv/`, 各用户独立。多用户共享:

```bash
# ~/.bashrc 或 ~/.zshrc
export ARXIV_CACHE_DIR="/shared/arxiv-cache"
```

缓存里有: 下载的 tex/PDF 源文件 + SQLite 元数据 (按 arxiv:/pmid:/doi:/pmcid: 多 key 索引同一篇) + 跨进程限流锁 + API key (`.env`)。共享后任一用户下载过的论文, 其他人直接命中。

### 3. API Keys

在 `$ARXIV_CACHE_DIR/.env`:

```bash
S2_API_KEY=xxx                # 推荐, 高限速
OPENALEX_API_KEY=xxx          # 2026.02 后强制
PUBMED_API_KEY=xxx            # 推荐, 3→10 req/s
CORE_API_KEY=xxx              # 推荐, 给 OA 长尾补 ~10%
CONTACT_EMAIL=you@example.com # Crossref/Unpaywall polite pool 必需
```

申请步骤详见 `~/notes/universal-literature-tool-plan.md` §5。

## 架构概览

- `arxiv_tool.py` — CLI 入口 + 公开接口契约 (re-export); 业务逻辑在 `lit/` 包
- `lit/aggregator.py` — 多源并行: `aggregate_search` (search) + `aggregate_lookup` (info), DOI/arXiv-ID/PMID/PMCID 去重 + 字段合并
- `lit/preprint_lookup.py` — OpenAlex `locations` + S2 `externalIds` 反查 preprint 版本
- `lit/oa_mirror.py` — OA mirror 聚合: Unpaywall + OpenAlex + CORE + Crossref TDM
- `lit/fetch.py` — `Layer` + `walk_layers` 通用分层调度
- `lit/pdf.py` — PDF magic 校验 + PyMuPDF 文本提取 + save/ingest
- `lit/batch.py` — `run_batch` (manifest TSV) + `run_import`
- `lit/display.py` — 跨源 print
- `lit/sources/` — 7 个适配器 (arxiv_api / s2 / openalex / pubmed / europepmc / chemrxiv / ncbi_bioc)
- `paper_cache.py` — SQLite, PK 带前缀 (`arxiv:` / `pmid:` / `doi:` / `pmcid:`), cross-ref 别名行

## 设计原则

1. **多源聚合优先**: search/info 都并行多源 + 字段合并; 任一源 down 不阻塞其他
2. **分层 fulltext, 显式 bool 控制流**: `_try_*_to_disk` 返回 bool, `_fetch_*_to_disk` 是薄壳; **不用 `except SystemExit`**
3. **PDF magic 必须验**: 每个下载点都过 `lit.pdf.is_pdf_bytes()`, 否则 Cloudflare 挑战 HTML 会被误存为 .pdf
4. **Source-specific sanitization 在源 wrapper 里做**: 例如 OpenAlex 对 arXiv 论文返回的合成 DOI / 错误年份, 在 `_fetch_paper_openalex` 和 `_hits_from_openalex` 都已硬拨
5. **Public 接口契约 100% 保留**: 所有 `arxiv_tool._fetch_*_to_disk` / `_search_*` / `_normalize_*` / `_print_*` / `OUTPUT_DIR` 都是 re-export, 重构内部不动这些名字
6. **缓存 best-effort**: cache 写失败不阻塞返回结果

## 测试

```bash
uv run --with-requirements requirements-dev.txt -m pytest tests/ -v
uv run --with-requirements requirements-dev.txt -m pytest tests/ -v -m "not network"
```

当前验证: 212 个测试通过, 6 个 OpenAlex live tests 因默认禁用而 skip。

## 详细文档

- 用户手册 (slash command): `skills/arxiv/SKILL.md`
- 开发笔记 (架构、API 详情、踩过的坑、待做): `~/notes/universal-literature-tool-plan.md`
