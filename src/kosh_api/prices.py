"""Free, public price sources — no API keys, no cost.

- Mutual funds: AMFI's daily NAV file (official, every scheme, with plan and category).
- Stocks: NSE's daily "bhavcopy" CSV (closing prices for every listed share, keyed by ISIN).
- 1-year NAV history (for the Direct vs Regular comparison): api.mfapi.in, a free open API.

Prices are cached in Mongo and refreshed at most every few hours, so the sources are
hit a handful of times a day no matter how many people use Kosh.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
from pymongo import UpdateOne

from . import db
from .config import settings

log = logging.getLogger("kosh.prices")

AMFI_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
NSE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip"
MFAPI_URL = "https://api.mfapi.in/mf/{code}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
REFRESH_EVERY = timedelta(hours=4)

_lock = asyncio.Lock()


# ---------------- AMFI ----------------
def asset_class(category: str, name: str) -> str:
    c, n = category.lower(), name.lower()
    if "equity scheme" in c or "elss" in c:
        return "equity"
    if "debt scheme" in c:
        return "debt"
    if "hybrid scheme" in c:
        return "hybrid"
    if any(k in n for k in ("gold", "silver")):
        return "gold"
    if any(k in n for k in ("liquid", "gilt", "bond", "debt", "overnight", "money market", "sdl", "g-sec", "crisil ibx", "treasury")):
        return "debt"
    if "solution oriented" in c or "children" in c or "retirement" in c:
        return "hybrid"
    return "equity"  # index funds, ETFs and FoFs are mostly equity


def scheme_base(name: str) -> str:
    """'HDFC Mid Cap Fund - Direct Plan - Growth Option' -> 'hdfc mid cap fund' (same for both plans)."""
    n = name.lower()
    n = re.sub(r"\((?:[^)]*)\)", " ", n)
    n = re.sub(r"\b(direct|regular)\b(\s+plan)?", " ", n)
    n = re.sub(r"\b(growth|idcw|dividend|bonus|payout|reinvest(ment)?|option|plan|monthly|quarterly|annual|half yearly|weekly|daily|cum capital withdrawal|income distribution)\b", " ", n)
    n = re.sub(r"[-–_/]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def option_key(text: str) -> str:
    t = text.lower()
    if "growth" in t:
        return "growth"
    if "bonus" in t:
        return "bonus"
    freq = next((f for f in ("daily", "weekly", "monthly", "quarterly", "half yearly", "annual") if f in t), "")
    kind = "reinvest" if "reinvest" in t else "payout"
    return f"idcw {freq} {kind}".replace("  ", " ").strip()


def parse_amfi(text: str) -> list[dict]:
    rows: list[dict] = []
    category, amc = "", ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(";")
        if len(parts) >= 8 and parts[0].isdigit():
            code, isin1, isin2, name, plan, option, nav, nav_date = (p.strip() for p in parts[:8])
            try:
                nav_v = float(nav)
            except ValueError:
                continue
            try:
                d = datetime.strptime(nav_date, "%d-%b-%Y").date().isoformat()
            except ValueError:
                d = None
            plan_text = plan or name  # older rows carry the plan inside the name
            base = dict(
                kind="mf",
                scheme_code=code,
                name=name,
                base=scheme_base(name),
                plan="direct" if "direct" in plan_text.lower() else "regular",
                option=option_key(option or name),
                price=nav_v,
                date=d,
                amc=amc,
                category=category,
                asset=asset_class(category, name),
            )
            for isin in (isin1, isin2):
                if re.fullmatch(r"IN[A-Z0-9]{10}", isin or ""):
                    rows.append({**base, "_id": isin})
        elif line.startswith(("Open Ended", "Close Ended", "Interval Fund")):
            m = re.search(r"\((.*)\)", line)
            category = m.group(1) if m else line
        elif len(parts) == 1 and not line.startswith("Scheme Code"):
            amc = line
    return rows


async def _refresh_amfi(client: httpx.AsyncClient) -> int:
    r = await client.get(AMFI_URL)
    r.raise_for_status()
    rows = parse_amfi(r.text)
    if len(rows) < 1000:
        raise RuntimeError(f"AMFI file looked wrong ({len(rows)} rows)")
    old = {d["_id"]: d async for d in db.prices.find({"kind": "mf"}, {"price": 1, "date": 1, "prev_price": 1})}
    ops = []
    for row in rows:
        prev = old.get(row["_id"])
        if prev and prev.get("date") and prev.get("date") != row["date"]:
            row["prev_price"] = prev.get("price")
        elif prev:
            row["prev_price"] = prev.get("prev_price")
        ops.append(UpdateOne({"_id": row["_id"]}, {"$set": row}, upsert=True))
    for i in range(0, len(ops), 2000):
        await db.prices.bulk_write(ops[i : i + 2000], ordered=False)
    return len(rows)


# ---------------- NSE ----------------
def parse_bhavcopy(raw_zip: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", "ignore")
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        isin = (r.get("ISIN") or "").strip()
        if not isin.startswith("IN") or r.get("FinInstrmTp") != "STK":
            continue
        try:
            close = float(r["ClsPric"])
            prev = float(r.get("PrvsClsgPric") or 0) or None
        except (ValueError, KeyError):
            continue
        out.append({"_id": isin, "kind": "stock", "symbol": r.get("TckrSymb"), "series": r.get("SctySrs"), "name": r.get("FinInstrmNm"), "price": close, "prev_price": prev, "date": r.get("TradDt")})
    # several series can share an ISIN; prefer EQ, then BE
    best: dict[str, dict] = {}
    rank = {"EQ": 0, "BE": 1, "BZ": 2}
    for row in out:
        cur = best.get(row["_id"])
        if cur is None or rank.get(row["series"] or "", 9) < rank.get(cur["series"] or "", 9):
            best[row["_id"]] = row
    return list(best.values())


async def _refresh_nse(client: httpx.AsyncClient) -> int:
    today = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    for back in range(0, 7):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        r = await client.get(NSE_URL.format(d=d))
        if r.status_code == 200 and r.content[:2] == b"PK":
            rows = parse_bhavcopy(r.content)
            ops = [UpdateOne({"_id": x["_id"]}, {"$set": x}, upsert=True) for x in rows]
            for i in range(0, len(ops), 2000):
                await db.prices.bulk_write(ops[i : i + 2000], ordered=False)
            return len(rows)
    raise RuntimeError("No NSE bhavcopy found for the last 7 days")


async def refresh(force: bool = False) -> dict:
    """Refresh AMFI and NSE prices if they're older than REFRESH_EVERY. Safe to call often."""
    if not settings.fetch_prices:
        return {"skipped": "FETCH_PRICES is off"}
    async with _lock:
        m = await db.meta.find_one({"_id": "prices"}) or {}
        now = datetime.now(UTC)
        result: dict = {}
        async with httpx.AsyncClient(timeout=40, headers={"User-Agent": UA}, follow_redirects=True) as client:
            for key, fn in (("amfi", _refresh_amfi), ("nse", _refresh_nse)):
                last = m.get(f"{key}_at")
                if not force and last and now - last < REFRESH_EVERY:
                    continue
                try:
                    result[key] = await fn(client)
                    await db.meta.update_one({"_id": "prices"}, {"$set": {f"{key}_at": now, f"{key}_error": None}}, upsert=True)
                except Exception as e:  # a source being down must never break the app
                    log.warning("price refresh %s failed: %s", key, e)
                    result[key] = f"failed: {e}"
                    # back off for an hour before trying again
                    await db.meta.update_one({"_id": "prices"}, {"$set": {f"{key}_at": now - REFRESH_EVERY + timedelta(hours=1), f"{key}_error": str(e)[:200]}}, upsert=True)
        return result


