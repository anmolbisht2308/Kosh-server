"""Portfolio maths: live values, XIRR, allocation and plain-language insights.

Insights are facts about the investor's own holdings (costs, concentration, staleness).
Kosh never tells anyone what to buy or sell.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from . import prices
from .normalize import statement_gaps


# ---------------- XIRR ----------------
def xirr(flows: list[tuple[date, float]]) -> float | None:
    """Annualised return for dated cash flows (paid in < 0, received > 0). None if it can't be solved."""
    flows = [(d, a) for d, a in flows if a]
    if len(flows) < 2 or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    flows.sort()
    t0 = flows[0][0]
    years = [((d - t0).days / 365.0, a) for d, a in flows]
    if years[-1][0] < 1 / 365:
        return None

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** t for t, a in years)

    def d_npv(r: float) -> float:
        return sum(-t * a / (1 + r) ** (t + 1) for t, a in years)

    r = 0.1
    for _ in range(60):  # Newton
        f, df = npv(r), d_npv(r)
        if df == 0:
            break
        nr = r - f / df
        if nr <= -0.9999 or nr > 100:
            break
        if abs(nr - r) < 1e-9:
            return nr
        r = nr
    lo, hi = -0.9999, 10.0  # bisection fallback
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-7:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


# ---------------- live valuation ----------------
def _fresh(price_doc: dict | None, as_of: str | None) -> bool:
    if not price_doc or not price_doc.get("price"):
        return False
    pd = price_doc.get("date")
    return not as_of or not pd or pd >= as_of


async def value_holdings(h: dict) -> dict:
    """Attach live price, value, day change and asset class to each holding."""
    isins = [x["isin"] for x in h["mf"] + h["stocks"] if x.get("isin")]
    live = await prices.latest(isins)
    today = datetime.now(UTC) + timedelta(hours=5, minutes=30)

    mf_out = []
    for m in h["mf"]:
        p = live.get(m.get("isin") or "")
        item = dict(m)
        item["asset"] = (p or {}).get("asset") or "equity"
        item["category"] = (p or {}).get("category")
        if p and p.get("plan"):
            item["plan"] = p["plan"]
        if m.get("held_in") == "demat" and _fresh((p or {}).get("exchange"), m.get("as_of")):
            # an ETF in demat: what it would sell for on NSE, as the statement values it
            x = p["exchange"]
            item["price"] = x["price"]
            item["price_date"] = x.get("date")
            item["price_source"] = "nse"
            item["value"] = round(m["units"] * x["price"], 2)
            if x.get("prev_price"):
                item["day_change"] = round(m["units"] * (x["price"] - x["prev_price"]), 2)
        elif _fresh(p, m.get("as_of")):
            item["price"] = p["price"]
            item["price_date"] = p.get("date")
            item["value"] = round(m["units"] * p["price"], 2)
            if p.get("prev_price"):
                item["day_change"] = round(m["units"] * (p["price"] - p["prev_price"]), 2)
        else:
            item["price"] = m.get("nav")
            item["price_date"] = m.get("as_of")
        if item.get("cost"):
            item["gain"] = round(item["value"] - item["cost"], 2)
        item["xirr"] = fund_xirr(item, today.date())
        item.pop("transactions", None)
        item["has_transactions"] = bool(m.get("transactions"))
        mf_out.append(item)

    stocks_out = []
    for s in h["stocks"]:
        p = live.get(s.get("isin") or "")
        item = dict(s)
        item["asset"] = "stocks"
        if _fresh(p, s.get("as_of")):
            item["price"] = p["price"]
            item["price_date"] = p.get("date")
            item["value"] = round(s["qty"] * p["price"], 2)
            item["symbol"] = item.get("symbol") or p.get("symbol")
            if p.get("prev_price"):
                item["day_change"] = round(s["qty"] * (p["price"] - p["prev_price"]), 2)
        else:
            item["price_date"] = s.get("as_of")
        stocks_out.append(item)
    return {"mf": mf_out, "stocks": stocks_out, "bonds": h["bonds"], "nps": h["nps"], "_raw_mf": h["mf"]}


