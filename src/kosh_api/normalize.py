"""Turn casparser output into Kosh holdings.

Two families of statement:
  - RTA CAS (CAMS / KFintech): mutual fund folios, often with every transaction.
  - Depository eCAS (NSDL / CDSL): demat stocks, bonds, NPS, MFs held in demat,
    and usually a "Mutual Fund Folios" section repeating the RTA folios.
Each upload replaces the holdings of its own family, and folios present in both
are counted once (the RTA copy wins because it carries transactions).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

RTA_TYPES = {"CAMS", "KFINTECH"}
DEMAT_TYPES = {"NSDL", "CDSL"}

OUTFLOW = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER", "STT_TAX", "STAMP_DUTY_TAX", "TDS_TAX"}
INFLOW = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(Decimal(str(x).replace(",", "")))
    except Exception:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%B-%Y")


def to_iso(d: Any) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def mask(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return None
    v = re.sub(r"\s", "", str(value))
    return "••" + v[-keep:] if len(v) > keep else v


def plan_from_name(name: str) -> str:
    n = name.lower()
    return "direct" if "direct" in n else "regular"


def cash_flow(txn_type: str, amount: float | None) -> float | None:
    """Money from the investor's point of view: paid in is negative, received is positive."""
    if amount is None or amount == 0:
        return None
    if txn_type in OUTFLOW:
        return -abs(amount)
    if txn_type in INFLOW:
        return abs(amount)
    return None  # reinvested dividends, segregation, gifts, misc: no cash moved


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def statement_meta(cas: Any, filename: str | None) -> dict:
    period = _get(cas, "statement_period")
    file_type = str(_get(cas, "file_type") or "UNKNOWN")
    file_type = file_type.split(".")[-1]  # enum repr -> name
    return {
        "source": file_type,
        "family": "rta" if file_type in RTA_TYPES else "demat" if file_type in DEMAT_TYPES else "unknown",
        "detailed": str(_get(cas, "cas_type") or "").endswith("DETAILED"),
        "period_from": to_iso(_get(period, "from_") or _get(period, "from")),
        "period_to": to_iso(_get(period, "to")),
        "filename": (filename or "statement.pdf")[:120],
    }


def from_rta(cas: Any, as_of_default: str | None) -> list[dict]:
    """CAMS / KFintech: one holding per scheme per folio. Closed schemes (0 units) are skipped."""
    out: list[dict] = []
    for folio in _get(cas, "folios", []) or []:
        folio_no = str(_get(folio, "folio") or "").strip()
        amc = _get(folio, "amc") or ""
        for s in _get(folio, "schemes", []) or []:
            units = _f(_get(s, "close")) or 0.0
            val = _get(s, "valuation")
            value = _f(_get(val, "value")) or 0.0
            if units <= 0.0005 and value <= 1:
                continue
            name = str(_get(s, "scheme") or "Unknown scheme").strip()
            txns = []
            for t in _get(s, "transactions", []) or []:
                ttype = str(_get(t, "type") or "UNKNOWN").split(".")[-1]
                amount = _f(_get(t, "amount"))
                txns.append(
                    {
                        "date": to_iso(_get(t, "date")),
                        "type": ttype,
                        "amount": amount,
                        "units": _f(_get(t, "units")),
                        "nav": _f(_get(t, "nav")),
                        "flow": cash_flow(ttype, amount),
                    }
                )
            isin = _get(s, "isin")
            out.append(
                {
                    "id": f"mf:{isin or _get(s, 'rta_code')}:{folio_no}",
                    "kind": "mf",
                    "held_in": "folio",
                    "name": name,
                    "isin": isin,
                    "amfi": str(_get(s, "amfi")) if _get(s, "amfi") else None,
                    "amc": amc,
                    "folio": folio_no,
                    "units": units,
                    "nav": _f(_get(val, "nav")),
                    "value": value,
                    "cost": _f(_get(val, "cost")),
                    "as_of": to_iso(_get(val, "date")) or as_of_default,
                    "plan": plan_from_name(name),
                    "transactions": [t for t in txns if t["date"]],
                }
            )
    return out


