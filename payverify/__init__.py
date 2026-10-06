"""PayVerify: check UPI payment screenshots sent on WhatsApp against the money
that actually reached your bank. One web app for phones and computers."""

import hashlib
import os
import sqlite3

from dotenv import load_dotenv
from flask import Flask, flash, jsonify, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import default_config, instance_dir
from .extensions import csrf, db, login_manager
from .icons import icon
from .utils import format_inr, mask_utr, month_label, paise_to_input, to_local

APP_NAME = "PayVerify"


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_path=str(instance_dir()))
    app.config.from_mapping(default_config())
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    if app.config["BEHIND_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    from . import auth, bank, payments, settings, webhooks

    app.register_blueprint(auth.bp)
    app.register_blueprint(payments.bp)
    app.register_blueprint(bank.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(webhooks.bp)
    csrf.exempt(webhooks.bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()

    _register_template_helpers(app)
    _register_pwa_routes(app)
    _register_security(app)
    _register_errors(app)
    _register_cli(app)
    return app


def _sql_literal(value):
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _add_missing_columns():
    """create_all() creates missing tables but never changes existing ones.

    When an update adds a column, add it to the existing database here, so
    data from earlier versions keeps working. Additive changes only.
    """
    inspector = inspect(db.engine)
    with db.engine.begin() as connection:
        for table in db.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(dialect=db.engine.dialect)}'
                if column.default is not None and column.default.is_scalar:
                    ddl += f" DEFAULT {_sql_literal(column.default.arg)}"
                    if not column.nullable:
                        ddl += " NOT NULL"
                connection.execute(text(ddl))


def _register_template_helpers(app):
    asset_hashes = {}

    def asset(path):
        """Static URL with a content hash so phones pick up new versions."""
        full = os.path.join(app.static_folder, path)
        try:
            stamp = os.path.getmtime(full)
        except OSError:
            return url_for("static", filename=path)
        cached = asset_hashes.get(path)
        if not cached or cached[0] != stamp:
            with open(full, "rb") as fh:
                cached = (stamp, hashlib.sha256(fh.read()).hexdigest()[:10])
            asset_hashes[path] = cached
        return url_for("static", filename=path, v=cached[1])

    def localtime(value, fmt="%d %b %Y, %I:%M %p"):
        if value is None:
            return "—"
        tz = current_user.timezone if current_user.is_authenticated else "Asia/Kolkata"
        return _strip_zero(to_local(value, tz).strftime(fmt))

    def localdt(value, fmt="%d %b %Y, %I:%M %p"):
        """For times already stored in local time (receipt and bank times)."""
        return _strip_zero(value.strftime(fmt)) if value else "—"

    def input_dt(value):
        return value.strftime("%Y-%m-%dT%H:%M") if value else ""

    app.jinja_env.filters.update(
        inr=format_inr, localtime=localtime, localdt=localdt, input_dt=input_dt,
        month_label=month_label, mask_utr=mask_utr, rupees_input=paise_to_input,
    )
    app.jinja_env.globals.update(asset=asset, app_name=APP_NAME, icon=icon)


def _strip_zero(text):
    """"05 Oct 2026, 09:30 AM" -> "5 Oct 2026, 9:30 AM"."""
    text = text[1:] if text.startswith("0") else text
    return text.replace(", 0", ", ")


def _register_pwa_routes(app):
    @app.route("/manifest.webmanifest")
    def manifest():
        response = jsonify({
            "name": f"{APP_NAME} – UPI payment checker",
            "short_name": APP_NAME,
            "description": "Check UPI payment screenshots against the money in your bank.",
            "id": "/",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#f5f6fa",
            "theme_color": "#4338ca",
            "icons": [
                {"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {"src": url_for("static", filename="icons/icon-maskable-512.png"), "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
        })
        response.mimetype = "application/manifest+json"
        return response

    @app.route("/sw.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "js/sw.js", mimetype="application/javascript", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/offline")
    def offline():
        return render_template("offline.html")

    @app.route("/healthz")
    def healthz():
        return {"ok": True}


def _register_security(app):
    csp = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
        "manifest-src 'self'; worker-src 'self'"
    )

    @app.after_request
    def security_headers(response):
        headers = response.headers
        headers.setdefault("Content-Security-Policy", csp)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if app.config["SESSION_COOKIE_SECURE"]:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Never let a browser or shared device keep a copy of account pages.
        if current_user.is_authenticated and request.endpoint != "static":
            headers["Cache-Control"] = "no-store"
        return response


def _register_errors(app):
    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", code=404, message="That page doesn't exist."), 404

    @app.errorhandler(413)
    def too_large(_error):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_template("error.html", code=413, message=f"That file is too large (limit {limit} MB)."), 413

    @app.errorhandler(CSRFError)
    def csrf_failed(_error):
        flash("Your session timed out. Please try again.", "error")
        return redirect(request.referrer if _same_site(request.referrer) else url_for("payments.dashboard"))

    @app.errorhandler(500)
    def server_error(_error):
        return render_template("error.html", code=500, message="Something went wrong on our side."), 500


def _same_site(url):
    return bool(url) and url.startswith(request.host_url)


def _register_cli(app):
    from .cli import register

    register(app)
