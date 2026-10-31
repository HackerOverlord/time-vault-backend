"""Forgot-password / reset-password flow."""
from datetime import datetime, timedelta

import pytest

import app as A


@pytest.fixture(autouse=True)
def _reset_state():
    A._reset_requests.clear()
    A._login_failures.clear()
    yield
    A._reset_requests.clear()
    A._login_failures.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture reset emails instead of sending them."""
    outbox = []
    monkeypatch.setattr(A, "send_password_reset_email",
                        lambda to, name, raw: outbox.append({"to": to, "token": raw}))
    return outbox


def _register(client, email="reset@example.com", password="old-password"):
    r = client.post("/api/register", json={"email": email, "password": password, "name": "Res"})
    assert r.status_code == 201
    return r.get_json()["token"]


def test_same_response_for_known_and_unknown_email(client, sent):
    _register(client)
    known   = client.post("/api/forgot-password", json={"email": "reset@example.com"})
    unknown = client.post("/api/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.get_json() == unknown.get_json()
    assert [m["to"] for m in sent] == ["reset@example.com"]


def test_full_reset_flow(client, sent):
    old_token = _register(client)
    client.post("/api/forgot-password", json={"email": "Reset@Example.com "})
    raw = sent[0]["token"]

    r = client.post("/api/reset-password", json={"token": raw, "new_password": "new-password"})
    assert r.status_code == 200

    # New password works, old one doesn't, old sessions are revoked.
    assert client.post("/api/login", json={"email": "reset@example.com", "password": "new-password"}).status_code == 200
    assert client.post("/api/login", json={"email": "reset@example.com", "password": "old-password"}).status_code == 401
    assert client.get("/api/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401


def test_token_is_single_use(client, sent):
    _register(client)
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    raw = sent[0]["token"]
    assert client.post("/api/reset-password", json={"token": raw, "new_password": "first-new-pw"}).status_code == 200
    assert client.post("/api/reset-password", json={"token": raw, "new_password": "second-new-pw"}).status_code == 400


def test_only_newest_link_works(client, sent):
    _register(client)
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    first, second = sent[0]["token"], sent[1]["token"]
    assert client.post("/api/reset-password", json={"token": first, "new_password": "new-password"}).status_code == 400
    assert client.post("/api/reset-password", json={"token": second, "new_password": "new-password"}).status_code == 200


def test_expired_token_rejected(client, sent, app_ctx):
    _register(client)
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    row = A.PasswordResetToken.query.first()
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    A.db.session.commit()
    r = client.post("/api/reset-password", json={"token": sent[0]["token"], "new_password": "new-password"})
    assert r.status_code == 400


def test_token_stored_hashed(client, sent, app_ctx):
    _register(client)
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    row = A.PasswordResetToken.query.first()
    assert row.token_hash != sent[0]["token"]
    assert len(row.token_hash) == 64


def test_bad_inputs(client, sent):
    assert client.post("/api/forgot-password", json={}).status_code == 400
    assert client.post("/api/reset-password", json={"token": "x"}).status_code == 400
    assert client.post("/api/reset-password", json={"token": "nope", "new_password": "long-enough"}).status_code == 400
    _register(client)
    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    assert client.post("/api/reset-password",
                       json={"token": sent[0]["token"], "new_password": "short"}).status_code == 400


def test_rate_limited_silently(client, sent):
    _register(client)
    for _ in range(5):
        r = client.post("/api/forgot-password", json={"email": "reset@example.com"})
        assert r.status_code == 200
    assert len(sent) == 3


def test_reset_clears_login_lockout(client, sent):
    _register(client)
    for _ in range(5):
        client.post("/api/login", json={"email": "reset@example.com", "password": "wrong-pass"})
    assert client.post("/api/login", json={"email": "reset@example.com", "password": "old-password"}).status_code == 429

    client.post("/api/forgot-password", json={"email": "reset@example.com"})
    client.post("/api/reset-password", json={"token": sent[0]["token"], "new_password": "new-password"})
    assert client.post("/api/login", json={"email": "reset@example.com", "password": "new-password"}).status_code == 200


def test_email_failure_still_returns_generic_response(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(A, "send_password_reset_email", boom)
    _register(client)
    r = client.post("/api/forgot-password", json={"email": "reset@example.com"})
    assert r.status_code == 200
