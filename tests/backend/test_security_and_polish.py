"""Tests for token revocation, timezone-aware capsule dates, security headers,
legacy notification filtering, and removal of /api/check-email."""
from datetime import datetime, timedelta

import pytest

import app as A


@pytest.fixture(autouse=True)
def _reset_limiter():
    A._login_failures.clear()
    yield
    A._login_failures.clear()


def _register(client, email="tok@example.com", password="original-pass"):
    r = client.post("/api/register", json={"email": email, "password": password, "name": "Tok"})
    assert r.status_code == 201
    return r.get_json()["token"]


# ── Token revocation ─────────────────────────────────────────────────────────

def test_token_works_normally(client):
    tok = _register(client)
    assert client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_password_change_revokes_old_tokens(client):
    old = _register(client)
    h = {"Authorization": f"Bearer {old}"}
    r = client.post("/api/change-password", headers=h,
                    json={"current_password": "original-pass", "new_password": "brand-new-pass"})
    assert r.status_code == 200
    assert client.get("/api/me", headers=h).status_code == 401

    new = client.post("/api/login", json={"email": "tok@example.com", "password": "brand-new-pass"}).get_json()["token"]
    assert client.get("/api/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_deleted_user_token_rejected(client, app_ctx):
    tok = _register(client)
    user = A.User.query.filter_by(email="tok@example.com").first()
    A.db.session.delete(user)
    A.db.session.commit()
    assert client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_token_without_fingerprint_rejected(client, app_ctx):
    import jwt
    _register(client)
    user = A.User.query.filter_by(email="tok@example.com").first()
    forged = jwt.encode({"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)},
                        A.app.config["SECRET_KEY"], algorithm="HS256")
    assert client.get("/api/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


# ── Timezone-aware capsule dates ─────────────────────────────────────────────

def _post_capsule(client, vault, auth, unlock_at, tz=None):
    body = {"caption": "c", "media_type": "text", "unlock_at": unlock_at}
    if tz is not None:
        body["tz_offset_minutes"] = tz
    return client.post(f"/api/vaults/{vault.id}/posts", json=body, headers=auth)


def test_local_tomorrow_accepted_when_utc_is_already_tomorrow(client, vault, auth_owner):
    # Toronto (UTC-4) at 9 PM local = 01:00 UTC next day.
    fake_now = datetime(2026, 10, 31, 1, 0, 0)  # UTC
    with pytest.MonkeyPatch.context() as mp:
        class FakeDT(datetime):
            @classmethod
            def utcnow(cls):
                return fake_now
        mp.setattr(A, "datetime", FakeDT)
        # Local date is Oct 30, so Oct 31 is "tomorrow" for this user.
        r = _post_capsule(client, vault, auth_owner, "2026-10-31", tz=240)
        assert r.status_code == 201, r.get_json()
        # ...but Oct 30 (their today) is rejected.
        r = _post_capsule(client, vault, auth_owner, "2026-10-30", tz=240)
        assert r.status_code == 400


def test_date_only_unlock_is_local_midnight_in_utc(client, vault, auth_owner):
    future = (datetime.utcnow() + timedelta(days=5)).date().isoformat()
    r = _post_capsule(client, vault, auth_owner, future, tz=240)
    assert r.status_code == 201
    assert r.get_json()["unlock_at"] == f"{future}T04:00:00Z"


def test_invalid_tz_offset_falls_back_to_utc(client, vault, auth_owner):
    future = (datetime.utcnow() + timedelta(days=5)).date().isoformat()
    r = _post_capsule(client, vault, auth_owner, future, tz="garbage")
    assert r.status_code == 201
    assert r.get_json()["unlock_at"] == f"{future}T00:00:00Z"
    r = _post_capsule(client, vault, auth_owner, future, tz=99999)
    assert r.get_json()["unlock_at"] == f"{future}T00:00:00Z"


# ── Security headers ─────────────────────────────────────────────────────────

def test_security_headers_present(client):
    r = client.post("/api/login", json={})
    for h in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
              "Content-Security-Policy", "Strict-Transport-Security"):
        assert h in r.headers


# ── Removed enumeration endpoint ─────────────────────────────────────────────

def test_check_email_endpoint_removed(client):
    r = client.post("/api/check-email", json={"email": "anyone@example.com"})
    assert r.status_code in (404, 405)


# ── Legacy notifications hidden ──────────────────────────────────────────────

def test_legacy_notifications_hidden(client, users, auth_owner):
    owner = users[0]
    A.create_notification(owner.id, "family_joined", "old family thing")
    A.create_notification(owner.id, "member_joined", "someone joined your vault")
    A.db.session.commit()
    msgs = [n["message"] for n in client.get("/api/notifications", headers=auth_owner).get_json()]
    assert "someone joined your vault" in msgs
    assert "old family thing" not in msgs


# ── Unlock job runs from requests (no scheduler on free hosting) ─────────────

def _due_capsule(users, vault):
    owner = users[0]
    p = A.Post(vault_id=vault.id, author_id=owner.id, caption="hello future",
               media_type="text", media_url=None,
               unlock_at=datetime.utcnow() - timedelta(minutes=1), is_unlocked=False,
               posted_at=None, created_at=datetime.utcnow() - timedelta(days=30))
    A.db.session.add(p)
    A.db.session.commit()
    return p.id


def test_request_unlocks_due_capsule(client, users, vault, auth_owner, monkeypatch):
    monkeypatch.setenv("RUN_JOBS_ON_REQUEST", "1")
    monkeypatch.setattr(A, "_last_job_run", None)
    post_id = _due_capsule(users, vault)

    posts = client.get("/api/posts", headers=auth_owner).get_json()
    posts = posts.get("posts", posts) if isinstance(posts, dict) else posts
    mine = [p for p in posts if p["id"] == str(post_id)][0]
    assert mine["is_unlocked"] is True
    assert mine["caption"] == "hello future"

    notifs = client.get("/api/notifications", headers=auth_owner).get_json()
    assert any(n["type"] == "capsule_unlocked" for n in notifs)


def test_job_is_throttled(client, users, vault, auth_owner, monkeypatch):
    monkeypatch.setenv("RUN_JOBS_ON_REQUEST", "1")
    monkeypatch.setattr(A, "_last_job_run", datetime.utcnow())  # ran just now
    post_id = _due_capsule(users, vault)
    client.get("/api/notifications", headers=auth_owner)
    assert A.Post.query.get(post_id).is_unlocked is False


def test_hook_disabled_by_env(client, users, vault, auth_owner, monkeypatch):
    monkeypatch.setenv("RUN_JOBS_ON_REQUEST", "0")
    monkeypatch.setattr(A, "_last_job_run", None)
    post_id = _due_capsule(users, vault)
    client.get("/api/notifications", headers=auth_owner)
    assert A.Post.query.get(post_id).is_unlocked is False
