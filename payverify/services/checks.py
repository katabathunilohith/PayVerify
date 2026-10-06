"""Decide whether a payment is Verified, Needs review or a Duplicate.

Layer 1 checks that the screenshot makes sense (paid to you, right amount,
recent, UTR not used before). Layer 2 checks that the money really arrived
(a bank credit with the same UTR and amount). Only both together verify.
"""

import difflib
import json
import re
from datetime import timedelta

from ..models import (
    STATUS_DUPLICATE,
    STATUS_NEEDS_REVIEW,
    STATUS_VERIFIED,
    BankCredit,
    Payment,
)
from ..utils import format_inr, is_valid_utr, local_to_utc, to_local

PASS, FAIL, WARN, SKIP, WAIT = "pass", "fail", "warn", "skip", "wait"
# Checks that stop a payment from being verified. The first failing one becomes
# the headline: unreadable details first (the root cause), then the strongest
# fraud signal (bank mismatch).
BLOCKING = ["details", "bank", "payee", "txn_status", "recent", "utr_format", "amount"]
SOURCE_LABELS = {"sms": "bank SMS", "statement": "bank statement", "manual": "added by you"}


def _check(key, label, state, detail, ref_id=None):
    item = {"key": key, "label": label, "state": state, "detail": detail}
    if ref_id:
        item["ref_id"] = ref_id
    return item


def _short_dt(dt):
    return f"{dt.day} {dt:%b}, {dt.hour % 12 or 12}:{dt:%M %p}" if dt else ""


def _humanize(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "moments"
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{delta.days} days"


def check_details(p):
    label = "Payment details found"
    missing = [name for name, value in (("amount", p.amount_paise), ("UTR", p.utr)) if not value]
    if not missing:
        how = "typed in by you" if p.details_source == "manual" else "read from the screenshot"
        return _check("details", label, PASS, f"Amount and UTR {how}.")
    detail = f"Couldn't find the {' or '.join(missing)}."
    if p.extraction_error:
        detail += f" {p.extraction_error}"
    return _check("details", label, FAIL, detail)


def check_txn_status(p):
    label = "Receipt says “successful”"
    if p.txn_status == "success":
        return _check("txn_status", label, PASS, "The receipt shows a successful payment.")
    if p.txn_status in ("failed", "pending"):
        return _check("txn_status", label, FAIL, f"The receipt shows the payment as {p.txn_status}.")
    if p.details_source == "manual":
        return _check("txn_status", label, SKIP, "Details were entered by hand.")
    return _check("txn_status", label, WARN, "The receipt doesn't clearly say the payment succeeded.")


def check_utr_format(p):
    label = "UTR looks valid"
    if not p.utr:
        return _check("utr_format", label, SKIP, "No UTR to check.")
    if is_valid_utr(p.utr):
        return _check("utr_format", label, PASS, f"{p.utr} is a 12-digit UPI reference.")
    return _check("utr_format", label, FAIL, f"“{p.utr}” isn't a 12-digit UPI reference.")


def _norm_name(value):
    return " ".join(re.sub(r"[^a-z ]", " ", (value or "").lower()).split())


def _name_matches(found, mine):
    a, b = _norm_name(found), _norm_name(mine)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a.split(), b.split()), key=len)
    if any(len(word) >= 3 for word in short) and all(word in long_ for word in short):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _upi_matches(found, mine):
    found = (found or "").strip().lower().replace(" ", "")
    if not found:
        return False
    if found == mine:
        return True
    # Some apps mask part of the UPI ID: "rav****@okhdfcbank", "xxxxxx3210@ybl".
    local, _, domain = found.partition("@")
    local = re.sub(r"\*+|^x{3,}(?=\d)", "*", local)
    if "*" not in local:
        return False
    pattern = "".join(".+" if ch == "*" else re.escape(ch) for ch in local)
    return re.fullmatch(f"{pattern}@{re.escape(domain)}", mine) is not None


def check_payee(p, user):
    label = "Paid to you"
    upis, names = user.upi_id_list, user.payee_name_list
    if not upis and not names:
        return _check("payee", label, SKIP, "Add your UPI IDs in Settings to check who was paid.")
    if not p.payee_upi and not p.payee_name:
        return _check("payee", label, FAIL, "The screenshot doesn't show who was paid.")
    if p.payee_upi and upis:
        if any(_upi_matches(p.payee_upi, mine) for mine in upis):
            return _check("payee", label, PASS, f"Paid to {p.payee_upi}.")
        return _check("payee", label, FAIL, f"Paid to {p.payee_upi}, which isn't one of your UPI IDs.")
    if p.payee_name and any(_name_matches(p.payee_name, mine) for mine in names):
        return _check("payee", label, PASS, f"Paid to {p.payee_name}.")
    shown = p.payee_upi or p.payee_name
    return _check("payee", label, FAIL, f"Paid to {shown}, which doesn't match your payee names.")


def check_amount(p, user):
    label = "Amount matches your prices"
    accepted = user.accepted_amount_list
    if not accepted:
        return _check("amount", label, SKIP, "Optional: list your usual amounts in Settings.")
    if not p.amount_paise:
        return _check("amount", label, SKIP, "No amount to check.")
    if p.amount_paise in accepted:
        return _check("amount", label, PASS, f"{format_inr(p.amount_paise)} is one of your prices.")
    prices = ", ".join(format_inr(a) for a in accepted)
    return _check("amount", label, FAIL, f"{format_inr(p.amount_paise)} isn't one of your usual amounts ({prices}).")


