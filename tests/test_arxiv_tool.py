#!/usr/bin/env python3
"""arxiv_tool.py 单元测试

测试论文: 1706.03762 — Attention Is All You Need (Vaswani et al., 2017)

运行:
    uv run -m pytest tests/ -v
    uv run -m pytest tests/ -v -m "not network"  # 跳过网络测试
"""

from __future__ import annotations

import argparse
import gzip
import io
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

import arxiv_tool

# ── 测试用论文 ──────────────────────────────────────────────────────

TEST_ID = "1706.03762"
TEST_TITLE = "Attention Is All You Need"
TEST_FIRST_AUTHOR_LAST = "vaswani"
TEST_YEAR = 2017
TEST_CITATION_KEY = "vaswani2017attention"
TEST_PRIMARY_CLASS = "cs.CL"

# ── Mock 对象（纯函数测试用，不走网络） ──────────────────────────────


@dataclass
class MockAuthor:
    name: str


@dataclass
class MockPaper:
    title: str
    authors: list
    published: datetime
    updated: datetime = None
    categories: list = field(default_factory=list)
    summary: str = "Mock abstract."
    pdf_url: str = "https://arxiv.org/pdf/0000.00000"

    def __post_init__(self):
        if self.updated is None:
            self.updated = self.published


MOCK_PAPER = MockPaper(
    title="Attention Is All You Need",
    authors=[
        MockAuthor("Ashish Vaswani"),
        MockAuthor("Noam Shazeer"),
        MockAuthor("Niki Parmar"),
    ],
    published=datetime(2017, 6, 12),
    categories=["cs.CL", "cs.LG"],
    summary="The dominant sequence transduction models are based on complex recurrent or convolutional neural networks...",
    pdf_url=f"https://arxiv.org/pdf/{TEST_ID}",
)

# ── 自定义 marker ──────────────────────────────────────────────────

network = pytest.mark.network


# ════════════════════════════════════════════════════════════════════
#  1. 纯函数测试（无网络）
# ════════════════════════════════════════════════════════════════════


class TestExtractArxivId:
    """arXiv ID 提取"""

    def test_plain_id(self):
        assert arxiv_tool.extract_arxiv_id("1706.03762") == "1706.03762"

    def test_with_version(self):
        assert arxiv_tool.extract_arxiv_id("1706.03762v5") == "1706.03762v5"

    def test_arxiv_prefix(self):
        assert arxiv_tool.extract_arxiv_id("arXiv:1706.03762") == "1706.03762"

    def test_abs_url(self):
        assert arxiv_tool.extract_arxiv_id("https://arxiv.org/abs/1706.03762") == "1706.03762"

    def test_pdf_url(self):
        assert arxiv_tool.extract_arxiv_id("https://arxiv.org/pdf/1706.03762.pdf") == "1706.03762"

    def test_old_format(self):
        assert arxiv_tool.extract_arxiv_id("cs/0401001") == "cs/0401001"

    def test_five_digit_id(self):
        assert arxiv_tool.extract_arxiv_id("2505.08783") == "2505.08783"

    def test_passthrough_garbage(self):
        assert arxiv_tool.extract_arxiv_id("not-an-id") == "not-an-id"


class TestSanitizeFilename:
    """文件名清理"""

    def test_spaces_to_underscores(self):
        assert arxiv_tool.sanitize_filename("Attention Is All You Need") == "Attention_Is_All_You_Need"

    def test_removes_illegal_chars(self):
        result = arxiv_tool.sanitize_filename('Title: "A <B> C/D|E?F*G"')
        for ch in '<>:"/\\|?*':
            assert ch not in result

    def test_max_length(self):
        result = arxiv_tool.sanitize_filename("A" * 200, max_length=80)
        assert len(result) <= 80

    def test_strips_leading_dots_underscores(self):
        result = arxiv_tool.sanitize_filename("._hidden._")
        assert not result.startswith(".")
        assert not result.startswith("_")
        assert not result.endswith(".")
        assert not result.endswith("_")

    def test_collapses_whitespace(self):
        result = arxiv_tool.sanitize_filename("hello   world\t\nfoo")
        assert result == "hello_world_foo"


class TestGenerateCitationKey:
    """BibTeX citation key 生成"""

    def test_attention_paper(self):
        assert arxiv_tool.generate_citation_key(MOCK_PAPER) == TEST_CITATION_KEY

    def test_skips_stopwords(self):
        paper = MockPaper(
            title="The Art of Programming",
            authors=[MockAuthor("Donald Knuth")],
            published=datetime(1968, 1, 1),
        )
        assert arxiv_tool.generate_citation_key(paper) == "knuth1968art"

    def test_all_stopword_title(self):
        """标题全是停用词时，first_word 为空"""
        paper = MockPaper(
            title="The Of And In",
            authors=[MockAuthor("Jane Doe")],
            published=datetime(2024, 1, 1),
        )
        key = arxiv_tool.generate_citation_key(paper)
        assert key == "doe2024"

    def test_hyphenated_last_name(self):
        paper = MockPaper(
            title="Some Result",
            authors=[MockAuthor("Jean-Pierre Serre")],
            published=datetime(2000, 1, 1),
        )
        key = arxiv_tool.generate_citation_key(paper)
        assert key.startswith("serre2000")


