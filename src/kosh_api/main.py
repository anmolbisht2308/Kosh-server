"""Kosh API: one view of your mutual funds and stocks, from the free CAS statement."""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from bson import ObjectId
from casparser import read_cas_pdf
from casparser.exceptions import IncorrectPasswordError, ParserException
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from . import analytics, db, prices
from .config import settings
from .demo import demo_portfolio
from .normalize import merged, normalize
from .security import (
    check_secret,
    client_ip,
    hash_secret,
    login_limiter,
    new_recovery_code,
    normalize_recovery,
    read_token,
    sign_token,
    signup_limiter,
    upload_limiter,
)

log = logging.getLogger("kosh")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.ensure_indexes()
    yield


app = FastAPI(title="Kosh API", lifespan=lifespan, docs_url=None if settings.is_prod else "/docs", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.web_origin, allow_methods=["*"], allow_headers=["*"], max_age=3600)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    res = await call_next(request)
    res.headers["X-Content-Type-Options"] = "nosniff"
    res.headers["Referrer-Policy"] = "no-referrer"
    res.headers["Cache-Control"] = "no-store"  # financial data never sits in a shared cache
    return res


# every error comes back as {"error": "..."} — the web app shows it as is
@app.exception_handler(HTTPException)
async def http_error(_req: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_req: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = str((first.get("loc") or ["", ""])[-1])
    label = FIELD_LABELS.get(field, "This field")
    limit = (first.get("ctx") or {}).get("max_length") or (first.get("ctx") or {}).get("min_length")
    kind = first.get("type", "")
    if kind == "string_too_long":
        msg = f"{label} is too long (at most {limit} characters)"
    elif kind == "string_too_short":
        msg = f"{label} must be at least {limit} characters"
    elif kind == "missing":
        msg = f"{label} is required"
    else:
        msg = first.get("msg", "Invalid input").removeprefix("Value error, ")
    return JSONResponse({"error": msg}, status_code=400)


FIELD_LABELS = {
    "email": "Email",
    "password": "Password",
    "new_password": "New password",
    "current_password": "Current password",
    "name": "Name",
    "recovery_code": "Recovery code",
    "file": "The file",
}


# ---------------- auth ----------------
class SignupIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=60)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class RecoverIn(BaseModel):
    email: str = Field(max_length=254)
    recovery_code: str = Field(max_length=40)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DeleteIn(BaseModel):
    password: str = Field(max_length=128)


def _email(raw: str) -> str:
    e = raw.strip().lower()
    if not EMAIL_RE.match(e):
        raise HTTPException(400, "Enter a valid email address")
    return e


def _public(u: dict) -> dict:
    return {"id": str(u["_id"]), "email": u["email"], "name": u.get("name")}


async def current_user(payload: dict = Depends(read_token)) -> dict:
    try:
        u = await db.users.find_one({"_id": ObjectId(payload.get("sub"))})
    except Exception:
        u = None
    if not u or u.get("token_version", 0) != payload.get("v"):
        raise HTTPException(401, "Your session has ended. Please log in again.")
    return u


@app.post("/api/auth/signup", status_code=201)
async def signup(body: SignupIn, request: Request):
    signup_limiter.check(f"signup:{client_ip(request)}")
    email = _email(body.email)
    code = new_recovery_code()
    doc = {
        "email": email,
        "name": (body.name or "").strip() or None,
        "password_hash": hash_secret(body.password),
        "recovery_hash": hash_secret(code),
        "token_version": 0,
        "created_at": datetime.now(UTC),
    }
    try:
        res = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "An account with this email already exists. Log in instead.")
    doc["_id"] = res.inserted_id
    # The recovery code is shown once. It's the only way back in if the password is forgotten.
    return {"token": sign_token(str(res.inserted_id), 0), "user": _public(doc), "recovery_code": code}


@app.post("/api/auth/login")
async def login(body: LoginIn, request: Request):
    email = body.email.strip().lower()
    key = f"login:{client_ip(request)}:{email}"
    login_limiter.guard(key)
    u = await db.users.find_one({"email": email})
    if not u or not check_secret(body.password, u.get("password_hash")):
        login_limiter.hit(key)
        raise HTTPException(401, "Email or password is wrong")
    return {"token": sign_token(str(u["_id"]), u.get("token_version", 0)), "user": _public(u)}


@app.post("/api/auth/recover")
async def recover(body: RecoverIn, request: Request):
    """Forgot password: the recovery code from sign-up sets a new password and issues a new code."""
    email = body.email.strip().lower()
    key = f"recover:{client_ip(request)}:{email}"
    login_limiter.guard(key)
    u = await db.users.find_one({"email": email})
    if not u or not check_secret(normalize_recovery(body.recovery_code), u.get("recovery_hash")):
        login_limiter.hit(key)
        raise HTTPException(401, "That email and recovery code don't match")
    code = new_recovery_code()
    v = u.get("token_version", 0) + 1
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"password_hash": hash_secret(body.new_password), "recovery_hash": hash_secret(code), "token_version": v}})
    return {"token": sign_token(str(u["_id"]), v), "user": _public(u), "recovery_code": code}


