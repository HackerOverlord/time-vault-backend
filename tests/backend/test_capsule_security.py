"""
tests/backend/test_capsule_security.py — Pass 20 locked-post security tests.

Covers:
  - Strict locked serializer shape (exact allowed keys, forbidden keys absent)
  - Authorization: member, non-member, removed member, unauthenticated
  - Endpoint behavior: likes, comments, deletion, edit (405)
"""
import pytest
from datetime import datetime, timedelta
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db

# ── Exact locked payload shape ─────────────────────────────────────────────────

_LOCKED_REQUIRED = frozenset({
    "id", "vault_id", "vault_name",
    "author_id", "author_name", "author_avatar",
    "created_at", "unlock_at", "is_unlocked",
    "is_archived",  # Pass 23: non-sensitive metadata included in locked payload
})

_LOCKED_FORBIDDEN = frozenset({
    "caption", "media_type", "media_url", "posted_at",
    "like_count", "comment_count", "has_liked",
})


class TestLockedSerializerShape:
    """Locked payload must match the strict allowlist exactly."""

    def _get_locked(self, client, headers):
        r = client.get("/api/posts", headers=headers)
        assert r.status_code == 200
        posts = r.get_json()
        return next(p for p in posts if not p["is_unlocked"])

    def test_locked_payload_has_all_required_keys(self, client, vault, locked_post, auth_member):
        locked = self._get_locked(client, auth_member)
        missing = _LOCKED_REQUIRED - set(locked.keys())
        assert not missing, f"Required keys missing from locked payload: {missing}"

    def test_locked_payload_has_no_extra_keys(self, client, vault, locked_post, auth_member):
        locked = self._get_locked(client, auth_member)
        extra = set(locked.keys()) - _LOCKED_REQUIRED
        assert not extra, f"Unexpected keys in locked payload: {extra}"

    def test_locked_payload_no_caption(self, client, vault, locked_post, auth_member):
        assert "caption" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_media_type(self, client, vault, locked_post, auth_member):
        assert "media_type" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_media_url(self, client, vault, locked_post, auth_member):
        assert "media_url" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_posted_at(self, client, vault, locked_post, auth_member):
        assert "posted_at" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_like_count(self, client, vault, locked_post, auth_member):
        assert "like_count" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_comment_count(self, client, vault, locked_post, auth_member):
        assert "comment_count" not in self._get_locked(client, auth_member)

    def test_locked_payload_no_has_liked(self, client, vault, locked_post, auth_member):
        assert "has_liked" not in self._get_locked(client, auth_member)

    def test_locked_is_unlocked_field_is_false(self, client, vault, locked_post, auth_member):
        locked = self._get_locked(client, auth_member)
        assert locked["is_unlocked"] is False

    def test_locked_unlock_at_is_iso_string(self, client, vault, locked_post, auth_member):
        locked = self._get_locked(client, auth_member)
        val = locked["unlock_at"]
        assert isinstance(val, str) and "T" in val

    def test_owner_receives_same_redacted_payload(self, client, vault, locked_post, auth_owner):
        """Post owner is not exempt — they receive the same strict locked payload."""
        locked = self._get_locked(client, auth_owner)
        extra = set(locked.keys()) - _LOCKED_REQUIRED
        assert not extra, f"Owner receives extra keys: {extra}"

    def test_member_receives_same_redacted_payload(self, client, vault, locked_post, auth_member):
        locked = self._get_locked(client, auth_member)
        extra = set(locked.keys()) - _LOCKED_REQUIRED
        assert not extra


# ── Authorization ─────────────────────────────────────────────────────────────

