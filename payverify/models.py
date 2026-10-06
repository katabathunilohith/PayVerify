"""Database tables. Every Payment and BankCredit belongs to exactly one User,
and every query in the app filters on user_id, so accounts never see each
other's data."""

import json
import secrets

from flask_login import UserMixin

from .extensions import db
from .utils import decrypt_secret, encrypt_secret, parse_amount, split_list, utcnow

STATUS_PROCESSING = "processing"
STATUS_VERIFIED = "verified"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"

APPROVAL_AUTOMATIC = "automatic"
APPROVAL_BANK = "bank"

STATUS_LABELS = {
    STATUS_PROCESSING: "Reading…",
    STATUS_VERIFIED: "Verified",
    STATUS_NEEDS_REVIEW: "Needs review",
    STATUS_DUPLICATE: "Duplicate",
    STATUS_REJECTED: "Rejected",
}

DEFAULT_REPLY_VERIFIED = "Payment of {amount} received. Thank you, {name}! (UPI ref {utr})"
DEFAULT_REPLY_PENDING = (
    "Thanks {name}! We got your payment screenshot for {amount}. "
    "We're verifying it and will confirm shortly."
)
DEFAULT_REPLY_UNREADABLE = (
    "Thanks! We couldn't read that screenshot. Please send a clear screenshot of the "
    "payment that shows the amount and the UPI transaction ID."
)


