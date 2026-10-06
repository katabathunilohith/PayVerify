"""Read bank "money credited" SMS alerts.

Indian banks word these differently, so this looks for the pieces every
credit alert has: a credit word, a rupee amount and (usually) the 12-digit
UPI reference number.
"""

import re
from dataclasses import dataclass
from datetime import datetime

from ..utils import parse_amount

CREDIT_RE = re.compile(
    r"\b(credited|received|deposited|added to)\b|\bcredit(?:ed)? (?:to|in)\b|\bcr\b", re.I
)
DEBIT_RE = re.compile(
    r"\b(debited|withdrawn|spent|deducted|sent to|paid to|transferred to)\b|\bdr\b", re.I
)
IGNORE_RE = re.compile(
    r"\b(otp|one[- ]time password|will be credited|requested|request of|due on|is due)\b", re.I
)
AMOUNT_RE = re.compile(r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
REF_RE = re.compile(
    r"(?:utr|rrn|ref(?:erence)?(?:\s*(?:no|number|id))?|upi(?:\s*ref)?(?:\s*no)?|txn\s*id|transaction\s*id)"
    r"[\s.:#/-]*(\d{12})(?![\d@])",
    re.I,
)
# 12 digits not inside a longer number and not a phone-number UPI ID (9198...@ybl).
TWELVE_DIGITS_RE = re.compile(r"(?<!\d)(\d{12})(?![\d@])")
VPA_RE = re.compile(r"\b([a-z0-9][a-z0-9._-]{1,}@[a-z][a-z0-9]{1,})\b", re.I)

DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b"), ("%d", "%m", "%Y")),
    (re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2})\b"), ("%d", "%m", "%y")),
    (re.compile(r"\b(\d{1,2})[- ]?([A-Za-z]{3})[- ]?(\d{4})\b"), ("%d", "%b", "%Y")),
    (re.compile(r"\b(\d{1,2})[- ]?([A-Za-z]{3})[- ]?(\d{2})\b"), ("%d", "%b", "%y")),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("%Y", "%m", "%d")),
]
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


@dataclass
class ParsedCredit:
    amount_paise: int
    utr: str | None
    credited_at: datetime | None
    counterparty: str = ""


def is_credit_message(text):
    if not text or IGNORE_RE.search(text):
        return False
    credit = CREDIT_RE.search(text)
    if not credit:
        return False
    debit = DEBIT_RE.search(text)
    # Some alerts mention both ("credited to your a/c ... debited from VPA x").
    # Whichever comes first describes what happened to *your* account.
    return debit is None or credit.start() < debit.start()


def find_amount(text):
    for match in AMOUNT_RE.finditer(text):
        before = text[max(0, match.start() - 25):match.start()].lower()
        if "bal" in before or "limit" in before:
            continue
        amount = parse_amount(match.group(1))
        if amount:
            return amount
    return None


def find_utr(text):
    match = REF_RE.search(text) or TWELVE_DIGITS_RE.search(text)
    return match.group(1) if match else None


def find_datetime(text):
    found_date = None
    for pattern, parts in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            found_date = datetime.strptime(" ".join(match.groups()), " ".join(parts)).date()
            break
        except ValueError:
            continue
    if not found_date:
        return None
    hour = minute = 0
    time_match = TIME_RE.search(text)
    if time_match:
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        meridiem = (time_match.group(4) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            hour = minute = 0
    return datetime(found_date.year, found_date.month, found_date.day, hour, minute)


def parse_bank_sms(text):
    """Return a ParsedCredit for a credit alert, or None for anything else."""
    if not text:
        return None
    text = " ".join(str(text).split())
    if not is_credit_message(text):
        return None
    amount = find_amount(text)
    if not amount:
        return None
    vpa = VPA_RE.search(text)
    return ParsedCredit(
        amount_paise=amount,
        utr=find_utr(text),
        credited_at=find_datetime(text),
        counterparty=vpa.group(1) if vpa else "",
    )


def is_trusted_sender(sender, trusted_list):
    """Banks send alerts from registered headers such as "AX-HDFCBK".

    A plain phone number is never a bank, so a fake "credited" SMS sent from a
    personal number is rejected. If the owner lists their bank's sender names
    in settings, only those are accepted.
    """
    value = (sender or "").strip().upper()
    if not value:
        return False
    if trusted_list:
        return any(item in value for item in trusted_list)
    if re.fullmatch(r"[+\d\s()-]+", value):
        return False
    return bool(re.search(r"[A-Z]", value))