class TestAuthorization:

    def test_unauthenticated_feed_returns_401(self, client, vault, locked_post):
        r = client.get("/api/posts")
        assert r.status_code == 401

    def test_member_can_see_locked_placeholder_in_feed(self, client, vault, locked_post, auth_member):
        r = client.get("/api/posts", headers=auth_member)
        assert r.status_code == 200
        posts = r.get_json()
        assert any(not p["is_unlocked"] for p in posts)

    def test_non_member_sees_empty_feed(self, client, vault, locked_post, auth_outsider):
        r = client.get("/api/posts", headers=auth_outsider)
        assert r.status_code == 200
        assert r.get_json() == []

    def test_non_member_cannot_like_locked_post(self, client, vault, locked_post, auth_outsider):
        r = client.post(f"/api/posts/{locked_post.id}/like", headers=auth_outsider)
        assert r.status_code in (403, 404)

    def test_non_member_cannot_like_unlocked_post(self, client, vault, unlocked_post, auth_outsider):
        r = client.post(f"/api/posts/{unlocked_post.id}/like", headers=auth_outsider)
        assert r.status_code in (403, 404)

    def test_non_member_cannot_comment_on_locked_post(self, client, vault, locked_post, auth_outsider):
        r = client.post(
            f"/api/posts/{locked_post.id}/comments",
            json={"body": "hi"}, headers=auth_outsider,
        )
        assert r.status_code in (403, 404)

    def test_removed_member_loses_feed_access(self, app_ctx, client, users, vault, locked_post, member_token):
        owner, member, _ = users
        with app_ctx.app_context():
            vm = A.VaultMember.query.filter_by(vault_id=vault.id, user_id=member.id).first()
            db.session.delete(vm); db.session.commit()
        r = client.get("/api/posts", headers={"Authorization": f"Bearer {member_token}"})
        assert r.status_code == 200
        assert r.get_json() == []

    def test_removed_member_cannot_like(self, app_ctx, client, users, vault, unlocked_post, member_token):
        owner, member, _ = users
        with app_ctx.app_context():
            vm = A.VaultMember.query.filter_by(vault_id=vault.id, user_id=member.id).first()
            db.session.delete(vm); db.session.commit()
        r = client.post(
            f"/api/posts/{unlocked_post.id}/like",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        assert r.status_code == 403

    def test_unauthenticated_cannot_like(self, client, vault, unlocked_post):
        r = client.post(f"/api/posts/{unlocked_post.id}/like")
        assert r.status_code == 401

    def test_unauthenticated_cannot_comment(self, client, vault, unlocked_post):
        r = client.post(f"/api/posts/{unlocked_post.id}/comments", json={"body": "hi"})
        assert r.status_code == 401


# ── Locked post interaction enforcement ───────────────────────────────────────

class TestLockedEndpointBehavior:

    def test_like_locked_post_returns_403(self, client, vault, locked_post, auth_member):
        r = client.post(f"/api/posts/{locked_post.id}/like", headers=auth_member)
        assert r.status_code == 403

    def test_unlike_locked_post_returns_403(self, client, vault, locked_post, auth_member):
        r = client.delete(f"/api/posts/{locked_post.id}/like", headers=auth_member)
        assert r.status_code == 403

    def test_like_unlocked_post_succeeds(self, client, vault, unlocked_post, auth_member):
        r = client.post(f"/api/posts/{unlocked_post.id}/like", headers=auth_member)
        assert r.status_code == 201

    def test_unlike_unlocked_post_after_like(self, client, vault, unlocked_post, auth_member):
        client.post(f"/api/posts/{unlocked_post.id}/like", headers=auth_member)
        r = client.delete(f"/api/posts/{unlocked_post.id}/like", headers=auth_member)
        assert r.status_code == 200

    def test_get_comments_locked_returns_403(self, client, vault, locked_post, auth_member):
        r = client.get(f"/api/posts/{locked_post.id}/comments", headers=auth_member)
        assert r.status_code == 403

    def test_post_comment_locked_returns_403(self, client, vault, locked_post, auth_member):
        r = client.post(
            f"/api/posts/{locked_post.id}/comments",
            json={"body": "hi"}, headers=auth_member,
        )
        assert r.status_code == 403

    def test_get_comments_unlocked_succeeds(self, client, vault, unlocked_post, auth_member):
        r = client.get(f"/api/posts/{unlocked_post.id}/comments", headers=auth_member)
        assert r.status_code == 200

    def test_post_comment_unlocked_succeeds(self, client, vault, unlocked_post, auth_member):
        r = client.post(
            f"/api/posts/{unlocked_post.id}/comments",
            json={"body": "hello"}, headers=auth_member,
        )
        assert r.status_code == 201

    def test_author_can_delete_locked_capsule(self, client, vault, locked_post, auth_owner):
        r = client.delete(f"/api/posts/{locked_post.id}", headers=auth_owner)
        assert r.status_code == 204

    def test_ordinary_member_cannot_delete_locked_capsule_they_did_not_author(
        self, client, vault, locked_post, auth_member
    ):
        r = client.delete(f"/api/posts/{locked_post.id}", headers=auth_member)
        assert r.status_code == 403

    def test_put_post_returns_405_method_not_allowed(self, client, vault, locked_post, auth_owner):
        r = client.put(f"/api/posts/{locked_post.id}", json={"caption": "HACKED"}, headers=auth_owner)
        assert r.status_code == 405

    def test_patch_post_returns_405(self, client, vault, locked_post, auth_owner):
        r = client.patch(f"/api/posts/{locked_post.id}", json={"caption": "HACKED"}, headers=auth_owner)
        assert r.status_code == 405

    def test_past_unlock_date_rejected(self, client, vault, auth_owner):
        yesterday = (datetime.utcnow() - timedelta(days=1)).date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "X", "media_type": "text", "unlock_at": yesterday},
            headers=auth_owner,
        )
        assert r.status_code == 400

    def test_future_unlock_date_creates_locked_post(self, client, vault, auth_owner):
        future = (datetime.utcnow() + timedelta(days=10)).date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "X", "media_type": "text", "unlock_at": future},
            headers=auth_owner,
        )
        assert r.status_code == 201
        data = r.get_json()
        assert data["is_unlocked"] is False
        extra = set(data.keys()) - frozenset({
            "id", "vault_id", "vault_name", "author_id", "author_name",
            "author_avatar", "created_at", "unlock_at", "is_unlocked",
            "is_archived",  # Pass 23
        })
        assert not extra, f"New locked post leaks extra keys: {extra}"
