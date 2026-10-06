"""The pipeline: screenshot arrives -> details read -> checks -> customer reply.
Also records bank credits and re-checks the payments they match."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from flask import current_app
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    STATUS_NEEDS_REVIEW,
    STATUS_PROCESSING,
    STATUS_REJECTED,
    STATUS_VERIFIED,
    BankCredit,
    Payment,
)
from ..utils import (
    format_inr,
    local_now,
    month_key,
    normalize_utr,
    parse_amount,
    parse_local_datetime,
    to_local,
    utcnow,
)
from . import storage, whatsapp
from .checks import evaluate
from .extraction import TRANSACTION_STATUSES, extract_payment_details

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="payverify")

REPLY_FIELDS = {"verified": "reply_verified", "pending": "reply_pending", "unreadable": "reply_unreadable"}


def run_in_background(fn, *args):
    """Run slow work (AI reading, WhatsApp calls) after the HTTP response."""
    app = current_app._get_current_object()
    if app.config.get("PROCESS_INLINE"):
        fn(*args)
        return

    def runner():
        with app.app_context():
            try:
                fn(*args)
            except Exception:
                log.exception("Background job %s failed", fn.__name__)
            finally:
                db.session.remove()

    _executor.submit(runner)


# --- Screenshots -------------------------------------------------------------

def create_payment(user, source, customer_name="", customer_phone="", customer_note="",
                   wa_message_id=None, wa_media_id=None):
    now = utcnow()
    payment = Payment(
        user_id=user.id,
        source=source,
        received_at=now,
        month_key=month_key(to_local(now, user.timezone)),
        customer_name=(customer_name or "")[:120],
        customer_phone=(customer_phone or "")[:32],
        customer_note=customer_note or "",
        wa_message_id=wa_message_id,
        wa_media_id=wa_media_id,
        status=STATUS_PROCESSING,
        status_reason="Reading the screenshot…",
    )
    db.session.add(payment)
    db.session.flush()
    return payment


def _text(data, key, limit, lower=False):
    value = str(data.get(key) or "").strip()
    if value.lower() in ("null", "none", "n/a"):
        value = ""
    return (value.lower() if lower else value)[:limit]


def _flag(value):
    """The model should send real booleans, but accept "true"/"false" too."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def apply_extraction(payment, data):
    warnings = data.get("warnings")
    warnings = [str(item)[:300] for item in warnings][:5] if isinstance(warnings, list) else []
    payment.ai_warnings = json.dumps(warnings)
    payment.looks_edited = _flag(data.get("looks_edited"))
    if not _flag(data.get("is_payment_screenshot")):
        payment.details_source = "none"
        payment.extraction_error = "This doesn't look like a payment screenshot."
        return
    payment.details_source = "ai"
    payment.extraction_error = ""
    payment.amount_paise = parse_amount(data.get("amount"))
    utr = normalize_utr(data.get("utr"))
    payment.utr = utr[:32] if utr else None
    payment.paid_at = parse_local_datetime(data.get("paid_at"))
    payment.payee_name = _text(data, "payee_name", 120)
    payment.payee_upi = _text(data, "payee_upi_id", 120, lower=True)
    payment.payer_name = _text(data, "payer_name", 120)
    payment.payer_upi = _text(data, "payer_upi_id", 120, lower=True)
    payment.payment_app = _text(data, "payment_app", 40)
    status = _text(data, "transaction_status", 20, lower=True)
    payment.txn_status = status if status in TRANSACTION_STATUSES else "unknown"


def read_screenshot(payment, user, image_bytes=None):
    """Save the image (when new), read it with Meta's AI and run the checks.

    Raises storage.InvalidImage for files that aren't usable images.
    """
    if image_bytes is not None:
        stored = storage.save_screenshot(user.id, image_bytes)
        storage.delete_screenshot(payment.screenshot_path)
        payment.screenshot_path = stored.rel_path
        payment.screenshot_mime = stored.mime
        payment.image_sha256 = stored.sha256
        data, mime = stored.data, stored.mime
    else:
        data, mime = storage.read_screenshot(payment.screenshot_path), payment.screenshot_mime
    result = extract_payment_details(data, mime, local_now(user.timezone).date(), user.timezone)
    if result.ok:
        apply_extraction(payment, result.data)
    else:
        payment.extraction_error = result.error
    evaluate(payment, user)


def process_upload(user, image_bytes, customer_name="", customer_phone=""):
    payment = create_payment(user, "upload", customer_name, customer_phone)
    try:
        read_screenshot(payment, user, image_bytes)
    except storage.InvalidImage:
        db.session.rollback()
        raise
    db.session.commit()
    return payment


