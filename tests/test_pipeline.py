"""Screenshot -> checks -> bank match -> status."""

import io

from conftest import make_owner, png_bytes, receipt, upload, user_by_email
from payverify.models import Payment

SMS = "Rs.500.00 credited to a/c XX1234 on 05-10-26 by a/c linked to VPA ravi@okaxis (UPI Ref No {utr})."


def payments(app, email):
    with app.app_context():
        user = user_by_email(email)
        return [
            {"id": p.id, "status": p.status, "reason": p.status_reason, "checks": {c["key"]: c["state"] for c in p.checks}}
            for p in Payment.query.filter_by(user_id=user.id).order_by(Payment.id)
        ]


def sms_url(app, email):
    with app.app_context():
        return f"/webhooks/sms/{user_by_email(email).sms_token}"


def test_screenshot_waits_for_bank_then_verifies_from_sms(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    response = upload(client)
    assert response.status_code == 302
    [payment] = payments(app, "owner@example.com")
    assert payment["status"] == "needs_review"
    assert payment["checks"]["bank"] == "wait"
    assert payment["checks"]["payee"] == "pass"
    assert payment["checks"]["recent"] == "pass"

    result = client.post(sms_url(app, "owner@example.com"), json={"from": "AX-HDFCBK", "text": SMS.format(utr="627812345678")})
    assert result.get_json()["verified_payments"] == 1
    assert payments(app, "owner@example.com")[0]["status"] == "verified"


def test_bank_sms_first_then_screenshot_verifies_immediately(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    client.post("/bank/sms", data={"sms_text": SMS.format(utr="627812345678")})
    upload(client)
    assert payments(app, "owner@example.com")[0]["status"] == "verified"


def test_resent_screenshot_is_a_duplicate(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    upload(client)
    upload(client)
    first, second = payments(app, "owner@example.com")
    assert first["status"] == "needs_review"
    assert second["status"] == "duplicate"


def test_identical_image_is_a_duplicate_even_if_unreadable(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.error = "Claude is busy"
    image = png_bytes(color=(1, 2, 3))
    upload(client, image=image)
    upload(client, image=image)
    assert [p["status"] for p in payments(app, "owner@example.com")] == ["needs_review", "duplicate"]


def test_edited_amount_is_caught_by_bank_match(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    client.post("/settings/", data={"name": "Owner", "upi_ids": "shop@okhdfcbank", "accepted_amounts": "500",
                                     "max_age_days": "3", "timezone": "Asia/Kolkata"})
    client.post("/bank/sms", data={"sms_text": SMS.format(utr="627812345678")})  # bank got ₹500
    fake_ai.data = receipt(amount=5000)  # screenshot claims ₹5,000
    upload(client)
    [payment] = payments(app, "owner@example.com")
    assert payment["status"] == "needs_review"
    assert payment["checks"]["amount"] == "fail"
    # The bank mismatch is the headline, not the "unusual amount" check.
    assert payment["reason"] == "Your bank received ₹500, but the screenshot shows ₹5,000."


def test_wrong_payee_old_date_and_failed_status_need_review(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.data = receipt(utr="600000000001", payee="someone@ybl")
    upload(client)
    fake_ai.data = receipt(utr="600000000002", minutes_ago=60 * 24 * 10)
    upload(client)
    fake_ai.data = receipt(utr="600000000003", status="failed")
    upload(client)
    wrong_payee, old, failed = payments(app, "owner@example.com")
    assert wrong_payee["checks"]["payee"] == "fail" and wrong_payee["status"] == "needs_review"
    assert old["checks"]["recent"] == "fail" and old["status"] == "needs_review"
    assert failed["checks"]["txn_status"] == "fail" and failed["status"] == "needs_review"


def test_masked_upi_id_matches(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.data = receipt(payee="sho****@okhdfcbank")
    upload(client)
    assert payments(app, "owner@example.com")[0]["checks"]["payee"] == "pass"


def test_not_a_payment_and_ai_errors_need_review(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.data = {**receipt(), "is_payment_screenshot": False}
    upload(client)
    fake_ai.data = receipt()
    fake_ai.error = "Claude API error (529). Try “Read again” later."
    upload(client)
    not_payment, errored = payments(app, "owner@example.com")
    assert not_payment["checks"]["details"] == "fail"
    assert "Claude API error" in errored["reason"]


def test_read_again_after_error(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.error = "Claude is busy"
    upload(client)
    fake_ai.error = None
    [payment] = payments(app, "owner@example.com")
    client.post(f"/payments/{payment['id']}/reread")
    assert payments(app, "owner@example.com")[0]["checks"]["details"] == "pass"
    assert fake_ai.calls == 2


def test_invalid_image_is_rejected(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    response = upload(client, image=b"%PDF-1.4 not an image")
    assert response.status_code == 400
    assert payments(app, "owner@example.com") == []


def test_manual_decisions_and_undo(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    upload(client)
    pid = payments(app, "owner@example.com")[0]["id"]
    client.post(f"/payments/{pid}/status", data={"action": "verify"})
    assert payments(app, "owner@example.com")[0]["status"] == "verified"
    client.post(f"/payments/{pid}/status", data={"action": "reject"})
    assert payments(app, "owner@example.com")[0]["status"] == "rejected"
    client.post(f"/payments/{pid}/status", data={"action": "auto"})
    assert payments(app, "owner@example.com")[0]["status"] == "needs_review"


def test_manual_rejection_survives_bank_credit(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    upload(client)
    pid = payments(app, "owner@example.com")[0]["id"]
    client.post(f"/payments/{pid}/status", data={"action": "reject"})
    client.post("/bank/manual", data={"amount": "500", "utr": "627812345678"})
    assert payments(app, "owner@example.com")[0]["status"] == "rejected"


def test_correcting_details_rechecks(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    fake_ai.error = "Claude is busy"
    upload(client)
    client.post("/bank/manual", data={"amount": "750", "utr": "600000000009"})
    pid = payments(app, "owner@example.com")[0]["id"]
    client.post(f"/payments/{pid}/edit", data={
        "amount": "750", "utr": "6000 0000 0009", "paid_at": "", "payee_upi": "shop@okhdfcbank",
        "payee_name": "", "payment_app": "PhonePe", "customer_name": "Ravi", "customer_phone": "", "notes": "",
    })
    [payment] = payments(app, "owner@example.com")
    assert payment["checks"]["details"] == "pass"
    assert payment["checks"]["bank"] == "pass"
    assert payment["status"] == "needs_review"  # the payment date is still missing


def test_deleting_original_promotes_duplicate(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    upload(client)
    upload(client)
    first, second = payments(app, "owner@example.com")
    client.post(f"/payments/{first['id']}/delete")
    [remaining] = payments(app, "owner@example.com")
    assert remaining["id"] == second["id"]
    assert remaining["status"] == "needs_review"


def test_statement_import_verifies(app, fake_ai):
    client = make_owner(app, "owner@example.com")
    upload(client)
    csv_text = (
        "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
        "05/10/26,UPI-RAVI-ravi@okaxis-UTIB0000123-627812345678-RENT,0000627812345678,05/10/26,,500.00,10500.00\n"
        "05/10/26,ATM WDL,000000000000123,05/10/26,1000.00,,9500.00\n"
    )
    response = client.post(
        "/bank/import",
        data={"statement": (io.BytesIO(csv_text.encode()), "statement.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Imported 1 new credit" in response.get_data(as_text=True)
    assert payments(app, "owner@example.com")[0]["status"] == "verified"


def test_settings_change_rechecks_waiting_payments(app, fake_ai):
    client = make_owner(app, "owner@example.com", upi="")
    client.post("/bank/manual", data={"amount": "500", "utr": "627812345678"})
    fake_ai.data = receipt(payee="other@okicici")
    upload(client)
    assert payments(app, "owner@example.com")[0]["status"] == "verified"  # no UPI IDs set: payee check skipped

    client.post("/settings/", data={"name": "Owner", "upi_ids": "shop@okhdfcbank", "max_age_days": "3",
                                     "timezone": "Asia/Kolkata", "accepted_amounts": "500, 1000"})
    with app.app_context():
        user = user_by_email("owner@example.com")
        assert user.accepted_amount_list == [50000, 100000]
    # Verified payments are not downgraded by a settings change.
    assert payments(app, "owner@example.com")[0]["status"] == "verified"
