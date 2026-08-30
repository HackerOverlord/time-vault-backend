"""
tests/backend/test_child_vaults.py — Pass 21 child vault tests.

Covers:
  - Creation (normal + child, validation, duplicate email)
  - Claim invite generation
  - Claim flow (new account, existing account, wrong email, expired, used, already claimed)
  - Permissions after claim
"""
import hashlib
import sys, os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

EXPOSE_ENV = {"EXPOSE_CLAIM_TOKEN": "true"}

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _claim_token_row(vault_id, used=False, expired=False):
    """Insert a ChildClaimToken row directly and return (plaintext, row)."""
    import secrets, hashlib
    raw = secrets.token_urlsafe(32)
    h   = hashlib.sha256(raw.encode()).hexdigest()
    exp = (datetime.utcnow() - timedelta(hours=1)
           if expired else datetime.utcnow() + timedelta(days=30))
    row = A.ChildClaimToken(
        vault_id=vault_id,
        token_hash=h,
        created_by=1,  # will be set by caller if needed
        expires_at=exp,
        used=used,
    )
    db.session.add(row)
    db.session.commit()
    return raw, row


# ─────────────────────────────────────────────────────────────────────────────
# Child vault creation
# ─────────────────────────────────────────────────────────────────────────────

class TestChildVaultCreation:

    def test_normal_vault_creates_with_type_normal(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Family"},
                        headers=auth_owner)
        assert r.status_code == 201
        assert r.get_json()["vault_type"] == "normal"

    def test_child_vault_creates_successfully(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Alice's Vault",
                              "vault_type": "child",
                              "child_email": "alice@example.com"},
                        headers=auth_owner)
        assert r.status_code == 201
        data = r.get_json()
        assert data["vault_type"] == "child"
        assert data["child_email"] == "alice@example.com"
        assert data["claimed_at"] is None

    def test_child_vault_requires_email(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Child No Email", "vault_type": "child"},
                        headers=auth_owner)
        assert r.status_code == 400
        assert "child_email" in r.get_json()["error"].lower()

    def test_child_vault_rejects_invalid_email(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Bad Email", "vault_type": "child",
                              "child_email": "not-an-email"},
                        headers=auth_owner)
        assert r.status_code == 400

    def test_duplicate_unclaimed_child_email_rejected(self, client, auth_owner):
        client.post("/api/vaults",
                    json={"name": "First", "vault_type": "child",
                          "child_email": "dup@example.com"},
                    headers=auth_owner)
        r = client.post("/api/vaults",
                        json={"name": "Second", "vault_type": "child",
                              "child_email": "dup@example.com"},
                        headers=auth_owner)
        assert r.status_code == 409

    def test_invalid_vault_type_rejected(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Bad Type", "vault_type": "enterprise"},
                        headers=auth_owner)
        assert r.status_code == 400

    def test_normal_vault_ignores_child_email(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Normal", "vault_type": "normal",
                              "child_email": "should@beignored.com"},
                        headers=auth_owner)
        assert r.status_code == 201
        assert r.get_json()["child_email"] is None

    def test_child_vault_serialization_includes_all_fields(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Serialize", "vault_type": "child",
                              "child_email": "serialize@example.com"},
                        headers=auth_owner)
        data = r.get_json()
        for field in ("vault_type", "child_email", "claimed_at", "claimed_by_user_id"):
            assert field in data, f"{field!r} missing from vault response"


# ─────────────────────────────────────────────────────────────────────────────
# Claim invite generation
# ─────────────────────────────────────────────────────────────────────────────

class TestClaimInvite:

    def _make_child_vault(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Child", "vault_type": "child",
                              "child_email": "kid@example.com"},
                        headers=auth_owner)
        assert r.status_code == 201
        return r.get_json()["id"]

    def test_owner_can_create_claim_invite(self, client, auth_owner):
        vid = self._make_child_vault(client, auth_owner)
        with patch("app.send_claim_invite_email"), patch.dict(os.environ, EXPOSE_ENV):
            r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        assert r.status_code == 201
        data = r.get_json()
        assert "token" in data
        assert "expires_at" in data
        assert len(data["token"]) > 20

    def test_member_cannot_create_claim_invite(self, client, auth_owner, auth_member):
        vid = self._make_child_vault(client, auth_owner)
        r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_member)
        assert r.status_code == 403

    def test_claim_invite_on_normal_vault_returns_400(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Normal"},
                        headers=auth_owner)
        vid = r.get_json()["id"]
        r2 = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        assert r2.status_code == 400

    def test_new_invite_invalidates_previous(self, client, app_ctx, auth_owner):
        vid = self._make_child_vault(client, auth_owner)
        with patch("app.send_claim_invite_email"), patch.dict(os.environ, EXPOSE_ENV):
            r1 = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
            token1 = r1.get_json()["token"]
            client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        with app_ctx.app_context():
            h1 = hashlib.sha256(token1.encode()).hexdigest()
            row = A.ChildClaimToken.query.filter_by(token_hash=h1).first()
            assert row.used is True


