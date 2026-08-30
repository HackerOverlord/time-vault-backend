"""
tests/backend/test_child_vault_email.py — Pass 21.1 email and serialization tests.

All email sending is mocked — no real SMTP calls are made.
"""
import hashlib, os, sys
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db

# Expose token for testing by patching the env flag
EXPOSE_ENV = {"EXPOSE_CLAIM_TOKEN": "true"}


def _child_vault(client, auth_owner, email="kid@example.com", name="Kid's Vault"):
    r = client.post("/api/vaults",
                    json={"name": name, "vault_type": "child", "child_email": email},
                    headers=auth_owner)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


# ── Email: claim invite ────────────────────────────────────────────────────────

class TestClaimInviteEmail:

    @patch("app.send_claim_invite_email")
    def test_send_claim_invite_calls_email_with_correct_recipient(
        self, mock_send, client, auth_owner
    ):
        vid = _child_vault(client, auth_owner, email="alice@example.com")
        with patch.dict(os.environ, EXPOSE_ENV):
            r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        assert r.status_code == 201
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        # First arg is to_email
        assert call_kwargs[1]["to_email"] == "alice@example.com" or \
               call_kwargs[0][0] == "alice@example.com"

    @patch("app.send_claim_invite_email")
    def test_send_claim_invite_includes_vault_name(self, mock_send, client, auth_owner):
        vid = _child_vault(client, auth_owner, name="Alex's Memories")
        with patch.dict(os.environ, EXPOSE_ENV):
            client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        args = mock_send.call_args
        vault_name_passed = args[1].get("vault_name") or args[0][2]
        assert "Alex's Memories" in vault_name_passed

    @patch("app.send_claim_invite_email")
    def test_send_claim_invite_includes_expiry(self, mock_send, client, auth_owner):
        vid = _child_vault(client, auth_owner)
        with patch.dict(os.environ, EXPOSE_ENV):
            r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        data = r.get_json()
        assert "expires_at" in data

    @patch("app.send_claim_invite_email")
    def test_claim_invite_includes_claim_url_via_exposed_token(
        self, mock_send, client, auth_owner
    ):
        """In dev mode, the raw token is returned so the link can be constructed."""
        vid = _child_vault(client, auth_owner)
        with patch.dict(os.environ, EXPOSE_ENV):
            r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        data = r.get_json()
        assert "token" in data
        assert len(data["token"]) > 20

    def test_plaintext_token_not_in_production_response(self, client, auth_owner):
        """Without EXPOSE_CLAIM_TOKEN the raw token must NOT be in the response."""
        vid = _child_vault(client, auth_owner)
        env = {k: v for k, v in os.environ.items() if k != "EXPOSE_CLAIM_TOKEN"}
        with patch("app.send_claim_invite_email"), \
             patch.dict(os.environ, env, clear=True):
            r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        data = r.get_json()
        assert "token" not in data, "Raw token must not appear in production response"

    @patch("app.send_claim_invite_email")
    def test_new_invite_invalidates_previous_token(
        self, mock_send, app_ctx, client, auth_owner
    ):
        vid = _child_vault(client, auth_owner)
        with patch.dict(os.environ, EXPOSE_ENV):
            r1 = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
            token1 = r1.get_json()["token"]
            client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        with app_ctx.app_context():
            h = hashlib.sha256(token1.encode()).hexdigest()
            row = A.ChildClaimToken.query.filter_by(token_hash=h).first()
            assert row.used is True

    def test_non_owner_cannot_send_invite(self, client, auth_owner, auth_member):
        vid = _child_vault(client, auth_owner)
        r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_member)
        assert r.status_code == 403

    def test_normal_vault_cannot_generate_invite(self, client, auth_owner):
        r = client.post("/api/vaults", json={"name": "Normal"}, headers=auth_owner)
        vid = r.get_json()["id"]
        r2 = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        assert r2.status_code == 400

    @patch("app.send_claim_invite_email", side_effect=Exception("SMTP down"))
    def test_email_failure_still_returns_response(self, mock_send, client, auth_owner):
        """Email failure must not leave the token in an ambiguous state."""
        vid = _child_vault(client, auth_owner)
        r = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
        # The invite endpoint should still return 201; email_error reported
        assert r.status_code == 201
        data = r.get_json()
        assert data["sent"] is False
        assert "email_error" in data


# ── claimed_by serialization ───────────────────────────────────────────────────

