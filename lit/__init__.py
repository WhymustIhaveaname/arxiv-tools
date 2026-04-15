"""Internal library for arxiv-tools.

Split from the former monolithic arxiv_tool.py. The top-level arxiv_tool.py
remains the CLI entry point and keeps orchestration (cmd_*, get_paper_info)
so existing tests that patch `arxiv_tool.X` continue to work.
"""
