# Kosh: all your mutual funds and stocks, one honest view

Kosh is Business 2. People upload the free **CAS** statement (Consolidated Account Statement) that every Indian investor can get. Kosh then shows everything they own in one place:
- **Holdings:** every mutual fund (from CAMS/KFintech) plus stocks, bonds and NPS (from an NSDL/CDSL eCAS), counted once.
- **Live value:** fund NAVs from AMFI and stock closing prices from NSE, refreshed every 4 hours.
- **True yearly return (XIRR):** for each fund and the whole portfolio, worked out from every SIP and redemption in a detailed CAS.
- **What Regular plans cost:** the last 12 months' return of each Regular plan compared with the Direct plan of the same fund, shown in ₹ per year.
- **Plain facts:** concentration ("one holding is 32% of everything"), several funds in the same category, a statement that's getting old.
- **Today's change:** for stocks and funds.

It is **not investment advice** and never recommends what to buy or sell.

## Running cost: only the server

| Need | What Kosh uses | Cost |
|---|---|---|
| Read CAS PDFs | [casparser](https://github.com/codereverser/casparser) (MIT, open source), which runs inside the API | Free |
| Fund prices and categories | AMFI daily NAV file (`portal.amfiindia.com/spages/NAVAll.txt`) | Free, official |
| Stock prices | NSE daily bhavcopy CSV, matched by ISIN | Free, public |
| 1-year NAV history (Direct vs Regular) | [api.mfapi.in](https://www.mfapi.in) | Free, no key |
| Login | Email + password, with a one-time **recovery code** instead of password-reset emails | Free, no email service |
| Database | MongoDB Atlas free tier (M0) | Free |
| API hosting | Google Cloud Run (scales to zero) | Free tier at small scale |
| Website | Netlify | Free tier |

There is no SMS, payment gateway, paid market data or email service.

## Project layout

```
kosh/
  api/   Python 3.13 · FastAPI · casparser · MongoDB  → Cloud Run
  web/   Next.js 16 · React 19 · Tailwind 4            → Netlify
```

**API** (`api/src/kosh_api/`)
- `main.py`: routes for sign-up/login/recovery, statement upload, portfolio, demo, CSV export, account deletion and the scheduler price refresh.
- `normalize.py`: turns casparser output into holdings, and merges CAMS and eCAS statements without double counting (the CAMS copy wins because it has transactions).
- `prices.py`: AMFI and NSE downloads, caching, and pairing each Regular plan with its Direct twin.
- `analytics.py`: XIRR, live valuation, allocation and insights.
- `demo.py`: a sample portfolio built from real ISINs, so people can try Kosh without a statement.
- `security.py`: bcrypt hashes, JWT sessions (a password change logs out other devices) and rate limits.

**Privacy**
- The PDF and its password are read in memory and never written anywhere.
- PAN, phone and address are never stored, and demat account numbers are cut to their last 4 digits.
- Users can export or delete everything themselves.

## Run it locally

You need Python with [uv](https://docs.astral.sh/uv/), Node 22 and MongoDB on `127.0.0.1:27017`.

```bash
cd api && cp .env.example .env && uv sync && uv run --env-file .env uvicorn kosh_api.main:app --reload --port 8000
```

```bash
cd web && npm install && npm run dev -- -p 3300
```

Open http://localhost:3300. Sign up and press **Try a sample portfolio**, or upload a real CAS.

## Tests

```bash
cd api && FETCH_PRICES=false uv run pytest
```
These unit tests cover XIRR, the AMFI and NSE parsers, statement normalising, de-duplication, ETF pricing and the rate limits.

```bash
cd web && npx playwright test
```
This runs 61 end-to-end checks on desktop and a Pixel 7 phone (the API and web app must be running):
- every page: no errors, no sideways scroll, and an axe accessibility scan (WCAG AA)
- sign-up, login, logout and password recovery
- the dashboard and the fund detail popup
- upload errors (wrong password, not a PDF, not a CAS), CSV export, password change and account deletion
- popup and menu behaviour, plus API rules: auth, rate limits, one user never seeing another's data, and the portfolio maths

```bash
cd web && TOUR=1 npx playwright test e2e/tour.spec.ts
```
This saves screenshots of every screen, popup and menu (phone and desktop) to `web/e2e/.results/tour/`, for a person to look over after UI changes.

## Deploy (once the GitHub repos exist)

1. **Database:** in MongoDB Atlas, create a free M0 cluster and a user just for Kosh. Use a connection string ending in `/kosh`.
2. **API:** in `api/`, copy `deploy/.env.deploy.example` to `deploy/.env.deploy` and fill it in. Then run:
   ```bash
   ./deploy/gcp.sh
   ```
   It creates the secrets, deploys to Cloud Run in Mumbai, and schedules a price refresh every 4 hours.
3. **Website:** connect the `web` repo to Netlify. `netlify.toml` is already there. Set `NEXT_PUBLIC_API_URL` to the Cloud Run URL from step 2.
4. **Connect them:** run `./deploy/gcp.sh` again with `WEB_ORIGIN=https://<your Netlify or custom domain>`, so the browser is allowed to call the API.

## Known limits

- **Not yet tested with a real CAS PDF:** none was available while building it. The parser is casparser (widely used and actively maintained), and the mapping is unit-tested against its exact data types. Upload your own CAS first and check the numbers against it.
- **NSE sometimes blocks cloud servers.** If it does, stock prices fall back to the statement's prices (with their date shown), and fund prices from AMFI still update.
- **The Direct vs Regular figure is an estimate:** it is the last 12 months' return gap, applied to today's value.
- **Bonds and NPS are shown at statement values** (there is no free daily price for them).
- **Forgotten passwords:** with no email service, the recovery code is the only way back in. A user who loses both the code and the password must create a new account and upload the statement again (about a minute).
