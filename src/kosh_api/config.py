"""Settings from environment variables (a local .env is loaded by `uv run --env-file .env`)."""

import os


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    port = int(os.getenv("PORT", "8000"))
    mongo_uri = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/kosh")
    jwt_secret = os.getenv("JWT_SECRET", "dev-secret")
    web_origin = [o.strip() for o in os.getenv("WEB_ORIGIN", "http://localhost:3300").split(",") if o.strip()]
    is_prod = os.getenv("ENV", "development") == "production"
    jobs_secret = os.getenv("JOBS_SECRET")
    # Turn off to run fully offline (tests): prices then come only from the uploaded statement.
    fetch_prices = _bool("FETCH_PRICES", True)
    max_upload_mb = 10


settings = Settings()

if settings.is_prod and settings.jwt_secret == "dev-secret":
    raise RuntimeError("Set JWT_SECRET in production")
