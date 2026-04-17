"""Batch full-text fetch + manual-import workflow.

Two halves:

  - :func:`run_batch` walks a list of paper IDs through the same fulltext
    chain ``cmd_fulltext`` uses, collecting per-ID success/failure. Failed
    IDs are written to a TSV manifest with the publisher landing-page URL
    so the user can browser-download them manually.

  - :func:`run_import` reads such a manifest, scans a directory for PDFs
    matching the suggested basenames, and ingests each via the same path
    as ``--from-file``. Lets the user batch-resolve everything that
    automatic chains couldn't.

Both halves treat per-ID failures as data, not exceptions: one bad ID
never aborts the run.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Callable

from lit.ids import basename_for_id, extract_paper_id
from lit.pdf import ingest_local_pdf


# TSV columns the manifest uses. ``url_to_try`` is what the user opens in a
# browser to download the PDF; for DOIs that's https://doi.org/{doi}, for
# arXiv it's https://arxiv.org/abs/{id}, etc.
MANIFEST_COLUMNS = ("id", "id_type", "basename", "url_to_try", "reason")


def _publisher_url(id_type: str, clean_id: str) -> str:
    """Best-guess landing URL for manual download."""
    if id_type == "doi":
        return f"https://doi.org/{clean_id}"
    if id_type == "arxiv":
        return f"https://arxiv.org/abs/{clean_id}"
    if id_type == "pmid":
        return f"https://pubmed.ncbi.nlm.nih.gov/{clean_id}/"
    if id_type == "pmcid":
        return f"https://pmc.ncbi.nlm.nih.gov/articles/{clean_id.upper()}/"
    return ""


def _read_id_lines(path: Path) -> list[str]:
    """One ID per line; ``#`` comments and blank lines ignored."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def run_batch(
    ids_path: Path,
    *,
    try_fetch: Callable[[str, str], bool],
    manifest_path: Path,
) -> tuple[int, int]:
    """Walk ``ids_path``; for each line call ``try_fetch(id_type, clean_id)``.

    ``try_fetch`` is the caller's adapter to its full-text dispatch (returns
    True on success, False otherwise). All bookkeeping — counters, manifest
    writing, per-ID stderr framing — lives here so the caller stays small.

    Returns ``(success_count, fail_count)``.
    """
    ids = _read_id_lines(ids_path)
    if not ids:
        print(f"No IDs found in {ids_path}", file=sys.stderr)
        return 0, 0

    successes = 0
    failures: list[dict] = []
    for n, raw_id in enumerate(ids, 1):
        id_type, clean_id = extract_paper_id(raw_id)
        print(
            f"\n=== [{n}/{len(ids)}] {raw_id} ({id_type or 'unknown'}) ===",
            file=sys.stderr,
        )
        if id_type == "unknown":
            failures.append({
                "id": raw_id,
                "id_type": "unknown",
                "basename": "",
                "url_to_try": "",
                "reason": "ID not parseable",
            })
            continue

        basename = basename_for_id(id_type, clean_id)
        try:
            ok = try_fetch(id_type, clean_id)
        except Exception as e:  # noqa: BLE001 — never let one ID kill the run
            print(f"  ERROR: {e}", file=sys.stderr)
            ok = False

        if ok:
            successes += 1
        else:
            failures.append({
                "id": raw_id,
                "id_type": id_type,
                "basename": basename,
                "url_to_try": _publisher_url(id_type, clean_id),
                "reason": "automatic chain exhausted",
            })

    if failures:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
            w.writeheader()
            w.writerows(failures)

    print(
        f"\nBatch done: {successes} ok, {len(failures)} failed.",
        file=sys.stderr,
    )
    if failures:
        print(
            f"Failed manifest: {manifest_path}\n"
            f"  Open the listed URLs in a browser, save each PDF as "
            f"<basename>.pdf into a directory,\n"
            f"  then run `fulltext-import <dir> --manifest {manifest_path}`.",
            file=sys.stderr,
        )
    return successes, len(failures)


def _read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def run_import(
    pdf_dir: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
) -> tuple[int, int]:
    """Scan ``pdf_dir`` for PDFs and ingest each into ``output_dir``.

    Matching strategy:
      1. If ``manifest_path`` is given and the file exists: each PDF whose
         stem matches a manifest ``basename`` is ingested under that basename.
      2. Unmatched PDFs are tried against :func:`extract_paper_id` on the
         filename stem — supports stems like ``PMID12345``, ``PMC9999``,
         ``2401.12345``, ``10.1038_foo``.

    Returns ``(imported_count, skipped_count)``.
    """
    pdf_dir = pdf_dir.expanduser().resolve()
    if not pdf_dir.is_dir():
        print(f"Not a directory: {pdf_dir}", file=sys.stderr)
        return 0, 0

    manifest = _read_manifest(manifest_path) if manifest_path else []
    by_basename = {row["basename"]: row for row in manifest if row.get("basename")}

    imported = 0
    skipped = 0
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        stem = pdf.stem
        # 1. exact basename match against manifest
        if stem in by_basename:
            print(f"  → ingesting {pdf.name} as {stem} (manifest match)", file=sys.stderr)
            ingest_local_pdf(str(pdf), stem, output_dir)
            imported += 1
            continue

        # 2. parse the filename and re-derive a basename
        # Try a couple of reverse rules: PMID/PMC stems, raw arxiv IDs,
        # underscored DOIs (foo_bar -> foo/bar).
        recovered = _reverse_basename_to_id(stem)
        if recovered is None:
            print(f"  ? skipping {pdf.name} (no matching ID)", file=sys.stderr)
            skipped += 1
            continue
        id_type, clean_id = recovered
        basename = basename_for_id(id_type, clean_id)
        print(f"  → ingesting {pdf.name} as {basename} ({id_type})", file=sys.stderr)
        ingest_local_pdf(str(pdf), basename, output_dir)
        imported += 1

    print(f"\nImport done: {imported} ingested, {skipped} skipped.", file=sys.stderr)
    return imported, skipped


def _reverse_basename_to_id(stem: str) -> tuple[str, str] | None:
    """Best-effort reverse of :func:`basename_for_id` from a filename stem."""
    if stem.startswith("PMID") and stem[4:].isdigit():
        return ("pmid", stem[4:])
    if stem.upper().startswith("PMC") and stem[3:].isdigit():
        return ("pmcid", stem.upper())
    # Underscored DOI: 10.1038_s41586-020-2649-2 → 10.1038/s41586-020-2649-2
    if stem.startswith("10.") and "_" in stem:
        return ("doi", stem.replace("_", "/", 1))
    # Bare arXiv ID
    id_type, clean_id = extract_paper_id(stem)
    if id_type == "arxiv":
        return ("arxiv", clean_id)
    return None