class TestGenerateBibtex:
    """BibTeX 条目生成"""

    def test_contains_required_fields(self):
        bib = arxiv_tool.generate_bibtex(MOCK_PAPER, TEST_ID)
        assert f"@misc{{{TEST_CITATION_KEY}," in bib
        assert "title={Attention Is All You Need}" in bib
        assert "Ashish Vaswani" in bib
        assert "year={2017}" in bib
        assert f"eprint={{{TEST_ID}}}" in bib
        assert "archivePrefix={arXiv}" in bib
        assert f"primaryClass={{{TEST_PRIMARY_CLASS}}}" in bib
        assert f"url={{https://arxiv.org/abs/{TEST_ID}}}" in bib

    def test_strips_version_from_eprint(self):
        bib = arxiv_tool.generate_bibtex(MOCK_PAPER, "1706.03762v5")
        assert "eprint={1706.03762}" in bib
        assert "v5" not in bib


class TestPrintTree:
    """目录树生成"""

    def test_basic_tree(self, tmp_path):
        (tmp_path / "a.tex").write_text("hello")
        (tmp_path / "b.bib").write_text("bib")
        sub = tmp_path / "figs"
        sub.mkdir()
        (sub / "fig1.pdf").write_bytes(b"pdf")

        lines = arxiv_tool.print_tree(tmp_path)
        text = "\n".join(lines)
        assert "figs" in text
        assert "a.tex" in text
        assert "fig1.pdf" in text

    def test_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("x")

        lines = arxiv_tool.print_tree(tmp_path, max_depth=2)
        text = "\n".join(lines)
        assert "a" in text
        assert "b" in text
        assert "file.txt" not in text

    def test_empty_dir(self, tmp_path):
        lines = arxiv_tool.print_tree(tmp_path)
        assert lines == []

    def test_dirs_before_files(self, tmp_path):
        """目录排在文件前面"""
        (tmp_path / "z_file.txt").write_text("x")
        (tmp_path / "a_dir").mkdir()
        lines = arxiv_tool.print_tree(tmp_path)
        dir_idx = next(i for i, line in enumerate(lines) if "a_dir" in line)
        file_idx = next(i for i, line in enumerate(lines) if "z_file" in line)
        assert dir_idx < file_idx


# ════════════════════════════════════════════════════════════════════
#  1b. _strip_tex_comments / _extract_braced_arg 纯函数测试
# ════════════════════════════════════════════════════════════════════


class TestStripTexComments:
    """_strip_tex_comments 注释剥离"""

    def test_whole_line_comment_removed(self):
        assert arxiv_tool._strip_tex_comments("% this is a comment") == ""

    def test_whole_line_with_leading_spaces(self):
        assert arxiv_tool._strip_tex_comments("  % indented comment") == ""

    def test_inline_comment_removed(self):
        result = arxiv_tool._strip_tex_comments(r"\title{Foo} % title comment")
        assert result == r"\title{Foo} "

    def test_escaped_percent_preserved(self):
        result = arxiv_tool._strip_tex_comments(r"50\% of papers")
        assert r"50\%" in result

    def test_no_comment_unchanged(self):
        line = r"\begin{document}"
        assert arxiv_tool._strip_tex_comments(line) == line

    def test_multiline(self):
        content = "line1\n% comment\nline3 % inline\nline4"
        result = arxiv_tool._strip_tex_comments(content)
        lines = result.split("\n")
        assert len(lines) == 3  # whole-line comment removed
        assert lines[0] == "line1"
        assert lines[1] == "line3 "
        assert lines[2] == "line4"


class TestExtractBracedArg:
    """_extract_braced_arg 花括号提取"""

    def test_simple(self):
        assert arxiv_tool._extract_braced_arg("{hello}", 0) == "hello"

    def test_nested(self):
        assert arxiv_tool._extract_braced_arg(r"{\textbf{Bold} Title}", 0) == r"\textbf{Bold} Title"

    def test_start_not_brace(self):
        assert arxiv_tool._extract_braced_arg("hello", 0) is None

    def test_start_out_of_bounds(self):
        assert arxiv_tool._extract_braced_arg("abc", 10) is None

    def test_unclosed_brace(self):
        assert arxiv_tool._extract_braced_arg("{never closed", 0) is None

    def test_offset_start(self):
        assert arxiv_tool._extract_braced_arg("xx{val}yy", 2) == "val"

    def test_deeply_nested(self):
        assert arxiv_tool._extract_braced_arg("{a{b{c}}d}", 0) == "a{b{c}}d"


# ════════════════════════════════════════════════════════════════════
#  1c. _normalize_s2_search / _normalize_openalex_search 纯函数测试
# ════════════════════════════════════════════════════════════════════


