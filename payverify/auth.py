"""Sign up, log in and log out. Each account only ever sees its own data."""

import hmac
import re
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager
from .models import User

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
# Compared against when the email doesn't exist, so both cases take equally long.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


class AttemptLimiter:
    """Slow down password guessing: N failures per key within a time window."""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key, now):
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def blocked(self, key):
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.limit

    def hit(self, key):
        with self._lock:
            now = time.monotonic()
            if len(self._hits) > 10_000:  # forget stale keys so memory stays bounded
                for stale in [k for k, hits in self._hits.items() if not hits or now - hits[-1] > self.window]:
                    del self._hits[stale]
            self._trim(key, now).append(now)

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


login_failures = AttemptLimiter(limit=5, window_seconds=15 * 60)
ip_failures = AttemptLimiter(limit=30, window_seconds=15 * 60)
signups = AttemptLimiter(limit=10, window_seconds=60 * 60)


@login_manager.user_loader
def load_user(user_id):
    raw_id, _, token = (user_id or "").partition(":")
    if not raw_id.isdigit() or not token:
        return None
    user = db.session.get(User, int(raw_id))
    if user is None or not hmac.compare_digest(user.session_token, token):
        return None
    return user


def safe_next(target):
    """Only redirect to paths on this site after login."""
    if not target:
        return None
    parts = urlsplit(target)
    # Browsers treat "/\\evil.com" like "//evil.com", so reject backslashes too.
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def _client_ip():
    return request.remote_addr or "unknown"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("payments.dashboard"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = f"{_client_ip()}|{email}"
        if login_failures.blocked(key) or ip_failures.blocked(_client_ip()):
            flash("Too many attempts. Please wait 15 minutes and try again.", "error")
            return render_template("auth/login.html", email=email), 429
        user = User.query.filter_by(email=email).first()
        if user is None:
            check_password_hash(_DUMMY_HASH, password)
        elif check_password_hash(user.password_hash, password):
            login_failures.reset(key)
            session.clear()
            login_user(user, remember=bool(request.form.get("remember")))
            return redirect(safe_next(request.args.get("next")) or url_for("payments.dashboard"))
        login_failures.hit(key)
        ip_failures.hit(_client_ip())
        flash("That email and password don't match.", "error")
    return render_template("auth/login.html", email=email)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("payments.dashboard"))
    config = current_app.config
    if not config["ALLOW_SIGNUPS"]:
        return render_template("auth/signup.html", closed=True, form={})
    form = request.form
    if request.method == "POST":
        errors = []
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        if signups.blocked(_client_ip()):
            errors.append("Too many sign-ups from this network. Please try again later.")
        if config["SIGNUP_CODE"] and not hmac.compare_digest(
            form.get("signup_code", "").strip().encode(), config["SIGNUP_CODE"].encode()
        ):
            errors.append("The invite code is wrong.")
        if not form.get("name", "").strip():
            errors.append("Enter your name.")
        if not EMAIL_RE.match(email):
            errors.append("Enter a valid email address.")
        if len(password) < MIN_PASSWORD:
            errors.append(f"Use a password with at least {MIN_PASSWORD} characters.")
        if password != form.get("confirm", ""):
            errors.append("The two passwords don't match.")
        if not errors and User.query.filter_by(email=email).first():
            errors.append("An account with this email already exists. Log in instead.")
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("auth/signup.html", form=form), 400
        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            name=form.get("name", "").strip()[:120],
            business_name=form.get("business_name", "").strip()[:120],
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account with this email already exists. Log in instead.", "error")
            return render_template("auth/signup.html", form=form), 400
        signups.hit(_client_ip())
        session.clear()
        login_user(user, remember=True)
        flash("Welcome! Add your UPI IDs so payments can be checked.", "success")
        return redirect(url_for("settings.general"))
    return render_template("auth/signup.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    # Clear first: logout_user() then marks the "remember me" cookie for deletion.
    session.clear()
    logout_user()
    response = redirect(url_for("auth.login"))
    # Remove anything this browser cached for the signed-out account.
    response.headers["Clear-Site-Data"] = '"cache"'
    return response
