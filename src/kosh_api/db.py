from pymongo import ASCENDING, AsyncMongoClient

from .config import settings

client: AsyncMongoClient = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
db = client.get_default_database("kosh")

users = db.users
portfolios = db.portfolios  # one document per user (_id = user id)
prices = db.prices  # latest price per ISIN (mutual funds from AMFI, stocks from NSE)
meta = db.meta  # bookkeeping, e.g. when prices were last refreshed
plan_gaps = db.plan_gaps  # cached 1-year Direct vs Regular return gaps


async def ensure_indexes() -> None:
    await users.create_index([("email", ASCENDING)], unique=True)
    await prices.create_index([("scheme_code", ASCENDING)])
    await prices.create_index([("kind", ASCENDING), ("base", ASCENDING), ("option", ASCENDING), ("plan", ASCENDING)])