async def latest(isins: list[str]) -> dict[str, dict]:
    if not isins:
        return {}
    return {d["_id"]: d async for d in db.prices.find({"_id": {"$in": list(set(isins))}})}


# ---------------- Direct vs Regular ----------------
def _nav_on_or_before(series: list[dict], target: date) -> float | None:
    for row in series:  # mfapi returns newest first
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
        except (KeyError, ValueError):
            continue
        if d <= target:
            return float(row["nav"])
    return None


async def _one_year_return(client: httpx.AsyncClient, code: str) -> float | None:
    r = await client.get(MFAPI_URL.format(code=code))
    if r.status_code != 200:
        return None
    data = r.json().get("data") or []
    if len(data) < 200:
        return None
    try:
        now_nav = float(data[0]["nav"])
        now_d = datetime.strptime(data[0]["date"], "%d-%m-%Y").date()
    except (KeyError, ValueError):
        return None
    then = _nav_on_or_before(data, now_d - timedelta(days=365))
    return (now_nav / then - 1) if then else None


async def plan_gap(regular_isin: str) -> dict | None:
    """How much better the Direct plan of the same scheme did over the last year (as a fraction)."""
    reg = await db.prices.find_one({"_id": regular_isin, "kind": "mf"})
    if not reg or reg.get("plan") != "regular":
        return None
    direct = await db.prices.find_one({"kind": "mf", "base": reg["base"], "option": reg["option"], "plan": "direct"})
    if not direct:
        return None
    key = f"{reg['scheme_code']}:{direct['scheme_code']}"
    today = date.today().isoformat()
    cached = await db.plan_gaps.find_one({"_id": key})
    if cached and cached.get("day") == today:
        return cached.get("gap")
    if not settings.fetch_prices:
        return cached.get("gap") if cached else None
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
            r_reg, r_dir = await asyncio.gather(_one_year_return(client, reg["scheme_code"]), _one_year_return(client, direct["scheme_code"]))
    except httpx.HTTPError:
        return cached.get("gap") if cached else None
    gap = None
    if r_reg is not None and r_dir is not None:
        gap = {"regular": r_reg, "direct": r_dir, "diff": max(0.0, r_dir - r_reg), "direct_name": direct["name"], "direct_isin": direct["_id"]}
    await db.plan_gaps.update_one({"_id": key}, {"$set": {"gap": gap, "day": today}}, upsert=True)
    return gap
