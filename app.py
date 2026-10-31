"""
Time Vault API: application setup.

Creates the Flask app (config, CORS, security headers, background-job hook,
startup migrations) and registers the route blueprints in routes/.
Entry point for WSGI (PythonAnywhere imports `app` from here).
"""
from dotenv import load_dotenv
load_dotenv()

import os
import re
from datetime import datetime, timedelta

from flask import Flask, request, jsonify
from flask_cors import CORS

from extensions import db

app = Flask(__name__)

CORS(app, supports_credentials=True, origins=[
    "http://localhost:3000",
    "https://time-vault-imhg.vercel.app",
    re.compile(r"https://time-vault-imhg.*\.vercel\.app"),
], allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise ValueError("SECRET_KEY environment variable is not set")
app.config['SECRET_KEY'] = _secret_key

# Werkzeug rejects larger request bodies (413) before they reach app code:
# 50 MB video limit + headroom for multipart framing.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 + 64 * 1024
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///time_capsule.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# ── Background jobs without a scheduler ──────────────────────────────────────
# PythonAnywhere's free plan has no scheduled tasks, so the API runs the
# unlock job itself: at most once per JOB_INTERVAL per worker, triggered by
# incoming requests. run_unlock_job() claims each post with an atomic
# conditional UPDATE, so concurrent workers never double-unlock or
# double-notify. The daily digest runs after 20:00 UTC and is deduplicated
# per user/vault/day by DigestDelivery rows. Disable with RUN_JOBS_ON_REQUEST=0
# if a real scheduler (cron / PythonAnywhere task) is set up instead.
JOB_INTERVAL = timedelta(seconds=60)
_last_job_run = None

def run_due_jobs():
    global _last_job_run
    now = datetime.utcnow()
    if _last_job_run and now - _last_job_run < JOB_INTERVAL:
        return
    _last_job_run = now
    try:
        import unlock_job  # imported lazily: unlock_job imports this module
        unlock_job.run_unlock_job()
        if now.hour >= 20:
            unlock_job.run_daily_digest()
    except Exception as exc:  # never let a job failure break a user request
        db.session.rollback()
        app.logger.error(f'Background job failed: {exc}')

@app.before_request
def _jobs_hook():
    if request.method == 'OPTIONS' or os.environ.get('RUN_JOBS_ON_REQUEST', '1') == '0':
        return
    run_due_jobs()


@app.after_request
def set_security_headers(response):
    """This service only returns JSON, so lock everything else down."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
    response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


@app.errorhandler(413)
def request_too_large(e):
    """Return a JSON 413 instead of Flask's default HTML page."""
    return jsonify({'error': 'Request too large. Maximum upload size is 50 MB for video and 5 MB for images.'}), 413


# ── Routes ───────────────────────────────────────────────────────────────────
from routes.auth import bp as auth_bp                    # noqa: E402
from routes.vaults import bp as vaults_bp                # noqa: E402
from routes.posts import bp as posts_bp                  # noqa: E402
from routes.notifications import bp as notifications_bp  # noqa: E402

for _bp in (auth_bp, vaults_bp, posts_bp, notifications_bp):
    app.register_blueprint(_bp)

# Re-exported for unlock_job.py, scripts/ and tests, which import from `app`.
from models import *  # noqa: E402,F401,F403
from crypto import ENCRYPTION_KEY, encrypt_content, decrypt_content  # noqa: E402,F401
from security import (  # noqa: E402,F401
    issue_token, user_from_token, token_required, _login_failures,
    LOGIN_MAX_FAILURES, LOGIN_WINDOW,
)
from helpers import (  # noqa: E402,F401
    create_notification, notify_vault_members, utc_iso, generate_invite_code,
    generate_invite_token, serialize_post, serialize_vault, LEGACY_NOTIFICATION_TYPES,
)
from routes.auth import _reset_requests  # noqa: E402,F401


# ── Startup: create tables + safe additive column migrations ────────────────
#
# db.create_all() creates tables that do not yet exist but does NOT add new
# columns to existing tables.  The _run_migrations() function below handles
# the additive ALTER TABLE statements that are needed when upgrading an
# existing database (e.g. from pre-Pass-18 deployments).
#
# Each ALTER TABLE is wrapped in a try/except so that:
#   • the migration is idempotent — safe to run on every app restart;
#   • if the column already exists (OperationalError "duplicate column")
#     the error is silently swallowed;
#   • user data is never touched.
#
def _run_migrations(conn):
    """Execute safe additive ALTER TABLE statements.

    Works with both raw sqlite3.Connection objects (used in tests)
    and SQLAlchemy Connection objects (used at application startup).

    Only the "duplicate column" condition is silently ignored — every
    other exception (locked database, disk full, malformed SQL, permission
    error) is logged and re-raised so that a broken migration is never
    hidden from operators.
    """
    import sqlite3 as _sqlite3
    try:
        from sqlalchemy import text as _sa_text
    except ImportError:
        _sa_text = None

    _is_sa = not isinstance(conn, _sqlite3.Connection)

    _NEW_VAULT_COLUMNS = [
        # (column_name, DDL type)
        ("invite_token",          "VARCHAR(64)"),
        ("description",          "VARCHAR(160)"),
        ("accent_color",         "VARCHAR(20)"),
        ("cover_url",            "VARCHAR(500)"),
        # Pass 21: child vault fields
        ("vault_type",           "VARCHAR(20) NOT NULL DEFAULT 'normal'"),
        ("child_email",          "VARCHAR(120)"),
        ("claimed_at",           "DATETIME"),
        ("claimed_by_user_id",   "INTEGER"),
    ]
    for col, col_type in _NEW_VAULT_COLUMNS:
        ddl = f"ALTER TABLE vault ADD COLUMN {col} {col_type}"
        try:
            if _is_sa and _sa_text:
                conn.execute(_sa_text(ddl))
                conn.commit()
            else:
                conn.execute(ddl)
                conn.commit()
        except Exception as exc:
            # SQLite raises OperationalError with "duplicate column name"
            # when the column already exists.  That specific error is the
            # expected idempotency condition — ignore it and move on.
            # Every other error (locked DB, disk full, bad SQL, etc.)
            # must be surfaced so operators can act on it.
            msg = str(exc).lower()
            if "duplicate column" in msg:
                try:
                    conn.rollback()
                except Exception:
                    pass  # rollback failure after a no-op is safe to ignore
            else:
                app.logger.error(
                    "Migration failed for column %r: %s", col, exc
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    for _col, _ddl in [("is_archived", "BOOLEAN NOT NULL DEFAULT 0")]:
        _stmt = f"ALTER TABLE post ADD COLUMN {_col} {_ddl}"
        try:
            if _is_sa and _sa_text: conn.execute(_sa_text(_stmt)); conn.commit()
            else: conn.execute(_stmt); conn.commit()
        except Exception as _exc:
            if "duplicate column" in str(_exc).lower():
                try: conn.rollback()
                except Exception: pass
            else:
                app.logger.error("Post migration %r: %s", _col, _exc)
                try: conn.rollback()
                except Exception: pass
                raise

    # Pass 26.1: vault_id on notification (nullable, idempotent)
    _notif_stmt = "ALTER TABLE notification ADD COLUMN vault_id INTEGER"
    try:
        if _is_sa and _sa_text: conn.execute(_sa_text(_notif_stmt)); conn.commit()
        else: conn.execute(_notif_stmt); conn.commit()
    except Exception as _ne:
        if "duplicate column" in str(_ne).lower():
            try: conn.rollback()
            except Exception: pass
        else:
            app.logger.error("Notification migration vault_id: %s", _ne)
            try: conn.rollback()
            except Exception: pass
            raise


with app.app_context():
    db.create_all()          # create tables that do not exist yet
    with db.engine.connect() as _conn:
        _run_migrations(_conn)  # add new columns to existing tables


if __name__ == '__main__':
    # When run as a script this module is '__main__'; register it as 'app' too
    # so unlock_job's `from app import ...` reuses it instead of re-importing.
    import sys
    sys.modules.setdefault('app', sys.modules[__name__])
    app.run(debug=False, port=5000)
