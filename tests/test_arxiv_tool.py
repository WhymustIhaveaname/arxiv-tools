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
from paper_cache import CachedAuthor, CachedPaper

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
    """Dual-purpose mock: acts as CachedPaper (for bib/info tests) and arxiv.Result
    (for _normalize_arxiv_search test, which reads .summary and .published)"""
    title: str
    authors: list
    categories: list = field(default_factory=list)
    abstract: str = "Mock abstract."
    pdf_url: str = "https://arxiv.org/pdf/0000.00000"
    published: datetime = None  # only for arxiv.Result-style tests

    @property
    def summary(self):
        """Alias: arxiv.Result uses .summary, our CachedPaper uses .abstract"""
        return self.abstract


MOCK_PAPER = MockPaper(
    title="Attention Is All You Need",
    authors=[
        MockAuthor("Ashish Vaswani"),
        MockAuthor("Noam Shazeer"),
        MockAuthor("Niki Parmar"),
    ],
    categories=["cs.CL", "cs.LG"],
    abstract="The dominant sequence transduction models are based on complex recurrent or convolutional neural networks...",
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


class TestExtractPaperId:
    """Generic paper ID classification."""

    def test_bare_arxiv_new(self):
        assert arxiv_tool.extract_paper_id("2401.12345") == ("arxiv", "2401.12345")

    def test_bare_arxiv_with_version(self):
        assert arxiv_tool.extract_paper_id("1706.03762v5") == ("arxiv", "1706.03762")

    def test_arxiv_prefix(self):
        assert arxiv_tool.extract_paper_id("arXiv:2401.12345") == ("arxiv", "2401.12345")

    def test_arxiv_url(self):
        assert arxiv_tool.extract_paper_id("https://arxiv.org/abs/2401.12345v2") == ("arxiv", "2401.12345")

    def test_arxiv_old_format(self):
        assert arxiv_tool.extract_paper_id("cs/0401001") == ("arxiv", "cs/0401001")

    def test_pmid_bare(self):
        assert arxiv_tool.extract_paper_id("39876543") == ("pmid", "39876543")

    def test_pmid_url(self):
        assert arxiv_tool.extract_paper_id("https://pubmed.ncbi.nlm.nih.gov/39876543/") == ("pmid", "39876543")

    def test_pmcid_bare(self):
        assert arxiv_tool.extract_paper_id("PMC1234567") == ("pmcid", "PMC1234567")

    def test_pmcid_lowercase(self):
        assert arxiv_tool.extract_paper_id("pmc1234567") == ("pmcid", "PMC1234567")

    def test_pmcid_url(self):
        assert arxiv_tool.extract_paper_id("https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/") == ("pmcid", "PMC1234567")

    def test_doi_bare(self):
        assert arxiv_tool.extract_paper_id("10.1038/s41586-020-2649-2") == ("doi", "10.1038/s41586-020-2649-2")

    def test_doi_url(self):
        assert arxiv_tool.extract_paper_id("https://doi.org/10.1038/xxx") == ("doi", "10.1038/xxx")

    def test_unknown_falls_through(self):
        assert arxiv_tool.extract_paper_id("random keyword search") == ("unknown", "random keyword search")


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


class TestArxivYear:
    """arXiv ID 年份提取"""

    def test_new_format(self):
        assert arxiv_tool._arxiv_year("1706.03762") == 2017

    def test_new_format_five_digit(self):
        assert arxiv_tool._arxiv_year("2505.08783") == 2025

    def test_old_format(self):
        assert arxiv_tool._arxiv_year("cs/0401001") == 2004

    def test_old_format_90s(self):
        assert arxiv_tool._arxiv_year("hep-th/9108028") == 1991

    def test_garbage_returns_none(self):
        assert arxiv_tool._arxiv_year("not-an-id") is None


class TestGenerateCitationKey:
    """BibTeX citation key 生成"""

    def test_attention_paper(self):
        assert arxiv_tool.generate_citation_key(MOCK_PAPER, TEST_ID) == TEST_CITATION_KEY

    def test_year_from_arxiv_id(self):
        """年份从 arXiv ID 提取"""
        paper = MockPaper(
            title="Attention Is All You Need",
            authors=[MockAuthor("Ashish Vaswani")],
        )
        key = arxiv_tool.generate_citation_key(paper, "1706.03762")
        assert "2017" in key

    def test_skips_stopwords(self):
        paper = MockPaper(
            title="The Art of Programming",
            authors=[MockAuthor("Donald Knuth")],
        )
        assert arxiv_tool.generate_citation_key(paper, "2401.00001") == "knuth2024art"

    def test_all_stopword_title(self):
        """标题全是停用词时，first_word 为空"""
        paper = MockPaper(
            title="The Of And In",
            authors=[MockAuthor("Jane Doe")],
        )
        key = arxiv_tool.generate_citation_key(paper, "2401.00001")
        assert key == "doe2024"

    def test_hyphenated_last_name(self):
        paper = MockPaper(
            title="Some Result",
            authors=[MockAuthor("Jean-Pierre Serre")],
        )
        key = arxiv_tool.generate_citation_key(paper, "0001.00001")
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

    def test_omits_primary_class_when_no_categories(self):
        """categories 为空时不输出 primaryClass"""
        paper = MockPaper(
            title="Test Paper",
            authors=[MockAuthor("Alice Bob")],
            categories=[],
        )
        bib = arxiv_tool.generate_bibtex(paper, "2401.00001")
        assert "primaryClass" not in bib
        assert "eprint={2401.00001}" in bib
        assert "archivePrefix={arXiv}" in bib


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


class TestNormalizePubmedSearch:
    """ESummary records → standard search dicts."""

    def test_basic(self):
        raw = [{
            "uid": "39876543",
            "title": "CRISPR in cancer",
            "authors": [
                {"name": "Smith J", "authtype": "Author"},
                {"name": "Jones A", "authtype": "Author"},
            ],
            "pubdate": "2024 Jan 15",
        }]
        result = arxiv_tool._normalize_pubmed_search(raw)
        assert result[0]["id"] == "PMID:39876543"
        assert result[0]["title"] == "CRISPR in cancer"
        assert result[0]["authors"] == "Smith J, Jones A"
        assert result[0]["year"] == "2024"
        assert result[0]["cited_by"] is None
        assert result[0]["abstract"] is None

    def test_more_than_3_authors_truncated(self):
        raw = [{
            "uid": "1",
            "title": "T",
            "authors": [
                {"name": "A", "authtype": "Author"},
                {"name": "B", "authtype": "Author"},
                {"name": "C", "authtype": "Author"},
                {"name": "D", "authtype": "Author"},
            ],
            "pubdate": "2024",
        }]
        result = arxiv_tool._normalize_pubmed_search(raw)
        assert result[0]["authors"] == "A, B, C..."

    def test_falls_back_to_epubdate(self):
        raw = [{
            "uid": "1", "title": "T", "authors": [],
            "pubdate": "", "epubdate": "2023-06-01",
        }]
        result = arxiv_tool._normalize_pubmed_search(raw)
        assert result[0]["year"] == "2023"

    def test_missing_pubdate_is_question_mark(self):
        raw = [{"uid": "1", "title": "T", "authors": []}]
        result = arxiv_tool._normalize_pubmed_search(raw)
        assert result[0]["year"] == "?"

    def test_non_author_contributors_filtered_when_authors_present(self):
        """ESummary may include editors/translators; prefer authors when present."""
        raw = [{
            "uid": "1",
            "title": "T",
            "authors": [
                {"name": "Editor E", "authtype": "Editor"},
                {"name": "Author A", "authtype": "Author"},
            ],
            "pubdate": "2024",
        }]
        result = arxiv_tool._normalize_pubmed_search(raw)
        assert result[0]["authors"] == "Author A"


class TestOAMirror:
    """Layer-1 OA mirror discovery + PDF validation."""

    def test_find_urls_dedups_and_prefers_openalex_first(self):
        from lit.oa_mirror import find_oa_pdf_urls
        with patch("lit.oa_mirror._unpaywall_urls", return_value=["https://dup.pdf", "https://u.pdf"]), \
             patch("lit.oa_mirror._core_urls", return_value=["https://core.pdf", "https://dup.pdf"]), \
             patch("lit.oa_mirror._crossref_tdm_pdf_urls", return_value=["https://dup.pdf", "https://c.pdf"]):
            urls = find_oa_pdf_urls(doi="10.1/x", openalex_pdf_url="https://oa.pdf")
        assert urls == [
            "https://oa.pdf", "https://dup.pdf", "https://u.pdf",
            "https://core.pdf", "https://c.pdf",
        ]

    def test_find_urls_tolerates_missing_contact_email(self):
        """_unpaywall_urls silently skips when CONTACT_EMAIL is empty."""
        from lit.oa_mirror import _unpaywall_urls
        with patch("lit.oa_mirror.CONTACT_EMAIL", ""):
            assert _unpaywall_urls("10.1/x") == []

    def test_core_urls_skips_when_no_api_key(self):
        from lit.oa_mirror import _core_urls
        with patch("lit.oa_mirror.CORE_API_KEY", None):
            assert _core_urls("10.1/x") == []

    def test_core_urls_extracts_download_and_full_text_links(self):
        from lit.oa_mirror import _core_urls

        class R:
            def raise_for_status(self): pass
            def json(self):
                return {
                    "results": [
                        {
                            "downloadUrl": "https://repo.uni-x.edu/papers/abc.pdf",
                            "fullTextLink": "https://repo.uni-x.edu/papers/abc",
                        },
                        {"downloadUrl": "https://other.edu/y.pdf", "fullTextLink": None},
                        {"downloadUrl": None, "fullTextLink": None},  # skipped
                    ],
                }

        with patch("lit.oa_mirror.CORE_API_KEY", "fakekey"), \
             patch("lit.oa_mirror._request_with_retry", return_value=R()):
            urls = _core_urls("10.1/x")
        assert "https://repo.uni-x.edu/papers/abc.pdf" in urls
        assert "https://repo.uni-x.edu/papers/abc" in urls
        assert "https://other.edu/y.pdf" in urls
        # No duplicates from the second-empty entry.
        assert len(urls) == 3

    def test_core_urls_returns_empty_on_request_failure(self):
        from lit.oa_mirror import _core_urls
        import requests as _r
        with patch("lit.oa_mirror.CORE_API_KEY", "fakekey"), \
             patch(
                 "lit.oa_mirror._request_with_retry",
                 side_effect=_r.RequestException("network down"),
             ):
            assert _core_urls("10.1/x") == []

    def test_try_download_pdf_rejects_html_masquerading_as_pdf(self):
        """HTML bodies must not be saved as .pdf (Cloudflare challenge pages)."""
        from lit.oa_mirror import try_download_pdf

        class R:
            content = b"<!DOCTYPE html>...<title>Just a moment...</title>"
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def raise_for_status(self): pass

        with patch("lit.oa_mirror._request_with_retry", return_value=R()):
            assert try_download_pdf("https://host/file") is None

    def test_try_download_pdf_returns_bytes_for_valid_pdf(self):
        from lit.oa_mirror import try_download_pdf

        class R:
            content = b"%PDF-1.7\n... fake ..."
            def raise_for_status(self): pass

        with patch("lit.oa_mirror._request_with_retry", return_value=R()):
            got = try_download_pdf("https://host/file")
        assert got is not None and got.startswith(b"%PDF")


class TestFulltextDispatch:
    """cmd_fulltext ID-type routing."""

    def _args(self, arxiv_id, from_file=None):
        return argparse.Namespace(arxiv_id=arxiv_id, from_file=from_file)

    def test_biorxiv_doi_routes_to_biorxiv_handler(self):
        with patch("arxiv_tool._try_biorxiv_to_disk", return_value=True) as mock_bx, \
             patch("arxiv_tool._try_chemrxiv_to_disk") as mock_cx:
            arxiv_tool.cmd_fulltext(self._args("10.1101/2024.01.01.12345"))
            mock_bx.assert_called_once_with("10.1101/2024.01.01.12345")
            mock_cx.assert_not_called()

    def test_chemrxiv_doi_routes_to_chemrxiv_handler(self):
        with patch("arxiv_tool._try_biorxiv_to_disk") as mock_bx, \
             patch("arxiv_tool._try_chemrxiv_to_disk", return_value=True) as mock_cx:
            arxiv_tool.cmd_fulltext(self._args("10.26434/chemrxiv-2024-abc"))
            mock_cx.assert_called_once_with("10.26434/chemrxiv-2024-abc")
            mock_bx.assert_not_called()

    def test_from_file_bypasses_all_network_paths(self, tmp_path, cache_db):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n minimal \n%%EOF")
        with patch("arxiv_tool._try_biorxiv_to_disk") as mock_bx, \
             patch("arxiv_tool._try_chemrxiv_to_disk") as mock_cx, \
             patch("arxiv_tool._try_pmc_to_disk") as mock_pmc:
            # Sends through the --from-file path regardless of ID type.
            arxiv_tool.OUTPUT_DIR = tmp_path  # redirect save target
            arxiv_tool.cmd_fulltext(self._args("10.26434/foo", from_file=str(pdf)))
            mock_bx.assert_not_called()
            mock_cx.assert_not_called()
            mock_pmc.assert_not_called()
        assert (tmp_path / "10.26434_foo.pdf").exists()

    def test_from_file_rejects_non_pdf(self, tmp_path):
        not_a_pdf = tmp_path / "junk.txt"
        not_a_pdf.write_text("this is not a PDF")
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_fulltext(self._args("10.1/x", from_file=str(not_a_pdf)))

    def test_from_file_rejects_missing_path(self):
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_fulltext(self._args("10.1/x", from_file="/no/such/path.pdf"))


class TestEuropePMC:
    """Europe PMC search query builder + annotations grouping."""

    def test_build_query_plain(self):
        from lit.sources.europepmc import _build_europepmc_query
        assert _build_europepmc_query("CRISPR") == "(CRISPR)"

    def test_build_query_year_range(self):
        from lit.sources.europepmc import _build_europepmc_query
        t = _build_europepmc_query("cancer", year="2020-2024")
        assert "(cancer)" in t
        assert "FIRST_PDATE:[2020-01-01 TO 2024-12-31]" in t

    def test_build_query_open_access_and_source(self):
        from lit.sources.europepmc import _build_europepmc_query
        t = _build_europepmc_query("CRISPR", open_access=True, src="ppr")
        assert "OPEN_ACCESS:y" in t
        assert "SRC:PPR" in t

    def test_article_id_prefers_pmc(self):
        from lit.sources.europepmc import _article_id_for_annotations
        assert _article_id_for_annotations(pmid="1", pmcid="PMC99") == "PMC:PMC99"
        assert _article_id_for_annotations(pmid="1", pmcid=None) == "MED:1"
        assert _article_id_for_annotations(pmid=None, pmcid="99") == "PMC:PMC99"
        assert _article_id_for_annotations(pmid=None, pmcid=None) is None

    def test_normalize_search(self):
        from lit.sources.europepmc import _normalize_europepmc_search
        records = [{
            "source": "MED",
            "id": "123",
            "pmid": "123",
            "pmcid": "PMC999",
            "doi": "10.1/x",
            "title": "Sample Paper.",
            "authorString": "Smith J, Jones A",
            "pubYear": "2024",
            "citedByCount": 5,
            "abstractText": "<h4>Background</h4>This is the abstract.",
        }]
        out = _normalize_europepmc_search(records)
        assert out[0]["id"] == "PMID:123"
        assert out[0]["title"] == "Sample Paper"          # trailing '.' stripped
        assert out[0]["authors"] == "Smith J, Jones A"
        assert out[0]["year"] == "2024"
        assert out[0]["cited_by"] == 5
        assert "<" not in (out[0]["abstract"] or "")      # tags stripped

    def test_group_annotations_preserves_order(self):
        from lit.sources.europepmc import group_annotations_by_type
        annos = [
            {"type": "Gene_Proteins", "exact": "TP53"},
            {"type": "Diseases", "exact": "cancer"},
            {"type": "Gene_Proteins", "exact": "BRCA1"},
        ]
        g = group_annotations_by_type(annos)
        assert set(g.keys()) == {"Gene_Proteins", "Diseases"}
        assert [a["exact"] for a in g["Gene_Proteins"]] == ["TP53", "BRCA1"]

    def test_record_to_cached_paper(self):
        from lit.sources.europepmc import _record_to_cached_paper
        rec = {
            "title": "Foo.",
            "pubYear": "2023",
            "pmid": "123",
            "pmcid": "PMC9",
            "doi": "10.1/x",
            "authorList": {"author": [{"fullName": "Jane Smith"}, {"fullName": "Bob Jones"}]},
            "abstractText": "<p>Interesting.</p>",
            "journalInfo": {"journal": {"title": "Nature"}},
            "fullTextUrlList": {"fullTextUrl": [
                {"documentStyle": "html", "url": "https://h"},
                {"documentStyle": "pdf", "url": "https://p.pdf"},
            ]},
        }
        p = _record_to_cached_paper(rec)
        assert p.title == "Foo"
        assert p.year == 2023
        assert p.pmid == "123"
        assert p.pmcid == "PMC9"
        assert p.doi == "10.1/x"
        assert [a.name for a in p.authors] == ["Jane Smith", "Bob Jones"]
        assert p.abstract == "Interesting."
        assert p.categories == ["Nature"]
        assert p.pdf_url == "https://p.pdf"
        assert p.source == "europepmc"


class TestCmdAnnotations:
    """cmd_annotations dispatch and output."""

    def _args(self, arxiv_id, type_="all", max_per_type=30):
        return argparse.Namespace(arxiv_id=arxiv_id, type=type_, max_per_type=max_per_type)

    def test_pmcid_queries_pmc(self, capsys, cache_db):
        fake = [
            {"type": "Gene_Proteins", "exact": "TP53",
             "tags": [{"uri": "https://uniprot.org/TP53"}]},
            {"type": "Gene_Proteins", "exact": "TP53",
             "tags": [{"uri": "https://uniprot.org/TP53"}]},
            {"type": "Diseases", "exact": "cancer", "tags": []},
        ]
        with patch("arxiv_tool.fetch_annotations", return_value=fake) as mock_fa:
            arxiv_tool.cmd_annotations(self._args("PMC1234"))
            mock_fa.assert_called_once()
            # PMC path: called with pmcid, pmid=None
            assert mock_fa.call_args.kwargs["pmcid"] == "PMC1234"
            assert mock_fa.call_args.kwargs["pmid"] is None
        out = capsys.readouterr().out
        assert "3 annotations for PMC:PMC1234" in out
        assert "TP53" in out
        assert "[2×]" in out          # dedup count
        assert "cancer" in out

    def test_type_filter_translated(self, cache_db):
        with patch("arxiv_tool.fetch_annotations", return_value=[]) as mock_fa:
            arxiv_tool.cmd_annotations(self._args("PMC1", type_="genes,diseases"))
            assert mock_fa.call_args.kwargs["types"] == ["genes", "diseases"]

    def test_unknown_id_exits(self):
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_annotations(self._args("not an id"))


class TestChemRxivAdapter:
    """ChemRxiv-via-Crossref search + metadata + Cloudflare-block handling."""

    CROSSREF_ITEM = {
        "DOI": "10.26434/chemrxiv-2024-XYZ",
        "title": ["A Paper About Catalysis"],
        "author": [
            {"given": "Jane", "family": "Smith"},
            {"given": "Bob", "family": "Jones"},
        ],
        "posted": {"date-parts": [[2024, 6, 15]]},
        "is-referenced-by-count": 7,
        "abstract": "<jats:p>We report a novel <jats:italic>catalyst</jats:italic>.</jats:p>",
        "link": [
            {"URL": "https://chemrxiv.org/.../paper.pdf",
             "content-type": "application/pdf"},
        ],
        "subject": ["Catalysis", "Organic Chemistry"],
    }

    def test_is_chemrxiv_doi(self):
        from lit.sources.chemrxiv import is_chemrxiv_doi
        assert is_chemrxiv_doi("10.26434/chemrxiv-2024-abc")
        assert is_chemrxiv_doi("10.26434/chemrxiv.12151809.v1")
        assert not is_chemrxiv_doi("10.1038/s41586-020-2649-2")
        assert not is_chemrxiv_doi("10.48550/arXiv.1706.03762")

    def test_normalize_search(self):
        from lit.sources.chemrxiv import _normalize_chemrxiv_search
        out = _normalize_chemrxiv_search([self.CROSSREF_ITEM])
        assert out[0]["id"] == "DOI:10.26434/chemrxiv-2024-XYZ"
        assert out[0]["title"] == "A Paper About Catalysis"
        assert out[0]["authors"] == "Jane Smith, Bob Jones"
        assert out[0]["year"] == "2024"
        assert out[0]["cited_by"] == 7
        assert "catalyst" in (out[0]["abstract"] or "")
        assert "<" not in (out[0]["abstract"] or "")  # JATS tags stripped

    def test_fetch_paper_builds_full_cached_paper(self):
        from lit.sources.chemrxiv import _fetch_paper_chemrxiv
        with patch("lit.sources.chemrxiv.fetch_crossref_work", return_value=self.CROSSREF_ITEM):
            paper = _fetch_paper_chemrxiv("10.26434/chemrxiv-2024-XYZ")
        assert paper is not None
        assert paper.source == "chemrxiv"
        assert paper.doi == "10.26434/chemrxiv-2024-XYZ"
        assert paper.year == 2024
        assert [a.name for a in paper.authors] == ["Jane Smith", "Bob Jones"]
        assert "catalyst" in paper.abstract
        assert paper.pdf_url.endswith(".pdf")
        assert "Catalysis" in paper.categories

    def test_abstract_is_degenerate_sentinels(self):
        """The 'Publication status: Published' sentinel is the main bug this
        guards against — a Crossref admin string should not pass as an abstract."""
        from lit.sources.chemrxiv import _abstract_is_degenerate
        assert _abstract_is_degenerate("")
        assert _abstract_is_degenerate(None)
        assert _abstract_is_degenerate("Publication status: Published")
        assert _abstract_is_degenerate("publication status: published")  # case-insensitive
        assert _abstract_is_degenerate("tiny")  # too short (< 50 chars)
        real = (
            "We introduce the MultiModalSpectralTransformer (MMST), a machine "
            "learning method that predicts chemical structures directly from "
            "diverse spectral data (NMR, IR, and MS)."
        )
        assert not _abstract_is_degenerate(real)

    def test_degenerate_abstract_falls_back_to_published_twin(self):
        """When the preprint's Crossref abstract is the sentinel string, the
        adapter should follow relation.is-preprint-of → published DOI and
        pull a real abstract from there."""
        from lit.sources.chemrxiv import _fetch_paper_chemrxiv

        preprint_msg = {
            **self.CROSSREF_ITEM,
            "abstract": "<jats:p>Publication status: Published</jats:p>",
            "relation": {
                "is-preprint-of": [
                    {"id-type": "doi", "id": "10.1002/anie.99999", "asserted-by": "subject"}
                ]
            },
        }
        published_msg = {
            "abstract": "<jats:p>We introduce a transformer for spectral data that predicts molecular structures directly.</jats:p>",
        }
        calls = []

        def mock_fetch(doi):
            calls.append(doi)
            if doi == "10.26434/chemrxiv-2024-XYZ":
                return preprint_msg
            if doi == "10.1002/anie.99999":
                return published_msg
            return None

        with patch("lit.sources.chemrxiv.fetch_crossref_work", side_effect=mock_fetch):
            paper = _fetch_paper_chemrxiv("10.26434/chemrxiv-2024-XYZ")

        assert paper is not None
        assert "transformer for spectral data" in paper.abstract
        assert "Publication status" not in paper.abstract
        assert "10.1002/anie.99999" in calls  # followed the twin link

    def test_pdf_fetch_returns_none_on_cloudflare_challenge(self):
        """fetch_chemrxiv_pdf should return None when Cloudflare returns HTML."""
        from lit.sources.chemrxiv import fetch_chemrxiv_pdf

        class R:
            content = b"<!DOCTYPE html><html><head>Cloudflare challenge</head>"
            def raise_for_status(self): pass

        with patch("lit.sources.chemrxiv.fetch_crossref_work", return_value=self.CROSSREF_ITEM), \
             patch("lit.sources.chemrxiv._request_with_retry", return_value=R()):
            assert fetch_chemrxiv_pdf("10.26434/chemrxiv-2024-XYZ") is None

    def test_pdf_fetch_returns_bytes_when_response_is_pdf(self):
        from lit.sources.chemrxiv import fetch_chemrxiv_pdf

        class R:
            content = b"%PDF-1.4\n... fake pdf bytes ..."
            def raise_for_status(self): pass

        with patch("lit.sources.chemrxiv.fetch_crossref_work", return_value=self.CROSSREF_ITEM), \
             patch("lit.sources.chemrxiv._request_with_retry", return_value=R()):
            got = fetch_chemrxiv_pdf("10.26434/chemrxiv-2024-XYZ")
        assert got is not None and got.startswith(b"%PDF")


class TestBuildPubmedTerm:
    """PubMed ESearch term-string assembly."""

    def test_plain_query(self):
        from lit.sources.pubmed import _build_pubmed_term
        assert _build_pubmed_term("CRISPR") == "(CRISPR)"

    def test_single_year(self):
        from lit.sources.pubmed import _build_pubmed_term
        t = _build_pubmed_term("CRISPR", year="2020")
        assert t == '(CRISPR) AND "2020"[PDAT]'

    def test_year_range(self):
        from lit.sources.pubmed import _build_pubmed_term
        t = _build_pubmed_term("cancer", year="2020-2024")
        assert t == '(cancer) AND "2020":"2024"[PDAT]'

    def test_open_access_filter(self):
        from lit.sources.pubmed import _build_pubmed_term
        t = _build_pubmed_term("CRISPR", open_access=True)
        assert '[sb]' in t
        assert "loattrfree full text" in t

    def test_all_filters_combined(self):
        from lit.sources.pubmed import _build_pubmed_term
        t = _build_pubmed_term("cancer", year="2020-2024", open_access=True)
        assert t == '(cancer) AND "2020":"2024"[PDAT] AND "loattrfree full text"[sb]'


class TestEnrichPolicy:
    """enrich_paper_ids should only fill PMID/PMCID, never overwrite DOI/year/arxiv_id."""

    def test_fills_missing_pmid_and_pmcid(self):
        from lit.enrich import enrich_paper_ids
        enriched = CachedPaper(
            title="X", authors=[CachedAuthor("A")],
            pmid="123", pmcid="PMC9", doi="junk", year=9999, arxiv_id="junk",
        )
        paper = CachedPaper(title="X", authors=[CachedAuthor("A")], doi="10.x/y")
        with patch("lit.enrich.OPENALEX_ENABLED", True), \
             patch("lit.enrich._fetch_paper_openalex_spec", return_value=enriched):
            enrich_paper_ids(paper)
        assert paper.pmid == "123"
        assert paper.pmcid == "PMC9"

    def test_does_not_overwrite_doi_or_year_or_arxiv_id(self):
        from lit.enrich import enrich_paper_ids
        enriched = CachedPaper(
            title="X", authors=[CachedAuthor("A")],
            pmid="123", doi="10.65215/SYNTHETIC", year=2025, arxiv_id="WRONG",
        )
        paper = CachedPaper(
            title="X", authors=[CachedAuthor("A")],
            doi="10.real/paper", year=2017, arxiv_id="1706.03762",
        )
        with patch("lit.enrich.OPENALEX_ENABLED", True), \
             patch("lit.enrich._fetch_paper_openalex_spec", return_value=enriched):
            enrich_paper_ids(paper)
        assert paper.doi == "10.real/paper"
        assert paper.year == 2017
        assert paper.arxiv_id == "1706.03762"
        assert paper.pmid == "123"  # only this gets filled

    def test_short_circuits_when_both_pmid_and_pmcid_present(self):
        from lit.enrich import enrich_paper_ids
        paper = CachedPaper(title="X", authors=[], pmid="1", pmcid="PMC1")
        with patch("lit.enrich._fetch_paper_openalex_spec") as mock_oa:
            enrich_paper_ids(paper)
            mock_oa.assert_not_called()

    def test_openalex_disabled_short_circuits(self):
        from lit.enrich import enrich_paper_ids
        paper = CachedPaper(title="X", authors=[CachedAuthor("A")], doi="10.x/y")
        with patch("lit.enrich.OPENALEX_ENABLED", False), \
             patch("lit.enrich._fetch_paper_openalex_spec") as mock_oa:
            enrich_paper_ids(paper)
            mock_oa.assert_not_called()
        assert paper.pmid is None


class TestCrossrefCacheRows:
    """cache_paper_with_crossrefs writes one row per known ID."""

    def test_writes_alias_rows(self, cache_db):
        from paper_cache import cache_paper_with_crossrefs, get_cached_paper
        paper = CachedPaper(
            title="Multi-ID paper",
            authors=[CachedAuthor("A")],
            arxiv_id="2401.00001",
            doi="10.1/x",
            pmid="999",
            pmcid="PMC999",
            source="pubmed",
        )
        cache_paper_with_crossrefs("pmid:999", paper, "@article{x}")

        for key in ("pmid:999", "doi:10.1/x", "pmcid:PMC999", "arxiv:2401.00001"):
            hit = get_cached_paper(key)
            assert hit is not None, f"expected alias row for {key}"
            assert hit.title == "Multi-ID paper"

    def test_does_not_duplicate_primary(self, cache_db):
        """If the primary key would be one of the aliases, don't write it twice."""
        from paper_cache import cache_paper_with_crossrefs, _get_conn
        paper = CachedPaper(
            title="X",
            authors=[CachedAuthor("A")],
            pmid="999",
        )
        cache_paper_with_crossrefs("pmid:999", paper, "bib")
        conn = _get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE arxiv_id = 'pmid:999'"
        ).fetchone()[0]
        assert count == 1