class TestClaimedBySerialization:

    def _create_and_claim(self, client, auth_owner):
        vid = _child_vault(client, auth_owner, email="claim_ser@example.com")
        with patch("app.send_claim_invite_email"), \
             patch.dict(os.environ, EXPOSE_ENV):
            ri = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
            token = ri.get_json()["token"]
        r = client.post("/api/claim-vault",
                        json={"token": token, "display_name": "Alice",
                              "password": "securepass123"})
        assert r.status_code == 200
        return vid

    def test_unclaimed_child_vault_has_null_claimed_by(self, client, auth_owner):
        vid = _child_vault(client, auth_owner, email="uncl@example.com")
        r = client.get("/api/vaults", headers=auth_owner)
        vault = next(v for v in r.get_json() if v["id"] == str(vid))
        assert vault["claimed_by"] is None

    def test_claimed_vault_has_claimed_by_object(self, client, auth_owner):
        vid = self._create_and_claim(client, auth_owner)
        r = client.get("/api/vaults", headers=auth_owner)
        vault = next(v for v in r.get_json() if v["id"] == str(vid))
        assert vault["claimed_by"] is not None
        cb = vault["claimed_by"]
        assert "id" in cb
        assert "display_name" in cb
        assert "avatar" in cb

    def test_claimed_by_display_name_matches_claimer(self, client, auth_owner):
        vid = self._create_and_claim(client, auth_owner)
        r = client.get("/api/vaults", headers=auth_owner)
        vault = next(v for v in r.get_json() if v["id"] == str(vid))
        assert vault["claimed_by"]["display_name"] == "Alice"

    def test_claimed_by_does_not_expose_password_hash(self, client, auth_owner):
        vid = self._create_and_claim(client, auth_owner)
        r = client.get("/api/vaults", headers=auth_owner)
        vault = next(v for v in r.get_json() if v["id"] == str(vid))
        cb = vault["claimed_by"]
        assert "password" not in cb
        assert "password_hash" not in cb
        assert "email" not in cb  # email is private

    def test_normal_vault_has_null_claimed_by(self, client, auth_owner):
        r = client.post("/api/vaults", json={"name": "Normal"}, headers=auth_owner)
        vid = r.get_json()["id"]
        r2 = client.get("/api/vaults", headers=auth_owner)
        vault = next(v for v in r2.get_json() if v["id"] == str(vid))
        assert vault["claimed_by"] is None


# ── Security: rejected attempts must not consume token ────────────────────────

class TestClaimSecurity:

    def _make_token(self, client, auth_owner, email="sec@example.com"):
        vid = _child_vault(client, auth_owner, email=email)
        with patch("app.send_claim_invite_email"), \
             patch.dict(os.environ, EXPOSE_ENV):
            ri = client.post(f"/api/vaults/{vid}/claim-invite", headers=auth_owner)
            return vid, ri.get_json()["token"]

    def test_wrong_email_authenticated_user_does_not_consume_token(
        self, app_ctx, client, auth_owner, auth_member, member_token
    ):
        vid, token = self._make_token(client, auth_owner, email="different@example.com")
        # Member's email != vault child_email → rejected
        r = client.post("/api/claim-vault", json={"token": token},
                        headers=auth_member)
        assert r.status_code == 403
        # Token must still be unused
        with app_ctx.app_context():
            h = hashlib.sha256(token.encode()).hexdigest()
            row = A.ChildClaimToken.query.filter_by(token_hash=h).first()
            assert row.used is False

    def test_wrong_password_existing_account_does_not_consume_token(
        self, file_app_ctx, file_client, file_users, file_vault
    ):
        """Uses file-backed DB so cross-connection token reads work correctly."""
        owner, _, _ = file_users
        email = "wrongpw2@example.com"
        # Create child vault
        from werkzeug.security import generate_password_hash as gph
        with file_app_ctx.app_context():
            import secrets as _s
            cv = A.Vault(name="WPTest", invite_code=_s.token_hex(3).upper(),
                         created_by=owner.id, created_at=datetime.utcnow(),
                         vault_type="child", child_email=email)
            db.session.add(cv)
            db.session.flush()
            cvid = cv.id

            # Add owner membership
            db.session.add(A.VaultMember(vault_id=cvid, user_id=owner.id,
                role="owner", joined_at=datetime.utcnow()))

            # Create claim token
            import secrets
            raw = secrets.token_urlsafe(32)
            h   = hashlib.sha256(raw.encode()).hexdigest()
            exp = datetime.utcnow() + timedelta(days=30)
            db.session.add(A.ChildClaimToken(vault_id=cvid, token_hash=h,
                created_by=owner.id, expires_at=exp))

            # Create the existing account
            u = A.User(name="WP2", email=email, password_hash=gph("correctpass"))
            db.session.add(u)
            db.session.commit()

        import jwt as _jwt
        owner_tok = _jwt.encode({"user_id": owner.id},
                                A.app.config["SECRET_KEY"], algorithm="HS256")

        r = file_client.post("/api/claim-vault",
                             json={"token": raw, "password": "wrongpass"})
        assert r.status_code == 401

        with file_app_ctx.app_context():
            row = A.ChildClaimToken.query.filter_by(token_hash=h).first()
            assert row.used is False

    def test_parent_remains_owner_after_claim(self, app_ctx, client, users, auth_owner):
        vid, token = self._make_token(client, auth_owner)
        owner, _, _ = users
        client.post("/api/claim-vault",
                    json={"token": token, "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            vm = A.VaultMember.query.filter_by(
                vault_id=vid, user_id=owner.id
            ).first()
            assert vm is not None and vm.role == "owner"

    def test_child_becomes_owner_after_claim(self, app_ctx, client, auth_owner):
        vid, token = self._make_token(client, auth_owner)
        client.post("/api/claim-vault",
                    json={"token": token, "display_name": "Alice",
                          "password": "securepass123"})
        with app_ctx.app_context():
            vault = A.Vault.query.get(vid)
            child_vm = A.VaultMember.query.filter_by(
                vault_id=vid, user_id=vault.claimed_by_user_id
            ).first()
            assert child_vm is not None and child_vm.role == "owner"

    def test_claim_info_does_not_mark_token_used(self, app_ctx, client, auth_owner):
        vid, token = self._make_token(client, auth_owner)
        client.get(f"/api/claim-info?token={token}")  # peek only
        with app_ctx.app_context():
            h = hashlib.sha256(token.encode()).hexdigest()
            row = A.ChildClaimToken.query.filter_by(token_hash=h).first()
            assert row.used is False
