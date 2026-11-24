"""Small fixes on top of casparser for statement layouts it misses.

CDSL eCAS, page 2 ("Your Demat Account and Mutual Fund Folios") lists one row per demat account:
    CDSL Demat Account | <broker>⏎DP Id: … Client Id: … | <folios> | <value>
casparser only accepts that exact 4-cell shape. When a broker's name is long or printed on its own
line, the PDF puts it in a separate cell (5 cells), for example:
    CDSL Demat Account | DP Id: … Client Id: … | RAISE SECURITIES PRIVATE LIMITED | 24 | 1,47,004.66
casparser then never creates that account, so every holding in it is silently dropped.
We accept any row with the account type first, the DP/Client ids somewhere in the middle, and the
folio count and value as the last two cells, and hand casparser the 4-cell shape it expects.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from casparser.parsers import cdsl

_NUM = re.compile(r"^-?[\d,]*\.?\d+$")
_applied = False


def _texts(block) -> list[str]:
    return [c.text for c in getattr(block, "cells", []) or []]


def _four_cells(texts: list[str]) -> list[str] | None:
    """Fold a summary row of 4+ cells into [type, broker⏎DP Id line, folios, value]; None if it isn't one."""
    if len(texts) < 4 or not cdsl.DEMAT_TYPE_RE.match(texts[0].strip()):
        return None
    folios, value = texts[-2].strip(), texts[-1].strip()
    if not _NUM.match(folios.replace(",", "")) or not _NUM.match(value.replace(",", "")):
        return None
    lines = [ln.strip() for t in texts[1:-2] for ln in t.split("\n") if ln.strip()]
    ids = [ln for ln in lines if cdsl.SUMMARY_DPC_RE.search(ln)]
    if not ids:
        return None
    broker = [ln for ln in lines if not cdsl.SUMMARY_DPC_RE.search(ln)]
    return [texts[0], "\n".join(broker + ids), folios, value]


def apply() -> None:
    """Patch casparser's CDSL summary-row reader once. Safe if casparser changes: it only wraps what exists."""
    global _applied
    if _applied or not hasattr(cdsl, "_is_summary_demat_row") or not hasattr(cdsl, "_account_from_summary_row"):
        return
    orig_is_row = cdsl._is_summary_demat_row
    orig_account = cdsl._account_from_summary_row

    def is_summary_demat_row(block) -> bool:
        return orig_is_row(block) or _four_cells(_texts(block)) is not None

    def account_from_summary_row(block, owners):
        cells = _four_cells(_texts(block))
        if cells is None or orig_is_row(block):
            return orig_account(block, owners)
        return orig_account(SimpleNamespace(cells=[SimpleNamespace(text=t) for t in cells]), owners)

    cdsl._is_summary_demat_row = is_summary_demat_row
    cdsl._account_from_summary_row = account_from_summary_row
    _applied = True


apply()