class TestCmdReferences:
    """cmd_references dispatches and renders correctly."""

    def _args(self, arxiv_id, max_=5, offset=0):
        return argparse.Namespace(arxiv_id=arxiv_id, max=max_, offset=offset)

    def test_arxiv_goes_to_s2_with_arxiv_spec(self, capsys):
        fake = [{"title": "Ref A", "authors": [{"name": "A"}], "externalIds": {}, "citationCount": 5, "year": 2020}]
        with patch("arxiv_tool._fetch_references_s2_spec", return_value=(fake, 1)) as mock_s2:
            arxiv_tool.cmd_references(self._args("1706.03762"))
            mock_s2.assert_called_once_with("ArXiv:1706.03762", 5, 0)
        assert "Ref A" in capsys.readouterr().out

    def test_pmid_goes_to_s2_first(self, capsys):
        fake = [{"title": "Ref B", "authors": [], "externalIds": {}, "citationCount": 0, "year": 2021}]
        with patch("arxiv_tool._fetch_references_s2_spec", return_value=(fake, 1)) as mock_s2, \
             patch("arxiv_tool._references_via_pubmed") as mock_pm:
            arxiv_tool.cmd_references(self._args("32866453"))
            mock_s2.assert_called_once_with("PMID:32866453", 5, 0)
            mock_pm.assert_not_called()

    def test_pmid_falls_back_to_pubmed_when_s2_empty(self, capsys):
        with patch("arxiv_tool._fetch_references_s2_spec", return_value=([], 0)), \
             patch("arxiv_tool._references_via_pubmed",
                   return_value=([{"uid": "1", "title": "Pm Ref", "authors": [], "pubdate": "2022"}], 1)) as mock_pm:
            arxiv_tool.cmd_references(self._args("32866453"))
            mock_pm.assert_called_once_with("32866453", 5, 0)
        assert "Pm Ref" in capsys.readouterr().out

    def test_unknown_id_exits(self):
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_references(self._args("not an id"))