def check_recent(p, user):
    label = "Paid recently"
    if not p.paid_at:
        return _check("recent", label, FAIL, "The payment date isn't visible.")
    paid_utc = local_to_utc(p.paid_at, user.timezone)
    if paid_utc > p.received_at + timedelta(minutes=15):
        return _check("recent", label, FAIL, "The payment time is later than when the screenshot arrived.")
    age = p.received_at - paid_utc
    if age > timedelta(days=user.max_age_days):
        return _check("recent", label, FAIL, f"Paid {age.days} days before it was sent. This could be an old screenshot.")
    return _check("recent", label, PASS, f"Paid {_humanize(age)} before the screenshot was sent.")


def check_duplicate(p, user):
    label = "Not sent before"
    earlier_q = Payment.query.filter(Payment.user_id == p.user_id, Payment.id < p.id)
    earlier, what = None, ""
    if p.utr:
        earlier, what = earlier_q.filter(Payment.utr == p.utr).order_by(Payment.id).first(), "UTR"
    if not earlier and p.image_sha256:
        earlier, what = earlier_q.filter(Payment.image_sha256 == p.image_sha256).order_by(Payment.id).first(), "screenshot"
    if earlier:
        who = earlier.customer_name or earlier.customer_phone or f"payment #{earlier.id}"
        when = _short_dt(to_local(earlier.received_at, user.timezone))
        return _check("duplicate", label, FAIL, f"The same {what} was already sent on {when} ({who}).", ref_id=earlier.id)
    return _check("duplicate", label, PASS, "This UTR hasn't been used before." if p.utr else "No earlier copy of this screenshot.")


def check_bank(p):
    label = "Money arrived in your bank"
    if not p.utr:
        return _check("bank", label, SKIP, "A UTR is needed to match against your bank."), None
    credit = BankCredit.query.filter_by(user_id=p.user_id, utr=p.utr).order_by(BankCredit.id).first()
    if not credit:
        return _check("bank", label, WAIT, "No matching bank credit yet. This updates by itself when your bank SMS or statement arrives."), None
    if p.amount_paise and credit.amount_paise != p.amount_paise:
        detail = f"Your bank received {format_inr(credit.amount_paise)}, but the screenshot shows {format_inr(p.amount_paise)}."
        return _check("bank", label, FAIL, detail), credit
    when = f" on {_short_dt(credit.credited_at)}" if credit.credited_at else ""
    detail = f"{format_inr(credit.amount_paise)} credited{when} ({SOURCE_LABELS.get(credit.source, credit.source)})."
    return _check("bank", label, PASS, detail), credit


def check_edited(p):
    label = "No signs of editing"
    if p.details_source != "ai":
        return _check("edited", label, SKIP, "Only checked when the AI reads the screenshot.")
    if p.looks_edited:
        return _check("edited", label, WARN, "The AI thinks this screenshot may have been edited.")
    return _check("edited", label, PASS, "Nothing on the screenshot looks edited.")


def _auto_blocker(payment, by_key):
    """Why automatic approval can't approve this payment, or None if it can.

    Without the bank's confirmation the screenshot is the only evidence, so
    automatic mode asks for more than "nothing failed".
    """
    if by_key["payee"]["state"] != PASS:
        return "Not approved automatically: add your UPI IDs in Settings so the app can check who was paid."
    if by_key["txn_status"]["state"] != PASS:
        return "Not approved automatically: the receipt doesn't clearly say the payment succeeded."
    if by_key["edited"]["state"] != PASS:
        return "Not approved automatically: the AI thinks this screenshot may have been edited."
    return None


def evaluate(payment, user):
    """Run every check and set payment.status. The payment must already have an id."""
    duplicate = check_duplicate(payment, user)
    bank, credit = check_bank(payment)
    checks = [
        check_details(payment),
        check_txn_status(payment),
        check_utr_format(payment),
        check_payee(payment, user),
        check_amount(payment, user),
        check_recent(payment, user),
        check_edited(payment),
        duplicate,
        bank,
    ]
    if payment.warnings:
        checks.append(_check("ai_warnings", "AI notes", WARN, " ".join(payment.warnings)))
    payment.checks_json = json.dumps(checks)
    is_duplicate = duplicate["state"] == FAIL
    payment.bank_credit_id = credit.id if credit and not is_duplicate else None

    if payment.manual_status:
        return
    failures = sorted(
        (c for c in checks if c["key"] in BLOCKING and c["state"] == FAIL),
        key=lambda c: BLOCKING.index(c["key"]),
    )
    verified_by = ""
    if is_duplicate:
        status, reason = STATUS_DUPLICATE, duplicate["detail"]
    elif failures:
        status, reason = STATUS_NEEDS_REVIEW, failures[0]["detail"]
    elif bank["state"] == PASS:
        status, reason, verified_by = STATUS_VERIFIED, "Screenshot checks passed and the money is in your bank.", "bank"
    elif user.auto_approve:
        blocker = _auto_blocker(payment, {c["key"]: c for c in checks})
        if blocker:
            status, reason = STATUS_NEEDS_REVIEW, blocker
        else:
            status, verified_by = STATUS_VERIFIED, "auto"
            reason = "Approved automatically: every screenshot check passed. Your bank hasn't confirmed it yet."
    else:
        status, reason = STATUS_NEEDS_REVIEW, bank["detail"]
    payment.status = status
    payment.status_reason = reason[:300]
    payment.verified_by = verified_by
