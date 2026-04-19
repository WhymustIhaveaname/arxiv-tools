"""Batch full-text fetch + manual-import workflow.

Three halves:

  - :func:`run_batch` walks a list of paper IDs through the same fulltext
    chain ``cmd_fulltext`` uses, collecting per-ID success/failure.

  - :func:`run_import` reads a manifest, scans a directory for PDFs,
    and ingests each via the same path as ``--from-file``. Matching is
    content-first (DOI embedded in the PDF), then filename-based.

  - :func:`record_single_failure` / :func:`record_single_success` let
    the interactive ``cmd_fulltext`` flow contribute to the same
    manifest, so a single-paper failure is just as visible as a batch
    failure and a single-paper success quietly removes a stale entry.

Failed IDs land in a TSV manifest plus a human-friendly
``download_me.txt`` the user can follow start-to-finish without thinking
about filenames or basenames. :func:`_sync_failures` is the single
writer for both files: every code path that mutates the failure set
goes through it, so the manifest and download guide can never drift.

All halves treat per-ID failures as data, not exceptions: one bad ID
never aborts the run.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Callable

from lit.config import SCP_HOST, SCP_SOURCE
from lit.ids import basename_for_id, extract_paper_id
from lit.pdf import extract_doi_from_pdf, ingest_local_pdf, is_pdf_bytes


# ChemRxiv (and a few other preprint servers) issue versioned DOIs of the
# form ``10.x/preprint-id/v2``. After ``basename_for_id`` collapses ``/``
# to ``_`` that becomes ``..._v2``. The bare form is the same underlying
# preprint, so a queue listing the bare form is satisfied by ingesting any
# version. Match anchored at end so we don't strip incidental ``_v\d`` that
# happens to be part of a real ID body.
_VERSION_SUFFIX_RE = re.compile(r"_v\d+$")


def _basename_aliases(basename: str) -> set[str]:
    """All basenames that should be considered the same paper.

    Today this means: a versioned ChemRxiv basename also matches its bare
    form. Returns the input unchanged when no alias rule applies, so
    callers can use ``in aliases`` without special-casing.
    """
    aliases = {basename}
    stripped = _VERSION_SUFFIX_RE.sub("", basename)
    if stripped != basename:
        aliases.add(stripped)
    return aliases


# TSV columns the manifest uses. ``url_to_try`` is what the user opens in a
# browser to download the PDF; for DOIs that's https://doi.org/{doi}, for
# arXiv it's https://arxiv.org/abs/{id}, etc.
MANIFEST_COLUMNS = ("id", "id_type", "basename", "url_to_try", "reason")


def _publisher_url(id_type: str, clean_id: str) -> str:
    """Best-guess landing URL for manual download.

    DOI lands default at ``doi.org``; ChemRxiv / bioRxiv / medRxiv are
    special-cased to the publisher's article page because the generic
    ``doi.org`` redirect bounces through Cloudflare-protected asset URLs
    that typically fail from the same IPs the batch chain already failed
    on. Sending the user (or downstream Playwright-MCP agent) directly at
    the article page lets them see the publisher's own manual-download
    link without an extra redirect.
    """
    if id_type == "doi":
        low = clean_id.lower()
        if low.startswith("10.26434/"):
            return f"https://chemrxiv.org/doi/full/{clean_id}"
        if low.startswith("10.1101/"):
            # bioRxiv + medRxiv share the 10.1101 prefix. doi.org does the
            # right routing, but pointing at www.biorxiv.org/content/ gets
            # you a clickable PDF button one step earlier. medRxiv papers
            # redirect transparently from biorxiv if queried there.
            return f"https://www.biorxiv.org/content/{clean_id}"
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


def _write_download_me(
    failures: list[dict],
    *,
    download_me_path: Path,
    manual_pdf_dir: Path,
) -> None:
    """Render a human-friendly README + URL list the user can follow verbatim.

    The file is deliberately plain text (no markdown) so it renders cleanly
    in any editor or terminal. Contains: numbered URL list, the exact scp
    command for upload, and the exact import command to run afterward.
    """
    lines: list[str] = []
    lines.append(f"arxiv-tools: {len(failures)} papers need manual download.")
    lines.append("")
    lines.append("=" * 70)
    lines.append("STEP 1  Open each URL in your browser and save the PDF.")
    lines.append("        Keep whatever filename the browser gives — it doesn't matter.")
    lines.append("=" * 70)
    lines.append("")
    for n, row in enumerate(failures, 1):
        url = row.get("url_to_try") or ""
        paper_id = row.get("id") or ""
        if url:
            lines.append(f"  {n:2d}. {url}")
        else:
            lines.append(f"  {n:2d}. (no URL available for {paper_id})")
    lines.append("")
    lines.append("=" * 70)
    lines.append("STEP 2  Upload every downloaded PDF to the server.")
    lines.append("        From Windows PowerShell (Win10+ has scp built in):")
    lines.append("=" * 70)
    lines.append("")
    source = SCP_SOURCE or "$env:USERPROFILE\\Downloads\\*.pdf"
    host = SCP_HOST or "<user>@<host>"
    lines.append(f"  scp {source} {host}:{manual_pdf_dir}/")
    lines.append("")
    if not (SCP_HOST and SCP_SOURCE):
        lines.append(
            "  (Set $ARXIV_SCP_HOST and $ARXIV_SCP_SOURCE in your shell rc to"
        )
        lines.append(
            "  bake your own values into this command next time.)"
        )
        lines.append("")
    lines.append("=" * 70)
    lines.append('STEP 3  Tell Claude "PDFs uploaded". Claude will run:')
    lines.append("=" * 70)
    lines.append("")
    lines.append(
        f"  uv run arxiv_tool.py fulltext-import {manual_pdf_dir}"
    )
    lines.append("")
    lines.append("  Each PDF is identified by the DOI embedded in its own metadata /")
    lines.append("  first page, so filenames never need to match anything.")
    lines.append("")

    download_me_path.parent.mkdir(parents=True, exist_ok=True)
    download_me_path.write_text("\n".join(lines), encoding="utf-8")


def _read_manifest(path: Path) -> list[dict]:
    """Read a manifest TSV, returning [] if the file is missing."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _sync_failures(
    failures: list[dict],
    *,
    manifest_path: Path,
    download_me_path: Path | None = None,
    manual_pdf_dir: Path | None = None,
) -> None:
    """Single writer for the manifest TSV + ``download_me.txt`` pair.

    Every code path that mutates the failure set — batch run, single-paper
    failure, single-paper success that clears a prior entry, manual import
    that drains the queue — funnels through here so the two files never
    disagree. Empty ``failures`` deletes both; non-empty rewrites both.
    """
    if not failures:
        _remove_quietly(manifest_path)
        if download_me_path:
            _remove_quietly(download_me_path)
        return

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, delimiter="\t")
        w.writeheader()
        w.writerows(failures)
    if download_me_path and manual_pdf_dir:
        _write_download_me(
            failures,
            download_me_path=download_me_path,
            manual_pdf_dir=manual_pdf_dir,
        )