class TestFetchPaperPubmedParsing:
    """EFetch XML → CachedPaper."""

    EFETCH_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>39876543</PMID>
      <Article>
        <Journal><Title>Nature</Title></Journal>
        <ArticleTitle>CRISPR in cancer therapy</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Background text.</AbstractText>
          <AbstractText Label="METHODS">Methods text.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName>
            <ForeName>Jane</ForeName>
          </Author>
          <Author>
            <LastName>Jones</LastName>
            <ForeName>Bob</ForeName>
          </Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">39876543</ArticleId>
        <ArticleId IdType="doi">10.1038/xxx</ArticleId>
        <ArticleId IdType="pmc">PMC7654321</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""

    def _mock_resp(self, content):
        class R:
            pass
        r = R()
        r.content = content
        r.raise_for_status = lambda: None
        return r

    def test_parses_full_xml(self):
        with patch("lit.sources.pubmed._request_with_retry",
                   return_value=self._mock_resp(self.EFETCH_XML)):
            paper = arxiv_tool._fetch_paper_pubmed("39876543")
        assert paper is not None
        assert paper.title == "CRISPR in cancer therapy"
        assert [a.name for a in paper.authors] == ["Jane Smith", "Bob Jones"]
        assert "BACKGROUND: Background text." in paper.abstract
        assert "METHODS: Methods text." in paper.abstract
        assert paper.source == "pubmed"
        assert paper.pmid == "39876543"
        assert paper.doi == "10.1038/xxx"
        assert paper.pmcid == "PMC7654321"
        assert paper.categories == ["Nature"]
        assert paper.pdf_url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC7654321/pdf/"

    def test_no_article_returns_none(self):
        empty = b"<?xml version='1.0'?><PubmedArticleSet></PubmedArticleSet>"
        with patch("lit.sources.pubmed._request_with_retry",
                   return_value=self._mock_resp(empty)):
            paper = arxiv_tool._fetch_paper_pubmed("0")
        assert paper is None

    def test_reference_pmc_ids_do_not_leak(self):
        """ArticleIdLists nested under ReferenceList are for *cited* papers;
        they must not clobber the main article's PMC ID. Regression test —
        an earlier XPath `.//ArticleIdList/ArticleId` grabbed all of them."""
        xml = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <Article>
        <ArticleTitle>Main Paper</ArticleTitle>
        <AuthorList><Author><LastName>Smith</LastName><ForeName>J</ForeName></Author></AuthorList>
        <Abstract><AbstractText>Abs</AbstractText></Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pmc">PMC7610144</ArticleId>
      </ArticleIdList>
      <ReferenceList>
        <Reference>
          <ArticleIdList>
            <ArticleId IdType="pmc">PMC99999999</ArticleId>
          </ArticleIdList>
        </Reference>
        <Reference>
          <ArticleIdList>
            <ArticleId IdType="pmc">PMC88888888</ArticleId>
          </ArticleIdList>
        </Reference>
      </ReferenceList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""
        with patch("lit.sources.pubmed._request_with_retry",
                   return_value=self._mock_resp(xml)):
            paper = arxiv_tool._fetch_paper_pubmed("1")
        assert paper is not None
        assert paper.pmcid == "PMC7610144"


