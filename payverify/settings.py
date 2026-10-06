"""Per-account settings: checks, replies, WhatsApp, bank SMS and security."""

import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import MIN_PASSWORD, safe_next
from .extensions import db
from .models import (
    APPROVAL_AUTOMATIC,
    APPROVAL_BANK,
    DEFAULT_REPLY_PENDING,
    DEFAULT_REPLY_UNREADABLE,
    DEFAULT_REPLY_VERIFIED,
    new_token,
)
from .payments import external_url
from .services import whatsapp
from .services.processing import notify_verified, recheck_pending
from .utils import get_tz, paise_to_input, parse_amount, split_list

bp = Blueprint("settings", __name__, url_prefix="/settings")

TIMEZONES = [
    "Asia/Kolkata", "Asia/Kathmandu", "Asia/Dhaka", "Asia/Colombo", "Asia/Dubai",
    "Asia/Singapore", "Europe/London", "America/New_York", "UTC",
]


@bp.route("/", methods=["GET", "POST"])
@login_required
def general():
    user = current_user._get_current_object()
    if request.method == "POST":
        form = request.form
        amounts = split_list(form.get("accepted_amounts", ""))
        bad_amounts = [item for item in amounts if not parse_amount(item)]
        if bad_amounts:
            flash(f"These amounts aren't numbers: {', '.join(bad_amounts)}", "error")
            return render_template("settings/general.html", timezones=TIMEZONES), 400
        try:
            max_age = min(max(int(form.get("max_age_days", 3)), 1), 60)
        except ValueError:
            max_age = 3
        timezone = form.get("timezone", "Asia/Kolkata")
        user.name = form.get("name", "").strip()[:120] or user.name
        user.business_name = form.get("business_name", "").strip()[:120]
        user.upi_ids = "\n".join(item.lower().replace(" ", "") for item in split_list(form.get("upi_ids", "")))
        user.payee_names = "\n".join(split_list(form.get("payee_names", "")))
        user.accepted_amounts = ", ".join(paise_to_input(parse_amount(item)) for item in amounts)
        user.max_age_days = max_age
        user.timezone = timezone if get_tz(timezone).key == timezone else user.timezone
        user.reply_enabled = bool(form.get("reply_enabled"))
        user.reply_verified = form.get("reply_verified", "").strip()[:1000] or DEFAULT_REPLY_VERIFIED
        user.reply_pending = form.get("reply_pending", "").strip()[:1000] or DEFAULT_REPLY_PENDING
        user.reply_unreadable = form.get("reply_unreadable", "").strip()[:1000] or DEFAULT_REPLY_UNREADABLE
        db.session.flush()
        newly_verified = recheck_pending(user)
        db.session.commit()
        notify_verified(newly_verified)
        flash("Settings saved. Payments waiting for review were re-checked.", "success")
        return redirect(url_for(".general"))
    return render_template("settings/general.html", timezones=TIMEZONES)


@bp.route("/approval", methods=["POST"])
@login_required
def approval():
    """Turn automatic approval on or off (from Settings or the dashboard)."""
    user = current_user._get_current_object()
    mode = request.form.get("mode")
    if mode not in (APPROVAL_AUTOMATIC, APPROVAL_BANK):
        abort(400)
    user.approval_mode = mode
    db.session.flush()
    newly_verified = recheck_pending(user)
    db.session.commit()
    notify_verified(newly_verified)
    if mode == APPROVAL_AUTOMATIC:
        message = "Automatic approval is on. Payments are approved as soon as the screenshot passes every check."
        if newly_verified:
            message += f" {len(newly_verified)} waiting payment(s) were approved."
    else:
        message = "Automatic approval is off. New payments wait for your bank's confirmation. Already approved payments stay approved."
    flash(message, "success")
    return redirect(safe_next(request.form.get("next")) or url_for(".general"))


@bp.route("/whatsapp", methods=["GET", "POST"])
@login_required
def whatsapp_settings():
    user = current_user._get_current_object()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            user.wa_phone_number_id = re.sub(r"\D", "", request.form.get("phone_number_id", ""))[:64]
            token = request.form.get("access_token", "").strip()
            secret = request.form.get("app_secret", "").strip()
            if token:
                user.wa_access_token = token
            if secret:
                user.wa_app_secret = secret
            db.session.commit()
            flash("WhatsApp settings saved.", "success")
        elif action == "test":
            try:
                flash(f"Connected to {whatsapp.check_connection(user)}.", "success")
            except whatsapp.WhatsAppError as exc:
                flash(f"WhatsApp said: {exc}", "error")
        elif action == "send_test":
            to = re.sub(r"\D", "", request.form.get("test_phone", ""))
            if len(to) < 10:
                flash("Enter the phone number with its country code, for example 919876543210.", "error")
            else:
                try:
                    whatsapp.send_test_template(user, to)
                    flash(f"Test message sent to {to}. Check WhatsApp on that phone.", "success")
                except whatsapp.WhatsAppError as exc:
                    flash(f"WhatsApp said: {exc}", "error")
        elif action == "rotate":
            user.webhook_token = new_token()
            user.wa_verify_token = new_token()
            user.wa_verified_at = user.wa_last_message_at = None  # Meta must verify the new URL
            db.session.commit()
            flash("New webhook URL and verify token created. Update them in your Meta app.", "success")
        elif action == "disconnect":
            user.wa_phone_number_id = ""
            user.wa_access_token = ""
            user.wa_app_secret = ""
            user.wa_verified_at = user.wa_last_message_at = user.wa_last_error_at = None
            user.wa_last_error = ""
            db.session.commit()
            flash("WhatsApp disconnected.", "success")
        return redirect(url_for(".whatsapp_settings"))
    return render_template(
        "settings/whatsapp.html",
        webhook_url=external_url(url_for("webhooks.whatsapp_receive", token=user.webhook_token)),
        has_token=bool(user.wa_access_token_enc),
        has_secret=bool(user.wa_app_secret_enc),
    )


@bp.route("/sms", methods=["GET", "POST"])
@login_required
def sms_settings():
    user = current_user._get_current_object()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "save":
            senders = split_list(request.form.get("trusted_senders", ""))
            user.sms_trusted_senders = ", ".join(item.upper()[:30] for item in senders)
            db.session.commit()
            flash("Saved.", "success")
        elif action == "rotate":
            user.sms_token = new_token()
            db.session.commit()
            flash("New forwarding URL created. Update it in your SMS forwarding app.", "success")
        return redirect(url_for(".sms_settings"))
    return render_template(
        "settings/sms.html",
        sms_url=external_url(url_for("webhooks.sms_receive", token=user.sms_token)),
    )


@bp.route("/security", methods=["GET", "POST"])
@login_required
def security():
    user = current_user._get_current_object()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            if not check_password_hash(user.password_hash, current):
                flash("Your current password is wrong.", "error")
            elif len(new) < MIN_PASSWORD:
                flash(f"Use at least {MIN_PASSWORD} characters.", "error")
            elif new != request.form.get("confirm_password", ""):
                flash("The new passwords don't match.", "error")
            else:
                user.password_hash = generate_password_hash(new)
                user.rotate_session_token()  # signs out every other device
                db.session.commit()
                remembered = current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token") in request.cookies
                login_user(user, remember=remembered)
                flash("Password changed. Other devices have been signed out.", "success")
            return redirect(url_for(".security"))
        if action == "logout_all":
            user.rotate_session_token()
            db.session.commit()
            logout_user()
            flash("Signed out on every device.", "success")
            return redirect(url_for("auth.login"))
    return render_template("settings/security.html")