def _build_failure_row(raw_id: str, id_type: str, clean_id: str, *, reason: str) -> dict:
    """Shape a manifest row from a parsed ID. Single source of truth so
    batch / single / import paths produce identical column values."""
    if id_type == "unknown":
        return {
            "id": raw_id,
            "id_type": "unknown",
            "basename": "",
            "url_to_try": "",
            "reason": reason,
        }
    return {
        "id": raw_id,
        "id_type": id_type,
        "basename": basename_for_id(id_type, clean_id),
        "url_to_try": _publisher_url(id_type, clean_id),
        "reason": reason,
    }


def record_single_failure(
    raw_id: str,
    *,
    manifest_path: Path,
    download_me_path: Path | None = None,
    manual_pdf_dir: Path | None = None,
) -> None:
    """Append a single failed ID to the manifest, dedup on ``id``.

    Used by the interactive ``fulltext <id>`` flow so a one-off failure
    is just as visible as a batch failure. If the ID already has a row,
    we keep the original (no churn); otherwise we add it and re-render.
    """
    id_type, clean_id = extract_paper_id(raw_id)
    rows = _read_manifest(manifest_path)
    if any(r.get("id") == raw_id for r in rows):
        # Already listed — re-sync to make sure download_me.txt reflects
        # the current manifest even if the user nuked it manually.
        _sync_failures(
            rows,
            manifest_path=manifest_path,
            download_me_path=download_me_path,
            manual_pdf_dir=manual_pdf_dir,
        )
        return
    rows.append(_build_failure_row(raw_id, id_type, clean_id, reason="automatic chain exhausted"))
    _sync_failures(
        rows,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )


