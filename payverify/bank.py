"""Bank screen: money that really arrived (SMS, statement or typed in)."""

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .extensions import db
from .models import BankCredit, Payment
from .payments import selected_month
from .services.processing import add_bank_credit, notify_verified, recheck_utr
from .services.sms_parser import parse_bank_sms
from .services.statement import StatementError, parse_statement, read_rows
from .utils import is_valid_utr, normalize_utr, parse_amount, parse_local_datetime, shift_month

bp = Blueprint("bank", __name__, url_prefix="/bank")


@bp.route("/")
@login_required
def index():
    month, current = selected_month()
    credits = (
        BankCredit.query.filter_by(user_id=current_user.id, month_key=month)
        .order_by(BankCredit.credited_at.desc(), BankCredit.id.desc())
        .all()
    )
    ids = [credit.id for credit in credits]
    matched = {}
    if ids:
        for payment in Payment.query.filter(Payment.user_id == current_user.id, Payment.bank_credit_id.in_(ids)):
            matched.setdefault(payment.bank_credit_id, payment)
    return render_template(
        "bank/index.html",
        credits=credits,
        matched=matched,
        month=month,
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1) if month < current else None,
        total=sum(credit.amount_paise for credit in credits),
    )


def _finish(credit, created, newly_verified):
    db.session.commit()
    notify_verified(newly_verified)
    if not created:
        flash("That credit was already recorded.", "info")
    elif newly_verified:
        flash(f"Credit saved. {len(newly_verified)} payment(s) are now verified.", "success")
    else:
        flash("Credit saved.", "success")
    return redirect(url_for(".index", month=credit.month_key))


@bp.route("/sms", methods=["POST"])
@login_required
def paste_sms():
    text = request.form.get("sms_text", "").strip()[:2000]
    parsed = parse_bank_sms(text)
    if not parsed:
        flash("That doesn't look like a “money credited” SMS with an amount.", "error")
        return redirect(url_for(".index"))
    if not parsed.utr:
        flash("No 12-digit UPI reference found in that SMS, so it can't be matched to a screenshot.", "error")
        return redirect(url_for(".index"))
    result = add_bank_credit(
        current_user, parsed.amount_paise, parsed.utr, parsed.credited_at,
        source="sms", sender="Pasted", counterparty=parsed.counterparty, raw_text=text,
    )
    return _finish(*result)


@bp.route("/manual", methods=["POST"])
@login_required
def manual():
    amount = parse_amount(request.form.get("amount"))
    utr = normalize_utr(request.form.get("utr"))
    if not amount or not is_valid_utr(utr):
        flash("Enter the amount and the 12-digit UPI reference (UTR).", "error")
        return redirect(url_for(".index"))
    result = add_bank_credit(
        current_user, amount, utr, parse_local_datetime(request.form.get("credited_at")), source="manual",
    )
    return _finish(*result)


@bp.route("/import", methods=["POST"])
@login_required
def import_statement():
    file = request.files.get("statement")
    if not file or not file.filename:
        flash("Choose a statement file first.", "error")
        return redirect(url_for(".index"))
    try:
        credits, skipped = parse_statement(read_rows(file.filename, file.read()))
    except StatementError as exc:
        flash(str(exc), "error")
        return redirect(url_for(".index"))
    added = 0
    verified = []
    for item in credits:
        _credit, created, newly = add_bank_credit(
            current_user, item.amount_paise, item.utr, item.credited_at,
            source="statement", counterparty=item.counterparty,
        )
        added += created
        verified.extend(newly)
    db.session.commit()
    notify_verified(verified)
    parts = [f"Imported {added} new credit(s)"]
    if len(credits) > added:
        parts.append(f"{len(credits) - added} already recorded")
    if skipped:
        parts.append(f"{skipped} without a UPI reference skipped")
    message = ", ".join(parts) + "."
    if verified:
        message += f" {len(verified)} payment(s) are now verified."
    flash(message, "success")
    return redirect(url_for(".index"))


@bp.route("/<int:credit_id>/delete", methods=["POST"])
@login_required
def delete(credit_id):
    credit = BankCredit.query.filter_by(id=credit_id, user_id=current_user.id).first_or_404()
    utr, month = credit.utr, credit.month_key
    Payment.query.filter_by(user_id=current_user.id, bank_credit_id=credit.id).update({"bank_credit_id": None})
    db.session.delete(credit)
    db.session.flush()
    recheck_utr(current_user, utr)
    db.session.commit()
    flash("Credit deleted. Matching payments were re-checked.", "success")
    return redirect(url_for(".index", month=month))
