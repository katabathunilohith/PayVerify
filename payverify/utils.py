"""Small helpers shared across the app: money, time, lists and secrets."""

import base64
import hashlib
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

DEFAULT_TZ = "Asia/Kolkata"
UTR_RE = re.compile(r"^\d{12}$")
MONTH_KEY_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --- Money -----------------------------------------------------------------

def parse_amount(value):
    """Turn "₹1,250.50", "Rs.500/-" or 500 into paise (int). None if not a positive amount."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        text = str(value)
    else:
        text = re.sub(r"(?i)rs\.?|inr|₹|/-|,|\s", "", str(value))
        match = re.search(r"\d+(?:\.\d+)?", text)
        if not match:
            return None
        text = match.group(0)
    try:
        paise = (Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return int(paise) if paise > 0 else None


def format_inr(paise, symbol=True):
    """Indian-style grouping: 125000000 paise -> ₹12,50,000."""
    if paise is None:
        return "—"
    sign = "-" if paise < 0 else ""
    rupees, rest = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    text = f"{digits}.{rest:02d}" if rest else digits
    return f"{sign}{'₹' if symbol else ''}{text}"


def paise_to_input(paise):
    """Value for an <input> showing rupees."""
    if paise is None:
        return ""
    rupees, rest = divmod(int(paise), 100)
    return f"{rupees}.{rest:02d}" if rest else str(rupees)


# --- UTR -------------------------------------------------------------------

def normalize_utr(value):
    """Keep only the characters of a UPI reference. Returns None when empty."""
    if not value:
        return None
    cleaned = re.sub(r"[^0-9A-Za-z]", "", str(value))
    return cleaned.upper() or None


def is_valid_utr(value):
    return bool(value and UTR_RE.match(value))


def mask_utr(value):
    if not value:
        return "—"
    return value if len(value) <= 6 else f"{value[:4]}…{value[-4:]}"


# --- Lists typed into settings ----------------------------------------------

def split_list(text):
    """Split comma or newline separated settings text into clean items."""
    if not text:
        return []
    return [item.strip() for item in re.split(r"[,\n;]+", text) if item.strip()]


# --- Time ------------------------------------------------------------------

def utcnow():
    """Naive UTC datetime, the format stored in the database."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_tz(name):
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def to_local(dt_utc, tz_name):
    """Stored naive-UTC datetime -> naive local datetime in the account's time zone."""
    if dt_utc is None:
        return None
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(get_tz(tz_name)).replace(tzinfo=None)


def local_to_utc(dt_local, tz_name):
    if dt_local is None:
        return None
    return dt_local.replace(tzinfo=get_tz(tz_name)).astimezone(timezone.utc).replace(tzinfo=None)


def local_now(tz_name):
    return to_local(utcnow(), tz_name)


def month_key(dt):
    return f"{dt.year:04d}-{dt.month:02d}"


def month_label(key):
    """"2026-10" -> "Oct 2026" (also used as the Excel tab name)."""
    year, month = key.split("-")
    return f"{MONTH_NAMES[int(month) - 1]} {year}"


def shift_month(key, delta):
    year, month = (int(part) for part in key.split("-"))
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def valid_month_key(value):
    return bool(value and MONTH_KEY_RE.match(value))


def parse_local_datetime(value):
    """Parse "2026-10-05T14:30" (with optional seconds) into a naive datetime."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


# --- Secrets stored in the database -----------------------------------------

def _fernet():
    digest = hashlib.sha256(("payverify-secrets:" + current_app.config["SECRET_KEY"]).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext):
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # SECRET_KEY changed since this was saved; the user must re-enter it.
        return ""
