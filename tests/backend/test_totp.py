"""Two-step verification with an authenticator app (TOTP) + recovery codes."""
import hashlib
from datetime import datetime, timedelta

import pytest

import app as A
import routes.auth as auth_routes
import routes.mfa as mfa
import totp
from models import PasswordResetToken, User


EMAIL, PW = "app2fa@example.com", "correct-horse"


@pytest.fixture(autouse=True)
def _reset():
    A._login_failures.clear()
    auth_routes._reset_code_failures.clear()
    yield
    A._login_failures.clear()


@pytest.fixture
def clock(monkeypatch):
    """Controllable time for code generation and checking."""
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(totp, "_clock", lambda: state["t"])
    def tick(seconds=30):
        state["t"] += seconds
    state["tick"] = tick
    return state


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mfa, "send_verification_code_email",
                        lambda to, name, code, purpose: sent.append(code))
    return sent


def _register(client):
    r = client.post("/api/register", json={"email": EMAIL, "password": PW, "name": "App User"})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.get_json()['token']}"}


def _setup(client, auth, clock):
    r = client.post("/api/mfa/totp/setup", headers=auth)
    assert r.status_code == 200
    body = r.get_json()
    secret = body["secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/Time%20Vault%3A")
    assert f"secret={secret}" in body["otpauth_uri"]
    r = client.post("/api/mfa/totp/confirm", headers=auth,
                    json={"code": totp.code_at(secret, clock["t"])})
    assert r.status_code == 200, r.get_json()
    clock["tick"]()             # the setup code's time step is now spent
    return secret, r.get_json()["recovery_codes"]


def _password_ok(client):
    r = client.post("/api/login", json={"email": EMAIL, "password": PW})
    assert r.status_code == 200
    body = r.get_json()
    assert body["mfa_required"] is True and body["method"] == "totp"
    assert "email_hint" not in body and "token" not in body
    return body["mfa_token"]


def _second(client, mfa_token, code):
    return client.post("/api/login/mfa", json={"mfa_token": mfa_token, "code": code})


# ── Algorithm ────────────────────────────────────────────────────────────────

