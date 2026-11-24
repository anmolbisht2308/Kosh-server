import io
import zipfile
from datetime import date

from casparser.types import CASData, NSDLCASData

from kosh_api.analytics import xirr
from kosh_api.normalize import cash_flow, merged, normalize
from kosh_api.prices import option_key, parse_amfi, parse_bhavcopy, scheme_base


# ---------------- XIRR ----------------
def test_xirr_simple_year():
    r = xirr([(date(2025, 1, 1), -100_000), (date(2026, 1, 1), 112_000)])
    assert abs(r - 0.12) < 0.001


def test_xirr_monthly_sip():
    flows = [(date(2025, m, 1), -10_000) for m in range(1, 13)]
    flows.append((date(2026, 1, 1), 130_000))
    r = xirr(flows)
    assert 0.14 < r < 0.18  # ~15.6%


def test_xirr_needs_both_signs():
    assert xirr([(date(2025, 1, 1), -100)]) is None
    assert xirr([(date(2025, 1, 1), 100), (date(2025, 6, 1), 100)]) is None


def test_cash_flow_signs_ignore_statement_sign_convention():
    assert cash_flow("PURCHASE_SIP", 5000) == -5000
    assert cash_flow("PURCHASE", -5000) == -5000
    assert cash_flow("REDEMPTION", -2000) == 2000
    assert cash_flow("DIVIDEND_PAYOUT", 300) == 300
    assert cash_flow("DIVIDEND_REINVEST", 300) is None


# ---------------- AMFI ----------------
AMFI = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Mid Cap Fund)

HDFC Mutual Fund

118989;INF179K01XQ0;-;HDFC Mid Cap Fund;Direct Plan;Growth Option;231.351;23-Nov-2026
105758;INF179K01CR2;-;HDFC Mid Cap Fund;Regular Plan;Growth Option;208.386;23-Nov-2026

Open Ended Schemes(Debt Scheme - Liquid Fund)

Old Style AMC

