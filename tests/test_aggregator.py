"""Tests for lit/aggregator.py — the multi-source search aggregator."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from lit.aggregator import (
    AggregatedHit,
    DEFAULT_SOURCES,
    _canonical_keys,
    _dedup,
    _hits_from_arxiv,
    _hits_from_chemrxiv,
    _hits_from_europepmc,
    _hits_from_openalex,
    _hits_from_pubmed,
    _hits_from_s2,
    _merge,
    _rank,
    aggregate_search,
)


# --------------------------------------------------------------------------
# canonical_keys
# --------------------------------------------------------------------------


class TestCanonicalKeys:
    def test_arxiv_id_maps_to_synthetic_doi(self):
        h = AggregatedHit(arxiv_id="2401.12345")
        keys = _canonical_keys(h)
        assert "arxiv:2401.12345" in keys
        assert "doi:10.48550/arxiv.2401.12345" in keys

    def test_arxiv_doi_maps_back_to_arxiv_id(self):
        h = AggregatedHit(doi="10.48550/arXiv.2401.12345")
        keys = _canonical_keys(h)
        assert "doi:10.48550/arxiv.2401.12345" in keys
        assert "arxiv:2401.12345" in keys

    def test_pmid_and_pmcid(self):
        h = AggregatedHit(pmid="123456", pmcid="PMC9999")
        keys = _canonical_keys(h)
        assert "pmid:123456" in keys
        assert "pmcid:PMC9999" in keys

    def test_pmcid_normalized_to_uppercase(self):
        h = AggregatedHit(pmcid="pmc1234")
        assert "pmcid:PMC1234" in _canonical_keys(h)

    def test_title_year_fallback_only_when_no_ids(self):
        h = AggregatedHit(title="Attention Is All You Need", year=2017)
        keys = _canonical_keys(h)
        assert any(k.startswith("title:") for k in keys)

    def test_no_keys_returns_empty(self):
        assert _canonical_keys(AggregatedHit()) == set()


# --------------------------------------------------------------------------
# dedup / merge
# --------------------------------------------------------------------------


class TestDedup:
    def test_arxiv_and_journal_doi_collapse(self):
        """Same paper from arXiv (raw ID) and OpenAlex (synthetic arXiv DOI) → one row."""
        a = AggregatedHit(title="X", arxiv_id="2401.12345", sources=["arxiv"])
        b = AggregatedHit(
            title="X",
            doi="10.48550/arxiv.2401.12345",
            sources=["openalex"],
        )
        out = _dedup([a, b])
        assert len(out) == 1
        assert sorted(out[0].sources) == ["arxiv", "openalex"]

    def test_three_way_collapse_via_bridging_keys(self):
        """A holds arxiv_id, B holds DOI, C holds both → all three collapse."""
        a = AggregatedHit(title="P", arxiv_id="2401.0001", sources=["arxiv"])
        b = AggregatedHit(title="P", doi="10.1038/foo", sources=["s2"])
        c = AggregatedHit(
            title="P",
            arxiv_id="2401.0001",
            doi="10.1038/foo",
            sources=["openalex"],
        )
        out = _dedup([a, b, c])
        assert len(out) == 1
        assert sorted(out[0].sources) == ["arxiv", "openalex", "s2"]

    def test_distinct_papers_preserved(self):
        a = AggregatedHit(title="P1", doi="10.1/aaa", sources=["s2"])
        b = AggregatedHit(title="P2", doi="10.2/bbb", sources=["openalex"])
        out = _dedup([a, b])
        assert len(out) == 2

    def test_pmid_dedup(self):
        a = AggregatedHit(title="X", pmid="123", sources=["pubmed"])
        b = AggregatedHit(title="X", pmid="123", doi="10.1/y", sources=["s2"])
        out = _dedup([a, b])
        assert len(out) == 1
        assert out[0].doi == "10.1/y"
        assert sorted(out[0].sources) == ["pubmed", "s2"]

    def test_title_year_fallback_dedup(self):
        a = AggregatedHit(title="A Special Paper", year=2020, sources=["arxiv"])
        b = AggregatedHit(title="A Special Paper!!", year=2020, sources=["chemrxiv"])
        out = _dedup([a, b])
        assert len(out) == 1


class TestMerge:
    def test_longer_abstract_wins(self):
        a = AggregatedHit(abstract="short")
        b = AggregatedHit(abstract="much longer abstract text here")
        m = _merge(a, b)
        assert m.abstract == "much longer abstract text here"

    def test_max_citations_wins(self):
        a = AggregatedHit(cited_by=10)
        b = AggregatedHit(cited_by=42)
        assert _merge(a, b).cited_by == 42

    def test_ids_unioned(self):
        a = AggregatedHit(doi="10.1/a", arxiv_id="2401.1")
        b = AggregatedHit(pmid="999", pmcid="PMC1")
        m = _merge(a, b)
        assert m.doi == "10.1/a"
        assert m.arxiv_id == "2401.1"
        assert m.pmid == "999"
        assert m.pmcid == "PMC1"

    def test_more_authors_wins(self):
        a = AggregatedHit(authors=["Alice"])
        b = AggregatedHit(authors=["Alice", "Bob", "Carol"])
        assert _merge(a, b).authors == ["Alice", "Bob", "Carol"]

    def test_sources_deduplicated(self):
        a = AggregatedHit(sources=["s2", "openalex"])
        b = AggregatedHit(sources=["openalex", "pubmed"])
        assert sorted(_merge(a, b).sources) == ["openalex", "pubmed", "s2"]


# --------------------------------------------------------------------------
# rank
# --------------------------------------------------------------------------


class TestRank:
    def test_more_sources_first(self):
        a = AggregatedHit(title="A", sources=["s2"])
        b = AggregatedHit(title="B", sources=["s2", "openalex", "pubmed"])
        ranked = _rank([a, b])
        assert ranked[0].title == "B"

    def test_tiebreak_by_citations(self):
        a = AggregatedHit(title="A", sources=["s2"], cited_by=5)
        b = AggregatedHit(title="B", sources=["s2"], cited_by=100)
        ranked = _rank([a, b])
        assert ranked[0].title == "B"

    def test_tiebreak_by_year(self):
        a = AggregatedHit(title="A", sources=["s2"], cited_by=0, year=2010)
        b = AggregatedHit(title="B", sources=["s2"], cited_by=0, year=2024)
        ranked = _rank([a, b])
        assert ranked[0].title == "B"


# --------------------------------------------------------------------------
# per-source extractors (using mocked raw API responses)
# --------------------------------------------------------------------------


class TestHitsFromOpenAlex:
    def test_extracts_doi_arxiv_pmid(self):
        raw = [{
            "title": "Test",
            "publication_year": 2024,
            "cited_by_count": 7,
            "authorships": [{"author": {"display_name": "Alice"}}],
            "doi": "https://doi.org/10.1038/foo",
            "ids": {
                "doi": "https://doi.org/10.1038/foo",
                "pmid": "https://pubmed.ncbi.nlm.nih.gov/12345",
                "pmcid": "PMC9999",
                "openalex": "https://openalex.org/W123",
            },
            "abstract_inverted_index": {"hello": [0], "world": [1]},
        }]
        with patch("lit.aggregator._search_openalex", return_value=raw):
            hits = _hits_from_openalex("q", 10)
        assert len(hits) == 1
        h = hits[0]
        assert h.doi == "10.1038/foo"
        assert h.pmid == "12345"
        assert h.pmcid == "PMC9999"
        assert h.year == 2024
        assert h.cited_by == 7
        assert h.abstract == "hello world"
        assert h.sources == ["openalex"]

    def test_arxiv_year_overrides_openalex_reindexing_year(self):
        """OpenAlex returns the re-indexing year (e.g. 2025) for old arxiv papers.
        The aggregator must prefer the year encoded in the arXiv ID itself."""
        raw = [{
            "title": "Attention Is All You Need",
            "publication_year": 2025,  # OpenAlex's wrong re-indexing year
            "cited_by_count": 100,
            "authorships": [{"author": {"display_name": "Vaswani"}}],
            "doi": "10.48550/arxiv.1706.03762",
            "ids": {"doi": "10.48550/arxiv.1706.03762"},
            "abstract_inverted_index": None,
        }]
        with patch("lit.aggregator._search_openalex", return_value=raw):
            hits = _hits_from_openalex("q", 10)
        assert hits[0].year == 2017  # decoded from "1706"
        assert hits[0].arxiv_id == "1706.03762"

    def test_arxiv_doi_extracted_from_synthetic_doi(self):
        raw = [{
            "title": "T",
            "publication_year": 2024,
            "cited_by_count": 0,
            "authorships": [{"author": {"display_name": "X"}}],
            "doi": "10.48550/arxiv.2401.12345",
            "ids": {"doi": "10.48550/arxiv.2401.12345"},
            "abstract_inverted_index": None,
        }]
        with patch("lit.aggregator._search_openalex", return_value=raw):
            hits = _hits_from_openalex("q", 10)
        assert hits[0].arxiv_id == "2401.12345"

    def test_empty_returns_empty(self):
        with patch("lit.aggregator._search_openalex", return_value=None):
            assert _hits_from_openalex("q", 10) == []


class TestHitsFromS2:
    def test_extracts_external_ids(self):
        raw = [{
            "title": "Foo",
            "year": 2023,
            "citationCount": 50,
            "abstract": "Body",
            "authors": [{"name": "A"}, {"name": "B"}],
            "externalIds": {
                "DOI": "10.1/X",
                "ArXiv": "2301.99999",
                "PubMed": "111",
                "PubMedCentral": "9876",
            },
        }]
        with patch("lit.aggregator._search_s2", return_value=raw):
            hits = _hits_from_s2("q", 10)
        h = hits[0]
        assert h.doi == "10.1/x"
        assert h.arxiv_id == "2301.99999"
        assert h.pmid == "111"
        assert h.pmcid == "PMC9876"
        assert h.cited_by == 50
        assert h.sources == ["s2"]

    def test_filters_passed_through(self):
        with patch("lit.aggregator._search_s2", return_value=None) as mock:
            _hits_from_s2("q", 10, year="2024", open_access=True, offset=0)
        # offset is stripped (S2 doesn't take it); year + open_access kept.
        kwargs = mock.call_args.kwargs
        assert kwargs.get("year") == "2024"
        assert kwargs.get("open_access") is True
        assert "offset" not in kwargs


class TestHitsFromPubmed:
    def test_doi_pmcid_from_articleids(self):
        raw = [{
            "uid": "12345",
            "title": "Some Paper.",
            "pubdate": "2024 Mar 15",
            "authors": [
                {"name": "Smith J", "authtype": "Author"},
                {"name": "Doe X", "authtype": "Author"},
            ],
            "articleids": [
                {"idtype": "pubmed", "value": "12345"},
                {"idtype": "doi", "value": "10.1/Bar"},
                {"idtype": "pmc", "value": "9999"},
            ],
        }]
        with patch("lit.aggregator._search_pubmed", return_value=raw):
            hits = _hits_from_pubmed("q", 10)
        h = hits[0]
        assert h.pmid == "12345"
        assert h.doi == "10.1/bar"
        assert h.pmcid == "PMC9999"
        assert h.year == 2024
        assert h.title == "Some Paper"  # trailing period stripped
        assert h.sources == ["pubmed"]


class TestHitsFromEuropePmc:
    def test_basic_extraction(self):
        raw = [{
            "title": "EuroPaper",
            "doi": "10.1/Euro",
            "pmid": "777",
            "pmcid": "PMC4321",
            "pubYear": "2022",
            "authorString": "Foo A, Bar B",
            "abstractText": "<i>An</i> abstract.",
            "citedByCount": 12,
        }]
        with patch("lit.aggregator._search_europepmc", return_value=raw):
            hits = _hits_from_europepmc("q", 10)
        h = hits[0]
        assert h.doi == "10.1/euro"
        assert h.pmid == "777"
        assert h.pmcid == "PMC4321"
        assert h.year == 2022
        assert h.authors == ["Foo A", "Bar B"]
        assert h.abstract == "An abstract."
        assert h.cited_by == 12


class TestHitsFromChemrxiv:
    def test_crossref_style_extraction(self):
        raw = [{
            "DOI": "10.26434/chemrxiv-2024-abcde",
            "title": ["A Chem Paper"],
            "author": [
                {"given": "Marie", "family": "Curie"},
                {"name": "Anonymous"},
            ],
            "posted": {"date-parts": [[2024, 3]]},
            "abstract": "<jats:p>Chem stuff.</jats:p>",
            "is-referenced-by-count": 3,
        }]
        with patch("lit.aggregator._search_chemrxiv", return_value=raw):
            hits = _hits_from_chemrxiv("q", 10)
        h = hits[0]
        assert h.doi == "10.26434/chemrxiv-2024-abcde"
        assert h.title == "A Chem Paper"
        assert h.authors == ["Marie Curie", "Anonymous"]
        assert h.year == 2024
        assert h.abstract == "Chem stuff."
        assert h.cited_by == 3


class TestHitsFromArxiv:
    def test_strips_version_and_fills_arxiv_id(self):
        class _MockAuthor:
            def __init__(self, name):
                self.name = name

        class _MockPaper:
            def __init__(self):
                self.entry_id = "http://arxiv.org/abs/2401.00001v3"
                self.title = "AX Paper"
                self.authors = [_MockAuthor("Alice")]
                self.published = datetime(2024, 1, 5)
                self.summary = "Summary text"
                self.doi = None

        with patch("lit.aggregator._search_arxiv", return_value=[_MockPaper()]):
            hits = _hits_from_arxiv("q", 10)
        h = hits[0]
        assert h.arxiv_id == "2401.00001"  # version stripped
        assert h.year == 2024
        assert h.abstract == "Summary text"


# --------------------------------------------------------------------------
# end-to-end: aggregate_search with all fetchers mocked
# --------------------------------------------------------------------------


class TestAggregateSearch:
    def test_dedups_across_sources(self):
        """Same paper reported by S2 (DOI) and OpenAlex (arXiv DOI) should collapse."""
        oa = [{
            "title": "Attention",
            "publication_year": 2017,
            "cited_by_count": 100,
            "authorships": [{"author": {"display_name": "Vaswani"}}],
            "doi": "10.48550/arxiv.1706.03762",
            "ids": {"doi": "10.48550/arxiv.1706.03762"},
            "abstract_inverted_index": None,
        }]
        s2 = [{
            "title": "Attention",
            "year": 2017,
            "citationCount": 95,
            "abstract": "Self-attention paper.",
            "authors": [{"name": "Vaswani"}],
            "externalIds": {"ArXiv": "1706.03762", "DOI": "10.48550/arXiv.1706.03762"},
        }]
        with patch("lit.aggregator._search_openalex", return_value=oa), \
             patch("lit.aggregator._search_s2", return_value=s2), \
             patch("lit.aggregator._search_pubmed", return_value=None), \
             patch("lit.aggregator._search_europepmc", return_value=None), \
             patch("lit.aggregator._search_chemrxiv", return_value=None), \
             patch("lit.aggregator._search_arxiv", return_value=[]):
            hits = aggregate_search("attention", max_results=10)
        assert len(hits) == 1
        h = hits[0]
        assert sorted(h.sources) == ["openalex", "s2"]
        # Citations should be max of the two.
        assert h.cited_by == 100
        # Abstract should come from S2 (OpenAlex's was None).
        assert h.abstract == "Self-attention paper."

    def test_one_source_failing_does_not_kill_run(self):
        """If a fetcher raises, others still produce results."""
        oa = [{
            "title": "OK",
            "publication_year": 2024,
            "cited_by_count": 0,
            "authorships": [{"author": {"display_name": "A"}}],
            "doi": "10.1/ok",
            "ids": {"doi": "10.1/ok"},
            "abstract_inverted_index": None,
        }]

        def _boom(*a, **kw):
            raise RuntimeError("S2 down")

        with patch("lit.aggregator._search_openalex", return_value=oa), \
             patch("lit.aggregator._search_s2", side_effect=_boom), \
             patch("lit.aggregator._search_pubmed", return_value=None), \
             patch("lit.aggregator._search_europepmc", return_value=None), \
             patch("lit.aggregator._search_chemrxiv", return_value=None), \
             patch("lit.aggregator._search_arxiv", return_value=[]):
            hits = aggregate_search("q", max_results=10)
        assert len(hits) == 1
        assert hits[0].sources == ["openalex"]

    def test_max_results_truncates(self):
        many = [{
            "title": f"P{i}",
            "publication_year": 2024,
            "cited_by_count": i,
            "authorships": [{"author": {"display_name": "X"}}],
            "doi": f"10.1/p{i}",
            "ids": {"doi": f"10.1/p{i}"},
            "abstract_inverted_index": None,
        } for i in range(50)]
        with patch("lit.aggregator._search_openalex", return_value=many), \
             patch("lit.aggregator._search_s2", return_value=None), \
             patch("lit.aggregator._search_pubmed", return_value=None), \
             patch("lit.aggregator._search_europepmc", return_value=None), \
             patch("lit.aggregator._search_chemrxiv", return_value=None), \
             patch("lit.aggregator._search_arxiv", return_value=[]):
            hits = aggregate_search("q", max_results=5)
        assert len(hits) == 5
        # ranking by citations desc means top 5 are i=49..45
        assert [h.title for h in hits] == [f"P{i}" for i in (49, 48, 47, 46, 45)]

    def test_default_sources_is_all(self):
        assert set(DEFAULT_SOURCES) == {
            "openalex", "s2", "pubmed", "europepmc", "chemrxiv", "arxiv"
        }


class TestSnippetMode:
    """Aggregator routes S2 to /snippet/search when filters['snippet'] is True."""

    def test_snippet_filter_invokes_snippet_endpoint(self):
        snippet_raw = [{
            "paperId": "abc",
            "title": "Snippet Paper",
            "year": 2024,
            "authors": [{"name": "Carol"}],
            "abstract": "matched body fragment",
            "citationCount": 7,
            "externalIds": {"DOI": "10.1/snip"},
        }]
        with patch("lit.aggregator._search_s2_snippet", return_value=snippet_raw) as snip, \
             patch("lit.aggregator._search_s2") as plain:
            hits = _hits_from_s2("query", 10, snippet=True)
        snip.assert_called_once()
        plain.assert_not_called()
        assert len(hits) == 1
        assert hits[0].abstract == "matched body fragment"

    def test_default_path_does_not_use_snippet(self):
        with patch("lit.aggregator._search_s2", return_value=None) as plain, \
             patch("lit.aggregator._search_s2_snippet") as snip:
            _hits_from_s2("query", 10)
        plain.assert_called_once()
        snip.assert_not_called()


class TestS2SnippetSearch:
    """lit/sources/s2.py::_search_s2_snippet — endpoint shape adaptation."""

    def _resp(self, json_data):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.json.return_value = json_data
        r.raise_for_status.return_value = None
        return r

    def test_collapses_multiple_snippets_per_paper(self):
        from lit.sources.s2 import _search_s2_snippet
        data = {"data": [
            {"snippet": {"text": "hit one"}, "paper": {"corpusId": "1", "title": "P1",
             "year": 2024, "authors": [{"name": "A"}], "externalIds": {}, "citationCount": 5}},
            {"snippet": {"text": "hit two same paper"}, "paper": {"corpusId": "1",
             "title": "P1", "year": 2024, "authors": [{"name": "A"}], "externalIds": {}, "citationCount": 5}},
            {"snippet": {"text": "hit three"}, "paper": {"corpusId": "2", "title": "P2",
             "year": 2023, "authors": [{"name": "B"}], "externalIds": {}, "citationCount": 0}},
        ]}
        with patch("lit.sources.s2._request_with_retry", return_value=self._resp(data)):
            out = _search_s2_snippet("hit", max_results=10)
        # Two unique papers, first snippet wins per paper.
        assert len(out) == 2
        assert out[0]["abstract"] == "hit one"
        assert out[1]["abstract"] == "hit three"


class TestDomainPresets:
    """Sanity-check the per-domain source/filter shortcuts."""

    def test_bio_excludes_arxiv_and_chemrxiv(self):
        from lit.aggregator import DOMAIN_PRESETS
        srcs = set(DOMAIN_PRESETS["bio"]["sources"])
        assert {"openalex", "s2", "pubmed", "europepmc"} <= srcs
        assert "arxiv" not in srcs
        assert "chemrxiv" not in srcs

    def test_chem_includes_chemrxiv_excludes_pubmed_arxiv(self):
        from lit.aggregator import DOMAIN_PRESETS
        srcs = set(DOMAIN_PRESETS["chem"]["sources"])
        assert "chemrxiv" in srcs
        assert "pubmed" not in srcs
        assert "arxiv" not in srcs

    def test_cs_includes_arxiv_excludes_biomed(self):
        from lit.aggregator import DOMAIN_PRESETS
        srcs = set(DOMAIN_PRESETS["cs"]["sources"])
        assert "arxiv" in srcs
        assert "pubmed" not in srcs
        assert "europepmc" not in srcs

    def test_every_domain_includes_openalex_and_s2(self):
        """OpenAlex+S2 are the broadest indexes — every domain should keep them."""
        from lit.aggregator import DOMAIN_PRESETS
        for name, preset in DOMAIN_PRESETS.items():
            srcs = set(preset["sources"])
            assert "openalex" in srcs, f"{name} missing openalex"
            assert "s2" in srcs, f"{name} missing s2"

    def test_every_domain_has_fields_of_study(self):
        from lit.aggregator import DOMAIN_PRESETS
        for name, preset in DOMAIN_PRESETS.items():
            assert preset.get("fields_of_study"), f"{name} missing fields_of_study"
