import hashlib
import hmac
import json

import pytest

from conftest import make_owner, png_bytes, user_by_email
from payverify.extensions import db
from payverify.models import Payment

SECRETS = {"alice@example.com": "alice-app-secret", "bob@example.com": "bob-app-secret"}


def connect(app, email, phone_number_id="1065"):
    with app.app_context():
        user = user_by_email(email)
        user.wa_phone_number_id = phone_number_id
        user.wa_access_token = "token-abc"
        user.wa_app_secret = SECRETS[email]
        db.session.commit()
        return user.webhook_token, user.wa_verify_token


def message_payload(message_id="wamid.1", phone_number_id="1065", sender="919800000001",
                    kind="image", mime="image/jpeg"):
    media = {"id": "media-1", "mime_type": mime, "caption": "Room 4 rent"}
    return {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Ravi"}, "wa_id": sender}],
        "messages": [{"from": sender, "id": message_id, "timestamp": "1700000000", "type": kind, kind: media}],
    }}]}]}


def post_signed(client, token, body, secret):
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(f"/webhooks/whatsapp/{token}", data=raw,
                       headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"})


@pytest.fixture
def fake_whatsapp(monkeypatch):
    sent = []

    def download(user, media_id):
        return png_bytes(color=(9, 9, 9)), "image/png"

    def send(user, to, body, reply_to=None):
        sent.append({"account": user.email, "to": to, "body": body, "reply_to": reply_to})

    monkeypatch.setattr("payverify.services.whatsapp.download_media", download)
    monkeypatch.setattr("payverify.services.whatsapp.send_text", send)
    return sent


def account_payments(app, email):
    with app.app_context():
        return Payment.query.filter_by(user_id=user_by_email(email).id).order_by(Payment.id).all()


def test_meta_verification_handshake(app):
    make_owner(app, "alice@example.com")
    token, verify = connect(app, "alice@example.com")
    client = app.test_client()
    ok = client.get(f"/webhooks/whatsapp/{token}?hub.mode=subscribe&hub.verify_token={verify}&hub.challenge=42")
    assert ok.status_code == 200 and ok.get_data(as_text=True) == "42"
    assert client.get(f"/webhooks/whatsapp/{token}?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1").status_code == 403
    assert client.get("/webhooks/whatsapp/not-a-real-token?hub.mode=subscribe").status_code == 404


def test_unsigned_or_wrongly_signed_messages_are_rejected(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    client = app.test_client()
    unsigned = client.post(f"/webhooks/whatsapp/{token}", json=message_payload())
    assert unsigned.status_code == 403
    assert post_signed(client, token, message_payload(), "guessed-secret").status_code == 403
    assert account_payments(app, "alice@example.com") == []


def test_screenshot_creates_payment_and_replies_once(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    client = app.test_client()

    response = post_signed(client, token, message_payload(), SECRETS["alice@example.com"])
    assert response.get_json() == {"ok": True, "received": 1}
    # Meta sometimes delivers the same webhook twice.
    assert post_signed(client, token, message_payload(), SECRETS["alice@example.com"]).get_json()["received"] == 0

    [payment] = account_payments(app, "alice@example.com")
    assert (payment.customer_name, payment.customer_phone, payment.customer_note) == ("Ravi", "919800000001", "Room 4 rent")
    assert payment.status == "needs_review"
    assert payment.screenshot_path
    assert len(fake_whatsapp) == 1
    assert fake_whatsapp[0]["to"] == "919800000001"
    assert fake_whatsapp[0]["reply_to"] == "wamid.1"
    assert "verifying" in fake_whatsapp[0]["body"]


def test_customer_gets_confirmation_when_money_arrives(app, fake_ai, fake_whatsapp):
    owner = make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    post_signed(app.test_client(), token, message_payload(), SECRETS["alice@example.com"])
    with app.app_context():
        sms_token = user_by_email("alice@example.com").sms_token
    owner.post(f"/webhooks/sms/{sms_token}", json={
        "from": "JD-HDFCBK", "text": "Rs.500.00 credited to a/c XX1234 (UPI Ref No 627812345678).",
    })
    [payment] = account_payments(app, "alice@example.com")
    assert payment.status == "verified"
    assert [m["body"].split(" ")[0] for m in fake_whatsapp] == ["Thanks", "Payment"]
    assert "₹500" in fake_whatsapp[1]["body"]


def test_messages_go_only_to_the_account_that_owns_the_url(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    make_owner(app, "bob@example.com")
    alice_token, _ = connect(app, "alice@example.com", phone_number_id="111")
    bob_token, _ = connect(app, "bob@example.com", phone_number_id="222")
    client = app.test_client()

    post_signed(client, alice_token, message_payload(phone_number_id="111"), SECRETS["alice@example.com"])
    # Alice's secret can't be used to push messages into Bob's account.
    assert post_signed(client, bob_token, message_payload(phone_number_id="222"), SECRETS["alice@example.com"]).status_code == 403
    # A message for a different business number is ignored.
    other = post_signed(client, bob_token, message_payload(phone_number_id="111", message_id="wamid.2"), SECRETS["bob@example.com"])
    assert other.get_json()["received"] == 0

    assert len(account_payments(app, "alice@example.com")) == 1
    assert account_payments(app, "bob@example.com") == []
    assert {m["account"] for m in fake_whatsapp} == {"alice@example.com"}


def test_image_documents_accepted_other_documents_ignored(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    client = app.test_client()
    secret = SECRETS["alice@example.com"]
    post_signed(client, token, message_payload(message_id="wamid.doc1", kind="document", mime="image/png"), secret)
    post_signed(client, token, message_payload(message_id="wamid.doc2", kind="document", mime="application/pdf"), secret)
    assert len(account_payments(app, "alice@example.com")) == 1


def test_no_reply_when_replies_are_off(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    with app.app_context():
        user_by_email("alice@example.com").reply_enabled = False
        db.session.commit()
    post_signed(app.test_client(), token, message_payload(), SECRETS["alice@example.com"])
    assert len(account_payments(app, "alice@example.com")) == 1
    assert fake_whatsapp == []


def test_sms_from_phone_numbers_is_ignored(app, fake_ai):
    owner = make_owner(app, "alice@example.com")
    with app.app_context():
        sms_token = user_by_email("alice@example.com").sms_token
    fake = owner.post(f"/webhooks/sms/{sms_token}", json={
        "from": "+91 98765 43210", "text": "Rs.500.00 credited to a/c XX1234 (UPI Ref No 627812345678).",
    })
    assert fake.get_json()["result"] == "ignored_sender"
    otp = owner.post(f"/webhooks/sms/{sms_token}", json={"from": "AX-HDFCBK", "text": "Your OTP is 123456. Rs 500 credited"})
    assert otp.get_json()["result"] == "not_a_credit"
    assert owner.post("/webhooks/sms/wrong-token", json={"from": "AX-HDFCBK", "text": "x"}).status_code == 404


def test_trusted_sender_list(app):
    owner = make_owner(app, "alice@example.com")
    with app.app_context():
        user = user_by_email("alice@example.com")
        user.sms_trusted_senders = "HDFCBK"
        db.session.commit()
        sms_token = user.sms_token
    text = "Rs.500.00 credited to a/c XX1234 (UPI Ref No 627812345678)."
    assert owner.post(f"/webhooks/sms/{sms_token}", json={"from": "VM-SBIUPI", "text": text}).get_json()["result"] == "ignored_sender"
    assert owner.post(f"/webhooks/sms/{sms_token}", json={"from": "VM-HDFCBK", "text": text}).get_json()["result"] == "recorded"
    assert owner.post(f"/webhooks/sms/{sms_token}", json={"from": "VM-HDFCBK", "text": text}).get_json()["result"] == "already_recorded"


def test_auto_mode_thanks_the_customer_immediately(app, fake_ai, fake_whatsapp):
    make_owner(app, "alice@example.com")
    token, _ = connect(app, "alice@example.com")
    with app.app_context():
        user_by_email("alice@example.com").approval_mode = "automatic"
        db.session.commit()
    post_signed(app.test_client(), token, message_payload(), SECRETS["alice@example.com"])
    [payment] = account_payments(app, "alice@example.com")
    assert payment.status == "verified" and payment.verified_by == "auto"
    assert len(fake_whatsapp) == 1
    assert fake_whatsapp[0]["body"].startswith("Payment of ₹500 received")