100001;INF000A01AA1;-;Old Liquid Fund - Direct Plan - Growth;;;1500.5;23-Nov-2026
100002;INF000A01AB9;-;Old Liquid Fund - Regular Plan - Growth;;;1490.1;23-Nov-2026
"""


def test_parse_amfi_both_layouts():
    rows = {r["_id"]: r for r in parse_amfi(AMFI)}
    assert rows["INF179K01CR2"]["plan"] == "regular" and rows["INF179K01XQ0"]["plan"] == "direct"
    assert rows["INF179K01CR2"]["base"] == rows["INF179K01XQ0"]["base"] == "hdfc mid cap fund"
    assert rows["INF179K01CR2"]["asset"] == "equity" and rows["INF179K01CR2"]["amc"] == "HDFC Mutual Fund"
    assert rows["INF000A01AB9"]["plan"] == "regular" and rows["INF000A01AA1"]["plan"] == "direct"
    assert rows["INF000A01AB9"]["base"] == rows["INF000A01AA1"]["base"]
    assert rows["INF000A01AB9"]["asset"] == "debt"
    assert rows["INF179K01CR2"]["price"] == 208.386 and rows["INF179K01CR2"]["date"] == "2026-11-23"


def test_option_and_base():
    assert option_key("Growth Option") == option_key("GROWTH") == "growth"
    assert option_key("Monthly IDCW Payout") == "idcw monthly payout"
    assert scheme_base("Axis ELSS Tax Saver Fund - Regular Plan - Growth") == scheme_base("Axis ELSS Tax Saver Fund - Direct Plan - Growth")


# ---------------- NSE ----------------
def test_parse_bhavcopy_prefers_eq_series():
    csv_text = (
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric\n"
        "2026-11-23,2026-11-23,CM,NSE,STK,1,INE002A01018,RELIANCE,BL,,,,,RELIANCE IND,1,1,1,1349.00,1,1340\n"
        "2026-11-23,2026-11-23,CM,NSE,STK,2,INE002A01018,RELIANCE,EQ,,,,,RELIANCE IND,1,1,1,1351.00,1,1340.50\n"
        "2026-11-23,2026-11-23,CM,NSE,ETF,3,INF000000000,SOMEETF,EQ,,,,,ETF,1,1,1,10,1,10\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bhav.csv", csv_text)
    rows = parse_bhavcopy(buf.getvalue())
    assert len(rows) == 1
    assert rows[0]["price"] == 1351.0 and rows[0]["prev_price"] == 1340.5 and rows[0]["symbol"] == "RELIANCE"


# ---------------- normalising statements ----------------
CAMS = {
    "statement_period": {"from": "01-Jan-2024", "to": "15-Nov-2026"},
    "investor_info": {"name": "ASHA VERMA", "email": "a@example.com", "address": "x", "mobile": "9999999999"},
    "cas_type": "DETAILED",
    "file_type": "CAMS",
    "folios": [
        {
            "folio": "1234567 / 89",
            "amc": "HDFC Mutual Fund",
            "PAN": "ABCDE1234F",
            "schemes": [
                {
                    "scheme": "HDFC Mid Cap Fund - Regular Plan - Growth",
                    "rta_code": "H01",
                    "rta": "CAMS",
                    "isin": "INF179K01CR2",
                    "amfi": "105758",
                    "open": "0",
                    "close": "100.5",
                    "close_calculated": "100.5",
                    "valuation": {"date": "2026-11-15", "nav": "200", "value": "20100", "cost": "15000"},
                    "transactions": [
                        {"date": "2024-01-10", "description": "SIP", "amount": "10000", "units": "60", "nav": "166", "balance": "60", "type": "PURCHASE_SIP"},
                        {"date": "2025-01-10", "description": "SIP", "amount": "5000", "units": "40.5", "nav": "123", "balance": "100.5", "type": "PURCHASE_SIP"},
                    ],
                },
                {
                    "scheme": "HDFC Old Fund - Growth",
                    "rta_code": "H02",
                    "rta": "CAMS",
                    "isin": "INF179K01ZZ9",
                    "open": "0",
                    "close": "0",
                    "close_calculated": "0",
                    "valuation": {"date": "2026-11-15", "nav": "10", "value": "0"},
                    "transactions": [],
                },
            ],
        }
    ],
}

CDSL = {
    "statement_period": {"from": "01-Oct-2026", "to": "31-Oct-2026"},
    "investor_info": {"name": "ASHA VERMA", "email": "", "address": "", "mobile": ""},
    "file_type": "CDSL",
    "accounts": [
        {
            "name": "ZERODHA BROKING LTD",
            "type": "CDSL Demat Account",
            "dp_id": "12081600",
            "client_id": "12081600123456",
            "folios": 0,
            "balance": "300000",
            "owners": [{"name": "ASHA VERMA", "PAN": "ABCDE1234F"}],
            "equities": [
                {"name": "RELIANCE INDUSTRIES LTD", "isin": "INE002A01018", "num_shares": "10", "price": "1300", "value": "13000", "symbol": "RELIANCE"},
                {"name": "SOLD LTD", "isin": "INE000X01010", "num_shares": "0", "price": "1", "value": "0"},
            ],
            "mutual_funds": [],
            "bonds": [],
        },
        {
            "name": "Mutual Fund Folios",
            "type": "Mutual Fund Folios",
            "folios": 1,
            "balance": "20000",
            "owners": [],
            "equities": [],
            "mutual_funds": [
                {"name": "HDFC MID CAP FUND - REGULAR PLAN - GROWTH", "isin": "INF179K01CR2", "balance": "100.5", "nav": "199", "value": "19999", "folio": "1234567", "total_cost": "15000"},
                {"name": "PARAG PARIKH FLEXI CAP - DIRECT - GROWTH", "isin": "INF879O01027", "balance": "50", "nav": "90", "value": "4500", "folio": "777", "total_cost": "4000"},
            ],
        },
    ],
}


def test_normalize_cams_detailed():
    out = normalize(CASData.model_validate(CAMS), "cams.pdf")
    assert out["family"] == "rta" and out["meta"]["source"] == "CAMS" and out["meta"]["detailed"]
    mf = out["holdings"]["mf"]
    assert len(mf) == 1  # the closed (0 unit) scheme is dropped
    f = mf[0]
    assert f["plan"] == "regular" and f["units"] == 100.5 and f["cost"] == 15000 and f["folio"] == "1234567 / 89"
    assert [t["flow"] for t in f["transactions"]] == [-10000, -5000]
    assert out["investor"] == "Asha Verma"
    assert "PAN" not in str(out) or "ABCDE1234F" not in str(out)  # PAN never stored


def test_normalize_cdsl_and_merge_without_double_counting():
    demat = normalize(NSDLCASData.model_validate(CDSL), "ecas.pdf")
    assert demat["family"] == "demat"
    st = demat["holdings"]["stocks"]
    assert len(st) == 1 and st[0]["symbol"] == "RELIANCE" and st[0]["account"].endswith("••3456")
    assert "12081600123456" not in str(demat)  # demat account number is masked
    rta = normalize(CASData.model_validate(CAMS), "cams.pdf")
    view = merged({"rta": {"holdings": rta["holdings"]}, "demat": {"holdings": demat["holdings"]}})
    isins = sorted(m["isin"] for m in view["mf"])
    assert isins == ["INF179K01CR2", "INF879O01027"]  # HDFC once (from CAMS), Parag Parikh from eCAS
    hdfc = next(m for m in view["mf"] if m["isin"] == "INF179K01CR2")
    assert hdfc["transactions"]  # the CAMS copy (with history) wins



# ---------------- statement layouts casparser misses ----------------
def _row(*texts):
    from types import SimpleNamespace

    return SimpleNamespace(cells=[SimpleNamespace(text=t) for t in texts])


def test_cdsl_summary_rows_with_broker_in_its_own_cell():
    """A long broker name gets its own cell on page 2; without casfix that whole account was dropped."""
    from casparser.parsers import cdsl

    import kosh_api.casfix  # noqa: F401

    usual = _row("CDSL Demat Account", "GROWW INVEST TECH PRIVATE LIMITED\nDP Id: 12088701 Client Id :11112222", "23", "3,33,434.01")
    ids_first = _row("CDSL Demat Account", "DP Id: 12083400 Client Id :33334444", "RAISE SECURITIES PRIVATE LIMITED", "24", "1,47,004.66")
    broker_first = _row("NSDL Demat Account", "WINT SECURITIES PRIVATE LIMITED", "DP Id: IN304633 Client Id :55556666", "0", "0.00")
    folios = _row("Mutual Fund Folios", "13 Folios", "13", "5,54,024.59")
    for r in (usual, ids_first, broker_first):
        assert cdsl._is_summary_demat_row(r)
    assert not cdsl._is_summary_demat_row(folios)

    ac, key = cdsl._account_from_summary_row(ids_first, [])
    assert ac.name == "RAISE SECURITIES PRIVATE LIMITED" and str(ac.balance) == "147004.66" and ac.folios == 24
    assert key == ("CDSL", "12083400", "33334444")  # matches the account header on the holdings pages
    ac, key = cdsl._account_from_summary_row(broker_first, [])
    assert ac.name == "WINT SECURITIES PRIVATE LIMITED" and key == ("NSDL", "IN304633", "55556666")
    ac, _ = cdsl._account_from_summary_row(usual, [])
    assert ac.name == "GROWW INVEST TECH PRIVATE LIMITED" and str(ac.balance) == "333434.01"


def test_statement_totals_are_checked_per_account():
    from kosh_api.normalize import statement_gaps

    demat = normalize(NSDLCASData.model_validate(CDSL), "ecas.pdf")
    accounts = demat["meta"]["accounts"]
    zerodha = next(a for a in accounts if a["kind"] == "cdsl")
    assert zerodha["statement"] == 300000 and zerodha["read"] == 13000  # statement says more than was itemised
    assert demat["meta"]["statement_total"] == 320000
    gaps = statement_gaps(accounts)
    assert [g["gap"] for g in gaps if g["kind"] == "cdsl"] == [287000]
    assert statement_gaps([{"label": "x", "statement": 100000, "read": 99990}]) == []  # rounding noise is fine
    assert "12081600123456" not in str(accounts)
