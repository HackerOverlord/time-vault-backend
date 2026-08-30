"""
tests/backend/conftest.py — shared fixtures for all backend tests.

Two database strategies are provided:

app_ctx (default, fast)
    In-memory SQLite ("sqlite://").  Creates and drops all tables per test.
    Fast and fully isolated.  Suitable for all tests EXCEPT those that need
    the unlock job and the Flask test client to share committed state across
    separate connections — in-memory SQLite is connection-scoped.

file_app_ctx (integration, slower)
    File-backed SQLite using pytest's tmp_path fixture.  Each test gets its
    own temporary database file that is automatically deleted on teardown.
    Separate connections (e.g. unlock_job.run_unlock_job() running in one
    context and the Flask test client running in another) all see the same
    committed rows.  Use this only for the HTTP post-unlock integration test.

No production database is touched. No R2, email, or external calls are made.
"""
import os
import sys
import pytest
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

# ── Environment ───────────────────────────────────────────────────────────────
# Must be set before app is imported anywhere in the session.
os.environ.setdefault("SECRET_KEY", "test-secret-conftest")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("R2_BUCKET_NAME", "test-bucket")
os.environ.setdefault("R2_ACCOUNT_ID", "test-acct")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test-key")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test-sk")
os.environ.setdefault("R2_PUBLIC_URL", "https://pub.example.com")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app as flask_app

_db  = flask_app.db
_app = flask_app.app


# ── Token helper ──────────────────────────────────────────────────────────────

def _make_token(user_id: int) -> str:
    import jwt
    return jwt.encode(
        {"user_id": user_id},
        _app.config["SECRET_KEY"],
        algorithm="HS256",
    )


# ── Strategy A: in-memory SQLite (fast, per-test, single-connection) ──────────

@pytest.fixture
def app_ctx():
    """
    Fresh in-memory SQLite database for each test.

    Fast and fully isolated.  Use for all tests that do not require the unlock
    job and the HTTP test client to share committed state.
    """
    _app.config["TESTING"] = True
    _app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    with _app.app_context():
        _db.engine.dispose()   # drop cached connections to previous URI
        _db.create_all()
        yield _app
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


@pytest.fixture
def client(app_ctx):
    return app_ctx.test_client()


# ── Strategy B: file-backed SQLite (integration, cross-connection) ────────────

@pytest.fixture
def file_app_ctx(tmp_path):
    """
    File-backed SQLite database for each test that needs cross-connection
    visibility (e.g. unlock job + HTTP client).

    tmp_path is a pytest built-in fixture that provides a per-test temporary
    directory.  The database file is deleted automatically after the test.
    """
    db_file = tmp_path / "test_capsule.db"
    db_uri  = f"sqlite:///{db_file}"

    _app.config["TESTING"] = True
    _app.config["SQLALCHEMY_DATABASE_URI"] = db_uri

    with _app.app_context():
        _db.engine.dispose()           # drop cached connections to old URI
        _db.create_all()
        yield _app
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()           # release file handle before tmp_path cleanup


@pytest.fixture
def file_client(file_app_ctx):
    return file_app_ctx.test_client()


# ── User / vault / post factories (work with both app_ctx and file_app_ctx) ───

def _seed_users():
    owner    = flask_app.User(name="Owner",    email="owner@t.com",    password_hash=generate_password_hash("pw"))
    member   = flask_app.User(name="Member",   email="member@t.com",   password_hash=generate_password_hash("pw"))
    outsider = flask_app.User(name="Outsider", email="outsider@t.com", password_hash=generate_password_hash("pw"))
    _db.session.add_all([owner, member, outsider])
    _db.session.flush()
    return owner, member, outsider


def _seed_vault(owner, member):
    now = datetime.utcnow()
    v = flask_app.Vault(
        name="Test Vault",
        invite_code="TST001",
        created_by=owner.id,
        created_at=now,
    )
    _db.session.add(v)
    _db.session.flush()
    _db.session.add(flask_app.VaultMember(vault_id=v.id, user_id=owner.id,  role="owner",  joined_at=now))
    _db.session.add(flask_app.VaultMember(vault_id=v.id, user_id=member.id, role="member", joined_at=now))
    _db.session.commit()
    return v


@pytest.fixture
def users(app_ctx):
    """(owner, member, outsider) — for in-memory tests."""
    return _seed_users()


@pytest.fixture
def vault(app_ctx, users):
    owner, member, _ = users
    return _seed_vault(owner, member)


@pytest.fixture
def locked_post(app_ctx, users, vault):
    owner, _, _ = users
    future = datetime.utcnow() + timedelta(days=90)
    p = flask_app.Post(
        vault_id=vault.id, author_id=owner.id,
        caption="SECRET CONTENT", media_type="text", media_url=None,
        unlock_at=future, is_unlocked=False,
        posted_at=None, created_at=datetime.utcnow(),
    )
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def unlocked_post(app_ctx, users, vault):
    owner, _, _ = users
    now = datetime.utcnow()
    p = flask_app.Post(
        vault_id=vault.id, author_id=owner.id,
        caption="PUBLIC CONTENT", media_type="text", media_url=None,
        unlock_at=None, is_unlocked=True,
        posted_at=now, created_at=now,
    )
    _db.session.add(p)
    _db.session.commit()
    return p


# ── File-backed variants of user/vault/post fixtures ─────────────────────────

@pytest.fixture
def file_users(file_app_ctx):
    """(owner, member, outsider) — for file-backed integration tests."""
    return _seed_users()


@pytest.fixture
def file_vault(file_app_ctx, file_users):
    owner, member, _ = file_users
    return _seed_vault(owner, member)


@pytest.fixture
def file_locked_post(file_app_ctx, file_users, file_vault):
    """An overdue locked post — unlock_at already in the past."""
    owner, _, _ = file_users
    overdue = datetime.utcnow() - timedelta(seconds=5)
    p = flask_app.Post(
        vault_id=file_vault.id, author_id=owner.id,
        caption="SECRET CONTENT", media_type="text", media_url=None,
        unlock_at=overdue, is_unlocked=False,
        posted_at=None, created_at=datetime.utcnow() - timedelta(days=1),
    )
    _db.session.add(p)
    _db.session.commit()
    return p


# ── Auth helpers ──────────────────────────────────────────────────────────────

@pytest.fixture
def owner_token(users):
    return _make_token(users[0].id)

@pytest.fixture
def member_token(users):
    return _make_token(users[1].id)

@pytest.fixture
def outsider_token(users):
    return _make_token(users[2].id)

@pytest.fixture
def auth_owner(owner_token):
    return {"Authorization": f"Bearer {owner_token}"}

@pytest.fixture
def auth_member(member_token):
    return {"Authorization": f"Bearer {member_token}"}

@pytest.fixture
def auth_outsider(outsider_token):
    return {"Authorization": f"Bearer {outsider_token}"}

# File-backed auth helpers
@pytest.fixture
def file_owner_token(file_users):
    return _make_token(file_users[0].id)

@pytest.fixture
def file_member_token(file_users):
    return _make_token(file_users[1].id)

@pytest.fixture
def file_auth_owner(file_owner_token):
    return {"Authorization": f"Bearer {file_owner_token}"}

@pytest.fixture
def file_auth_member(file_member_token):
    return {"Authorization": f"Bearer {file_member_token}"}
