"""Payment screens: monthly list, details, upload, export."""

import io
import os

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from .extensions import db
from .models import (
    STATUS_DUPLICATE,
    STATUS_LABELS,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    STATUS_VERIFIED,
    BankCredit,
    Payment,
)
from .services import storage
from .services.checks import evaluate
from .services.excel import build_workbook
from .services.extraction import PAYMENT_APPS
from .services.processing import notify_verified, process_upload, recheck_utr, reread_payment
from .utils import (
    local_now,
    month_key,
    normalize_utr,
    parse_amount,
    parse_local_datetime,
    shift_month,
    valid_month_key,
)

bp = Blueprint("payments", __name__)

FILTERS = [
    ("all", "All"),
    (STATUS_NEEDS_REVIEW, "Needs review"),
    (STATUS_VERIFIED, "Verified"),
    (STATUS_DUPLICATE, "Duplicates"),
    (STATUS_REJECTED, "Rejected"),
]


def owned_payment(payment_id):
    """Look a payment up only among the logged-in account's payments."""
    return Payment.query.filter_by(id=payment_id, user_id=current_user.id).first_or_404()


def external_url(path):
    base = current_app.config["PUBLIC_BASE_URL"] or request.url_root.rstrip("/")
    return base + path


def selected_month():
    current = month_key(local_now(current_user.timezone))
    month = request.args.get("month", "")
    return (month if valid_month_key(month) else current), current


@bp.route("/")
@login_required
def dashboard():
    month, current = selected_month()
    status = request.args.get("status", "all")
    search = request.args.get("q", "").strip()[:80]

    base = Payment.query.filter_by(user_id=current_user.id, month_key=month)
    summary = {key: {"count": 0, "total": 0} for key in STATUS_LABELS}
    grouped = (
        base.with_entities(Payment.status, func.count(Payment.id), func.coalesce(func.sum(Payment.amount_paise), 0))
        .group_by(Payment.status)
        .all()
    )
    for key, count, total in grouped:
        summary[key] = {"count": count, "total": total}

    query = base
    if status in STATUS_LABELS:
        query = query.filter_by(status=status)
    if search:
        query = query.filter(
            Payment.customer_name.icontains(search, autoescape=True)
            | Payment.customer_phone.icontains(search, autoescape=True)
            | Payment.utr.icontains(search, autoescape=True)
        )
    payments = query.order_by(Payment.received_at.desc()).limit(500).all()
    setup = {
        "ai": bool(current_app.config.get("META_MODEL_API_KEY")),
        "upi": bool(current_user.upi_ids or current_user.payee_names),
        "whatsapp": current_user.whatsapp_ready,
        "bank": BankCredit.query.filter_by(user_id=current_user.id).first() is not None,
    }
    # With auto-approve on, bank SMS forwarding is recommended, not required.
    required = [setup["ai"], setup["upi"], setup["whatsapp"]] + ([] if current_user.auto_approve else [setup["bank"]])
    setup["steps_left"] = not all(required)
    return render_template(
        "payments/dashboard.html",
        payments=payments,
        setup=setup,
        summary=summary,
        month=month,
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1) if month < current else None,
        status=status,
        search=search,
        filters=FILTERS,
        total_count=sum(item["count"] for item in summary.values()),
    )


@bp.route("/payments/<int:payment_id>")
@login_required
def detail(payment_id):
    payment = owned_payment(payment_id)
    return render_template("payments/detail.html", payment=payment, apps=PAYMENT_APPS)


