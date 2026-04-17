"""Layered full-text fetch dispatcher.

Each fetch path (PMC / ChemRxiv / bioRxiv / generic DOI / preprint reverse
lookup) walks an ordered list of :class:`Layer`s. A layer succeeds by
either:

  - returning PDF bytes — the dispatcher saves ``{basename}.pdf`` +
    ``{basename}.txt`` under ``output_dir`` via :func:`lit.pdf.save_pdf_and_text`,
  - or returning ``True`` — the layer already wrote its own files
    (e.g. JATS XML / BioC JSON) and nothing more is needed.

Falsy returns (``None`` / ``False`` / ``b""``) mean "try the next layer".
Exceptions propagate — the caller decides whether to swallow them.

The dispatcher returns ``True`` at first success, ``False`` if every layer
fell through. This replaces the prior pattern of each handler calling
``sys.exit(1)`` on terminal failure and the preprint-walker catching the
``SystemExit`` to keep iterating — control flow is now explicit.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lit.pdf import save_pdf_and_text


@dataclass
class Layer:
    """One step in a layered fetch chain.

    ``label`` is printed to stderr before ``fn`` runs, so users see the
    progression. Keep it short — e.g. ``"[2/4] Trying OA mirrors..."``.

    ``fn`` is a no-arg callable. Returning ``bytes``/``bytearray`` triggers
    the standard PDF save; returning ``True`` means the layer wrote files
    itself and the chain should stop; falsy returns advance to the next.
    """

    label: str
    fn: Callable[[], bytes | bool | None]


def walk_layers(
    layers: list[Layer],
    *,
    basename: str,
    output_dir: Path,
    source_url: str | None = None,
) -> bool:
    """Run ``layers`` in order; return True at first success, False if all fail."""
    for layer in layers:
        print(layer.label, file=sys.stderr)
        result = layer.fn()
        if isinstance(result, (bytes, bytearray)) and result:
            save_pdf_and_text(
                bytes(result), basename, output_dir, source_url=source_url,
            )
            return True
        if result is True:
            return True
    return False