def record_single_success(
    raw_id: str,
    *,
    manifest_path: Path,
    download_me_path: Path | None = None,
    manual_pdf_dir: Path | None = None,
) -> None:
    """Drop a previously-failed ID from the manifest after a successful fetch.

    Matches by ``id`` (exact) and ``basename`` (canonical), so callers
    that pass either form (raw user input vs canonical basename) both
    work. No-op when the ID isn't listed.
    """
    rows = _read_manifest(manifest_path)
    if not rows:
        return
    id_type, clean_id = extract_paper_id(raw_id)
    aliases: set[str] = set()
    if id_type != "unknown":
        aliases = _basename_aliases(basename_for_id(id_type, clean_id))
    remaining = [
        r for r in rows
        if r.get("id") != raw_id and r.get("basename") not in aliases
    ]
    if len(remaining) == len(rows):
        return  # not listed, nothing to do
    _sync_failures(
        remaining,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )


def run_batch(
    ids_path: Path,
    *,
    try_fetch: Callable[[str, str], bool],
    manifest_path: Path,
    download_me_path: Path | None = None,
    manual_pdf_dir: Path | None = None,
) -> tuple[int, int]:
    """Walk ``ids_path``; for each line call ``try_fetch(id_type, clean_id)``.

    ``try_fetch`` is the caller's adapter to its full-text dispatch (returns
    True on success, False otherwise). All bookkeeping — counters, manifest
    writing, per-ID stderr framing, human-friendly download README — lives
    here so the caller stays small.

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
            failures.append(_build_failure_row(raw_id, id_type, clean_id, reason="ID not parseable"))
            continue

        try:
            ok = try_fetch(id_type, clean_id)
        except Exception as e:  # noqa: BLE001 — never let one ID kill the run
            print(f"  ERROR: {e}", file=sys.stderr)
            ok = False

        if ok:
            successes += 1
        else:
            failures.append(_build_failure_row(raw_id, id_type, clean_id, reason="automatic chain exhausted"))

    _sync_failures(
        failures,
        manifest_path=manifest_path,
        download_me_path=download_me_path,
        manual_pdf_dir=manual_pdf_dir,
    )

    print(
        f"\nBatch done: {successes} ok, {len(failures)} failed.",
        file=sys.stderr,
    )
    if failures:
        print(f"Failed manifest: {manifest_path}", file=sys.stderr)
        if download_me_path and download_me_path.exists():
            print(
                f"Click-through list + upload & import commands: {download_me_path}",
                file=sys.stderr,
            )
    else:
        print("All IDs already in local cache or freshly fetched.", file=sys.stderr)
    return successes, len(failures)


def run_import(
    pdf_dir: Path,
    output_dir: Path,
    *,
    manifest_path: Path | None = None,
    download_me_path: Path | None = None,
    manual_pdf_dir: Path | None = None,
) -> tuple[int, int]:
    """Scan ``pdf_dir`` for PDFs and ingest each into ``output_dir``.

    Matching strategy, best signal first:

      1. **PDF content** — DOI from XMP metadata or first-page text. The
         PDF knows what it is regardless of filename.
      2. **Filename stem** — handles the legacy case where the user
         renamed the file to the canonical basename (``PMID12345``,
         ``PMC9999``, ``2401.12345``, ``10.1038_foo``).
      3. **Manifest basename** — exact stem match against a manifest
         entry. Useful when filename == basename but DOI isn't
         extractable (very old scanned PDFs).

    Already-cached basenames (``.pdf`` / ``.xml`` / ``.bioc.json`` /
    ``.txt`` present in ``output_dir``) are skipped; dropping the same
    PDF twice never overwrites a richer structured format.

    When ``manifest_path`` and ``download_me_path`` are both provided,
    successfully-ingested basenames are dropped from the manifest and
    ``download_me.txt`` is re-rendered with the remaining failures (or
    deleted along with the manifest when nothing remains). This keeps
    the user-facing download guide in sync after they finish manually
    pulling the paywalled papers.

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
    ingested_basenames: set[str] = set()
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        basename = _resolve_basename(pdf, by_basename)
        if basename is None:
            print(
                f"  ? skipping {pdf.name} (no DOI found in content, no recognisable filename)",
                file=sys.stderr,
            )
            skipped += 1
            continue
        if _already_cached(basename, output_dir):
            print(
                f"  = already in cache: {pdf.name} → {basename} (removing staged copy)",
                file=sys.stderr,
            )
            _remove_quietly(pdf)
            skipped += 1
            ingested_basenames.update(_basename_aliases(basename))  # already-cached counts as resolved
            continue
        print(f"  → ingesting {pdf.name} as {basename}", file=sys.stderr)
        ingest_local_pdf(str(pdf), basename, output_dir)
        _remove_quietly(pdf)  # cached copy in output_dir is canonical
        imported += 1
        ingested_basenames.update(_basename_aliases(basename))

    if manifest_path and manifest and ingested_basenames:
        remaining = [r for r in manifest if r.get("basename") not in ingested_basenames]
        if len(remaining) != len(manifest):
            _sync_failures(
                remaining,
                manifest_path=manifest_path,
                download_me_path=download_me_path,
                manual_pdf_dir=manual_pdf_dir,
            )
            cleared = len(manifest) - len(remaining)
            tail = (
                f"download guide cleared ({cleared} resolved, queue empty)."
                if not remaining
                else f"download guide updated ({cleared} resolved, {len(remaining)} still pending)."
            )
            print(tail, file=sys.stderr)

    print(f"\nImport done: {imported} ingested, {skipped} skipped.", file=sys.stderr)
    return imported, skipped