def fund_xirr(item: dict, today: date) -> float | None:
    flows = [(_d(t["date"]), t["flow"]) for t in item.get("transactions") or [] if t.get("flow") and _d(t["date"])]
    if not flows:
        return None
    flows.append((today, item["value"]))
    r = xirr(flows)
    return round(r, 4) if r is not None and -0.99 < r < 10 else None


def portfolio_xirr(raw_mf: list[dict], valued_mf: list[dict], today: date) -> float | None:
    values = {m["id"]: m["value"] for m in valued_mf}
    flows: list[tuple[date, float]] = []
    current = 0.0
    for m in raw_mf:
        txns = [(_d(t["date"]), t["flow"]) for t in m.get("transactions") or [] if t.get("flow") and _d(t["date"])]
        if not txns:
            continue
        flows += txns
        current += values.get(m["id"], 0.0)
    if not flows or current <= 0:
        return None
    flows.append((today, current))
    r = xirr(flows)
    return round(r, 4) if r is not None and -0.99 < r < 10 else None


# ---------------- summary + insights ----------------
ASSET_LABEL = {"stocks": "Stocks", "equity": "Equity funds", "debt": "Debt funds", "hybrid": "Hybrid funds", "gold": "Gold & silver", "bonds": "Bonds", "nps": "NPS"}