class TestNormalizeS2Search:
    """_normalize_s2_search 数据转换"""

    def test_basic(self):
        raw = [{
            "title": "Test Paper",
            "authors": [{"name": "Alice"}, {"name": "Bob"}],
            "externalIds": {"ArXiv": "2401.00001", "DOI": "10.1234/test"},
            "citationCount": 42,
            "year": 2024,
            "abstract": "An abstract.",
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert len(result) == 1
        assert result[0]["id"] == "arXiv:2401.00001"
        assert result[0]["authors"] == "Alice, Bob"
        assert result[0]["year"] == "2024"
        assert result[0]["cited_by"] == 42

    def test_no_arxiv_id_falls_back_to_doi(self):
        raw = [{
            "title": "T", "authors": [], "externalIds": {"DOI": "10.1/x"},
            "citationCount": 0, "year": 2020, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["id"] == "DOI:10.1/x"

    def test_no_ids_at_all(self):
        raw = [{
            "title": "T", "authors": [], "externalIds": {},
            "citationCount": 0, "year": 2020, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["id"] == ""

    def test_external_ids_none(self):
        raw = [{
            "title": "T", "authors": [], "externalIds": None,
            "citationCount": 0, "year": 2020, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["id"] == ""

    def test_more_than_3_authors_truncated(self):
        raw = [{
            "title": "T",
            "authors": [{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}],
            "externalIds": {}, "citationCount": 0, "year": 2020, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["authors"] == "A, B, C..."

    def test_authors_none(self):
        raw = [{
            "title": "T", "authors": None, "externalIds": {},
            "citationCount": 0, "year": 2020, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["authors"] == ""

    def test_year_none(self):
        raw = [{
            "title": "T", "authors": [], "externalIds": {},
            "citationCount": 0, "year": None, "abstract": None,
        }]
        result = arxiv_tool._normalize_s2_search(raw)
        assert result[0]["year"] == "?"


class TestNormalizeOpenAlexSearch:
    """_normalize_openalex_search 数据转换"""

    def test_basic_with_arxiv_id(self):
        raw = [{
            "title": "Test Paper",
            "authorships": [
                {"author": {"display_name": "Alice"}},
                {"author": {"display_name": "Bob"}},
            ],
            "ids": {"arxiv": "https://arxiv.org/abs/2401.00001", "doi": "10.1/x"},
            "publication_year": 2024,
            "cited_by_count": 10,
        }]
        result = arxiv_tool._normalize_openalex_search(raw)
        assert result[0]["id"] == "arXiv:2401.00001"
        assert result[0]["authors"] == "Alice, Bob"
        assert result[0]["abstract"] is None  # OpenAlex normalize 不返回 abstract

    def test_no_arxiv_falls_back_to_doi(self):
        raw = [{
            "title": "T",
            "authorships": [],
            "ids": {"doi": "https://doi.org/10.1/x"},
            "publication_year": 2020,
            "cited_by_count": 0,
        }]
        result = arxiv_tool._normalize_openalex_search(raw)
        assert result[0]["id"] == "https://doi.org/10.1/x"

    def test_no_ids_falls_back_to_openalex(self):
        raw = [{
            "title": "T",
            "authorships": [],
            "ids": {"openalex": "https://openalex.org/W123"},
            "publication_year": 2020,
            "cited_by_count": 0,
        }]
        result = arxiv_tool._normalize_openalex_search(raw)
        assert result[0]["id"] == "https://openalex.org/W123"

    def test_more_than_3_authors(self):
        raw = [{
            "title": "T",
            "authorships": [
                {"author": {"display_name": n}} for n in ["A", "B", "C", "D"]
            ],
            "ids": {}, "publication_year": 2020, "cited_by_count": 0,
        }]
        result = arxiv_tool._normalize_openalex_search(raw)
        assert result[0]["authors"] == "A, B, C..."

    def test_year_none(self):
        raw = [{
            "title": "T", "authorships": [], "ids": {},
            "publication_year": None, "cited_by_count": 0,
        }]
        result = arxiv_tool._normalize_openalex_search(raw)
        assert result[0]["year"] == "?"


# ════════════════════════════════════════════════════════════════════
#  2. _extract_source 多格式分支测试（无网络）
# ════════════════════════════════════════════════════════════════════


class TestExtractSource:
    """_extract_source 的多种解压格式"""

    def _make_tar_gz(self, files: dict[str, str]) -> bytes:
        """构造 tar.gz 字节流"""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    def _make_gzip_single(self, content: str) -> bytes:
        """构造 gzip 压缩的单文件"""
        return gzip.compress(content.encode())

    def test_tar_gz(self, tmp_path):
        """tar.gz 格式解压"""
        content = self._make_tar_gz({"main.tex": "\\title{Test}", "ref.bib": "@misc{}"})
        arxiv_tool._extract_source(content, tmp_path)
        assert (tmp_path / "main.tex").exists()
        assert (tmp_path / "ref.bib").exists()
        assert "\\title{Test}" in (tmp_path / "main.tex").read_text()

    def test_gzip_single_tex(self, tmp_path):
        """gzip 压缩的单个 tex 文件"""
        content = self._make_gzip_single("\\documentclass{article}\n\\begin{document}\nHello\n\\end{document}")
        arxiv_tool._extract_source(content, tmp_path)
        assert (tmp_path / "main.tex").exists()
        assert "\\documentclass" in (tmp_path / "main.tex").read_text()

    def test_plain_tex_fallback(self, tmp_path):
        """无法识别格式时作为纯文本 .tex 保存"""
        raw = b"\\documentclass{article}\n\\begin{document}\nPlain\n\\end{document}"
        arxiv_tool._extract_source(raw, tmp_path)
        assert (tmp_path / "main.tex").exists()
        assert "Plain" in (tmp_path / "main.tex").read_text()

    def test_gzip_tar(self, tmp_path):
        """gzip 包裹的 tar（非 tar.gz 标准头）"""
        # 先做一个纯 tar
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            data = b"\\title{GzipTar}"
            info = tarfile.TarInfo(name="paper.tex")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        # 再 gzip 压缩整个 tar
        gz_content = gzip.compress(tar_buf.getvalue())
        arxiv_tool._extract_source(gz_content, tmp_path)
        assert (tmp_path / "paper.tex").exists()


# ════════════════════════════════════════════════════════════════════
#  3. _try_rename_with_title 测试（无网络）
# ════════════════════════════════════════════════════════════════════


class TestTryRenameWithTitle:
    """从 tex 文件提取标题重命名目录"""

    def test_renames_with_title(self, tmp_path):
        target = tmp_path / "1706.03762"
        target.mkdir()
        (target / "main.tex").write_text(r"\title{Attention Is All You Need}")

        new_dir = arxiv_tool._try_rename_with_title(target, "1706.03762", tmp_path)
        assert new_dir is not None
        assert new_dir.exists()
        assert "1706.03762_" in new_dir.name
        assert "Attention" in new_dir.name
        assert not target.exists()  # 原目录已移走

    def test_no_tex_no_rename(self, tmp_path):
        """没有 tex 文件则不重命名"""
        target = tmp_path / "1706.03762"
        target.mkdir()
        (target / "readme.md").write_text("hi")

        result = arxiv_tool._try_rename_with_title(target, "1706.03762", tmp_path)
        assert result is None
        assert target.exists()

    def test_no_title_in_tex(self, tmp_path):
        """tex 文件中无 \\title{} 则不重命名"""
        target = tmp_path / "1706.03762"
        target.mkdir()
        (target / "main.tex").write_text("\\begin{document}\nHello\n\\end{document}")

        result = arxiv_tool._try_rename_with_title(target, "1706.03762", tmp_path)
        assert result is None

    def test_cleans_latex_commands_in_title(self, tmp_path):
        """标题中的 LaTeX 命令应被清理"""
        target = tmp_path / "1706.03762"
        target.mkdir()
        (target / "main.tex").write_text(r"\title{\textbf{Bold} Title}")

        new_dir = arxiv_tool._try_rename_with_title(target, "1706.03762", tmp_path)
        assert new_dir is not None
        assert "1706.03762_" in new_dir.name
        assert "textbf" not in new_dir.name

    def test_dest_exists_no_rename(self, tmp_path):
        """目标目录已存在时不重命名"""
        target = tmp_path / "1706.03762"
        target.mkdir()
        (target / "main.tex").write_text(r"\title{Test Paper}")

        # 预先创建目标
        safe_title = arxiv_tool.sanitize_filename("Test Paper", max_length=40)
        conflict = tmp_path / f"1706.03762_{safe_title}"
        conflict.mkdir()

        result = arxiv_tool._try_rename_with_title(target, "1706.03762", tmp_path)
        assert result is None  # 无法重命名，返回 None
        assert target.exists()  # 原目录仍在


# ════════════════════════════════════════════════════════════════════
#  4. fetch 失败/边界场景（mock 网络）
# ════════════════════════════════════════════════════════════════════


class TestFetchPdfFallback:
    """_fetch_pdf_fallback 失败场景"""

    def test_download_failure_raises(self, tmp_path):
        """PDF 下载失败（HTTP 错误）应抛异常"""
        with patch("arxiv_tool.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
            with pytest.raises(requests.HTTPError):
                arxiv_tool._fetch_pdf_fallback(TEST_ID, tmp_path)

    def test_old_format_id_slash_replaced(self, tmp_path):
        """旧格式 ID 的 / 应被替换为 _ 用于文件名"""
        with patch("arxiv_tool.requests.get") as mock_get:
            mock_get.return_value.raise_for_status.side_effect = requests.HTTPError("404")
            with pytest.raises(requests.HTTPError):
                arxiv_tool._fetch_pdf_fallback("cs/0401001", tmp_path)
            # 验证请求的 URL 使用了正确的 arXiv ID
            call_url = mock_get.call_args[0][0]
            assert "cs/0401001" in call_url


class TestFetchTexSourceFailure:
    """fetch_tex_source 失败场景"""

    def test_download_failure_returns_none(self, tmp_path):
        """源文件下载失败返回 None"""
        with patch("arxiv_tool.requests.get") as mock_get:
            mock_get.side_effect = requests.RequestException("mocked network error")
            result = arxiv_tool.fetch_tex_source("9999.99999", tmp_path)
            assert result is None

    def test_extraction_failure_cleans_up(self, tmp_path):
        """解压失败时清理已创建的目录"""
        with patch("arxiv_tool._extract_source", side_effect=RuntimeError("bad archive")):
            with patch("arxiv_tool.requests.get") as mock_get:
                mock_resp = mock_get.return_value
                mock_resp.raise_for_status = lambda: None
                mock_resp.content = b"fake content"
                result = arxiv_tool.fetch_tex_source("0000.00000", tmp_path)
                assert result is None
                # 目录应被清理
                assert not (tmp_path / "0000.00000").exists()


# ════════════════════════════════════════════════════════════════════
#  5. CMD 命令测试（CLI 输出行为）
# ════════════════════════════════════════════════════════════════════


class TestCmdInfo:
    """cmd_info 输出完整性"""

    def test_output_fields(self, capsys):
        """输出应包含所有关键字段"""
        with patch("arxiv_tool.get_paper_info", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID)
            arxiv_tool.cmd_info(args)

        out = capsys.readouterr().out
        assert "1706.03762" in out
        assert "Attention Is All You Need" in out
        assert "Ashish Vaswani" in out
        assert "2017" in out
        assert "cs.CL" in out
        assert "dominant sequence transduction" in out

    def test_not_found_no_output(self, capsys):
        """论文未找到时不输出论文信息"""
        with patch("arxiv_tool.get_paper_info", return_value=None):
            args = argparse.Namespace(arxiv_id="9999.99999")
            arxiv_tool.cmd_info(args)

        out = capsys.readouterr().out
        # 不应输出任何论文字段
        assert "Title" not in out
        assert "Authors" not in out
        assert "arXiv ID" not in out


class TestCmdBib:
    """cmd_bib CLI 行为"""

    def test_stdout_output(self, capsys):
        """无 -o 时输出到 stdout"""
        with patch("arxiv_tool.get_paper_info", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID, output=None)
            arxiv_tool.cmd_bib(args)

        out = capsys.readouterr().out
        assert "@misc{vaswani2017attention," in out
        assert "Attention Is All You Need" in out

    def test_write_new_file(self, tmp_path, capsys):
        """写入新文件"""
        bib_file = tmp_path / "new.bib"
        with patch("arxiv_tool.get_paper_info", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID, output=str(bib_file))
            arxiv_tool.cmd_bib(args)

        assert bib_file.exists()
        content = bib_file.read_text()
        assert "@misc{vaswani2017attention," in content
        assert content.endswith("\n")
        assert "Written" in capsys.readouterr().out

    def test_append_to_existing_file(self, tmp_path, capsys):
        """追加到已有文件，前后有正确分隔"""
        bib_file = tmp_path / "refs.bib"
        bib_file.write_text("@misc{existing,\n  title={Old},\n}\n")

        with patch("arxiv_tool.get_paper_info", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID, output=str(bib_file))
            arxiv_tool.cmd_bib(args)

        content = bib_file.read_text()
        # 保留旧内容
        assert "@misc{existing," in content
        # 新增内容
        assert "@misc{vaswani2017attention," in content
        # 追加前有 \n\n 分隔
        assert "\n\n@misc{vaswani2017attention," in content
        assert "Appended" in capsys.readouterr().out

    def test_append_to_empty_file(self, tmp_path, capsys):
        """追加到空文件（size=0），不应多加分隔符"""
        bib_file = tmp_path / "empty.bib"
        bib_file.write_text("")

        with patch("arxiv_tool.get_paper_info", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID, output=str(bib_file))
            arxiv_tool.cmd_bib(args)

        content = bib_file.read_text()
        assert content.startswith("@misc{vaswani2017attention,")  # 不以 \n\n 开头

    def test_not_found_exits(self):
        """论文未找到时 sys.exit(1)"""
        with patch("arxiv_tool.get_paper_info", return_value=None):
            args = argparse.Namespace(arxiv_id="9999.99999", output=None)
            with pytest.raises(SystemExit, match="1"):
                arxiv_tool.cmd_bib(args)


class TestFetchPdfFallbackCached:
    """_fetch_pdf_fallback 缓存行为"""

    def test_cached_returns_existing(self, tmp_path, capsys):
        """txt 已存在时跳过下载"""
        txt = tmp_path / f"{TEST_ID}.txt"
        txt.write_text("cached")

        arxiv_tool._fetch_pdf_fallback(TEST_ID, tmp_path)

        out = capsys.readouterr().out
        assert "Already exists" in out
        assert txt.read_text() == "cached"


class TestCmdTex:
    """cmd_tex CLI 行为（树结构输出）"""

    def test_success_prints_tree(self, tmp_path, capsys):
        """成功时输出目录树"""
        tex_dir = tmp_path / f"{TEST_ID}_Test"
        tex_dir.mkdir()
        (tex_dir / "main.tex").write_text("\\title{Test}")
        (tex_dir / "ref.bib").write_text("@misc{}")

        with patch("arxiv_tool.fetch_tex_source", return_value=tex_dir):
            args = argparse.Namespace(arxiv_id=TEST_ID, output=str(tmp_path))
            arxiv_tool.cmd_tex(args)

        out = capsys.readouterr().out
        assert "Directory structure" in out
        assert "main.tex" in out
        assert "ref.bib" in out

    def test_failure_falls_back_to_pdf(self, capsys):
        """tex 失败时 fallback 到 PDF 下载"""
        with patch("arxiv_tool.fetch_tex_source", return_value=None), \
             patch("arxiv_tool._fetch_pdf_fallback") as mock_fallback:
            args = argparse.Namespace(arxiv_id=TEST_ID, output="/tmp")
            arxiv_tool.cmd_tex(args)
            mock_fallback.assert_called_once()


class TestCmdCited:
    """cmd_cited 命令行为：数据源选择与回退"""

    def _make_args(self, source="auto", max_=5, offset=0):
        return argparse.Namespace(
            arxiv_id=TEST_ID, source=source, max=max_, offset=offset,
        )

    def test_s2_forced(self, capsys):
        """--source s2 只调 S2"""
        fake_results = [
            {"title": "Paper A", "authors": [{"name": "A"}], "externalIds": {"ArXiv": "2401.00001"}, "citationCount": 10, "year": 2024},
            {"title": "Paper B", "authors": [{"name": "B"}], "externalIds": {}, "citationCount": 5, "year": 2023},
        ]
        with patch("arxiv_tool._fetch_citations_s2", return_value=(fake_results, 100)) as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex") as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="s2"))
            mock_s2.assert_called_once()
            mock_oa.assert_not_called()

        out = capsys.readouterr().out
        assert "Semantic Scholar" in out
        assert "Paper A" in out

    def test_openalex_forced(self, capsys):
        """--source openalex 只调 OpenAlex"""
        fake_results = [{"title": "Paper X", "authorships": [], "cited_by_count": 10, "publication_year": 2020}]
        with patch("arxiv_tool._fetch_citations_s2") as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex", return_value=(fake_results, 50)) as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="openalex"))
            mock_s2.assert_not_called()
            mock_oa.assert_called_once()

        out = capsys.readouterr().out
        assert "OpenAlex" in out
        assert "Paper X" in out

    def test_auto_fallback_s2_to_openalex(self, capsys):
        """auto 模式: S2 失败后回退到 OpenAlex"""
        fake_results = [{"title": "Fallback Paper", "authorships": [], "cited_by_count": 5, "publication_year": 2021}]
        with patch("arxiv_tool._fetch_citations_s2", return_value=None) as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex", return_value=(fake_results, 30)) as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="auto"))
            mock_s2.assert_called_once()
            mock_oa.assert_called_once()

        out = capsys.readouterr().out
        assert "failed" in out  # "Semantic Scholar failed, switching to OpenAlex"
        assert "OpenAlex" in out
        assert "Fallback Paper" in out

    def test_auto_s2_success_no_openalex(self, capsys):
        """auto 模式: S2 成功则不调 OpenAlex"""
        fake_results = [{"title": "S2 Paper", "authors": [{"name": "A"}], "externalIds": {}, "citationCount": 10, "year": 2020}]
        with patch("arxiv_tool._fetch_citations_s2", return_value=(fake_results, 100)), \
             patch("arxiv_tool._fetch_citations_openalex") as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="auto"))
            mock_oa.assert_not_called()

        out = capsys.readouterr().out
        assert "Semantic Scholar" in out
        assert "S2 Paper" in out

    def test_both_fail(self, capsys):
        """两个数据源都失败"""
        with patch("arxiv_tool._fetch_citations_s2", return_value=None), \
             patch("arxiv_tool._fetch_citations_openalex", return_value=None):
            arxiv_tool.cmd_cited(self._make_args(source="auto"))

        out = capsys.readouterr().out
        assert "No citations found" in out

    def test_offset_passed_through(self):
        """offset 参数正确传递"""
        fake_results = [{"title": "P", "authors": [], "externalIds": {}, "citationCount": 0, "year": 2020}]
        with patch("arxiv_tool._fetch_citations_s2", return_value=(fake_results, 100)) as mock_s2:
            arxiv_tool.cmd_cited(self._make_args(source="s2", offset=20))
            # cmd_cited 调用: _fetch_citations_s2(clean_id, args.max, offset)
            mock_s2.assert_called_once_with(TEST_ID, 5, 20)


class TestCmdSearch:
    """cmd_search CLI 行为"""

    def test_no_results(self, capsys):
        """所有源都无结果时输出提示"""
        with patch("arxiv_tool._search_s2", return_value=None), \
             patch("arxiv_tool._search_openalex", return_value=None), \
             patch("arxiv_tool.search_papers", return_value=[]):
            args = argparse.Namespace(query="zzzzzzz_nonexistent", max=5, source="auto")
            arxiv_tool.cmd_search(args)

        out = capsys.readouterr().out
        assert "No results" in out

    def test_arxiv_fallback_formats_output(self, capsys):
        """arXiv fallback 时输出包含标题/作者/日期"""
        mock_paper = MockPaper(
            title="Test Paper",
            authors=[MockAuthor("Alice"), MockAuthor("Bob")],
            published=datetime(2024, 3, 15),
            categories=["cs.LG"],
            summary="A short abstract.",
        )
        mock_paper.entry_id = "http://arxiv.org/abs/2401.00001v1"

        with patch("arxiv_tool.search_papers", return_value=[mock_paper]):
            args = argparse.Namespace(query="test", max=5, source="arxiv")
            arxiv_tool.cmd_search(args)

        out = capsys.readouterr().out
        assert "Test Paper" in out
        assert "Alice" in out
        assert "2024-03-15" in out


# ════════════════════════════════════════════════════════════════════
#  5b. 格式化输出函数测试（无网络）
# ════════════════════════════════════════════════════════════════════


class TestPrintSearchResults:
    """_print_search_results 输出格式"""

    def test_basic_output(self, capsys):
        results = [{
            "id": "arXiv:2401.00001", "title": "Test", "authors": "Alice",
            "year": "2024", "cited_by": 10, "abstract": "Some abstract.",
        }]
        arxiv_tool._print_search_results(results)
        out = capsys.readouterr().out
        assert "arXiv:2401.00001" in out
        assert "Test" in out
        assert "Alice" in out
        assert "Cited: 10" in out
        assert "Some abstract." in out

    def test_cited_by_none_omitted(self, capsys):
        results = [{
            "id": "arXiv:2401.00001", "title": "T", "authors": "A",
            "year": "2024", "cited_by": None, "abstract": None,
        }]
        arxiv_tool._print_search_results(results)
        out = capsys.readouterr().out
        assert "Cited" not in out

    def test_abstract_none_omitted(self, capsys):
        results = [{
            "id": "arXiv:2401.00001", "title": "T", "authors": "A",
            "year": "2024", "cited_by": 5, "abstract": None,
        }]
        arxiv_tool._print_search_results(results)
        out = capsys.readouterr().out
        assert "Abstract" not in out

    def test_abstract_newlines_replaced(self, capsys):
        results = [{
            "id": "id", "title": "T", "authors": "A",
            "year": "2024", "cited_by": None, "abstract": "line1\nline2",
        }]
        arxiv_tool._print_search_results(results)
        out = capsys.readouterr().out
        assert "line1 line2" in out


class TestPrintCitationsS2:
    """_print_citations_s2 输出格式"""

    def test_basic_with_arxiv(self, capsys):
        results = [{
            "title": "Paper A",
            "authors": [{"name": "Alice"}, {"name": "Bob"}],
            "externalIds": {"ArXiv": "2401.00001"},
            "citationCount": 10,
            "year": 2024,
        }]
        arxiv_tool._print_citations_s2(results)
        out = capsys.readouterr().out
        assert "Paper A" in out
        assert "Alice, Bob" in out
        assert "arXiv:2401.00001" in out
        assert "Cited: 10" in out

    def test_no_arxiv_id(self, capsys):
        results = [{
            "title": "P", "authors": [], "externalIds": {},
            "citationCount": 0, "year": 2020,
        }]
        arxiv_tool._print_citations_s2(results)
        out = capsys.readouterr().out
        assert "arXiv:" not in out

    def test_year_none(self, capsys):
        results = [{
            "title": "P", "authors": [], "externalIds": None,
            "citationCount": 0, "year": None,
        }]
        arxiv_tool._print_citations_s2(results)
        out = capsys.readouterr().out
        assert "?" in out

    def test_more_than_3_authors_truncated(self, capsys):
        results = [{
            "title": "P",
            "authors": [{"name": n} for n in ["A", "B", "C", "D"]],
            "externalIds": {}, "citationCount": 0, "year": 2020,
        }]
        arxiv_tool._print_citations_s2(results)
        out = capsys.readouterr().out
        assert "A, B, C..." in out

    def test_start_numbering(self, capsys):
        results = [{
            "title": "P", "authors": [], "externalIds": {},
            "citationCount": 0, "year": 2020,
        }]
        arxiv_tool._print_citations_s2(results, start=5)
        out = capsys.readouterr().out
        assert "[5]" in out


class TestPrintCitationsOpenAlex:
    """_print_citations_openalex 输出格式"""

    def test_basic(self, capsys):
        results = [{
            "title": "Paper X",
            "authorships": [{"author": {"display_name": "Alice"}}],
            "publication_year": 2024,
            "cited_by_count": 7,
        }]
        arxiv_tool._print_citations_openalex(results)
        out = capsys.readouterr().out
        assert "Paper X" in out
        assert "Alice" in out
        assert "Cited: 7" in out

    def test_year_none(self, capsys):
        results = [{
            "title": "P", "authorships": [],
            "publication_year": None, "cited_by_count": 0,
        }]
        arxiv_tool._print_citations_openalex(results)
        out = capsys.readouterr().out
        assert "?" in out

    def test_more_than_3_authors(self, capsys):
        results = [{
            "title": "P",
            "authorships": [{"author": {"display_name": n}} for n in ["A", "B", "C", "D"]],
            "publication_year": 2020, "cited_by_count": 0,
        }]
        arxiv_tool._print_citations_openalex(results)
        out = capsys.readouterr().out
        assert "A, B, C..." in out


# ════════════════════════════════════════════════════════════════════
#  6. get_paper_info 重试逻辑（mock）
# ════════════════════════════════════════════════════════════════════


class TestRateLimiter:
    """RateLimiter 行为测试，使用 INTERVALS["ut"]=0.3s"""

    @pytest.fixture(autouse=True)
    def _clean_lock(self):
        """每个测试前后清理 lock 文件"""
        arxiv_tool.RateLimiter.LOCK_FILE.unlink(missing_ok=True)
        yield
        arxiv_tool.RateLimiter.LOCK_FILE.unlink(missing_ok=True)

    def test_available_when_no_lock(self):
        assert arxiv_tool.RateLimiter.available("ut") is True

    def test_not_available_right_after_record(self):
        arxiv_tool.RateLimiter.record("ut")
        assert arxiv_tool.RateLimiter.available("ut") is False

    def test_available_after_interval(self):
        arxiv_tool.RateLimiter.record("ut")
        time.sleep(0.35)
        assert arxiv_tool.RateLimiter.available("ut") is True

    def test_record_does_not_affect_other_services(self):
        arxiv_tool.RateLimiter.record("ut")
        assert arxiv_tool.RateLimiter.available("s2") is True

    def test_wait_returns_immediately_when_available(self):
        start = time.time()
        arxiv_tool.RateLimiter.wait("ut")
        assert time.time() - start < 0.1

    def test_wait_sleeps_remaining_time(self):
        arxiv_tool.RateLimiter.record("ut")
        time.sleep(0.1)  # 已过 0.1s，还需等 ~0.2s
        start = time.time()
        arxiv_tool.RateLimiter.wait("ut")
        elapsed = time.time() - start
        assert 0.1 < elapsed < 0.4

    def test_wait_retries_on_contention(self):
        """模拟另一个进程在 wait 期间更新了时间戳，使 wait 必须重试多次"""
        original_read = arxiv_tool.RateLimiter._read
        call_count = 0

        def _contended_read():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # 前两次读取都返回"刚刚记录"的时间戳，模拟另一个进程在争抢
                return {"ut": time.time()}
            return original_read()

        arxiv_tool.RateLimiter.record("ut")
        with patch.object(arxiv_tool.RateLimiter, "_read", side_effect=_contended_read):
            arxiv_tool.RateLimiter.wait("ut")

        assert call_count >= 3  # 确认至少重试了 3 次

    def test_corrupt_lock_file_auto_recovers(self):
        arxiv_tool.RateLimiter.LOCK_FILE.write_text("this is not json")
        assert arxiv_tool.RateLimiter.available("ut") is True
        assert not arxiv_tool.RateLimiter.LOCK_FILE.exists()


# ════════════════════════════════════════════════════════════════════
#  7. 集成测试（需要网络）—— 以 1706.03762 为例
# ════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def paper_info():
    """获取论文元数据（module 级缓存，只请求一次 arXiv API）"""
    paper = arxiv_tool.get_paper_info(TEST_ID)
    assert paper is not None, f"无法获取论文 {TEST_ID}"
    return paper


@network
class TestGetPaperInfo:
    """get_paper_info 集成测试"""

    def test_title(self, paper_info):
        assert TEST_TITLE in paper_info.title

    def test_first_author(self, paper_info):
        first = paper_info.authors[0].name.lower()
        assert TEST_FIRST_AUTHOR_LAST in first

    def test_year(self, paper_info):
        assert paper_info.published.year == TEST_YEAR

    def test_categories(self, paper_info):
        assert TEST_PRIMARY_CLASS in paper_info.categories

    def test_not_found_returns_none(self):
        result = arxiv_tool.get_paper_info("9999.99999")
        assert result is None


@network
class TestFetchPdfFallbackIntegration:
    """_fetch_pdf_fallback 集成测试（下载 PDF + 转 txt）"""

    def test_download_creates_pdf_and_txt(self, tmp_path):
        arxiv_tool._fetch_pdf_fallback(TEST_ID, tmp_path)

        txt = tmp_path / f"{TEST_ID}.txt"
        pdf = tmp_path / f"{TEST_ID}.pdf"

        assert txt.exists()
        assert pdf.exists()
        assert pdf.stat().st_size > 10_000

        content = txt.read_text()
        assert f"arXiv:{TEST_ID}" in content
        assert len(content) > 1000

    def test_cached_skips_download(self, tmp_path, capsys):
        """txt 已存在时跳过下载"""
        txt = tmp_path / f"{TEST_ID}.txt"
        txt.write_text("cached")

        arxiv_tool._fetch_pdf_fallback(TEST_ID, tmp_path)
        assert txt.read_text() == "cached"
        assert "Already exists" in capsys.readouterr().out


@network
class TestFetchTexSource:
    """fetch_tex_source 集成测试"""

    def test_download_and_extract(self, tmp_path):
        result = arxiv_tool.fetch_tex_source(TEST_ID, tmp_path)
        assert result is not None
        assert result.is_dir()

        tex_files = list(result.glob("**/*.tex"))
        assert len(tex_files) > 0

    def test_cached_dir_returns_existing(self, tmp_path):
        """目录已存在时跳过下载"""
        existing = tmp_path / TEST_ID
        existing.mkdir()
        (existing / "main.tex").write_text("cached")

        result = arxiv_tool.fetch_tex_source(TEST_ID, tmp_path)
        assert result == existing
        assert (existing / "main.tex").read_text() == "cached"

    def test_cached_renamed_dir(self, tmp_path):
        """带标题后缀的目录已存在时跳过下载"""
        renamed = tmp_path / f"{TEST_ID}_Attention_Is_All_You_Need"
        renamed.mkdir()
        (renamed / "main.tex").write_text("cached")

        result = arxiv_tool.fetch_tex_source(TEST_ID, tmp_path)
        assert result == renamed

    def test_glob_ignores_txt_files(self, tmp_path):
        """回归测试: glob 不应误匹配同名 txt 文件 (之前的 bug)"""
        fake_txt = tmp_path / f"{TEST_ID}_Attention_Is_All_You_Need.txt"
        fake_txt.write_text("this is a txt file, not a dir")

        result = arxiv_tool.fetch_tex_source(TEST_ID, tmp_path)
        assert result is not None
        assert result.is_dir()


class TestSearchPapersUnit:
    """search_papers 单元测试（mock）"""

    def test_max_results_passed(self):
        """max_results 正确传递"""
        with patch("arxiv_tool.arxiv.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.results.return_value = iter([])
            arxiv_tool.search_papers("test", max_results=7)

        search_obj = mock_client.results.call_args[0][0]
        assert search_obj.max_results == 7


@network
class TestSearchPapers:
    """search_papers 集成测试"""

    def test_search_returns_results(self):
        results = arxiv_tool.search_papers("Attention Is All You Need", max_results=3)
        assert len(results) > 0


@network
class TestBibIntegration:
    """bib 端到端测试（真实 API）"""

    def test_real_bibtex(self, paper_info):
        bib = arxiv_tool.generate_bibtex(paper_info, TEST_ID)
        assert "@misc{vaswani2017attention," in bib
        assert "Attention Is All You Need" in bib


@network
class TestCitedSemanticScholar:
    """被引反查 - Semantic Scholar"""

    @pytest.fixture(autouse=True)
    def _s2_rate_limit(self):
        """每个测试前 sleep 2s，避免 S2 429 限流（全量跑时前面已消耗配额）"""
        time.sleep(2)

    def test_citations_and_max_results(self):
        """基本查询 + max_results 限制"""
        ret = arxiv_tool._fetch_citations_s2(TEST_ID, max_results=3)
        assert ret is not None, "S2 返回 None（可能被限流）"
        results, total = ret
        assert total > 0
        assert 0 < len(results) <= 3
        for paper in results:
            assert paper.get("title")

    def test_offset(self):
        """offset 翻页返回不同结果"""
        ret = arxiv_tool._fetch_citations_s2(TEST_ID, max_results=3, offset=0)
        assert ret is not None, "S2 返回 None（可能被限流）"
        titles_page1 = {p.get("title") for p in ret[0]}

        time.sleep(1)
        ret = arxiv_tool._fetch_citations_s2(TEST_ID, max_results=3, offset=5)
        assert ret is not None, "S2 返回 None（可能被限流）"
        titles_page2 = {p.get("title") for p in ret[0]}

        assert titles_page1 != titles_page2


@network
class TestCitedOpenAlex:
    """被引反查 - OpenAlex"""

    def test_resolve_openalex_id(self):
        resolved = arxiv_tool._resolve_openalex_id(TEST_ID)
        assert resolved is not None
        work_id, title, cited_by = resolved
        assert work_id
        assert "attention" in title.lower()
        assert cited_by > 0

    def test_returns_citations(self):
        ret = arxiv_tool._fetch_citations_openalex(TEST_ID, max_results=5)
        assert ret is not None
        results, total = ret
        assert total > 0
        assert len(results) > 0
        for work in results:
            assert work.get("title")

    def test_respects_max_results(self):
        ret = arxiv_tool._fetch_citations_openalex(TEST_ID, max_results=3)
        assert ret is not None
        results, _ = ret
        assert len(results) <= 3
