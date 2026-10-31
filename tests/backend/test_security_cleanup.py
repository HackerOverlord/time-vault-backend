"""Tests for login rate limiting and Fernet content encryption."""
import base64

import pytest

import app as flask_app


@pytest.fixture(autouse=True)
def _reset_limiter():
    flask_app._login_failures.clear()
    yield
    flask_app._login_failures.clear()


def _register(client, email="rl@example.com", password="correct-horse"):
    r = client.post("/api/register", json={"email": email, "password": password, "name": "RL"})
    assert r.status_code == 201


def test_login_locks_after_five_failures(client):
    _register(client)
    for _ in range(5):
        r = client.post("/api/login", json={"email": "rl@example.com", "password": "wrong-pass"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"email": "rl@example.com", "password": "wrong-pass"})
    assert r.status_code == 429
    # Even the correct password is refused while locked.
    r = client.post("/api/login", json={"email": "rl@example.com", "password": "correct-horse"})
    assert r.status_code == 429


def test_successful_login_resets_counter(client):
    _register(client)
    for _ in range(4):
        client.post("/api/login", json={"email": "rl@example.com", "password": "wrong-pass"})
    r = client.post("/api/login", json={"email": "rl@example.com", "password": "correct-horse"})
    assert r.status_code == 200
    for _ in range(4):
        r = client.post("/api/login", json={"email": "rl@example.com", "password": "wrong-pass"})
        assert r.status_code == 401


def test_lockout_is_per_email(client):
    _register(client, "a@example.com")
    _register(client, "b@example.com")
    for _ in range(5):
        client.post("/api/login", json={"email": "a@example.com", "password": "wrong-pass"})
    r = client.post("/api/login", json={"email": "b@example.com", "password": "correct-horse"})
    assert r.status_code == 200


def test_encrypt_roundtrip_uses_fernet():
    token = flask_app.encrypt_content("a secret memory")
    assert token.startswith(b"fernet:")
    assert b"a secret memory" not in token
    assert flask_app.decrypt_content(token) == "a secret memory"


def test_same_plaintext_encrypts_differently():
    # Fernet uses a random IV, unlike the old repeating-key XOR.
    assert flask_app.encrypt_content("same") != flask_app.encrypt_content("same")


def test_legacy_xor_rows_still_decrypt():
    key = flask_app.ENCRYPTION_KEY.encode()
    legacy = base64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(b"old memory")))
    assert flask_app.decrypt_content(legacy) == "old memory"


def test_empty_content():
    assert flask_app.encrypt_content("") == b""
    assert flask_app.decrypt_content(b"") == ""
