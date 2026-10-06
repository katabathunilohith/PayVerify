import io
import itertools
import os
from datetime import timedelta

import pytest
from PIL import Image

os.environ["SECRET_KEY"] = "test-secret-key"

from payverify import auth, create_app  # noqa: E402
from payverify.extensions import db  # noqa: E402
from payverify.models import User  # noqa: E402
from payverify.services.extraction import Extraction  # noqa: E402
from payverify.utils import local_now  # noqa: E402

PASSWORD = "correct-horse-1"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    application = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "WTF_CSRF_ENABLED": False,
        "PROCESS_INLINE": True,
        "SESSION_COOKIE_SECURE": False,
        "REMEMBER_COOKIE_SECURE": False,
        "ALLOW_SIGNUPS": True,
        "SIGNUP_CODE": "",
    })
    yield application
    with application.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture(autouse=True)
def reset_limiters():
    for limiter in (auth.login_failures, auth.ip_failures, auth.signups):
        limiter._hits.clear()


def png_bytes(color=(210, 210, 210), size=(40, 80)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def receipt(amount=500, utr="627812345678", payee="shop@okhdfcbank", minutes_ago=5, status="success"):
    """What the AI would read from a typical Google Pay screenshot."""
    paid = local_now("Asia/Kolkata") - timedelta(minutes=minutes_ago)
    return {
        "is_payment_screenshot": True,
        "transaction_status": status,
        "amount": amount,
        "utr": utr,
        "app_transaction_id": None,
        "paid_at": paid.strftime("%Y-%m-%dT%H:%M"),
        "payee_name": "Shop",
        "payee_upi_id": payee,
        "payer_name": "Ravi",
        "payer_upi_id": "ravi@okaxis",
        "payment_app": "Google Pay",
        "warnings": [],
    }


class FakeAI:
    """Stands in for Claude. Set .data (or .error) to control what it 'reads'."""

    def __init__(self):
        self.data = receipt()
        self.error = None
        self.calls = 0

    def __call__(self, image_bytes, media_type, today, tz_name):
        self.calls += 1
        if self.error:
            return Extraction(False, self.error)
        return Extraction(True, data=dict(self.data))


@pytest.fixture
def fake_ai(monkeypatch):
    stub = FakeAI()
    monkeypatch.setattr("payverify.services.processing.extract_payment_details", stub)
    return stub


def signup(client, email, password=PASSWORD, name="Owner"):
    return client.post("/signup", data={"name": name, "email": email, "password": password, "confirm": password})


def make_owner(app, email, upi="shop@okhdfcbank"):
    """A logged-in client for a new account with its UPI ID configured."""
    client = app.test_client()
    assert signup(client, email).status_code == 302
    with app.app_context():
        user = User.query.filter_by(email=email).one()
        user.upi_ids = upi
        db.session.commit()
    return client


_shades = itertools.count(1)


def unique_png():
    """Each call gives a different image, so the duplicate-image check doesn't fire."""
    shade = next(_shades)
    return png_bytes(color=(shade % 256, (shade // 256) % 256, 120))


def upload(client, name="Ravi", phone="919800000001", image=None):
    return client.post(
        "/upload",
        data={"screenshot": (io.BytesIO(image or unique_png()), "shot.png"), "customer_name": name, "customer_phone": phone},
        content_type="multipart/form-data",
    )


def user_by_email(email):
    return User.query.filter_by(email=email).one()
