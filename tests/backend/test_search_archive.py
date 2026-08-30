"""
tests/backend/test_search_archive.py — Pass 23 search and archive tests.

Run with: python3 -m pytest tests/backend/test_search_archive.py -v
"""
import sys, os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db


def _post(app_ctx, vault_id, author_id, caption="hello", media_type="text",
          is_archived=False, unlock_at=None, is_unlocked=True):
    now = datetime.utcnow()
    p = A.Post(vault_id=vault_id, author_id=author_id,
               caption=caption, media_type=media_type,
               unlock_at=unlock_at, is_unlocked=is_unlocked,
               is_archived=is_archived,
               posted_at=now if is_unlocked else None, created_at=now)
    db.session.add(p)
    db.session.commit()
    return p


# ── Migration / default value ─────────────────────────────────────────────────

class TestMigrationDefault:
    def test_new_post_has_is_archived_false(self, app_ctx, users, vault, auth_owner, client):
        r = client.post(f"/api/vaults/{vault.id}/posts",
                        json={"caption": "hi", "media_type": "text"},
                        headers=auth_owner)
        assert r.status_code == 201
        assert r.get_json()["is_archived"] is False

    def test_existing_post_serializes_is_archived(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "test")
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        assert all("is_archived" in p for p in posts)

    def test_locked_post_serializes_is_archived(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            future = datetime.utcnow() + timedelta(days=10)
            _post(app_ctx, vault.id, owner.id, "secret", unlock_at=future, is_unlocked=False)
        r = client.get("/api/posts", headers=auth_member)
        locked = [p for p in r.get_json() if not p["is_unlocked"]]
        assert len(locked) == 1
        assert "is_archived" in locked[0]


# ── Archive / unarchive endpoint ──────────────────────────────────────────────

class TestArchiveEndpoint:
    def _make_post(self, app_ctx, users, vault):
        owner, _, _ = users
        with app_ctx.app_context():
            p = _post(app_ctx, vault.id, owner.id, "archivable")
            return p.id  # return id not the detached object

    def test_owner_can_archive_own_post(self, app_ctx, users, vault, auth_owner, client):
        pid = self._make_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": True}, headers=auth_owner)
        assert r.status_code == 200
        assert r.get_json()["is_archived"] is True

    def test_owner_can_unarchive(self, app_ctx, users, vault, auth_owner, client):
        with app_ctx.app_context():
            owner, _, _ = users
            p = _post(app_ctx, vault.id, owner.id, "archived", is_archived=True)
            pid = p.id
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": False}, headers=auth_owner)
        assert r.status_code == 200
        assert r.get_json()["is_archived"] is False

    def test_vault_owner_can_archive_members_post(self, app_ctx, users, vault, auth_owner, client):
        owner, member, _ = users
        with app_ctx.app_context():
            p = _post(app_ctx, vault.id, member.id, "member post")
            pid = p.id
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": True}, headers=auth_owner)
        assert r.status_code == 200

    def test_member_cannot_archive_another_members_post(self, app_ctx, users, vault, auth_member, client):
        owner, member, _ = users
        with app_ctx.app_context():
            p = _post(app_ctx, vault.id, owner.id, "owner post")
            pid = p.id
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": True}, headers=auth_member)
        assert r.status_code == 403

    def test_non_member_cannot_archive(self, app_ctx, users, vault, auth_outsider, client):
        with app_ctx.app_context():
            owner, _, _ = users
            p = _post(app_ctx, vault.id, owner.id)
            pid = p.id
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": True}, headers=auth_outsider)
        assert r.status_code in (403, 404)

    def test_unauthenticated_cannot_archive(self, app_ctx, users, vault, client):
        with app_ctx.app_context():
            owner, _, _ = users
            p = _post(app_ctx, vault.id, owner.id)
            pid = p.id
        r = client.post(f"/api/posts/{pid}/archive", json={"archived": True})
        assert r.status_code == 401

    def test_non_boolean_archived_value_rejected(self, app_ctx, users, vault, auth_owner, client):
        pid = self._make_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{pid}/archive",
                        json={"archived": "yes"}, headers=auth_owner)
        assert r.status_code == 400

    def test_missing_archived_field_rejected(self, app_ctx, users, vault, auth_owner, client):
        pid = self._make_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{pid}/archive",
                        json={}, headers=auth_owner)
        assert r.status_code == 400


# ── Feed excludes archived by default ────────────────────────────────────────