async def summarize(holdings: dict, statements: list[dict]) -> dict:
    v = await value_holdings(holdings)
    today = (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()
    total_mf = sum(m["value"] for m in v["mf"])
    total_st = sum(s["value"] for s in v["stocks"])
    total_bonds = sum(b["value"] for b in v["bonds"])
    total_nps = (v["nps"] or {}).get("value", 0.0)
    total = total_mf + total_st + total_bonds + total_nps

    alloc: dict[str, float] = defaultdict(float)
    for m in v["mf"]:
        alloc[m["asset"]] += m["value"]
    alloc["stocks"] += total_st
    alloc["bonds"] += total_bonds
    alloc["nps"] += total_nps
    allocation = [{"key": k, "label": ASSET_LABEL.get(k, k.title()), "value": round(val, 2), "share": (val / total if total else 0)} for k, val in alloc.items() if val > 0]
    allocation.sort(key=lambda a: -a["value"])

    known_cost = [m for m in v["mf"] if m.get("cost")]
    invested = sum(m["cost"] for m in known_cost)
    day_change = sum(x.get("day_change", 0.0) for x in v["mf"] + v["stocks"])

    insights = await build_insights(v, total, statements, today)
    # the same holdings at the prices printed on the statement, so the headline can be tied back to the PDF
    at_statement = sum(m.get("value") or 0.0 for m in holdings["mf"] + holdings["stocks"] + holdings["bonds"]) + total_nps
    return {
        "total": round(total, 2),
        "statement_value": round(at_statement, 2),
        "statement_date": max((s.get("period_to") or "" for s in statements), default="") or None,
        "day_change": round(day_change, 2),
        "mf": {"value": round(total_mf, 2), "invested": round(invested, 2), "gain": round(sum(m["value"] for m in known_cost) - invested, 2), "cost_known_for": len(known_cost), "count": len(v["mf"])},
        "stocks": {"value": round(total_st, 2), "count": len(v["stocks"])},
        "xirr": portfolio_xirr(v["_raw_mf"], v["mf"], today),
        "allocation": allocation,
        "holdings": {"mf": sorted(v["mf"], key=lambda x: -x["value"]), "stocks": sorted(v["stocks"], key=lambda x: -x["value"]), "bonds": v["bonds"], "nps": v["nps"]},
        "insights": insights,
    }


async def build_insights(v: dict, total: float, statements: list[dict], today: date) -> list[dict]:
    out: list[dict] = []
    if total <= 0:
        return out

    # 0. the statement prints a total per account; say so if we itemised less than it shows
    gaps = [g for s in statements for g in statement_gaps(s.get("accounts") or [])]
    if gaps:
        missing = sum(g["gap"] for g in gaps)
        out.append({
            "id": "statement-gap",
            "tone": "warn",
            "title": f"₹{abs(missing):,.0f} on your statement isn't itemised here",
            "body": "Your statement's own account totals don't match the holdings Kosh could read line by line, so the totals here are lower than they should be. The accounts below are the ones affected.",
            "rows": [{"name": g["label"], "value": g["gap"]} for g in gaps],
        })

    # 1. what Regular plans cost compared with the Direct plan of the same fund
    regular = [m for m in v["mf"] if m.get("plan") == "regular" and m.get("isin") and m["value"] > 0]
    try:  # free history API; never let it slow the dashboard down for long
        gaps = await asyncio.wait_for(asyncio.gather(*(prices.plan_gap(m["isin"]) for m in regular[:25])), timeout=8)
    except (asyncio.TimeoutError, Exception):
        gaps = [None] * len(regular[:25])
    rows = []
    for m, g in zip(regular, gaps):
        if g and g["diff"] > 0.001:
            rows.append({"name": m["name"], "value": m["value"], "gap": round(g["diff"], 4), "yearly": round(m["value"] * g["diff"]), "direct_name": g["direct_name"]})
    if rows:
        yearly = sum(r["yearly"] for r in rows)
        out.append({
            "id": "regular-plans",
            "tone": "warn",
            "title": f"Regular plans cost you about ₹{yearly:,.0f} a year",
            "body": f"{len(rows)} of your funds are Regular plans, which pay a distributor commission out of your returns. Over the last year, the Direct plan of the same funds returned more. This is what that gap is worth on your current holdings.",
            "rows": sorted(rows, key=lambda r: -r["yearly"]),
        })
    elif regular:
        out.append({"id": "regular-plans", "tone": "info", "title": f"{len(regular)} Regular plan{'s' if len(regular) > 1 else ''} in your funds", "body": "Regular plans include a distributor commission. The Direct plan of the same fund has a lower expense ratio.", "rows": [{"name": m["name"], "value": m["value"]} for m in regular]})

    # 2. one stock or fund holding a big share of everything
    every = [(x["name"], x["value"]) for x in v["mf"] + v["stocks"]]
    every.sort(key=lambda t: -t[1])
    if every and every[0][1] / total >= 0.25 and len(every) > 1:
        out.append({"id": "concentration", "tone": "warn", "title": f"{every[0][0]} is {every[0][1] / total:.0%} of everything", "body": "A single holding this large means its ups and downs drive your whole portfolio.", "rows": []})
    if v["stocks"]:
        st_total = sum(s["value"] for s in v["stocks"])
        top5 = sum(s["value"] for s in sorted(v["stocks"], key=lambda s: -s["value"])[:5])
        if st_total and len(v["stocks"]) > 5:
            out.append({"id": "stock-top5", "tone": "info", "title": f"Your top 5 stocks are {top5 / st_total:.0%} of your shares", "body": f"You hold {len(v['stocks'])} stocks in demat.", "rows": []})

    # 3. several funds in the same category usually hold many of the same shares
    by_cat: dict[str, list[str]] = defaultdict(list)
    for m in v["mf"]:
        if m.get("category") and m["asset"] == "equity":
            by_cat[m["category"].split(" - ")[-1]].append(m["name"])
    for cat, names in by_cat.items():
        if len(set(names)) >= 3:
            out.append({"id": f"same-category:{cat}", "tone": "info", "title": f"{len(set(names))} funds in {cat}", "body": "Funds in the same category often own many of the same companies, so holding several adds less variety than it seems.", "rows": [{"name": n} for n in sorted(set(names))]})

    # 4. statement too old for live prices to cover new purchases
    last = max((s.get("period_to") or "" for s in statements), default="")
    if last and (today - date.fromisoformat(last)).days > 45:
        out.append({"id": "stale", "tone": "info", "title": "Your statement is over 45 days old", "body": "Prices are live, but anything you bought or sold since then isn't counted. Upload a fresh CAS to bring it up to date.", "rows": []})
    return out