@app.get("/api/auth/me")
async def me(u: dict = Depends(current_user)):
    return {"user": _public(u)}


@app.post("/api/me/password")
async def change_password(body: PasswordIn, request: Request, u: dict = Depends(current_user)):
    key = f"password:{u['_id']}"
    login_limiter.guard(key)
    if not check_secret(body.current_password, u.get("password_hash")):
        login_limiter.hit(key)
        raise HTTPException(403, "Current password is wrong")
    v = u.get("token_version", 0) + 1  # logs out every other device
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"password_hash": hash_secret(body.new_password), "token_version": v}})
    return {"token": sign_token(str(u["_id"]), v)}


@app.post("/api/me/recovery-code")
async def rotate_recovery(body: DeleteIn, u: dict = Depends(current_user)):
    key = f"password:{u['_id']}"
    login_limiter.guard(key)
    if not check_secret(body.password, u.get("password_hash")):
        login_limiter.hit(key)
        raise HTTPException(403, "Password is wrong")
    code = new_recovery_code()
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"recovery_hash": hash_secret(code)}})
    return {"recovery_code": code}


@app.post("/api/me/delete")
async def delete_account(body: DeleteIn, u: dict = Depends(current_user)):
    key = f"password:{u['_id']}"
    login_limiter.guard(key)
    if not check_secret(body.password, u.get("password_hash")):
        login_limiter.hit(key)
        raise HTTPException(403, "Password is wrong")
    await db.portfolios.delete_one({"_id": str(u["_id"])})
    await db.users.delete_one({"_id": u["_id"]})
    return {"deleted": True}


# ---------------- statements ----------------
FRIENDLY_SOURCE = {"CAMS": "CAMS", "KFINTECH": "KFintech", "NSDL": "NSDL", "CDSL": "CDSL"}


@app.post("/api/statements", status_code=201)
async def upload_statement(request: Request, file: UploadFile = File(...), password: str = Form("", max_length=64), u: dict = Depends(current_user)):
    """Reads the PDF in memory. Neither the file nor its password is ever saved or logged."""
    upload_limiter.check(f"upload:{u['_id']}")
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"That file is over {settings.max_upload_mb} MB. CAS statements are usually under 2 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload the CAS as a PDF file")
    try:
        cas = await asyncio.wait_for(asyncio.to_thread(read_cas_pdf, io.BytesIO(data), password, "dict"), timeout=90)
        parsed = normalize(cas, file.filename)
    except IncorrectPasswordError:
        raise HTTPException(400, "Wrong PDF password. For NSDL/CDSL eCAS it's usually your PAN in capitals; for CAMS/KFintech it's the password you chose when requesting it.")
    except asyncio.TimeoutError:
        raise HTTPException(422, "This statement took too long to read. Try a shorter date range.")
    except (ParserException, ValueError) as e:
        log.info("CAS parse failed: %s", type(e).__name__)
        raise HTTPException(422, "We couldn't read this statement. Kosh reads the Consolidated Account Statement (CAS) from CAMS, KFintech, NSDL or CDSL.")
    except Exception as e:  # never echo PDF contents back
        log.warning("CAS parse crashed: %s", type(e).__name__)
        raise HTTPException(422, "We couldn't read this statement. If it's a genuine CAS, try downloading it again.")
    finally:
        del data

    uid = str(u["_id"])
    now = datetime.now(UTC)
    existing = await db.portfolios.find_one({"_id": uid}, {"is_demo": 1}) or {}
    update: dict = {"$set": {parsed["family"]: {"meta": parsed["meta"], "holdings": parsed["holdings"], "uploaded_at": now}, "updated_at": now, "is_demo": False}}
    if existing.get("is_demo"):  # real data replaces the sample entirely
        update["$unset"] = {"rta" if parsed["family"] == "demat" else "demat": ""}
    if parsed.get("investor"):
        update["$set"]["investor"] = parsed["investor"]
    await db.portfolios.update_one({"_id": uid}, update, upsert=True)
    meta = parsed["meta"]
    return {"statement": {**meta, "source_label": FRIENDLY_SOURCE.get(meta["source"], meta["source"])}}


@app.delete("/api/statements/{family}")
async def remove_statement(family: str, u: dict = Depends(current_user)):
    if family not in {"rta", "demat"}:
        raise HTTPException(404, "Not found")
    await db.portfolios.update_one({"_id": str(u["_id"])}, {"$unset": {family: ""}})
    return {"removed": family}