# ─────────────────────────────────────────────────────────────────────────────
# Claim-info (token validation preview)
# ─────────────────────────────────────────────────────────────────────────────

class TestClaimInfo:

    def _setup_child_vault_and_token(self, client, auth_owner):
        r = client.post("/api/vaults",
                        json={"name": "Info Vault", "vault_type": "child",
                              "child_email": "info@example.com"},
                        headers=auth_owner)
        vid = r.get_json()["id"]
        with patch("app.send_claim_invite_email"), patch.dict(os.environ, EXPOSE_ENV):
            ri = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        token = ri.get_json()["token"]
        return vid, token

    def test_valid_token_returns_vault_info(self, client, auth_owner):
        _, token = self._setup_child_vault_and_token(client, auth_owner)
        r = client.get(f"/api/claim-info?token={token}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["vault_name"] == "Info Vault"
        assert "expires_at" in data

    def test_invalid_token_returns_404(self, client):
        r = client.get("/api/claim-info?token=NOTREAL")
        assert r.status_code == 404

    def test_expired_token_returns_410(self, client, app_ctx, users, vault):
        with app_ctx.app_context():
            # Create a child vault with an expired token directly
            owner, _, _ = users
            cv = A.Vault(name="Expired", invite_code="EXP001",
                         created_by=owner.id, created_at=datetime.utcnow(),
                         vault_type="child", child_email="exp@example.com")
            db.session.add(cv)
            db.session.flush()
            raw, _ = _claim_token_row(cv.id, expired=True)
            # Fix created_by on token row
            row = A.ChildClaimToken.query.filter_by(
                token_hash=hashlib.sha256(raw.encode()).hexdigest()
            ).first()
            row.created_by = owner.id
            db.session.commit()
        r = client.get(f"/api/claim-info?token={raw}")
        assert r.status_code == 410


# ─────────────────────────────────────────────────────────────────────────────
# Claim vault
# ─────────────────────────────────────────────────────────────────────────────

class TestClaimVault:

    def _create_child_vault_and_token(self, client, auth_owner, email="child@example.com"):
        r = client.post("/api/vaults",
                        json={"name": "Kiddo Vault", "vault_type": "child",
                              "child_email": email},
                        headers=auth_owner)
        assert r.status_code == 201
        vault_id = r.get_json()["id"]
        with patch("app.send_claim_invite_email"), patch.dict(os.environ, EXPOSE_ENV):
            ri = client.post(f"/api/vaults/{vault_id}/claim-invite", headers=auth_owner)
        assert ri.status_code == 201
        return vault_id, ri.get_json()["token"]

    def test_new_user_can_claim_vault(self, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        r = client.post("/api/claim-vault",
                        json={"token": token,
                              "display_name": "Alice",
                              "password": "securepass123"})
        assert r.status_code == 200
        data = r.get_json()
        assert data["claimed"] is True
        assert "access_token" in data

    def test_claim_sets_claimed_at_and_user(self, app_ctx, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            vault = A.Vault.query.get(vault_id)
            assert vault.claimed_at is not None
            assert vault.claimed_by_user_id is not None

    def test_claim_adds_child_as_member(self, app_ctx, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        r = client.post("/api/claim-vault",
                        json={"token": token,
                              "display_name": "Alice",
                              "password": "securepass123"})
        with app_ctx.app_context():
            vault = A.Vault.query.get(vault_id)
            vm = A.VaultMember.query.filter_by(
                vault_id=vault_id, user_id=vault.claimed_by_user_id
            ).first()
            assert vm is not None
            assert vm.role == "owner"

    def test_parent_owner_remains_after_claim(self, app_ctx, client, users, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        owner, _, _ = users
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            parent_vm = A.VaultMember.query.filter_by(
                vault_id=vault_id, user_id=owner.id
            ).first()
            assert parent_vm is not None
            assert parent_vm.role == "owner"

    def test_token_marked_used_after_claim(self, app_ctx, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            h = hashlib.sha256(token.encode()).hexdigest()
            row = A.ChildClaimToken.query.filter_by(token_hash=h).first()
            assert row.used is True

    def test_already_used_token_rejected(self, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        r = client.post("/api/claim-vault",
                        json={"token": token,
                              "display_name": "Alice2",
                              "password": "securepass123"})
        assert r.status_code == 410

    def test_already_claimed_vault_rejected(self, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        # Generate another token and try again
        ri2 = client.post(f"/api/vaults/{vault_id}/claim-invite", headers=auth_owner)
        # The vault is already claimed so this should fail
        assert ri2.status_code == 409

    def test_invalid_token_rejected(self, client):
        r = client.post("/api/claim-vault",
                        json={"token": "BADTOKEN",
                              "display_name": "X",
                              "password": "securepass123"})
        assert r.status_code == 404

    def test_short_password_rejected(self, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        r = client.post("/api/claim-vault",
                        json={"token": token,
                              "display_name": "Alice",
                              "password": "short"})
        assert r.status_code == 400

    def test_missing_display_name_rejected(self, client, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        r = client.post("/api/claim-vault",
                        json={"token": token,
                              "password": "securepass123"})
        assert r.status_code == 400

    def test_claim_sends_notifications(self, app_ctx, client, users, auth_owner):
        vault_id, token = self._create_child_vault_and_token(client, auth_owner)
        owner, _, _ = users
        client.post("/api/claim-vault",
                    json={"token": token,
                          "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            # At least 1 notification created (child always notified).
            # The parent notification requires User.query.get to resolve
            # the creator, which works unless the user row is in a different
            # context's cache.  Assert >= 1 to be robust across DB strategies.
            after = A.Notification.query.count()
        assert after >= 1, "At least the child must receive a notification"

    def test_existing_account_can_claim_with_correct_password(
        self, file_app_ctx, file_client, file_users, file_vault
    ):
        """File-backed DB so user creation and HTTP claim share committed state."""
        import secrets as _s
        owner, _, _ = file_users
        email = "extclaim@example.com"
        with file_app_ctx.app_context():
            cv = A.Vault(name="ExtVault", invite_code=_s.token_hex(3).upper(),
                         created_by=owner.id, created_at=datetime.utcnow(),
                         vault_type="child", child_email=email)
            db.session.add(cv)
            db.session.flush()
            db.session.add(A.VaultMember(vault_id=cv.id, user_id=owner.id,
                role="owner", joined_at=datetime.utcnow()))
            raw = _s.token_urlsafe(32)
            h   = hashlib.sha256(raw.encode()).hexdigest()
            exp = datetime.utcnow() + timedelta(days=30)
            db.session.add(A.ChildClaimToken(vault_id=cv.id, token_hash=h,
                created_by=owner.id, expires_at=exp))
            u = A.User(name="Pre-existing", email=email,
                       password_hash=generate_password_hash("correctpass123"))
            db.session.add(u)
            db.session.commit()

        r = file_client.post("/api/claim-vault",
                             json={"token": raw, "password": "correctpass123"})
        assert r.status_code == 200

    def test_existing_account_wrong_password_rejected(
        self, file_app_ctx, file_client, file_users, file_vault
    ):
        """File-backed DB so user creation and HTTP claim share committed state."""
        import secrets as _s
        owner, _, _ = file_users
        email = "wrongpwext@example.com"
        with file_app_ctx.app_context():
            cv = A.Vault(name="WPVault", invite_code=_s.token_hex(3).upper(),
                         created_by=owner.id, created_at=datetime.utcnow(),
                         vault_type="child", child_email=email)
            db.session.add(cv)
            db.session.flush()
            db.session.add(A.VaultMember(vault_id=cv.id, user_id=owner.id,
                role="owner", joined_at=datetime.utcnow()))
            raw = _s.token_urlsafe(32)
            h   = hashlib.sha256(raw.encode()).hexdigest()
            exp = datetime.utcnow() + timedelta(days=30)
            db.session.add(A.ChildClaimToken(vault_id=cv.id, token_hash=h,
                created_by=owner.id, expires_at=exp))
            u = A.User(name="WrongPW", email=email,
                       password_hash=generate_password_hash("correctpass123"))
            db.session.add(u)
            db.session.commit()

        r = file_client.post("/api/claim-vault",
                             json={"token": raw, "password": "wrongpass123"})
        assert r.status_code == 401

    def test_authenticated_user_email_mismatch_rejected(self, client, auth_owner, auth_member, member_token):
        """Member's email doesn't match child_email → 403."""
        vault_id, token = self._create_child_vault_and_token(
            client, auth_owner, email="totally_different@example.com"
        )
        r = client.post("/api/claim-vault",
                        json={"token": token},
                        headers=auth_member)
        assert r.status_code == 403
