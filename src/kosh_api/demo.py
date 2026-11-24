"""A realistic sample portfolio (real ISINs, made-up investor) so people can try Kosh without a statement.

Units are chosen so values land near the NAVs of late 2026; live prices take over when available.
"""

from __future__ import annotations

from datetime import date, timedelta

from .normalize import cash_flow


def _sip(start: date, months: int, amount: float, start_nav: float, end_nav: float) -> tuple[list[dict], float]:
    txns, units = [], 0.0
    for i in range(months):
        d = start + timedelta(days=30 * i)
        nav = start_nav + (end_nav - start_nav) * (i / max(1, months - 1)) * (0.92 + 0.08 * ((i * 7) % 5) / 4)
        u = round(amount / nav, 3)
        units += u
        txns.append({"date": d.isoformat(), "type": "PURCHASE_SIP", "amount": amount, "units": u, "nav": round(nav, 4), "flow": cash_flow("PURCHASE_SIP", amount)})
    return txns, round(units, 3)


def _fund(fid: str, name: str, isin: str, amfi: str, amc: str, folio: str, plan: str, start: date, months: int, sip: float, start_nav: float, nav: float, as_of: str) -> dict:
    txns, units = _sip(start, months, sip, start_nav, nav)
    return {
        "id": fid, "kind": "mf", "held_in": "folio", "name": name, "isin": isin, "amfi": amfi, "amc": amc, "folio": folio,
        "units": units, "nav": nav, "value": round(units * nav, 2), "cost": round(sip * months, 2), "as_of": as_of,
        "plan": plan, "transactions": txns,
    }


def demo_portfolio(today: date | None = None) -> dict:
    today = today or date.today()
    as_of = (today - timedelta(days=3)).isoformat()
    s36 = today - timedelta(days=30 * 36)
    s24 = today - timedelta(days=30 * 24)
    mf = [
        _fund("mf:INF879O01027:DEMO1", "Parag Parikh Flexi Cap Fund - Direct Plan - Growth", "INF879O01027", "122639", "PPFAS Mutual Fund", "DEMO1", "direct", s36, 36, 10000, 58.0, 95.4, as_of),
        _fund("mf:INF179K01CR2:DEMO2", "HDFC Mid Cap Fund - Regular Plan - Growth", "INF179K01CR2", "105758", "HDFC Mutual Fund", "DEMO2", "regular", s36, 36, 5000, 120.0, 208.4, as_of),
        _fund("mf:INF200K01180:DEMO3", "SBI Large Cap Fund - Regular Plan - Growth", "INF200K01180", "103504", "SBI Mutual Fund", "DEMO3", "regular", s24, 24, 5000, 78.0, 96.4, as_of),
        _fund("mf:INF769K01010:DEMO4", "Mirae Asset Large Cap Fund - Regular Plan - Growth", "INF769K01010", "107578", "Mirae Asset Mutual Fund", "DEMO4", "regular", s24, 24, 3000, 98.0, 117.3, as_of),
        _fund("mf:INF174K01153:DEMO5", "Kotak Large Cap Fund - Regular Plan - Growth", "INF174K01153", "114458", "Kotak Mahindra Mutual Fund", "DEMO5", "regular", s24, 24, 2000, 480.0, 585.8, as_of),
        _fund("mf:INF179K01XD8:DEMO6", "HDFC Corporate Bond Fund - Direct Plan - Growth", "INF179K01XD8", "118987", "HDFC Mutual Fund", "DEMO6", "direct", s24, 12, 10000, 32.0, 35.3, as_of),
    ]
    stocks_raw = [
        ("INE002A01018", "RELIANCE", "Reliance Industries Ltd", 40, 1351.0),
        ("INE040A01034", "HDFCBANK", "HDFC Bank Ltd", 120, 817.8),
        ("INE467B01029", "TCS", "Tata Consultancy Services Ltd", 15, 2460.0),
        ("INE009A01021", "INFY", "Infosys Ltd", 30, 1103.4),
        ("INE154A01025", "ITC", "ITC Ltd", 200, 301.0),
        ("INE090A01021", "ICICIBANK", "ICICI Bank Ltd", 25, 1585.5),
        ("INE397D01024", "BHARTIARTL", "Bharti Airtel Ltd", 10, 2128.5),
    ]
    stocks = [
        {"id": f"eq:{isin}:demo", "kind": "stock", "name": name, "isin": isin, "symbol": sym, "qty": float(q), "price": p, "value": round(q * p, 2), "account": "Demo broker ••4821", "as_of": as_of}
        for isin, sym, name, q, p in stocks_raw
    ]
    period = {"period_from": (today - timedelta(days=33)).isoformat(), "period_to": as_of}
    return {
        "rta": {"meta": {"source": "CAMS", "family": "rta", "detailed": True, "filename": "Sample CAMS statement", **period, "counts": {"mf": len(mf)}}, "holdings": {"mf": mf, "stocks": [], "bonds": [], "nps": None}},
        "demat": {"meta": {"source": "CDSL", "family": "demat", "detailed": False, "filename": "Sample CDSL eCAS", **period, "counts": {"stocks": len(stocks)}}, "holdings": {"mf": [], "stocks": stocks, "bonds": [], "nps": None}},
    }