class TestCmdInfoDispatch:
    """cmd_info routes to the right source based on ID type."""

    def _args(self, arxiv_id):
        return argparse.Namespace(arxiv_id=arxiv_id)

    def test_pmid_routes_to_pubmed(self, capsys, cache_db):
        fake = CachedPaper(
            title="T",
            authors=[CachedAuthor("A")],
            abstract="abs",
            source="pubmed",
            pmid="123",
            pdf_url="u",
        )
        with patch("arxiv_tool.aggregate_lookup", return_value=fake) as mock_agg:
            arxiv_tool.cmd_info(self._args("39876543"))
        mock_agg.assert_called_once_with(pmid="39876543")
        out = capsys.readouterr().out
        assert "PMID: 39876543" in out
        assert "abs" in out

    def test_arxiv_id_routes_to_existing_path(self, cache_db):
        with patch("arxiv_tool.aggregate_lookup", return_value=MOCK_PAPER) as mock_agg:
            arxiv_tool.cmd_info(self._args("1706.03762"))
        mock_agg.assert_called_once_with(arxiv_id="1706.03762")

    def test_unknown_id_exits_nonzero(self):
        with patch("arxiv_tool._fetch_paper_pubmed") as mock_pm, \
             patch("arxiv_tool.get_paper_info") as mock_arxiv:
            with pytest.raises(SystemExit):
                arxiv_tool.cmd_info(self._args("totally not an id"))
        mock_pm.assert_not_called()
        mock_arxiv.assert_not_called()


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
        # _extract_source lives in lit.fulltext after the refactor; fetch_tex_source
        # looks it up from its own module, so patch must target that namespace.
        with patch("lit.fulltext._extract_source", side_effect=RuntimeError("bad archive")):
            with patch("lit.fulltext.requests.get") as mock_get:
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

    def test_output_fields(self, capsys, cache_db):
        """输出应包含所有关键字段"""
        with patch("arxiv_tool.aggregate_lookup", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID)
            arxiv_tool.cmd_info(args)

        out = capsys.readouterr().out
        assert "1706.03762" in out
        assert "Attention Is All You Need" in out
        assert "Ashish Vaswani" in out
        assert "2017" in out
        assert "cs.CL" in out
        assert "dominant sequence transduction" in out

    def test_output_includes_cached_tex_path(self, tmp_path, capsys, cache_db):
        tex_dir = tmp_path / f"{TEST_ID}_Attention_Is_All_You_Need"
        tex_dir.mkdir()
        with patch("arxiv_tool.OUTPUT_DIR", tmp_path), \
             patch("arxiv_tool.aggregate_lookup", return_value=MOCK_PAPER):
            args = argparse.Namespace(arxiv_id=TEST_ID)
            arxiv_tool.cmd_info(args)

        out = capsys.readouterr().out
        assert f"Tex (cached): {tex_dir}" in out

    def test_not_found_no_output(self, capsys, cache_db):
        """论文未找到时不输出论文信息"""
        with patch("arxiv_tool.aggregate_lookup", return_value=None):
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


