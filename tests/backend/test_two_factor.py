"""Two-step verification (email code)."""
from datetime import datetime, timedelta

import jwt
import pytest

import app as A
import routes.mfa as mfa


@pytest.fixture(autouse=True)
def _reset():
    A._login_failures.clear()
    yield
    A._login_failures.clear()


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mfa, "send_verification_code_email",
                        lambda to, name, code, purpose: sent.append({"to": to, "code": code, "purpose": purpose}))
    return sent


EMAIL, PW = "twofa@example.com", "correct-horse"


def _register(client):
    r = client.post("/api/register", json={"email": EMAIL, "password": PW, "name": "Two Fa"})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def _enable(client, auth, outbox):
    assert client.post("/api/mfa/enable", headers=auth).status_code == 200
    code = outbox[-1]["code"]
    r = client.post("/api/mfa/enable/confirm", headers=auth, json={"code": code})
    assert r.status_code == 200 and r.get_json()["mfaEnabled"] is True


def _login(client, password=PW):
    return client.post("/api/login", json={"email": EMAIL, "password": password})


def _wrong(code):
    return f"{(int(code) + 1) % 1_000_000:06d}"


# ── Enrolment ────────────────────────────────────────────────────────────────

def test_off_by_default_and_login_unchanged(client, outbox):
    auth = _register(client)
    assert client.get("/api/me", headers=auth).get_json()["mfaEnabled"] is False
    r = _login(client)
    assert r.status_code == 200 and "token" in r.get_json()
    assert outbox == []


def test_enable_requires_the_emailed_code(client, outbox):
    auth = _register(client)
    client.post("/api/mfa/enable", headers=auth)
    assert outbox[-1]["purpose"] == "enable" and outbox[-1]["to"] == EMAIL
    r = client.post("/api/mfa/enable/confirm", headers=auth, json={"code": _wrong(outbox[-1]["code"])})
    assert r.status_code == 400
    assert client.get("/api/me", headers=auth).get_json()["mfaEnabled"] is False
    client.post("/api/mfa/enable/confirm", headers=auth, json={"code": outbox[-1]["code"]})
    assert client.get("/api/me", headers=auth).get_json()["mfaEnabled"] is True


def test_disable_requires_password(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    assert client.post("/api/mfa/disable", headers=auth, json={"password": "nope"}).status_code == 400
    assert client.post("/api/mfa/disable", headers=auth, json={"password": PW}).status_code == 200
    assert _login(client).get_json().get("token")


# ── Login with 2FA on ────────────────────────────────────────────────────────

def test_password_alone_does_not_log_in(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    body = _login(client).get_json()
    assert body["mfa_required"] is True and "token" not in body
    assert body["email_hint"] == "t•••@example.com"
    assert outbox[-1]["purpose"] == "login"


def test_full_login_with_code(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    r = client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": outbox[-1]["code"]})
    assert r.status_code == 200
    me = client.get("/api/me", headers={"Authorization": f"Bearer {r.get_json()['token']}"})
    assert me.status_code == 200


def test_code_accepts_spaces_and_dashes(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    c = outbox[-1]["code"]
    r = client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": f"{c[:3]} {c[3:]}"})
    assert r.status_code == 200


def test_pending_token_is_not_a_login(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    assert client.get("/api/me", headers={"Authorization": f"Bearer {mfa_token}"}).status_code == 401


def test_access_token_cannot_be_used_as_pending_token(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    access = auth["Authorization"].split()[1]
    r = client.post("/api/login/mfa", json={"mfa_token": access, "code": "000000"})
    assert r.status_code == 401


def test_code_is_single_use(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    code = outbox[-1]["code"]
    assert client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": code}).status_code == 200
    assert client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": code}).status_code == 401


def test_five_wrong_codes_kill_the_code(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    code = outbox[-1]["code"]
    for _ in range(5):
        client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": _wrong(code)})
    r = client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": code})
    assert r.status_code == 401 and r.get_json().get("code_expired") is True


def test_expired_code_rejected(client, outbox, app_ctx):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    ch = A.MfaChallenge.query.filter_by(purpose="login").first()
    ch.expires_at = datetime.utcnow() - timedelta(seconds=1)
    A.db.session.commit()
    r = client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": outbox[-1]["code"]})
    assert r.status_code == 401


def test_resend_issues_new_code_and_old_one_dies(client, outbox, app_ctx):
    auth = _register(client)
    _enable(client, auth, outbox)
    first_token = _login(client).get_json()["mfa_token"]
    first_code = outbox[-1]["code"]
    assert client.post("/api/login/mfa/resend", json={"mfa_token": first_token}).status_code == 429  # cooldown
    A.MfaChallenge.query.update({"created_at": datetime.utcnow() - timedelta(minutes=1)})
    A.db.session.commit()
    r = client.post("/api/login/mfa/resend", json={"mfa_token": first_token})
    assert r.status_code == 200
    new_token, new_code = r.get_json()["mfa_token"], outbox[-1]["code"]
    assert client.post("/api/login/mfa", json={"mfa_token": first_token, "code": first_code}).status_code == 401
    assert client.post("/api/login/mfa", json={"mfa_token": new_token, "code": new_code}).status_code == 200


def test_hourly_cap_on_codes(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    statuses = [_login(client).status_code for _ in range(6)]
    assert statuses[:5] == [200] * 5 and statuses[5] == 429


def test_wrong_password_still_counts_toward_lockout(client, outbox):
    auth = _register(client)
    _enable(client, auth, outbox)
    for _ in range(5):
        assert _login(client, "wrong-pass").status_code == 401
    assert _login(client).status_code == 429
    assert all(m["purpose"] == "enable" for m in outbox)   # no login codes sent


def test_password_change_invalidates_pending_login(client, outbox, app_ctx):
    auth = _register(client)
    _enable(client, auth, outbox)
    mfa_token = _login(client).get_json()["mfa_token"]
    code = outbox[-1]["code"]
    client.post("/api/change-password", headers=auth,
                json={"current_password": PW, "new_password": "brand-new-pass"})
    assert client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": code}).status_code == 401


def test_forged_pending_token_rejected(client, outbox, app_ctx):
    _register(client)
    user = A.User.query.filter_by(email=EMAIL).first()
    forged = jwt.encode({"user_id": user.id, "typ": "mfa", "cid": 1,
                         "exp": datetime.utcnow() + timedelta(minutes=5)}, "not-the-key", algorithm="HS256")
    assert client.post("/api/login/mfa", json={"mfa_token": forged, "code": "123456"}).status_code == 401


def test_codes_are_stored_hashed(client, outbox, app_ctx):
    auth = _register(client)
    client.post("/api/mfa/enable", headers=auth)
    ch = A.MfaChallenge.query.first()
    assert outbox[-1]["code"] not in ch.code_hash and len(ch.code_hash) == 64


def test_email_failure_gives_clear_error(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smtp down")
    auth = _register(client)
    monkeypatch.setattr(mfa, "send_verification_code_email", boom)
    r = client.post("/api/mfa/enable", headers=auth)
    assert r.status_code == 503