@app.post("/api/demo")
async def load_demo(u: dict = Depends(current_user)):
    uid = str(u["_id"])
    current = await db.portfolios.find_one({"_id": uid}, {"is_demo": 1, "rta": 1, "demat": 1}) or {}
    if (current.get("rta") or current.get("demat")) and not current.get("is_demo"):
        raise HTTPException(409, "You already have your own statement loaded. Remove it first to try the sample.")
    demo = demo_portfolio()
    now = datetime.now(UTC)
    for fam in demo.values():
        fam["uploaded_at"] = now
    await db.portfolios.replace_one({"_id": uid}, {"_id": uid, **demo, "is_demo": True, "investor": "Sample Investor", "updated_at": now}, upsert=True)
    return {"loaded": True}


# ---------------- portfolio ----------------
_refresh_task: asyncio.Task | None = None


async def _fresh_prices():
    """Give a due price refresh a few seconds; if it takes longer, show cached prices and let it finish."""
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(prices.refresh())
    try:
        await asyncio.wait_for(asyncio.shield(_refresh_task), timeout=6)
    except asyncio.TimeoutError:
        log.warning("price refresh still running; using cached prices")


def _statements(p: dict) -> list[dict]:
    out = []
    for fam in ("rta", "demat"):
        if p.get(fam):
            meta = p[fam].get("meta") or {}
            out.append({**meta, "family": fam, "source_label": FRIENDLY_SOURCE.get(meta.get("source"), meta.get("source")), "uploaded_at": p[fam].get("uploaded_at")})
    return out


@app.get("/api/portfolio")
async def get_portfolio(u: dict = Depends(current_user)):
    p = await db.portfolios.find_one({"_id": str(u["_id"])})
    if not p or not (p.get("rta") or p.get("demat")):
        return {"empty": True}
    await _fresh_prices()
    statements = _statements(p)
    summary = await analytics.summarize(merged(p), statements)
    m = await db.meta.find_one({"_id": "prices"}) or {}
    return {
        "empty": False,
        "is_demo": bool(p.get("is_demo")),
        "investor": p.get("investor"),
        "statements": statements,
        "prices_updated": {"funds": m.get("amfi_at"), "stocks": m.get("nse_at")},
        **summary,
    }


@app.get("/api/portfolio/funds/{fund_id:path}")
async def fund_detail(fund_id: str, u: dict = Depends(current_user)):
    p = await db.portfolios.find_one({"_id": str(u["_id"])})
    fund = next((m for m in merged(p or {}).get("mf", []) if m["id"] == fund_id), None) if p else None
    if not fund:
        raise HTTPException(404, "Fund not found")
    return {"transactions": sorted(fund.get("transactions") or [], key=lambda t: t["date"], reverse=True)}


@app.get("/api/portfolio/export.csv")
async def export_csv(u: dict = Depends(current_user)):
    p = await db.portfolios.find_one({"_id": str(u["_id"])})
    if not p or not (p.get("rta") or p.get("demat")):
        raise HTTPException(404, "Nothing to export yet")
    s = await analytics.summarize(merged(p), _statements(p))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Type", "Name", "ISIN", "Symbol/Folio", "Units/Qty", "Price", "Price date", "Value", "Invested", "XIRR %"])
    for m in s["holdings"]["mf"]:
        w.writerow(["Mutual fund", m["name"], m.get("isin"), m.get("folio"), m["units"], m.get("price"), m.get("price_date"), m["value"], m.get("cost"), round(m["xirr"] * 100, 2) if m.get("xirr") is not None else ""])
    for x in s["holdings"]["stocks"]:
        w.writerow(["Stock", x["name"], x.get("isin"), x.get("symbol"), x["qty"], x.get("price"), x.get("price_date"), x["value"], "", ""])
    for b in s["holdings"]["bonds"]:
        w.writerow(["Bond", b["name"], b.get("isin"), "", b["qty"], "", b.get("as_of"), b["value"], "", ""])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="kosh-portfolio.csv"'})


# ---------------- ops ----------------
@app.get("/health")
async def health():
    try:
        await db.client.admin.command("ping")
        ok = True
    except Exception:
        ok = False
    return {"ok": True, "db": ok}


@app.post("/internal/refresh-prices")
async def refresh_prices(x_jobs_secret: str = Header("")):
    """Called by a scheduler every few hours in production, so the first visitor never waits."""
    if not settings.jobs_secret or not hmac.compare_digest(x_jobs_secret, settings.jobs_secret):
        raise HTTPException(403, "Forbidden")
    return {"ok": True, "result": await prices.refresh(force=True)}


def main() -> None:
    import uvicorn

    uvicorn.run("kosh_api.main:app", host="0.0.0.0", port=settings.port, proxy_headers=True, forwarded_allow_ips="*")