class TestCmdInfotex:
    def test_runs_info_then_tex(self):
        args = argparse.Namespace(arxiv_id=TEST_ID)
        with patch("arxiv_tool.cmd_info") as mock_info, \
             patch("arxiv_tool.cmd_tex") as mock_tex:
            arxiv_tool.cmd_infotex(args)
            mock_info.assert_called_once_with(args)
            mock_tex.assert_called_once_with(args)


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
        with patch("arxiv_tool._fetch_citations_s2_spec", return_value=(fake_results, 100)) as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex_spec") as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="s2"))
            mock_s2.assert_called_once()
            mock_oa.assert_not_called()

        out = capsys.readouterr().out
        assert "Semantic Scholar" in out
        assert "Paper A" in out

    def test_openalex_forced_disabled(self, capsys):
        """--source openalex exits clearly while OpenAlex is disabled."""
        with patch("arxiv_tool._fetch_citations_s2_spec") as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex_spec") as mock_oa:
            with pytest.raises(SystemExit) as exc:
                arxiv_tool.cmd_cited(self._make_args(source="openalex"))
            assert exc.value.code == 2
            mock_s2.assert_not_called()
            mock_oa.assert_not_called()

        err = capsys.readouterr().err
        assert "OpenAlex 源已被禁用" in err

    def test_auto_fallback_s2_to_openalex(self, capsys):
        """auto 模式: S2 失败后回退到 OpenAlex"""
        fake_results = [{"title": "Fallback Paper", "authorships": [], "cited_by_count": 5, "publication_year": 2021}]
        with patch("arxiv_tool.OPENALEX_ENABLED", True), \
             patch("arxiv_tool._fetch_citations_s2_spec", return_value=None) as mock_s2, \
             patch("arxiv_tool._fetch_citations_openalex_spec", return_value=(fake_results, 30)) as mock_oa:
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
        with patch("arxiv_tool._fetch_citations_s2_spec", return_value=(fake_results, 100)), \
             patch("arxiv_tool._fetch_citations_openalex_spec") as mock_oa:
            arxiv_tool.cmd_cited(self._make_args(source="auto"))
            mock_oa.assert_not_called()

        out = capsys.readouterr().out
        assert "Semantic Scholar" in out
        assert "S2 Paper" in out

    def test_both_fail(self, capsys):
        """两个数据源都失败"""
        with patch("arxiv_tool._fetch_citations_s2_spec", return_value=None), \
             patch("arxiv_tool._fetch_citations_openalex_spec", return_value=None):
            arxiv_tool.cmd_cited(self._make_args(source="auto"))

        out = capsys.readouterr().out
        assert "No citations found" in out

    def test_offset_passed_through(self):
        """offset 参数正确传递"""
        fake_results = [{"title": "P", "authors": [], "externalIds": {}, "citationCount": 0, "year": 2020}]
        with patch("arxiv_tool._fetch_citations_s2_spec", return_value=(fake_results, 100)) as mock_s2:
            arxiv_tool.cmd_cited(self._make_args(source="s2", offset=20))
            # cmd_cited builds an ArXiv: paper_spec before calling the spec fetcher.
            mock_s2.assert_called_once_with(f"ArXiv:{TEST_ID}", 5, 20)

    def test_pmid_routes_to_s2_with_pmid_spec(self):
        """PMID input → S2 paper_spec uses PMID: prefix"""
        fake_results = [{"title": "P", "authors": [], "externalIds": {}, "citationCount": 0, "year": 2024}]
        args = argparse.Namespace(arxiv_id="39876543", source="s2", max=5, offset=0)
        with patch("arxiv_tool._fetch_citations_s2_spec", return_value=(fake_results, 50)) as mock_s2:
            arxiv_tool.cmd_cited(args)
            mock_s2.assert_called_once_with("PMID:39876543", 5, 0)

    def test_pmid_falls_back_to_openalex_with_pmid_spec(self):
        fake_results = [{"title": "P", "authorships": [], "cited_by_count": 0, "publication_year": 2024}]
        args = argparse.Namespace(arxiv_id="39876543", source="auto", max=5, offset=0)
        with patch("arxiv_tool.OPENALEX_ENABLED", True), \
             patch("arxiv_tool._fetch_citations_s2_spec", return_value=None), \
             patch("arxiv_tool._fetch_citations_openalex_spec", return_value=(fake_results, 30)) as mock_oa:
            arxiv_tool.cmd_cited(args)
            mock_oa.assert_called_once_with("PMID:39876543", 5, 0)

    def test_unknown_id_exits_nonzero(self):
        args = argparse.Namespace(arxiv_id="totally not an id", source="auto", max=5, offset=0)
        with patch("arxiv_tool._fetch_citations_s2_spec") as mock_s2:
            with pytest.raises(SystemExit):
                arxiv_tool.cmd_cited(args)
            mock_s2.assert_not_called()


