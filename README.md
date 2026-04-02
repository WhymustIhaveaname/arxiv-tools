# Arxiv Tools

给 agent 用的文章搜索工具。不局限于 arXiv，叫 arxiv_tools 只是因为我们从 arXiv 开始的。

## 目标

计划支持的功能：

- **搜索文章** — 给关键词搜索，支持 arXiv 原生、Semantic Scholar 和 OpenAlex 三个源
- **高级搜索** — 搜索支持各种过滤条件（年份、作者、分类等）
- **下载源文件** — 下载 tex 源文件并返回目录结构；tex 下载失败时退而下载 PDF 并转 txt
- **引用查询** — 查一篇文章引用了哪些文章，两种方式：1) 解析 tex 中的 bib 2) 通过 S2 或 OpenAlex API
- **被引反查** — 查哪些文章引用了这篇文章
- **BibTeX 规范化** — shortname 和 bibtex 都有数据库，防止 LLM 出现幻觉
- **本地缓存** — tex 不重复下载（除非版本更新）
- **其他文献调研工具** — 文献调研中需要用到的周边功能
- **Agent 友好接口** — 言简意赅地写明白每个工具能干什么

Q&A:
- 为什么不下载 PDF？下载 PDF 公式解析是大问题
- tex 需要拼接吗？不需要，拼接会挑战 LLM 上下文极限（文章都很长），就返回目录让 agent 自己看

## 安装与配置（多用户共享）

本插件支持多用户共享同一台机器上的缓存和 API key。

### 1. 安装插件

其他用户在 Claude Code 中执行：

```
/plugin marketplace add /path/to/arxiv-tools
/plugin install arxiv-tools
```

### 2. 共享缓存目录

默认缓存在脚本同目录下的 `.arxiv/`，各用户独立。要共享缓存，所有用户设同一个环境变量指向同一个目录：

```bash
# 加到 ~/.bashrc 或 ~/.zshrc
export ARXIV_CACHE_DIR="/shared/arxiv-cache"
```

缓存目录存放：下载的 tex/PDF 源文件、SQLite 元数据数据库、限流锁文件、API key（`.env`）。共享后只要有一个人下载过某篇论文，其他人直接命中缓存。

## 技术细节

- **本地是不是有一个 sql 数据库协调用的?** 是，`paper_cache.py` 用 SQLite，数据库文件在 `.arxiv/paper_cache.db`。`papers` 表存论文元数据（标题、作者、摘要、日期、分类、PDF URL）和 BibTeX，用 `arxiv_id` 做主键。`get_paper_info()` 先查缓存，命中就不再调 arXiv API。
- **tex 会存在哪里? 怎么检查这个 tex 有没有下载过的?** tex 源文件解压到 `.arxiv/{id}` 或 `.arxiv/{id}_{标题}` 目录。下载前检查：先精确匹配 `{id}` 目录是否存在，再 glob 匹配 `{id}_*` 是否有已重命名的目录，命中任一则直接返回已有路径，不重复下载。
- **arxiv, S2 和 OpenAlex 的 ratelimit 是怎么实现的? 如果还是被 limit 了, 会输出什么?** `RateLimiter` 类用 `.ratelimit.lock`（json5 格式 + `fcntl` 文件锁）记录每个服务的上次请求时间，跨进程生效。间隔：arxiv 3.0s，S2 2.0s；OpenAlex 没有本地限流。如果服务端仍返回 429，`_request_with_retry` 做指数退避重试（最多 2 次），输出 `"HTTP 429，{wait}s 后重试..."`（stderr）。如果 `RateLimiter.wait()` 连续 5 次拿不到窗口，抛 `RuntimeError`。

