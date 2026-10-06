"""Command-line helpers:

    flask --app payverify check
    flask --app payverify reset-password you@example.com
    flask --app payverify seed-demo
"""

import secrets
from datetime import timedelta

import click
from werkzeug.security import generate_password_hash

from .extensions import db
from .models import BankCredit, Payment, User
from .services.checks import evaluate
from .services.extraction import check_api_key
from .utils import local_now, local_to_utc, month_key

DEMO_UPI = "demo.rentals@okhdfcbank"


def _line(ok, label, message):
    click.echo(f"  {'✔' if ok else '✘'} {label}: {message}")


def register(app):
    @app.cli.command("check")
    @click.option("--offline", is_flag=True, help="Don't contact Meta to test the API key.")
    def check(offline):
        """Show what's connected: AI key, database, public address and each account."""
        config = app.config
        click.echo("PayVerify status")

        if offline or not config.get("META_MODEL_API_KEY"):
            ok = bool(config.get("META_MODEL_API_KEY"))
            _line(ok, "Meta AI key", "set (not tested)." if ok else "not set. Add META_MODEL_API_KEY to the .env file.")
        else:
            ok, message = check_api_key()
            _line(ok, "Meta AI key", message)

        url = db.engine.url
        where = url.database if url.get_backend_name() == "sqlite" else url.render_as_string(hide_password=True)
        users, payments, credits = User.query.count(), Payment.query.count(), BankCredit.query.count()
        _line(True, "Database", f"connected ({where}): {users} account(s), {payments} payment(s), {credits} bank credit(s).")

        public = config.get("PUBLIC_BASE_URL", "")
        if public.startswith("https://"):
            _line(True, "Public address", public)
        else:
            _line(False, "Public address", "PUBLIC_BASE_URL isn't an https:// address yet, so WhatsApp can't reach this app.")

        for user in User.query.order_by(User.id):
            click.echo(f"\n  Account {user.email}")
            _line(bool(user.upi_ids or user.payee_names), "UPI IDs", user.upi_ids.replace("\n", ", ") or "not added (Settings → Checks & replies).")
            _line(user.whatsapp_ready, "WhatsApp details", "saved." if user.whatsapp_ready else "not saved (Settings → WhatsApp).")
            _line(bool(user.wa_verified_at), "Webhook verified by Meta", str(user.wa_verified_at) + " UTC" if user.wa_verified_at else "not yet.")
            _line(bool(user.wa_last_message_at), "Last WhatsApp update", str(user.wa_last_message_at) + " UTC" if user.wa_last_message_at else "none yet.")
            _line(user.auto_approve, "Auto-approve", "on." if user.auto_approve else "off (payments wait for your bank or for you).")
            if user.wa_last_error:
                _line(False, "Last WhatsApp problem", user.wa_last_error)

    @app.cli.command("reset-password")
    @click.argument("email")
    @click.password_option(help="The new password")
    def reset_password(email, password):
        """Set a new password for an account (there is no email-based reset)."""
        user = User.query.filter_by(email=email.strip().lower()).first()
        if user is None:
            raise click.ClickException(f"No account with email {email}")
        if len(password) < 8:
            raise click.ClickException("Use at least 8 characters.")
        user.password_hash = generate_password_hash(password)
        user.rotate_session_token()
        db.session.commit()
        click.echo(f"Password updated for {user.email}. All their devices were signed out.")

    @app.cli.command("seed-demo")
    @click.option("--email", default="demo@payverify.test", show_default=True)
    def seed_demo(email):
        """Create a demo account with sample payments (for trying the app out)."""
        email = email.strip().lower()
        if User.query.filter_by(email=email).first():
            raise click.ClickException(f"{email} already exists.")
        password = secrets.token_urlsafe(9)
        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            name="Demo Owner",
            business_name="Demo Rentals",
            upi_ids=DEMO_UPI,
            payee_names="Demo Rentals",
            accepted_amounts="500, 800, 1500",
        )
        db.session.add(user)
        db.session.flush()
        _add_demo_data(user)
        db.session.commit()
        click.echo(f"Demo account created.\n  Email:    {email}\n  Password: {password}")
        click.echo("This account is for trying the app only; delete the database before going live.")


def _demo_payment(user, days_ago, hours_ago, name, phone, rupees, utr, app_name,
                  payee_upi=DEMO_UPI, paid_minutes_before=10, paid_days_before=0):
    received = local_now(user.timezone) - timedelta(days=days_ago, hours=hours_ago)
    payment = Payment(
        user_id=user.id,
        source="whatsapp",
        received_at=local_to_utc(received, user.timezone),
        month_key=month_key(received),
        customer_name=name,
        customer_phone=phone,
        details_source="ai",
        amount_paise=rupees * 100,
        utr=utr,
        payment_app=app_name,
        paid_at=received - timedelta(days=paid_days_before, minutes=paid_minutes_before),
        payee_name="Demo Rentals",
        payee_upi=payee_upi,
        txn_status="success",
    )
    db.session.add(payment)
    db.session.flush()
    return payment


def _demo_credit(user, days_ago, hours_ago, rupees, utr):
    when = local_now(user.timezone) - timedelta(days=days_ago, hours=hours_ago)
    db.session.add(BankCredit(
        user_id=user.id, source="sms", amount_paise=rupees * 100, utr=utr, credited_at=when,
        month_key=month_key(when), sender="AX-HDFCBK",
        raw_text=f"Rs.{rupees}.00 credited to a/c XX1234 by UPI Ref No {utr}",
    ))


def _add_demo_data(user):
    _demo_credit(user, 0, 2, 1500, "627812345601")
    _demo_credit(user, 1, 5, 800, "627812345602")
    _demo_credit(user, 2, 1, 500, "627812345605")
    _demo_credit(user, 18, 3, 1500, "627812345610")
    db.session.flush()
    payments = [
        _demo_payment(user, 0, 2, "Ravi Kumar", "919800000001", 1500, "627812345601", "Google Pay"),
        _demo_payment(user, 1, 5, "Priya S", "919800000002", 800, "627812345602", "PhonePe"),
        _demo_payment(user, 0, 1, "Anil", "919800000003", 1500, "627812345603", "Paytm"),
        _demo_payment(user, 0, 0, "Ravi Kumar", "919800000001", 1500, "627812345601", "Google Pay"),
        _demo_payment(user, 1, 1, "Arjun", "919800000004", 1500, "627812345604", "PhonePe",
                      payee_upi="someone.else@ybl"),
        _demo_payment(user, 2, 1, "Meena", "919800000005", 5000, "627812345605", "Google Pay"),
        _demo_payment(user, 3, 2, "Kiran", "919800000006", 500, "627812345606", "BHIM", paid_days_before=9),
        _demo_payment(user, 18, 3, "Sunita", "919800000007", 1500, "627812345610", "Google Pay"),
    ]
    for payment in payments:
        evaluate(payment, user)