def _remove_quietly(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _already_cached(basename: str, output_dir: Path) -> bool:
    for ext in ("pdf", "txt", "xml", "bioc.json"):
        if (output_dir / f"{basename}.{ext}").exists():
            return True
    return False


def _resolve_basename(pdf: Path, by_basename: dict[str, dict]) -> str | None:
    """Walk the matching chain to pick a canonical basename.

    Returns None when no signal produces a parseable ID.
    """
    try:
        data = pdf.read_bytes()
    except OSError:
        return None
    if not is_pdf_bytes(data):
        return None

    # 1. DOI from PDF content (most reliable — publisher-embedded).
    doi = extract_doi_from_pdf(data)
    if doi:
        return basename_for_id("doi", doi)

    # 2. Filename stem looks like a canonical ID format.
    recovered = _reverse_basename_to_id(pdf.stem)
    if recovered is not None:
        id_type, clean_id = recovered
        return basename_for_id(id_type, clean_id)

    # 3. Publisher-native filename matches the tail of a manifest basename.
    # Handles cases like ``scisignal.ado6430.pdf`` for manifest basename
    # ``10.1126_scisignal.ado6430`` — AAAS / Elsevier often drop the
    # registrant prefix in their native filenames.
    stem_lc = pdf.stem.lower()
    for basename in by_basename:
        if "_" not in basename:
            continue
        _, tail = basename.split("_", 1)
        if stem_lc == tail.lower():
            return basename

    # 4. Exact filename match against manifest basename column.
    if pdf.stem in by_basename:
        return pdf.stem

    return None


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