@bp.route("/payments/<int:payment_id>/screenshot")
@login_required
def screenshot(payment_id):
    payment = owned_payment(payment_id)
    if not payment.screenshot_path:
        abort(404)
    try:
        path = storage.absolute_path(payment.screenshot_path)
    except ValueError:
        abort(404)
    if not os.path.exists(path):
        abort(404)
    response = send_file(path, mimetype=payment.screenshot_mime, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@bp.route("/payments/<int:payment_id>/status", methods=["POST"])
@login_required
def set_status(payment_id):
    payment = owned_payment(payment_id)
    action = request.form.get("action")
    if action == "verify":
        payment.manual_status = True
        payment.status = STATUS_VERIFIED
        payment.verified_by = "manual"
        payment.status_reason = "Marked as verified by you."
        message = "Marked as verified."
    elif action == "reject":
        payment.manual_status = True
        payment.status = STATUS_REJECTED
        payment.verified_by = ""
        payment.status_reason = "Rejected by you."
        message = "Marked as rejected."
    elif action == "auto":
        payment.manual_status = False
        evaluate(payment, current_user)
        message = "Back to automatic checking."
    else:
        abort(400)
    db.session.commit()
    if payment.status == STATUS_VERIFIED:
        notify_verified([payment])
    flash(message, "success")
    return redirect(url_for(".detail", payment_id=payment.id))


@bp.route("/payments/<int:payment_id>/edit", methods=["POST"])
@login_required
def edit(payment_id):
    payment = owned_payment(payment_id)
    form = request.form
    before_status, old_utr = payment.status, payment.utr

    amount = parse_amount(form.get("amount")) if form.get("amount", "").strip() else None
    utr = normalize_utr(form.get("utr"))
    paid_at = parse_local_datetime(form.get("paid_at"))
    payee_upi = form.get("payee_upi", "").strip().lower()[:120]
    payee_name = form.get("payee_name", "").strip()[:120]
    changed = (amount, utr, paid_at, payee_upi, payee_name) != (
        payment.amount_paise, payment.utr, payment.paid_at, payment.payee_upi, payment.payee_name
    )
    payment.amount_paise = amount
    payment.utr = utr[:32] if utr else None
    payment.paid_at = paid_at
    payment.payee_upi = payee_upi
    payment.payee_name = payee_name
    app_name = form.get("payment_app", "")
    payment.payment_app = app_name if app_name in PAYMENT_APPS else payment.payment_app
    payment.customer_name = form.get("customer_name", "").strip()[:120]
    payment.customer_phone = form.get("customer_phone", "").strip()[:32]
    payment.notes = form.get("notes", "").strip()[:2000]
    if changed:
        payment.details_source = "manual"
    db.session.flush()
    evaluate(payment, current_user)
    newly_verified = recheck_utr(current_user, old_utr) if old_utr and old_utr != payment.utr else []
    db.session.commit()
    if payment.status == STATUS_VERIFIED and before_status != STATUS_VERIFIED:
        newly_verified.append(payment)
    notify_verified(newly_verified)
    flash("Details saved and checks re-run.", "success")
    return redirect(url_for(".detail", payment_id=payment.id))


@bp.route("/payments/<int:payment_id>/reread", methods=["POST"])
@login_required
def reread(payment_id):
    payment = owned_payment(payment_id)
    before_status = payment.status
    reread_payment(payment, current_user)
    if payment.status == STATUS_VERIFIED and before_status != STATUS_VERIFIED:
        notify_verified([payment])
    if payment.extraction_error:
        flash(payment.extraction_error, "error")
    else:
        flash("Screenshot read again.", "success")
    return redirect(url_for(".detail", payment_id=payment.id))


@bp.route("/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
def delete(payment_id):
    payment = owned_payment(payment_id)
    utr, path, month = payment.utr, payment.screenshot_path, payment.month_key
    db.session.delete(payment)
    db.session.flush()
    newly_verified = recheck_utr(current_user, utr)
    db.session.commit()
    storage.delete_screenshot(path)
    notify_verified(newly_verified)
    flash("Payment deleted.", "success")
    return redirect(url_for(".dashboard", month=month))


@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        file = request.files.get("screenshot")
        if not file or not file.filename:
            flash("Choose a screenshot first.", "error")
            return render_template("payments/upload.html"), 400
        try:
            payment = process_upload(
                current_user,
                file.read(),
                customer_name=request.form.get("customer_name", "").strip(),
                customer_phone=request.form.get("customer_phone", "").strip(),
            )
        except storage.InvalidImage as exc:
            flash(str(exc), "error")
            return render_template("payments/upload.html"), 400
        if payment.status == STATUS_VERIFIED:
            flash("Verified: the money is in your bank.", "success")
        elif payment.status == STATUS_DUPLICATE:
            flash("This payment was already sent before.", "error")
        else:
            flash(payment.status_reason or "Saved. It needs a quick review.", "info")
        return redirect(url_for(".detail", payment_id=payment.id))
    return render_template("payments/upload.html")


@bp.route("/export.xlsx")
@login_required
def export():
    query = Payment.query.filter_by(user_id=current_user.id)
    month = request.args.get("month", "")
    if valid_month_key(month):
        query = query.filter_by(month_key=month)
        filename = f"payments-{month}.xlsx"
    else:
        filename = "payments-all-months.xlsx"
    data = build_workbook(
        current_user,
        query.all(),
        lambda p: external_url(url_for(".detail", payment_id=p.id)),
    )
    return send_file(
        io.BytesIO(data),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@bp.route("/api/changes")
@login_required
def changes():
    """Lets open pages notice new WhatsApp payments or bank credits."""
    last_payment = db.session.query(func.max(Payment.updated_at)).filter(Payment.user_id == current_user.id).scalar()
    last_credit = db.session.query(func.max(BankCredit.created_at)).filter(BankCredit.user_id == current_user.id).scalar()
    count = db.session.query(func.count(Payment.id)).filter(Payment.user_id == current_user.id).scalar()
    return jsonify(stamp=f"{last_payment}|{last_credit}|{count}")
