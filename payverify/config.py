"""Configuration, read from environment variables (or a .env file)."""

import os
import secrets
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def instance_dir():
    return Path(os.environ.get("INSTANCE_DIR") or BASE_DIR / "instance")


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    # No SECRET_KEY set: create one and keep it in the instance folder, so
    # logins and saved WhatsApp tokens keep working after a restart.
    folder = instance_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        folder = instance_dir()
        folder.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{folder / 'payverify.db'}"
    # Render/Heroku style URLs use the old "postgres://" scheme.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _effort(value):
    value = value.strip().lower()
    return value if value in {"minimal", "low", "medium", "high", "xhigh"} else "low"


def default_config():
    public_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    secure_cookies = _bool("SESSION_COOKIE_SECURE", public_url.startswith("https://"))
    return {
        "SECRET_KEY": _secret_key(),
        "SQLALCHEMY_DATABASE_URI": _database_url(),
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_pre_ping": True},
        "UPLOAD_FOLDER": os.environ.get("UPLOAD_FOLDER") or str(instance_dir() / "uploads"),
        "MAX_CONTENT_LENGTH": int(os.environ.get("MAX_UPLOAD_MB", "15")) * 1024 * 1024,
        "PUBLIC_BASE_URL": public_url,
        "ALLOW_SIGNUPS": _bool("ALLOW_SIGNUPS", True),
        "SIGNUP_CODE": os.environ.get("SIGNUP_CODE", "").strip(),
        # Meta Model API (dev.meta.ai) reads the screenshots.
        "META_MODEL_API_KEY": (os.environ.get("META_MODEL_API_KEY") or os.environ.get("MODEL_API_KEY") or "").strip(),
        # App-specific names, so variables meant for other tools don't leak in.
        "AI_MODEL": os.environ.get("PAYVERIFY_AI_MODEL", "").strip() or "muse-spark-1.3",
        "AI_EFFORT": _effort(os.environ.get("PAYVERIFY_AI_EFFORT", "")),
        "WHATSAPP_GRAPH_VERSION": os.environ.get("WHATSAPP_GRAPH_VERSION", "").strip() or "v25.0",
        "BEHIND_PROXY": _bool("BEHIND_PROXY", False),
        # Tests set this so webhook work runs synchronously.
        "PROCESS_INLINE": False,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": secure_cookies,
        "REMEMBER_COOKIE_HTTPONLY": True,
        "REMEMBER_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_SECURE": secure_cookies,
        "REMEMBER_COOKIE_DURATION": timedelta(days=30),
        "PERMANENT_SESSION_LIFETIME": timedelta(days=30),
        # The app stays open on phones for days; tie CSRF tokens to the session
        # instead of expiring them after an hour.
        "WTF_CSRF_TIME_LIMIT": None,
    }