class TestCmdBibPubmed:
    """cmd_bib routes PMIDs through Crossref → fallback."""

    PUBMED_PAPER = CachedPaper(
        title="X",
        authors=[CachedAuthor("Author A")],
        source="pubmed",
        pmid="123",
        doi="10.1038/foo",
        year=2024,
        categories=["Nature"],
    )

    def test_pmid_uses_crossref_when_doi_present(self, capsys, cache_db):
        with patch("arxiv_tool._fetch_paper_pubmed", return_value=self.PUBMED_PAPER), \
             patch("arxiv_tool.fetch_bibtex_crossref", return_value="@article{cr,title={X}}") as mock_cr:
            args = argparse.Namespace(arxiv_id="39876543", output=None)
            arxiv_tool.cmd_bib(args)
        mock_cr.assert_called_once_with("10.1038/foo")
        assert "@article{cr" in capsys.readouterr().out

    def test_pmid_falls_back_when_crossref_returns_none(self, capsys, cache_db):
        with patch("arxiv_tool._fetch_paper_pubmed", return_value=self.PUBMED_PAPER), \
             patch("arxiv_tool.fetch_bibtex_crossref", return_value=None):
            args = argparse.Namespace(arxiv_id="39876543", output=None)
            arxiv_tool.cmd_bib(args)
        out = capsys.readouterr().out
        assert "@article{" in out
        # _bib_for_pmid uses the PMID supplied on the command line, not paper.pmid.
        assert "pmid={39876543}" in out
        assert "journal={Nature}" in out
        assert "year={2024}" in out

    def test_pmid_skips_crossref_when_no_doi(self, cache_db):
        no_doi = CachedPaper(title="X", authors=[CachedAuthor("Author A")], pmid="123", year=2024)
        with patch("arxiv_tool._fetch_paper_pubmed", return_value=no_doi), \
             patch("arxiv_tool.fetch_bibtex_crossref") as mock_cr:
            args = argparse.Namespace(arxiv_id="39876543", output=None)
            arxiv_tool.cmd_bib(args)
        mock_cr.assert_not_called()

    def test_pmid_not_found_exits(self, cache_db):
        with patch("arxiv_tool._fetch_paper_pubmed", return_value=None):
            args = argparse.Namespace(arxiv_id="39876543", output=None)
            with pytest.raises(SystemExit):
                arxiv_tool.cmd_bib(args)

    def test_pmid_bib_uses_cache_on_second_call(self, cache_db):
        """Once bib has rendered + cached a BibTeX entry, the second call must
        not touch _fetch_paper_pubmed or Crossref."""
        with patch("arxiv_tool._fetch_paper_pubmed", return_value=self.PUBMED_PAPER), \
             patch("arxiv_tool.fetch_bibtex_crossref", return_value="@article{cr,...}"):
            args = argparse.Namespace(arxiv_id="39876543", output=None)
            arxiv_tool.cmd_bib(args)
        with patch("arxiv_tool._fetch_paper_pubmed") as mock_pm, \
             patch("arxiv_tool.fetch_bibtex_crossref") as mock_cr:
            arxiv_tool.cmd_bib(args)
            mock_pm.assert_not_called()
            mock_cr.assert_not_called()