def new_token():
    return secrets.token_urlsafe(24)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False, default="")
    business_name = db.Column(db.String(120), nullable=False, default="")
    # Part of the login cookie. Rotating it logs the account out everywhere.
    session_token = db.Column(db.String(64), nullable=False, default=new_token)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # "automatic": approve as soon as every screenshot check passes.
    # "bank": approve only once the money shows up in the bank (or by hand).
    approval_mode = db.Column(db.String(12), nullable=False, default=APPROVAL_BANK)

    # Payment checks
    timezone = db.Column(db.String(64), nullable=False, default="Asia/Kolkata")
    upi_ids = db.Column(db.Text, nullable=False, default="")
    payee_names = db.Column(db.Text, nullable=False, default="")
    accepted_amounts = db.Column(db.Text, nullable=False, default="")
    max_age_days = db.Column(db.Integer, nullable=False, default=3)

    # Automatic WhatsApp replies
    reply_enabled = db.Column(db.Boolean, nullable=False, default=True)
    reply_verified = db.Column(db.Text, nullable=False, default=DEFAULT_REPLY_VERIFIED)
    reply_pending = db.Column(db.Text, nullable=False, default=DEFAULT_REPLY_PENDING)
    reply_unreadable = db.Column(db.Text, nullable=False, default=DEFAULT_REPLY_UNREADABLE)

    # WhatsApp Cloud API connection (each account connects its own number)
    webhook_token = db.Column(db.String(64), unique=True, nullable=False, default=new_token)
    wa_verify_token = db.Column(db.String(64), nullable=False, default=new_token)
    wa_phone_number_id = db.Column(db.String(64), nullable=False, default="")
    wa_access_token_enc = db.Column(db.Text, nullable=False, default="")
    wa_app_secret_enc = db.Column(db.Text, nullable=False, default="")
    # Connection health, shown in Settings → WhatsApp
    wa_verified_at = db.Column(db.DateTime, nullable=True)
    wa_last_message_at = db.Column(db.DateTime, nullable=True)
    wa_last_error = db.Column(db.Text, nullable=False, default="")
    wa_last_error_at = db.Column(db.DateTime, nullable=True)

    # Bank SMS forwarding
    sms_token = db.Column(db.String(64), unique=True, nullable=False, default=new_token)
    sms_trusted_senders = db.Column(db.Text, nullable=False, default="")

    payments = db.relationship("Payment", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    bank_credits = db.relationship("BankCredit", backref="user", lazy="dynamic", cascade="all, delete-orphan")

    def get_id(self):
        return f"{self.id}:{self.session_token}"

    def rotate_session_token(self):
        self.session_token = new_token()

    @property
    def auto_approve(self):
        return self.approval_mode == APPROVAL_AUTOMATIC

    @property
    def display_name(self):
        return self.business_name or self.name or self.email

    @property
    def upi_id_list(self):
        return [item.lower() for item in split_list(self.upi_ids)]

    @property
    def payee_name_list(self):
        return split_list(self.payee_names)

    @property
    def accepted_amount_list(self):
        amounts = (parse_amount(item) for item in split_list(self.accepted_amounts))
        return [amount for amount in amounts if amount]

    @property
    def trusted_sender_list(self):
        return [item.upper() for item in split_list(self.sms_trusted_senders)]

    @property
    def wa_access_token(self):
        return decrypt_secret(self.wa_access_token_enc)

    @wa_access_token.setter
    def wa_access_token(self, value):
        self.wa_access_token_enc = encrypt_secret(value)

    @property
    def wa_app_secret(self):
        return decrypt_secret(self.wa_app_secret_enc)

    @wa_app_secret.setter
    def wa_app_secret(self, value):
        self.wa_app_secret_enc = encrypt_secret(value)

    @property
    def whatsapp_ready(self):
        return bool(self.wa_phone_number_id and self.wa_access_token and self.wa_app_secret)


class Payment(db.Model):
    __tablename__ = "payments"
    __table_args__ = (
        db.UniqueConstraint("user_id", "wa_message_id", name="uq_payment_wa_message"),
        db.Index("ix_payment_user_month", "user_id", "month_key"),
        db.Index("ix_payment_user_utr", "user_id", "utr"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False, default="upload")  # whatsapp | upload
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    month_key = db.Column(db.String(7), nullable=False)

    customer_name = db.Column(db.String(120), nullable=False, default="")
    customer_phone = db.Column(db.String(32), nullable=False, default="")
    customer_note = db.Column(db.Text, nullable=False, default="")
    wa_message_id = db.Column(db.String(128), nullable=True)
    wa_media_id = db.Column(db.String(128), nullable=True)

    screenshot_path = db.Column(db.String(255), nullable=True)
    screenshot_mime = db.Column(db.String(64), nullable=True)
    image_sha256 = db.Column(db.String(64), nullable=True, index=True)

    # What was read from the screenshot (or typed in by the owner)
    details_source = db.Column(db.String(10), nullable=False, default="none")  # ai | manual | none
    amount_paise = db.Column(db.BigInteger, nullable=True)
    utr = db.Column(db.String(32), nullable=True)
    payment_app = db.Column(db.String(40), nullable=False, default="")
    paid_at = db.Column(db.DateTime, nullable=True)  # local time, as shown on the receipt
    payee_name = db.Column(db.String(120), nullable=False, default="")
    payee_upi = db.Column(db.String(120), nullable=False, default="")
    payer_name = db.Column(db.String(120), nullable=False, default="")
    payer_upi = db.Column(db.String(120), nullable=False, default="")
    txn_status = db.Column(db.String(20), nullable=False, default="unknown")
    looks_edited = db.Column(db.Boolean, nullable=False, default=False)
    ai_warnings = db.Column(db.Text, nullable=False, default="[]")
    extraction_error = db.Column(db.Text, nullable=False, default="")

    checks_json = db.Column(db.Text, nullable=False, default="[]")
    status = db.Column(db.String(20), nullable=False, default=STATUS_PROCESSING, index=True)
    status_reason = db.Column(db.String(300), nullable=False, default="")
    manual_status = db.Column(db.Boolean, nullable=False, default=False)
    verified_by = db.Column(db.String(10), nullable=False, default="")  # bank | auto | manual
    bank_credit_id = db.Column(db.Integer, db.ForeignKey("bank_credits.id", ondelete="SET NULL"), nullable=True)
    last_reply_kind = db.Column(db.String(20), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    bank_credit = db.relationship("BankCredit", foreign_keys=[bank_credit_id])

    @property
    def status_label(self):
        if self.status == STATUS_VERIFIED and self.verified_by == "auto":
            return "Auto-approved"
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def checks(self):
        try:
            return json.loads(self.checks_json or "[]")
        except ValueError:
            return []

    @property
    def warnings(self):
        try:
            return json.loads(self.ai_warnings or "[]")
        except ValueError:
            return []


class BankCredit(db.Model):
    """Money that really arrived, from a bank SMS, a statement or typed in."""

    __tablename__ = "bank_credits"
    __table_args__ = (
        db.Index("ix_credit_user_month", "user_id", "month_key"),
        db.Index("ix_credit_user_utr", "user_id", "utr"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source = db.Column(db.String(20), nullable=False, default="manual")  # sms | statement | manual
    amount_paise = db.Column(db.BigInteger, nullable=False)
    utr = db.Column(db.String(32), nullable=True)
    credited_at = db.Column(db.DateTime, nullable=True)  # local time
    month_key = db.Column(db.String(7), nullable=False)
    sender = db.Column(db.String(64), nullable=False, default="")
    counterparty = db.Column(db.String(120), nullable=False, default="")
    raw_text = db.Column(db.Text, nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