def test_rfc6238_vectors():
    key = b"12345678901234567890"
    for t, want in {59: "94287082", 1111111109: "07081804", 1234567890: "89005924",
                    2000000000: "69279037", 20000000000: "65353130"}.items():
        assert totp.hotp(key, t // 30, digits=8) == want


def test_verify_window_and_replay():
    s = totp.new_secret()
    t = 1_800_000_000
    code = totp.code_at(s, t)
    step = totp.verify(s, code, None, now=t)
    assert step == t // 30
    assert totp.verify(s, code, step, now=t) is None           # replay rejected
    assert totp.verify(s, code, None, now=t + 30) == step      # one step of drift OK
    assert totp.verify(s, code, None, now=t + 90) is None      # too old
    assert totp.verify(s, "12 34 5", None, now=t) is None      # wrong length


def test_secret_stored_encrypted(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    with A.app.app_context():
        u = User.query.filter_by(email=EMAIL).first()
        assert secret not in (u.totp_secret_enc or "")
        assert totp.decrypt_secret(u.totp_secret_enc) == secret
        assert u.totp_pending_secret_enc is None


# ── Enrolment ────────────────────────────────────────────────────────────────

def test_setup_needs_a_correct_code_before_turning_on(client, clock):
    auth = _register(client)
    secret = client.post("/api/mfa/totp/setup", headers=auth).get_json()["secret"]
    wrong = f"{(int(totp.code_at(secret, clock['t'])) + 1) % 1_000_000:06d}"
    assert client.post("/api/mfa/totp/confirm", headers=auth, json={"code": wrong}).status_code == 400
    me = client.get("/api/me", headers=auth).get_json()
    assert me["mfaEnabled"] is False and me["mfaMethod"] is None
    # login still a single step
    assert "token" in client.post("/api/login", json={"email": EMAIL, "password": PW}).get_json()


def test_confirm_turns_on_and_returns_ten_recovery_codes(client, clock):
    auth = _register(client)
    _, codes = _setup(client, auth, clock)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 11 and c[5] == "-" for c in codes)
    me = client.get("/api/me", headers=auth).get_json()
    assert me["mfaEnabled"] is True and me["mfaMethod"] == "totp"
    assert me["recoveryCodesLeft"] == 10


def test_confirm_without_setup_is_rejected(client, clock):
    auth = _register(client)
    r = client.post("/api/mfa/totp/confirm", headers=auth, json={"code": "123456"})
    assert r.status_code == 400


def test_cannot_set_up_while_already_on(client, clock):
    auth = _register(client)
    _setup(client, auth, clock)
    assert client.post("/api/mfa/totp/setup", headers=auth).status_code == 400


# ── Sign-in ──────────────────────────────────────────────────────────────────

def test_login_with_app_code(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    tok = _password_ok(client)
    r = _second(client, tok, totp.code_at(secret, clock["t"]))
    assert r.status_code == 200 and r.get_json()["token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {r.get_json()['token']}"})
    assert me.status_code == 200


def test_app_code_cannot_be_reused(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    code = totp.code_at(secret, clock["t"])
    assert _second(client, _password_ok(client), code).status_code == 200
    assert _second(client, _password_ok(client), code).status_code == 401


def test_wrong_codes_are_limited(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    tok = _password_ok(client)
    good = totp.code_at(secret, clock["t"])
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    for _ in range(mfa.MAX_ATTEMPTS - 1):
        assert _second(client, tok, wrong).status_code == 401
    r = _second(client, tok, wrong)
    assert r.get_json().get("code_expired") is True
    assert _second(client, tok, good).status_code == 401       # attempt is dead


def test_resend_not_available_for_app(client, clock, outbox):
    auth = _register(client)
    _setup(client, auth, clock)
    tok = _password_ok(client)
    assert client.post("/api/login/mfa/resend", json={"mfa_token": tok}).status_code == 400
    assert outbox == []


def test_recovery_code_signs_in_once(client, clock):
    auth = _register(client)
    _, codes = _setup(client, auth, clock)
    r = _second(client, _password_ok(client), codes[0].upper().replace("-", " "))
    assert r.status_code == 200 and r.get_json()["recovery_codes_left"] == 9
    assert _second(client, _password_ok(client), codes[0]).status_code == 401


def test_regenerate_recovery_codes_needs_password(client, clock):
    auth = _register(client)
    _, old = _setup(client, auth, clock)
    assert client.post("/api/mfa/recovery-codes", headers=auth, json={"password": "x"}).status_code == 400
    r = client.post("/api/mfa/recovery-codes", headers=auth, json={"password": PW})
    new = r.get_json()["recovery_codes"]
    assert r.status_code == 200 and len(new) == 10 and not set(new) & set(old)
    assert _second(client, _password_ok(client), old[1]).status_code == 401
    assert _second(client, _password_ok(client), new[1]).status_code == 200


def test_disable_clears_the_app(client, clock):
    auth = _register(client)
    _setup(client, auth, clock)
    assert client.post("/api/mfa/disable", headers=auth, json={"password": PW}).status_code == 200
    me = client.get("/api/me", headers=auth).get_json()
    assert me["mfaEnabled"] is False and me["recoveryCodesLeft"] == 0
    with A.app.app_context():
        u = User.query.filter_by(email=EMAIL).first()
        assert u.totp_secret_enc is None and u.mfa_method == "email"
    assert "token" in client.post("/api/login", json={"email": EMAIL, "password": PW}).get_json()


def test_switching_to_email_clears_the_app(client, clock, outbox):
    auth = _register(client)
    _setup(client, auth, clock)
    client.post("/api/mfa/disable", headers=auth, json={"password": PW})
    client.post("/api/mfa/enable", headers=auth)
    client.post("/api/mfa/enable/confirm", headers=auth, json={"code": outbox[-1]})
    r = client.post("/api/login", json={"email": EMAIL, "password": PW}).get_json()
    assert r["method"] == "email" and r["email_hint"]


# ── Forgot password ──────────────────────────────────────────────────────────

def _reset_link(client):
    raw = "reset-token-for-tests"
    with A.app.app_context():
        u = User.query.filter_by(email=EMAIL).first()
        A.db.session.add(PasswordResetToken(
            user_id=u.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.utcnow() + timedelta(minutes=30)))
        A.db.session.commit()
    return raw


def test_reset_needs_app_code_when_app_is_on(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    raw = _reset_link(client)
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw"})
    assert r.status_code == 403 and r.get_json()["mfa_code_required"] is True
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw",
                                                  "code": totp.code_at(secret, clock["t"])})
    assert r.status_code == 200
    assert client.post("/api/login", json={"email": EMAIL, "password": "brand-new-pw"}).status_code == 200


def test_reset_accepts_a_recovery_code(client, clock):
    auth = _register(client)
    _, codes = _setup(client, auth, clock)
    raw = _reset_link(client)
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw",
                                                  "code": codes[3]})
    assert r.status_code == 200


def test_reset_link_burned_after_wrong_codes(client, clock):
    auth = _register(client)
    secret, _ = _setup(client, auth, clock)
    raw = _reset_link(client)
    for _ in range(auth_routes.RESET_CODE_MAX_FAILURES - 1):
        r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw", "code": "000000"})
        assert r.status_code == 403
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw", "code": "000000"})
    assert r.status_code == 400
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw",
                                                  "code": totp.code_at(secret, clock["t"])})
    assert r.status_code == 400                                # link is dead
    assert client.post("/api/login", json={"email": EMAIL, "password": PW}).status_code == 200


def test_reset_unchanged_for_email_method(client, outbox):
    auth = _register(client)
    client.post("/api/mfa/enable", headers=auth)
    client.post("/api/mfa/enable/confirm", headers=auth, json={"code": outbox[-1]})
    raw = _reset_link(client)
    r = client.post("/api/reset-password", json={"token": raw, "new_password": "brand-new-pw"})
    assert r.status_code == 200


# ── Sign-up prompt ───────────────────────────────────────────────────────────

def test_prompt_flag_and_dismiss(client):
    auth = _register(client)
    assert client.get("/api/me", headers=auth).get_json()["mfaPromptDismissed"] is False
    assert client.post("/api/me/mfa-prompt/dismiss").status_code == 401
    assert client.post("/api/me/mfa-prompt/dismiss", headers=auth).status_code == 200
    assert client.get("/api/me", headers=auth).get_json()["mfaPromptDismissed"] is True


def test_delete_account_removes_recovery_codes(client, clock):
    from models import RecoveryCode
    auth = _register(client)
    _setup(client, auth, clock)
    r = client.delete("/api/delete-account", headers=auth, json={"password": PW})
    assert r.status_code == 200, r.get_json()
    with A.app.app_context():
        assert RecoveryCode.query.count() == 0
