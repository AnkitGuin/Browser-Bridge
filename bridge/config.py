import os
from pathlib import Path


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes"}


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./browserbridge.db")
REDIS_URL = os.getenv("REDIS_URL", "")
BROWSER_URL = os.getenv("BROWSER_URL", "http://127.0.0.1:8000").rstrip("/")
BROWSER_CHROMIUM = os.getenv("BROWSER_CHROMIUM") or None
DEMO = flag("DEMO_IDENTITY", True)
TEST_MODE = flag("TEST_MODE")
SECURE_COOKIE = flag("SECURE_COOKIE")
DEV_PASSWORD = os.getenv("DEMO_PASSWORD", "demo-local-only")
COOKIE = "bb_session"
SESSION_SECONDS = 3600
RETENTION_SECONDS = int(os.getenv("EVIDENCE_RETENTION_SECONDS", "604800"))
ORIGINS = frozenset(
    item.rstrip("/")
    for item in os.getenv(
        "ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000,http://app:8000"
    ).split(",")
    if item
)
MODEL_KEY = os.getenv("MODEL_API_KEY", "")
MODEL_NAME = os.getenv("MODEL_NAME", "")
MODEL_URL = os.getenv("MODEL_URL", "https://api.openai.com/v1").rstrip("/")
MODEL_INPUT_LIMIT = 16_000
CONSOLE = Path(__file__).resolve().parent.parent / "console" / "dist"