def receive_whatsapp_image(user, message):
    """Store a new WhatsApp screenshot and read it in the background.

    Returns None when this message was already received (Meta retries webhooks).
    """
    if Payment.query.filter_by(user_id=user.id, wa_message_id=message["message_id"]).first():
        return None
    payment = create_payment(
        user, "whatsapp",
        customer_name=message.get("name", ""),
        customer_phone=message.get("from", ""),
        customer_note=message.get("caption", ""),
        wa_message_id=message["message_id"],
        wa_media_id=message["media_id"],
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return None
    run_in_background(process_whatsapp_payment, payment.id)
    return payment


def _read_whatsapp_payment(payment, user):
    try:
        if payment.screenshot_path:
            read_screenshot(payment, user)
        else:
            raw, _mime = whatsapp.download_media(user, payment.wa_media_id)
            read_screenshot(payment, user, raw)
    except whatsapp.WhatsAppError as exc:
        payment.extraction_error = f"Couldn't download the image from WhatsApp: {exc}"
        record_whatsapp_error(user, payment.extraction_error)
        evaluate(payment, user)
    except storage.InvalidImage as exc:
        payment.extraction_error = str(exc)
        evaluate(payment, user)


def process_whatsapp_payment(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return
    _read_whatsapp_payment(payment, payment.user)
    db.session.commit()
    send_customer_reply(payment, payment.user)


def reread_payment(payment, user):
    """The owner pressed "Read again"."""
    if payment.screenshot_path:
        read_screenshot(payment, user)
    elif payment.wa_media_id:
        _read_whatsapp_payment(payment, user)
    else:
        evaluate(payment, user)
    db.session.commit()


# --- Customer replies --------------------------------------------------------

def reply_kind(payment):
    if payment.status == STATUS_VERIFIED:
        return "verified"
    if payment.status in (STATUS_REJECTED, STATUS_PROCESSING):
        return None
    if payment.details_source == "none":
        return "unreadable"
    return "pending"


def render_reply(template, payment, user):
    values = {
        "{name}": payment.customer_name or "there",
        "{amount}": format_inr(payment.amount_paise) if payment.amount_paise else "the payment",
        "{utr}": payment.utr or "-",
        "{business}": user.display_name,
    }
    for key, value in values.items():
        template = template.replace(key, value)
    return template.strip()


def send_customer_reply(payment, user):
    """Send at most one reply per outcome, and nothing after "verified"."""
    if payment.source != "whatsapp" or not payment.customer_phone:
        return False
    if not user.reply_enabled or not user.whatsapp_ready:
        return False
    kind = reply_kind(payment)
    if not kind or kind == payment.last_reply_kind or payment.last_reply_kind == "verified":
        return False
    text = render_reply(getattr(user, REPLY_FIELDS[kind]), payment, user)
    if not text:
        return False
    try:
        whatsapp.send_text(user, payment.customer_phone, text, reply_to=payment.wa_message_id)
    except whatsapp.WhatsAppError as exc:
        log.warning("WhatsApp reply for payment %s failed: %s", payment.id, exc)
        record_whatsapp_error(user, f"Reply to {payment.customer_phone} failed: {exc}")
        db.session.commit()
        return False
    payment.last_reply_kind = kind
    user.wa_last_error = ""
    db.session.commit()
    return True


def record_whatsapp_error(user, message):
    user.wa_last_error = message[:500]
    user.wa_last_error_at = utcnow()


def _send_reply_job(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment is not None:
        send_customer_reply(payment, payment.user)


def notify_verified(payments):
    """Tell WhatsApp customers once their payment is confirmed. Call after commit."""
    for payment in payments:
        if payment.source == "whatsapp":
            run_in_background(_send_reply_job, payment.id)


# --- Bank credits ------------------------------------------------------------

def recheck_utr(user, utr):
    """Re-run the checks on this account's payments with this UTR.

    Returns the payments that just became verified.
    """
    if not utr:
        return []
    newly_verified = []
    payments = (
        Payment.query.filter_by(user_id=user.id, utr=utr)
        .filter(Payment.status != STATUS_PROCESSING)
        .order_by(Payment.id)
    )
    for payment in payments:
        before = payment.status
        evaluate(payment, user)
        if payment.status == STATUS_VERIFIED and before != STATUS_VERIFIED:
            newly_verified.append(payment)
    return newly_verified


def recheck_pending(user):
    """After settings change, re-check payments that are still waiting for review."""
    newly_verified = []
    for payment in Payment.query.filter_by(user_id=user.id, status=STATUS_NEEDS_REVIEW, manual_status=False):
        evaluate(payment, user)
        if payment.status == STATUS_VERIFIED:
            newly_verified.append(payment)
    return newly_verified


def add_bank_credit(user, amount_paise, utr, credited_at=None, source="manual",
                    sender="", counterparty="", raw_text=""):
    """Record money that arrived. Does not commit.

    Returns (credit, created, newly_verified_payments). A credit whose UTR is
    already recorded is not added twice (SMS and statement often overlap).
    """
    utr = normalize_utr(utr)
    if utr:
        existing = BankCredit.query.filter_by(user_id=user.id, utr=utr).first()
        if existing:
            return existing, False, []
    when = credited_at or local_now(user.timezone)
    credit = BankCredit(
        user_id=user.id,
        source=source,
        amount_paise=amount_paise,
        utr=utr[:32] if utr else None,
        credited_at=credited_at,
        month_key=month_key(when),
        sender=(sender or "")[:64],
        counterparty=(counterparty or "")[:120],
        raw_text=(raw_text or "")[:2000],
    )
    db.session.add(credit)
    db.session.flush()
    return credit, True, recheck_utr(user, credit.utr)
