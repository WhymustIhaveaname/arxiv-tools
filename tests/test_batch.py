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
    record_single_failure,
    record_single_success,
    run_batch,
    run_import,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class TestPublisherUrl:
    def test_doi(self):
        assert _publisher_url("doi", "10.1038/foo") == "https://doi.org/10.1038/foo"

    def test_chemrxiv_doi_uses_article_page(self):
        """ChemRxiv DOIs point at chemrxiv.org/doi/full/ so the user (or a
        Playwright-MCP agent) lands on the article page instead of
        bouncing through the Cloudflare-blocked asset URL."""
        assert (
            _publisher_url("doi", "10.26434/chemrxiv-2024-zmmnw")
            == "https://chemrxiv.org/doi/full/10.26434/chemrxiv-2024-zmmnw"
        )

    def test_biorxiv_doi_uses_content_url(self):
        assert (
            _publisher_url("doi", "10.1101/2023.01.01.523456")
            == "https://www.biorxiv.org/content/10.1101/2023.01.01.523456"
        )

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


# --------------------------------------------------------------------------
# Failure-set sync: import prunes, single-paper failures append, single-paper
# successes drop.  These three flows share `_sync_failures` so the manifest
# and download_me.txt cannot drift.
# --------------------------------------------------------------------------


def _seed_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
        w.writeheader()
        w.writerows(rows)


def _doi_row(doi: str) -> dict:
    base = doi.lower().replace("/", "_")
    return {
        "id": doi, "id_type": "doi", "basename": base,
        "url_to_try": f"https://doi.org/{doi}",
        "reason": "automatic chain exhausted",
    }


class TestImportPrunesManifest:
    def test_partial_import_leaves_remaining_failures(self, tmp_path):
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a"), _doi_row("10.1038/b")])
        download_me.write_text("stale guide")
        _write_pdf(pdf_dir / "10.1038_a.pdf")

        with patch("lit.batch.extract_doi_from_pdf", return_value="10.1038/a"), \
             patch("lit.batch.ingest_local_pdf"):
            run_import(
                pdf_dir, out_dir,
                manifest_path=manifest,
                download_me_path=download_me,
                manual_pdf_dir=drop,
            )

        # One failure remains; manifest + download_me both rewritten.
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert [r["id"] for r in rows] == ["10.1038/b"]
        text = download_me.read_text()
        assert "10.1038/b" in text and "10.1038/a" not in text

    def test_full_import_clears_both_files(self, tmp_path):
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])
        download_me.write_text("stale guide")
        _write_pdf(pdf_dir / "10.1038_a.pdf")

        with patch("lit.batch.extract_doi_from_pdf", return_value="10.1038/a"), \
             patch("lit.batch.ingest_local_pdf"):
            run_import(
                pdf_dir, out_dir,
                manifest_path=manifest,
                download_me_path=download_me,
                manual_pdf_dir=drop,
            )

        assert not manifest.exists()
        assert not download_me.exists()

    def test_already_cached_basename_still_prunes(self, tmp_path):
        """If the user races and the basename is already in cache, we still
        treat the manifest entry as resolved — the download is no longer
        needed regardless of who wrote the bytes."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])
        download_me.write_text("stale guide")
        (out_dir / "10.1038_a.xml").write_text("<jats/>")
        _write_pdf(pdf_dir / "10.1038_a.pdf")

        with patch("lit.batch.extract_doi_from_pdf", return_value="10.1038/a"), \
             patch("lit.batch.ingest_local_pdf"):
            run_import(
                pdf_dir, out_dir,
                manifest_path=manifest,
                download_me_path=download_me,
                manual_pdf_dir=drop,
            )

        assert not manifest.exists()
        assert not download_me.exists()

    def test_no_manifest_path_keeps_legacy_behaviour(self, tmp_path):
        """Callers that don't pass manifest_path still get the original
        ingest-only behaviour with no surprise file writes."""
        pdf_dir = tmp_path / "downloads"
        pdf_dir.mkdir()
        out_dir = tmp_path / "out"
        _write_pdf(pdf_dir / "10.1038_a.pdf")

        with patch("lit.batch.extract_doi_from_pdf", return_value="10.1038/a"), \
             patch("lit.batch.ingest_local_pdf"):
            imported, _ = run_import(pdf_dir, out_dir)
        assert imported == 1
        # No manifest implies no download_me side-effects to worry about.


class TestRecordSingleFailure:
    def test_appends_new_id(self, tmp_path):
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"

        record_single_failure(
            "10.1038/a",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert [r["id"] for r in rows] == ["10.1038/a"]
        assert "10.1038/a" in download_me.read_text()

    def test_dedup_on_repeat(self, tmp_path):
        """A retry that fails again must not double-list."""
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])

        record_single_failure(
            "10.1038/a",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert len(rows) == 1

    def test_appends_to_existing_manifest(self, tmp_path):
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])

        record_single_failure(
            "10.1038/b",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert sorted(r["id"] for r in rows) == ["10.1038/a", "10.1038/b"]


class TestRecordSingleSuccess:
    def test_drops_listed_id(self, tmp_path):
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a"), _doi_row("10.1038/b")])
        download_me.write_text("stale")

        record_single_success(
            "10.1038/a",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert [r["id"] for r in rows] == ["10.1038/b"]

    def test_clears_files_when_last_entry_removed(self, tmp_path):
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])
        download_me.write_text("stale")

        record_single_success(
            "10.1038/a",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        assert not manifest.exists()
        assert not download_me.exists()

    def test_no_op_when_id_not_listed(self, tmp_path):
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        _seed_manifest(manifest, [_doi_row("10.1038/a")])

        record_single_success(
            "10.1038/never-listed",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        with manifest.open() as f:
            rows = list(csv.DictReader(f, delimiter="\t"))
        assert [r["id"] for r in rows] == ["10.1038/a"]

    def test_no_manifest_file_is_safe(self, tmp_path):
        """Successful fetch when the queue file doesn't exist is a no-op."""
        manifest = tmp_path / "m.tsv"
        download_me = tmp_path / "download_me.txt"
        drop = tmp_path / "drops"
        record_single_success(
            "10.1038/a",
            manifest_path=manifest,
            download_me_path=download_me,
            manual_pdf_dir=drop,
        )
        assert not manifest.exists()
        assert not download_me.exists()