class TestGenerateBibtexPubmed:
    """Local @article fallback structure."""

    def test_includes_all_optional_fields(self):
        paper = CachedPaper(
            title="My Paper",
            authors=[CachedAuthor("Jane Smith"), CachedAuthor("Bob Jones")],
            year=2024,
            categories=["Nature"],
            doi="10.1038/foo",
            pmcid="PMC1234",
        )
        bib = arxiv_tool.generate_bibtex_pubmed(paper, "39876543")
        assert bib.startswith("@article{")
        assert "title={My Paper}" in bib
        assert "author={Jane Smith and Bob Jones}" in bib
        assert "journal={Nature}" in bib
        assert "year={2024}" in bib
        assert "doi={10.1038/foo}" in bib
        assert "pmid={39876543}" in bib
        assert "pmcid={PMC1234}" in bib
        assert "url={https://pubmed.ncbi.nlm.nih.gov/39876543/}" in bib

    def test_minimal_paper_uses_pmid_fallback_key(self):
        """No authors and no year → key falls back to pm{pmid}"""
        paper = CachedPaper(title="X", authors=[])
        bib = arxiv_tool.generate_bibtex_pubmed(paper, "999")
        assert bib.startswith("@article{pm999,")

    def test_citation_key_format(self):
        paper = CachedPaper(
            title="A Study of CRISPR",
            authors=[CachedAuthor("Jane Smith")],
            year=2024,
        )
        bib = arxiv_tool.generate_bibtex_pubmed(paper, "39876543")
        assert bib.startswith("@article{smith2024study,")


class TestCmdSearch:
    """cmd_search CLI 行为"""

    def test_openalex_source_disabled(self, capsys):
        args = argparse.Namespace(query="test", max=5, source="openalex")
        with pytest.raises(SystemExit) as exc:
            arxiv_tool.cmd_search(args)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "OpenAlex 源已被禁用" in err

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
            abstract="A short abstract.",
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

    def test_stub_entries_filtered(self, capsys):
        """S2 /references sometimes returns JATS body fragments as stub
        rows (no authors, no year, no IDs, citationCount=None). The output
        hides them and re-numbers the remaining real entries."""
        results = [
            {
                "title": "Real Paper",
                "authors": [{"name": "Alice"}], "externalIds": {"DOI": "10.1/x"},
                "citationCount": 10, "year": 2024,
            },
            {
                "title": "Filter Efficacy : a sentence from the body text",
                "authors": [], "externalIds": {}, "citationCount": None, "year": None,
            },
            {
                "title": "Another Real Paper",
                "authors": [{"name": "Bob"}], "externalIds": {}, "citationCount": 0,
                "year": 2023,
            },
        ]
        arxiv_tool._print_citations_s2(results)
        captured = capsys.readouterr()
        assert "Real Paper" in captured.out
        assert "Another Real Paper" in captured.out
        assert "Filter Efficacy" not in captured.out
        # Survivors keep contiguous numbering.
        assert "[1] Real Paper" in captured.out
        assert "[2] Another Real Paper" in captured.out
        # Filter summary lands on stderr so it doesn't pollute piped output.
        assert "1 stub entries filtered" in captured.err

    def test_stub_detector_skips_papers_with_any_signal(self, capsys):
        """A paper with *any* of authors/year/IDs/citationCount is kept."""
        results = [
            {"title": "has year only", "authors": [], "externalIds": {},
             "citationCount": None, "year": 2020},
            {"title": "has authors only", "authors": [{"name": "X"}],
             "externalIds": {}, "citationCount": None, "year": None},
            {"title": "has ext ids only", "authors": [],
             "externalIds": {"DOI": "10.1/y"}, "citationCount": None, "year": None},
            {"title": "has zero cites (not None)", "authors": [], "externalIds": {},
             "citationCount": 0, "year": None},
        ]
        arxiv_tool._print_citations_s2(results)
        out = capsys.readouterr().out
        for t in ("has year only", "has authors only",
                  "has ext ids only", "has zero cites"):
            assert t in out


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

    def test_acquire_returns_immediately_when_no_lock(self):
        start = time.time()
        arxiv_tool.RateLimiter.acquire("ut")
        assert time.time() - start < 0.1

    def test_acquire_waits_for_interval(self):
        """连续两次 acquire 应等待 interval"""
        arxiv_tool.RateLimiter.acquire("ut")
        start = time.time()
        arxiv_tool.RateLimiter.acquire("ut")
        elapsed = time.time() - start
        assert elapsed >= 0.25  # interval=0.3s，允许少量时钟误差

    def test_acquire_does_not_block_other_services(self):
        """acquire("ut") 不影响 acquire("openalex")"""
        arxiv_tool.RateLimiter.acquire("ut")
        start = time.time()
        arxiv_tool.RateLimiter.acquire("openalex")
        assert time.time() - start < 0.15  # openalex interval=0.1s

    def test_acquire_after_interval_elapsed(self):
        """等待 interval 后第二次 acquire 应立即返回"""
        arxiv_tool.RateLimiter.acquire("ut")
        time.sleep(0.35)
        start = time.time()
        arxiv_tool.RateLimiter.acquire("ut")
        assert time.time() - start < 0.1

    def test_backoff_formula(self):
        # Nominal backoff is INTERVALS[service] * 2**attempt, with ±20% jitter
        # applied so parallel workers don't synchronise retries after a shared
        # 429. Assert the jittered value lands in [0.8x, 1.2x] of nominal.
        for attempt, nominal in [(0, 0.3), (1, 0.6), (2, 1.2)]:
            for _ in range(20):
                v = arxiv_tool.RateLimiter.backoff("ut", attempt)
                assert nominal * 0.8 <= v <= nominal * 1.2

    def test_corrupt_lock_file_auto_recovers(self):
        """损坏的 lock 文件不阻断 acquire"""
        arxiv_tool.RateLimiter.LOCK_FILE.write_text("this is not json")
        arxiv_tool.RateLimiter.acquire("ut")  # 不应抛异常


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

    def test_categories(self, paper_info):
        # 只有 arXiv 源提供 categories，fallback 到其他源时为空
        if paper_info.categories:
            assert TEST_PRIMARY_CLASS in paper_info.categories

    def test_not_found_returns_none(self):
        result = arxiv_tool.get_paper_info("9999.99999")
        assert result is None