def from_demat(cas: Any, as_of_default: str | None) -> dict:
    """NSDL / CDSL eCAS: stocks, bonds, MFs (demat and folio sections) and NPS."""
    mfs: list[dict] = []
    stocks: list[dict] = []
    bonds: list[dict] = []
    for acc in _get(cas, "accounts", []) or []:
        acc_type = str(_get(acc, "type") or "")
        is_folios = acc_type.lower().startswith("mutual fund folios")
        label = "Mutual fund folios" if is_folios else f"{_get(acc, 'name') or acc_type} {mask(_get(acc, 'client_id')) or ''}".strip()
        for e in _get(acc, "equities", []) or []:
            qty = _f(_get(e, "num_shares")) or 0.0
            if qty <= 0:
                continue
            isin = _get(e, "isin")
            stocks.append(
                {
                    "id": f"eq:{isin}:{mask(_get(acc, 'client_id')) or acc_type}",
                    "kind": "stock",
                    "name": (_get(e, "name") or isin or "").strip(),
                    "isin": isin,
                    "symbol": _get(e, "symbol"),
                    "qty": qty,
                    "price": _f(_get(e, "price")),
                    "value": _f(_get(e, "value")) or 0.0,
                    "account": label,
                    "as_of": as_of_default,
                }
            )
        for m in _get(acc, "mutual_funds", []) or []:
            units = _f(_get(m, "balance")) or 0.0
            if units <= 0.0005:
                continue
            isin = _get(m, "isin")
            name = (_get(m, "name") or isin or "").strip()
            folio_no = str(_get(m, "folio") or "").strip()
            mfs.append(
                {
                    "id": f"mf:{isin}:{folio_no or mask(_get(acc, 'client_id')) or 'demat'}",
                    "kind": "mf",
                    "held_in": "folio" if is_folios else "demat",
                    "name": name,
                    "isin": isin,
                    "amfi": str(_get(m, "amfi")) if _get(m, "amfi") else None,
                    "amc": None,
                    "folio": folio_no or None,
                    "units": units,
                    "nav": _f(_get(m, "nav")),
                    "value": _f(_get(m, "value")) or 0.0,
                    "cost": _f(_get(m, "total_cost")),
                    "as_of": as_of_default,
                    "plan": plan_from_name(name),
                    "transactions": [],
                    "account": label,
                }
            )
        for b in _get(acc, "bonds", []) or []:
            bonds.append(
                {
                    "id": f"bond:{_get(b, 'isin')}:{mask(_get(acc, 'client_id')) or acc_type}",
                    "kind": "bond",
                    "name": (_get(b, "name") or _get(b, "isin") or "").strip(),
                    "isin": _get(b, "isin"),
                    "qty": _f(_get(b, "num_bonds")) or 0.0,
                    "value": _f(_get(b, "value")) or 0.0,
                    "coupon": _f(_get(b, "coupon_rate")),
                    "maturity": _get(b, "maturity_date"),
                    "account": label,
                    "as_of": as_of_default,
                }
            )
    nps = None
    raw_nps = _get(cas, "nps")
    if raw_nps:
        nps = {
            "value": _f(_get(raw_nps, "value")) or 0.0,
            "schemes": [
                {"scheme": _get(s, "scheme"), "tier": _get(s, "tier"), "units": _f(_get(s, "units")), "nav": _f(_get(s, "nav")), "value": _f(_get(s, "value")) or 0.0}
                for s in _get(raw_nps, "schemes", []) or []
            ],
            "as_of": as_of_default,
        }
    return {"mf": mfs, "stocks": stocks, "bonds": bonds, "nps": nps}


def investor_name(cas: Any) -> str | None:
    info = _get(cas, "investor_info")
    name = _get(info, "name") if info else None
    return str(name).strip().title() if name else None


def normalize(cas: Any, filename: str | None = None) -> dict:
    """Returns {"meta": …, "family": "rta"|"demat", "holdings": {…}} ready to store."""
    meta = statement_meta(cas, filename)
    as_of = meta["period_to"]
    if meta["family"] == "rta":
        holdings = {"mf": from_rta(cas, as_of), "stocks": [], "bonds": [], "nps": None}
    elif meta["family"] == "demat":
        holdings = from_demat(cas, as_of)
    else:
        raise ValueError("This doesn't look like a CAS from CAMS, KFintech, NSDL or CDSL")
    meta["counts"] = {k: len(v) for k, v in holdings.items() if isinstance(v, list)}
    return {"meta": meta, "family": meta["family"], "holdings": holdings, "investor": investor_name(cas)}


def merged(portfolio: dict) -> dict:
    """Combine the RTA and demat families into one view without double counting."""
    rta = (portfolio.get("rta") or {}).get("holdings") or {}
    demat = (portfolio.get("demat") or {}).get("holdings") or {}
    rta_mf = rta.get("mf") or []
    seen_folio = {(m.get("isin"), (m.get("folio") or "").split("/")[0].strip()) for m in rta_mf}
    seen_isin = {m.get("isin") for m in rta_mf}
    mf = list(rta_mf)
    for m in demat.get("mf") or []:
        folio = (m.get("folio") or "").split("/")[0].strip()
        if (m.get("isin"), folio) in seen_folio:
            continue
        # eCAS folio sections sometimes print the folio differently; same ISIN outside demat = same holding
        if m.get("held_in") == "folio" and m.get("isin") in seen_isin:
            continue
        mf.append(m)
    return {"mf": mf, "stocks": demat.get("stocks") or [], "bonds": demat.get("bonds") or [], "nps": demat.get("nps")}
