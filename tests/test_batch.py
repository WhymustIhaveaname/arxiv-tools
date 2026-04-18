"""Tests for lit/batch.py — batch fulltext + manual-import workflow."""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from lit.batch import (
    MANIFEST_COLUMNS,
    _publisher_url,
    _read_id_lines,
    _reverse_basename_to_id,
    run_batch,
    run_import,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class TestPublisherUrl:
    def test_doi(self):
        assert _publisher_url("doi", "10.1038/foo") == "https://doi.org/10.1038/foo"

    def test_arxiv(self):
        assert _publisher_url("arxiv", "2401.12345") == "https://arxiv.org/abs/2401.12345"

    def test_pmid(self):
        assert _publisher_url("pmid", "12345") == "https://pubmed.ncbi.nlm.nih.gov/12345/"

    def test_pmcid_uppercase(self):
        assert _publisher_url("pmcid", "pmc999") == "https://pmc.ncbi.nlm.nih.gov/articles/PMC999/"

    def test_unknown(self):
        assert _publisher_url("unknown", "x") == ""


class TestReadIdLines:
    def test_skips_blanks_and_comments(self, tmp_path):
        p = tmp_path / "ids.txt"
        p.write_text("# header\n\n2401.12345\n  39876543  \n# trailing\n10.1038/x\n")
        assert _read_id_lines(p) == ["2401.12345", "39876543", "10.1038/x"]


class TestReverseBasenameToId:
    def test_pmid_stem(self):
        assert _reverse_basename_to_id("PMID12345") == ("pmid", "12345")

    def test_pmcid_stem(self):
        assert _reverse_basename_to_id("PMC9999") == ("pmcid", "PMC9999")

    def test_pmcid_lowercase_normalized(self):
        # PMC prefix detection is case-insensitive; result is upper.
        assert _reverse_basename_to_id("pmc9999") == ("pmcid", "PMC9999")

    def test_underscored_doi(self):
        assert _reverse_basename_to_id("10.1038_s41586-020-2649-2") == (
            "doi", "10.1038/s41586-020-2649-2",
        )

    def test_arxiv_stem(self):
        assert _reverse_basename_to_id("2401.12345") == ("arxiv", "2401.12345")

    def test_unknown_returns_none(self):
        assert _reverse_basename_to_id("random_paper_title") is None


# --------------------------------------------------------------------------
# run_batch
# --------------------------------------------------------------------------


class TestRunBatch:
    def test_all_succeed_no_manifest_written(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("2401.12345\n39876543\n")
        manifest = tmp_path / "out.tsv"

        succ, fail = run_batch(
            ids,
            try_fetch=lambda id_type, clean_id: True,
            manifest_path=manifest,
        )
        assert succ == 2
        assert fail == 0
        assert not manifest.exists()  # no failures → no manifest

    def test_failures_written_to_manifest(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("2401.12345\n10.1038/foo\n")
        manifest = tmp_path / "out.tsv"

        # All fail.
        succ, fail = run_batch(
            ids,
            try_fetch=lambda id_type, clean_id: False,
            manifest_path=manifest,
        )
        assert succ == 0
        assert fail == 2
        assert manifest.exists()

        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert len(rows) == 2
        assert set(rows[0].keys()) == set(MANIFEST_COLUMNS)
        # First failure: arxiv ID
        assert rows[0]["id_type"] == "arxiv"
        assert rows[0]["basename"] == "2401.12345"
        assert rows[0]["url_to_try"] == "https://arxiv.org/abs/2401.12345"
        # Second: DOI
        assert rows[1]["id_type"] == "doi"
        assert rows[1]["url_to_try"] == "https://doi.org/10.1038/foo"

    def test_unknown_id_recorded_as_failure(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("not-a-real-id\n")
        manifest = tmp_path / "out.tsv"

        succ, fail = run_batch(
            ids,
            try_fetch=lambda *_: True,
            manifest_path=manifest,
        )
        assert succ == 0 and fail == 1
        with manifest.open() as f:
            row = list(csv.DictReader(f, delimiter="\t"))[0]
        assert row["id_type"] == "unknown"
        assert "not parseable" in row["reason"]

    def test_per_id_exception_does_not_kill_run(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("2401.12345\n39876543\n")
        manifest = tmp_path / "out.tsv"

        calls = []
        def _flaky(id_type, clean_id):
            calls.append(clean_id)
            if clean_id == "2401.12345":
                raise RuntimeError("boom")
            return True

        succ, fail = run_batch(ids, try_fetch=_flaky, manifest_path=manifest)
        assert calls == ["2401.12345", "39876543"]  # second still ran
        assert succ == 1 and fail == 1

    def test_empty_file_no_op(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("\n# only comments\n\n")
        manifest = tmp_path / "out.tsv"
        succ, fail = run_batch(ids, try_fetch=lambda *_: True, manifest_path=manifest)
        assert succ == 0 and fail == 0
        assert not manifest.exists()


# --------------------------------------------------------------------------
# run_import
# --------------------------------------------------------------------------


def _write_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n%fake content for tests\n%%EOF\n")


class TestRunImport:
    def test_manifest_basename_match_invokes_ingest(self, tmp_path):
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        manifest = tmp_path / "m.tsv"

        # One PDF named exactly per manifest basename
        _write_pdf(pdf_dir / "PMID12345.pdf")
        with manifest.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
            w.writeheader()
            w.writerow({
                "id": "12345", "id_type": "pmid",
                "basename": "PMID12345",
                "url_to_try": "https://pubmed.ncbi.nlm.nih.gov/12345/",
                "reason": "exhausted",
            })

        with patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir, manifest_path=manifest)
        assert imported == 1
        assert skipped == 0
        mock_ingest.assert_called_once()
        args, _ = mock_ingest.call_args
        assert args[1] == "PMID12345"
        assert args[2] == out_dir

    def test_filename_heuristic_when_no_manifest(self, tmp_path):
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        _write_pdf(pdf_dir / "PMID12345.pdf")
        _write_pdf(pdf_dir / "10.1038_s41586-020-2649-2.pdf")
        _write_pdf(pdf_dir / "2401.12345.pdf")

        with patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir)
        assert imported == 3
        assert skipped == 0
        basenames = sorted(call.args[1] for call in mock_ingest.call_args_list)
        assert basenames == [
            "10.1038_s41586-020-2649-2",
            "2401.12345",
            "PMID12345",
        ]

    def test_unmatched_filename_skipped(self, tmp_path):
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        _write_pdf(pdf_dir / "random_unrelated.pdf")

        with patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir)
        assert imported == 0
        assert skipped == 1
        mock_ingest.assert_not_called()

    def test_missing_dir_returns_zeros(self, tmp_path):
        out_dir = tmp_path / "out"
        imported, skipped = run_import(tmp_path / "no-such-dir", out_dir)
        assert imported == 0 and skipped == 0

    def test_pdf_content_doi_beats_filename(self, tmp_path):
        """Filename is garbage; DOI in PDF content is what matters."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        _write_pdf(pdf_dir / "untitled_download.pdf")

        with patch(
            "lit.batch.extract_doi_from_pdf",
            return_value="10.1038/s41586-025-08800-x",
        ), patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir)
        assert imported == 1 and skipped == 0
        assert mock_ingest.call_args.args[1] == "10.1038_s41586-025-08800-x"

    def test_already_cached_basename_is_skipped(self, tmp_path):
        """Dropping the same PDF twice must not overwrite cached structured data."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _write_pdf(pdf_dir / "paper.pdf")
        # Simulate pre-existing richer cache entry (JATS XML).
        (out_dir / "10.1038_x.xml").write_text("<jats>...")

        with patch(
            "lit.batch.extract_doi_from_pdf", return_value="10.1038/x"
        ), patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir)
        assert imported == 0 and skipped == 1
        mock_ingest.assert_not_called()

    def test_non_pdf_file_skipped(self, tmp_path):
        """Files without %PDF magic must never be ingested."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        (pdf_dir / "not_a_pdf.pdf").write_bytes(b"<html>error</html>")

        with patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir)
        assert imported == 0 and skipped == 1
        mock_ingest.assert_not_called()

    def test_manifest_suffix_match_for_publisher_native_filename(self, tmp_path):
        """AAAS / Elsevier drop the DOI prefix in native filenames."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        manifest = tmp_path / "m.tsv"
        _write_pdf(pdf_dir / "scisignal.ado6430.pdf")
        with manifest.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
            w.writeheader()
            w.writerow({
                "id": "10.1126/scisignal.ado6430", "id_type": "doi",
                "basename": "10.1126_scisignal.ado6430",
                "url_to_try": "https://doi.org/10.1126/scisignal.ado6430",
                "reason": "exhausted",
            })

        with patch("lit.batch.extract_doi_from_pdf", return_value=None), \
             patch("lit.batch.ingest_local_pdf") as mock_ingest:
            imported, skipped = run_import(pdf_dir, out_dir, manifest_path=manifest)
        assert imported == 1 and skipped == 0
        assert mock_ingest.call_args.args[1] == "10.1126_scisignal.ado6430"

    def test_ingested_pdf_removed_from_staging(self, tmp_path):
        """After successful ingest the staged copy is cleaned up."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        src = pdf_dir / "10.1038_x.pdf"
        _write_pdf(src)

        with patch("lit.batch.extract_doi_from_pdf", return_value=None), \
             patch("lit.batch.ingest_local_pdf"):
            run_import(pdf_dir, out_dir)
        assert not src.exists()


class TestDownloadMeFile:
    def test_written_on_failure(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("10.1038/a\n10.1038/b\n")
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"

        run_batch(
            ids,
            try_fetch=lambda *_: False,
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        text = download_me.read_text()
        assert "https://doi.org/10.1038/a" in text
        assert "https://doi.org/10.1038/b" in text
        assert "scp" in text
        assert str(drop) in text
        assert "fulltext-import" in text

    def test_not_written_when_all_succeed(self, tmp_path):
        ids = tmp_path / "ids.txt"
        ids.write_text("10.1038/a\n")
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"

        run_batch(
            ids,
            try_fetch=lambda *_: True,
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        assert not download_me.exists()

    def test_stale_artefacts_cleaned_on_all_success(self, tmp_path):
        """Re-running batch after everything's cached must wipe old manifest/guide."""
        ids = tmp_path / "ids.txt"
        ids.write_text("10.1038/a\n")
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        # Seed stale artefacts from a prior (failed) run.
        manifest.write_text("stale manifest")
        download_me.write_text("stale download guide")

        run_batch(
            ids,
            try_fetch=lambda *_: True,
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        assert not manifest.exists()
        assert not download_me.exists()