@network
class TestFetchPaperSources:
    """三个数据源的独立测试 + 一致性验证"""

    @pytest.mark.skipif(not arxiv_tool.OPENALEX_ENABLED, reason="OpenAlex disabled")
    def test_openalex_returns_paper(self):
        result = arxiv_tool._fetch_paper_openalex(TEST_ID)
        assert result is not None
        assert TEST_TITLE in result.title
        assert TEST_FIRST_AUTHOR_LAST in result.authors[0].name.lower()

    def test_s2_returns_paper(self):
        result = arxiv_tool._fetch_paper_s2(TEST_ID)
        assert result is not None
        assert "attention" in result.title.lower()
        assert TEST_FIRST_AUTHOR_LAST in result.authors[0].name.lower()

    def test_arxiv_returns_paper(self):
        result = arxiv_tool._fetch_paper_arxiv(TEST_ID)
        assert result is not None
        assert TEST_TITLE in result.title
        assert result.categories  # 只有 arXiv 源有 categories

    @pytest.mark.skipif(not arxiv_tool.OPENALEX_ENABLED, reason="OpenAlex disabled")
    def test_sources_consistent(self):
        """三个源返回的 title 和第一作者应一致"""
        oa = arxiv_tool._fetch_paper_openalex(TEST_ID)
        s2 = arxiv_tool._fetch_paper_s2(TEST_ID)
        ar = arxiv_tool._fetch_paper_arxiv(TEST_ID)
        assert oa and s2 and ar

        # title 忽略大小写比较（S2 大小写可能不同）
        assert oa.title.lower() == ar.title.lower()
        assert s2.title.lower() == ar.title.lower()
        # 第一作者一致
        assert oa.authors[0].name == ar.authors[0].name
        assert s2.authors[0].name == ar.authors[0].name

    @pytest.mark.skipif(not arxiv_tool.OPENALEX_ENABLED, reason="OpenAlex disabled")
    def test_bibtex_year_from_arxiv_id(self):
        """无论哪个源，BibTeX year 都应从 arXiv ID 提取"""
        oa = arxiv_tool._fetch_paper_openalex(TEST_ID)
        assert oa is not None
        bib = arxiv_tool.generate_bibtex(oa, TEST_ID)
        assert "year={2017}" in bib  # 不是 OpenAlex 的 2025

    def test_openalex_not_found_returns_none(self):
        result = arxiv_tool._fetch_paper_openalex("9999.99999")
        assert result is None

    def test_s2_not_found_returns_none(self):
        result = arxiv_tool._fetch_paper_s2("9999.99999")
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
        ret = arxiv_tool._fetch_citations_s2_spec(f"ArXiv:{TEST_ID}", max_results=3)
        assert ret is not None, "S2 返回 None（可能被限流）"
        results, total = ret
        assert total > 0
        assert 0 < len(results) <= 3
        for paper in results:
            assert paper.get("title")

    def test_offset(self):
        """offset 翻页返回不同结果"""
        ret = arxiv_tool._fetch_citations_s2_spec(f"ArXiv:{TEST_ID}", max_results=3, offset=0)
        assert ret is not None, "S2 返回 None（可能被限流）"
        titles_page1 = {p.get("title") for p in ret[0]}

        time.sleep(1)
        ret = arxiv_tool._fetch_citations_s2_spec(f"ArXiv:{TEST_ID}", max_results=3, offset=5)
        assert ret is not None, "S2 返回 None（可能被限流）"
        titles_page2 = {p.get("title") for p in ret[0]}

        assert titles_page1 != titles_page2


@network
@pytest.mark.skipif(not arxiv_tool.OPENALEX_ENABLED, reason="OpenAlex disabled")
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
        ret = arxiv_tool._fetch_citations_openalex_spec(TEST_ID, max_results=5)
        assert ret is not None
        results, total = ret
        assert total > 0
        assert len(results) > 0
        for work in results:
            assert work.get("title")

    def test_respects_max_results(self):
        ret = arxiv_tool._fetch_citations_openalex_spec(TEST_ID, max_results=3)
        assert ret is not None
        results, _ = ret
        assert len(results) <= 3


# ════════════════════════════════════════════════════════════════════
#  similar (PubMed ELink pubmed_pubmed)
# ════════════════════════════════════════════════════════════════════


class TestFetchSimilarPmids:
    """fetch_similar_pmids — PubMed similar-article ELink wrapper."""

    def _resp(self, json_data):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.json.return_value = json_data
        r.raise_for_status.return_value = None
        return r

    def test_extracts_links_in_order(self):
        from lit.sources import pubmed
        data = {
            "linksets": [{
                "linksetdbs": [{
                    "linkname": "pubmed_pubmed",
                    "links": ["32866453", "12345", "67890", "11111"],
                }],
            }],
        }
        with patch("lit.sources.pubmed._request_with_retry", return_value=self._resp(data)):
            out = pubmed.fetch_similar_pmids("32866453", max_results=10)
        # First entry was the seed PMID — should be dropped.
        assert out == ["12345", "67890", "11111"]

    def test_max_results_truncates(self):
        from lit.sources import pubmed
        data = {
            "linksets": [{
                "linksetdbs": [{
                    "linkname": "pubmed_pubmed",
                    "links": ["seed"] + [f"sim{i}" for i in range(10)],
                }],
            }],
        }
        with patch("lit.sources.pubmed._request_with_retry", return_value=self._resp(data)):
            out = pubmed.fetch_similar_pmids("seed", max_results=3)
        assert out == ["sim0", "sim1", "sim2"]

    def test_no_matching_linkname_returns_empty(self):
        """ELink may return only refs / citedin links if pubmed_pubmed has no data."""
        from lit.sources import pubmed
        data = {
            "linksets": [{
                "linksetdbs": [
                    {"linkname": "pubmed_pubmed_refs", "links": ["111"]},
                ],
            }],
        }
        with patch("lit.sources.pubmed._request_with_retry", return_value=self._resp(data)):
            out = pubmed.fetch_similar_pmids("seed")
        assert out == []

    def test_request_failure_returns_none(self):
        from lit.sources import pubmed
        with patch(
            "lit.sources.pubmed._request_with_retry",
            side_effect=requests.RequestException("net down"),
        ):
            out = pubmed.fetch_similar_pmids("seed")
        assert out is None


class TestCmdSimilar:
    """cmd_similar CLI behavior."""

    def _args(self, arxiv_id, max=20, offset=0):
        return argparse.Namespace(arxiv_id=arxiv_id, max=max, offset=offset)

    def test_rejects_arxiv_id(self):
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_similar(self._args("2401.12345"))

    def test_rejects_doi(self):
        with pytest.raises(SystemExit):
            arxiv_tool.cmd_similar(self._args("10.1038/foo"))

    def test_pmid_invokes_elink_then_esummary(self, capsys):
        sim = ["111", "222", "333"]
        records = [
            {"uid": "111", "title": "Paper One", "authors": [{"name": "A", "authtype": "Author"}], "pubdate": "2024"},
            {"uid": "222", "title": "Paper Two", "authors": [{"name": "B", "authtype": "Author"}], "pubdate": "2023"},
            {"uid": "333", "title": "Paper Three", "authors": [{"name": "C", "authtype": "Author"}], "pubdate": "2022"},
        ]
        with patch("arxiv_tool.fetch_similar_pmids", return_value=sim), \
             patch("arxiv_tool.fetch_esummary_batch", return_value=records):
            arxiv_tool.cmd_similar(self._args("9999999", max=3))
        out = capsys.readouterr().out
        assert "Paper One" in out
        assert "Paper Two" in out
        assert "Paper Three" in out
        assert "Showing similar articles #1-3" in out

    def test_no_similar_prints_friendly_message(self, capsys):
        with patch("arxiv_tool.fetch_similar_pmids", return_value=[]):
            arxiv_tool.cmd_similar(self._args("9999999"))
        out = capsys.readouterr().out
        assert "No similar articles" in out