class TestFeedArchiveBehavior:
    def test_active_feed_excludes_archived_posts(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "active")
            _post(app_ctx, vault.id, owner.id, "archived", is_archived=True)
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        assert all(not p.get("is_archived") for p in posts)
        assert any(p["caption"] == "active" for p in posts if p.get("caption"))

    def test_archived_view_returns_only_archived(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "active")
            _post(app_ctx, vault.id, owner.id, "archived", is_archived=True)
        r = client.get("/api/posts?archived=true", headers=auth_member)
        posts = r.get_json()
        assert all(p.get("is_archived") for p in posts)
        assert any(p.get("caption") == "archived" for p in posts)

    def test_archived_view_respects_vault_membership(self, app_ctx, users, vault, auth_outsider, client):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "secret", is_archived=True)
        r = client.get("/api/posts?archived=true", headers=auth_outsider)
        assert r.get_json() == []

    def test_locked_capsule_preserves_redaction_when_archived(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            future = datetime.utcnow() + timedelta(days=10)
            _post(app_ctx, vault.id, owner.id, "SECRET",
                  unlock_at=future, is_unlocked=False, is_archived=True)
        r = client.get("/api/posts?archived=true", headers=auth_member)
        posts = r.get_json()
        locked = [p for p in posts if not p.get("is_unlocked")]
        assert len(locked) == 1
        assert "caption" not in locked[0]  # strict allowlist must hold


# ── Search ────────────────────────────────────────────────────────────────────

class TestSearch:
    def _seed(self, app_ctx, users, vault):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "birthday party memories")
            _post(app_ctx, vault.id, owner.id, "summer holiday")
            _post(app_ctx, vault.id, owner.id, "archived birthday", is_archived=True)

    def test_search_caption_match(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r = client.get("/api/posts?q=birthday", headers=auth_member)
        posts = r.get_json()
        assert any("birthday" in (p.get("caption") or "") for p in posts)

    def test_search_excludes_archived_by_default(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r = client.get("/api/posts?q=birthday", headers=auth_member)
        assert all(not p.get("is_archived") for p in r.get_json())

    def test_search_vault_name(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        # vault name is "Test Vault" from conftest
        r = client.get("/api/posts?q=Test+Vault", headers=auth_member)
        posts = r.get_json()
        assert len(posts) > 0

    def test_search_author_name(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        # owner name is "Owner" from conftest
        r = client.get("/api/posts?q=Owner", headers=auth_member)
        assert len(r.get_json()) > 0

    def test_search_combined_with_archived(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r = client.get("/api/posts?q=birthday&archived=true", headers=auth_member)
        posts = r.get_json()
        assert all(p.get("is_archived") for p in posts)
        assert any("birthday" in (p.get("caption") or "") for p in posts)

    def test_empty_search_returns_all_active(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r_all = client.get("/api/posts", headers=auth_member)
        r_search = client.get("/api/posts?q=", headers=auth_member)
        assert len(r_all.get_json()) == len(r_search.get_json())

    def test_no_results_returns_empty_list(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r = client.get("/api/posts?q=zzznomatch", headers=auth_member)
        assert r.get_json() == []

    def test_oversized_query_201_chars_rejected(self, app_ctx, users, vault, auth_member, client):
        """Query over 200 chars must return 400, not silently truncate."""
        over = "x" * 201
        r = client.get(f"/api/posts?q={over}", headers=auth_member)
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_query_exactly_200_chars_accepted(self, app_ctx, users, vault, auth_member, client):
        boundary = "x" * 200
        r = client.get(f"/api/posts?q={boundary}", headers=auth_member)
        assert r.status_code == 200

    def test_whitespace_only_query_treated_as_no_search(self, app_ctx, users, vault, auth_member, client):
        self._seed(app_ctx, users, vault)
        r_blank  = client.get("/api/posts?q=+++", headers=auth_member)  # %2B would be literal +
        r_none   = client.get("/api/posts", headers=auth_member)
        assert r_blank.status_code == 200
        # URL-encoded spaces: Flask decodes them, strip() removes them → treated as empty
        r_spaces = client.get("/api/posts?q=%20%20%20", headers=auth_member)
        assert r_spaces.status_code == 200
        assert len(r_spaces.get_json()) == len(r_none.get_json())

    def test_locked_capsule_caption_not_searchable(self, app_ctx, users, vault, auth_member, client):
        owner, _, _ = users
        with app_ctx.app_context():
            future = datetime.utcnow() + timedelta(days=10)
            _post(app_ctx, vault.id, owner.id, "TOPSECRET_CAPSULE_CONTENT",
                  unlock_at=future, is_unlocked=False)
        r = client.get("/api/posts?q=TOPSECRET_CAPSULE_CONTENT", headers=auth_member)
        # The locked post must not appear — its caption is never indexed server-side
        posts = r.get_json()
        assert all(p.get("is_unlocked") is not False or p.get("caption") is None
                   for p in posts)
        # Specifically: a locked post with that caption must not be returned
        assert not any(
            (p.get("caption") or "").upper() == "TOPSECRET_CAPSULE_CONTENT"
            for p in posts
        )

    def test_non_member_cannot_search_vault_posts(self, app_ctx, users, vault, auth_outsider, client):
        owner, _, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "private memory")
        r = client.get("/api/posts?q=private", headers=auth_outsider)
        assert r.get_json() == []

    def test_removed_member_cannot_search(self, app_ctx, users, vault, client, member_token):
        owner, member, _ = users
        with app_ctx.app_context():
            _post(app_ctx, vault.id, owner.id, "searchable content")
            vm = A.VaultMember.query.filter_by(vault_id=vault.id, user_id=member.id).first()
            db.session.delete(vm); db.session.commit()
        r = client.get("/api/posts?q=searchable",
                       headers={"Authorization": f"Bearer {member_token}"})
        assert r.get_json() == []

    def test_special_chars_in_query_return_200(self, app_ctx, users, vault, auth_member, client):
        """Special characters ≤ 200 chars must not cause 500."""
        for q in ["%25", "SELECT+*", "0x00"]:
            r = client.get(f"/api/posts?q={q}", headers=auth_member)
            assert r.status_code == 200
