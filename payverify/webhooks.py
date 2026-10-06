"""Endpoints called by WhatsApp (Meta) and by an SMS forwarding app.

Each account has its own secret URL token, so a message can only ever land
in the account it belongs to. WhatsApp calls are also signature-checked.
"""

import hmac
import json

from flask import Blueprint, abort, jsonify, request

from .extensions import db
from .models import User
from .services import whatsapp
from .services.processing import add_bank_credit, notify_verified, receive_whatsapp_image
from .services.sms_parser import is_trusted_sender, parse_bank_sms
from .utils import utcnow

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")

TEXT_KEYS = ("text", "message", "body", "msg", "sms", "content")
SENDER_KEYS = ("from", "sender", "address", "phone", "number")


def _account(column, token):
    if not token or len(token) > 64:
        abort(404)
    user = User.query.filter(column == token).first()
    if user is None:
        abort(404)
    return user


@bp.route("/whatsapp/<token>", methods=["GET"])
def whatsapp_verify(token):
    """Meta's one-time check when the webhook URL is saved in the app dashboard."""
    user = _account(User.webhook_token, token)
    given = request.args.get("hub.verify_token", "").encode()
    if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(given, user.wa_verify_token.encode()):
        user.wa_verified_at = utcnow()  # shown as "Webhook verified by Meta" in Settings
        db.session.commit()
        return request.args.get("hub.challenge", ""), 200, {"Content-Type": "text/plain"}
    abort(403)


@bp.route("/whatsapp/<token>", methods=["POST"])
def whatsapp_receive(token):
    user = _account(User.webhook_token, token)
    raw = request.get_data()
    if not whatsapp.verify_signature(user.wa_app_secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        abort(403)
    try:
        payload = json.loads(raw)
    except ValueError:
        abort(400)
    user.wa_last_message_at = utcnow()
    db.session.commit()
    received = 0
    for message in whatsapp.iter_incoming_images(payload, user.wa_phone_number_id):
        if receive_whatsapp_image(user, message):
            received += 1
    return jsonify(ok=True, received=received)


def _sms_fields():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form.to_dict()
    text = next((str(data[key]) for key in TEXT_KEYS if data.get(key)), "")
    sender = next((str(data[key]) for key in SENDER_KEYS if data.get(key)), "")
    if not text and not data:
        text = request.get_data(as_text=True)
    return text.strip()[:2000], (sender or request.args.get("from", "")).strip()[:64]


@bp.route("/sms/<token>", methods=["POST"])
def sms_receive(token):
    user = _account(User.sms_token, token)
    text, sender = _sms_fields()
    if not text:
        return jsonify(ok=False, result="no_text"), 400
    if not is_trusted_sender(sender, user.trusted_sender_list):
        return jsonify(ok=True, result="ignored_sender")
    parsed = parse_bank_sms(text)
    if parsed is None:
        return jsonify(ok=True, result="not_a_credit")
    credit, created, newly_verified = add_bank_credit(
        user, parsed.amount_paise, parsed.utr, parsed.credited_at,
        source="sms", sender=sender, counterparty=parsed.counterparty, raw_text=text,
    )
    db.session.commit()
    notify_verified(newly_verified)
    return jsonify(
        ok=True,
        result="recorded" if created else "already_recorded",
        verified_payments=len(newly_verified),
    )
