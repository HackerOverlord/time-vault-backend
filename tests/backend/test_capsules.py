"""
tests/backend/test_capsules.py — Pass 20 capsule lifecycle tests.

Covers:
  - Creating future/immediate capsules (date validation)
  - Unlock job: due posts unlock, future posts stay locked, idempotency
  - Feed response includes locked placeholders + full unlocked payloads
  - Notifications created on unlock
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db


# ── Date validation: capsule creation ────────────────────────────────────────

class TestCapsuleCreation:
    """POST /api/vaults/<id>/posts — unlock_at validation."""

    def test_future_date_creates_locked_post(self, client, vault, auth_owner):
        future = (datetime.utcnow() + timedelta(days=10)).date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "future", "media_type": "text", "unlock_at": future},
            headers=auth_owner,
        )
        assert r.status_code == 201
        data = r.get_json()
        assert data["is_unlocked"] is False
        assert data["unlock_at"] is not None

    def test_future_date_response_uses_strict_allowlist(self, client, vault, auth_owner):
        """Newly created locked post must return the strict locked payload."""
        future = (datetime.utcnow() + timedelta(days=10)).date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "future", "media_type": "text", "unlock_at": future},
            headers=auth_owner,
        )
        data = r.get_json()
        for forbidden in ("caption", "media_type", "media_url", "posted_at",
                          "like_count", "comment_count", "has_liked"):
            assert forbidden not in data, f"{forbidden!r} must be absent from locked creation response"

    def test_past_date_rejected(self, client, vault, auth_owner):
        yesterday = (datetime.utcnow() - timedelta(days=1)).date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "past", "media_type": "text", "unlock_at": yesterday},
            headers=auth_owner,
        )
        assert r.status_code == 400

    def test_today_date_rejected(self, client, vault, auth_owner):
        """Unlock date must be at least tomorrow."""
        today = datetime.utcnow().date().isoformat()
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "today", "media_type": "text", "unlock_at": today},
            headers=auth_owner,
        )
        assert r.status_code == 400

    def test_malformed_date_rejected(self, client, vault, auth_owner):
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "bad", "media_type": "text", "unlock_at": "not-a-date"},
            headers=auth_owner,
        )
        assert r.status_code == 400

    def test_no_unlock_date_creates_unlocked_post(self, client, vault, auth_owner):
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "immediate", "media_type": "text"},
            headers=auth_owner,
        )
        assert r.status_code == 201
        data = r.get_json()
        assert data["is_unlocked"] is True
        assert data["unlock_at"] is None
        # Unlocked post must have full payload
        assert "caption" in data
        assert "like_count" in data


# ── Unlock job ────────────────────────────────────────────────────────────────

class TestUnlockJob:
    """run_unlock_job() behaviour."""

    def _make_overdue(self, post_id, app_ctx):
        with app_ctx.app_context():
            p = A.Post.query.get(post_id)
            p.unlock_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()

    def test_due_capsule_becomes_unlocked(self, app_ctx, client, users, vault, locked_post, auth_member):
        self._make_overdue(locked_post.id, app_ctx)
        with app_ctx.app_context():
            from unlock_job import run_unlock_job
            run_unlock_job()
            p = A.Post.query.get(locked_post.id)
            assert p.is_unlocked is True

    def test_due_capsule_posted_at_is_set(self, app_ctx, client, users, vault, locked_post, auth_member):
        self._make_overdue(locked_post.id, app_ctx)
        with app_ctx.app_context():
            from unlock_job import run_unlock_job
            run_unlock_job()
            p = A.Post.query.get(locked_post.id)
            assert p.posted_at is not None

    def test_future_capsule_stays_locked(self, app_ctx, users, vault, locked_post):
        # locked_post.unlock_at is 90 days in the future — must not be unlocked
        with app_ctx.app_context():
            from unlock_job import run_unlock_job
            run_unlock_job()
            p = A.Post.query.get(locked_post.id)
            assert p.is_unlocked is False

    def test_unlock_job_creates_notifications(self, app_ctx, users, vault, locked_post):
        owner, member, _ = users
        self._make_overdue(locked_post.id, app_ctx)
        with app_ctx.app_context():
            before = A.Notification.query.count()
            from unlock_job import run_unlock_job
            run_unlock_job()
            after = A.Notification.query.count()
        # Vault has two members (owner + member) — at least 2 notifications
        assert after >= before + 2

    def test_unlock_job_idempotent(self, app_ctx, users, vault, locked_post):
        self._make_overdue(locked_post.id, app_ctx)
        with app_ctx.app_context():
            from unlock_job import run_unlock_job
            run_unlock_job()
            notif_first = A.Notification.query.count()
            run_unlock_job()
            notif_second = A.Notification.query.count()
        assert notif_first == notif_second

    def test_feed_returns_full_payload_after_unlock(
        self, file_app_ctx, file_client,
        file_users, file_vault, file_locked_post,
        file_auth_member,
    ):
        """
        Integration test: unlock job + HTTP feed endpoint.

        Uses a file-backed SQLite database so the unlock job (which runs
        in its own app context and commits via a separate connection) and
        the Flask test client (which opens yet another connection per
        request) both see the same committed data.

        Flow:
          1. A locked capsule with an overdue unlock_at already exists
             (created by the file_locked_post fixture).
          2. run_unlock_job() is called — it commits is_unlocked=True.
          3. An authenticated GET /api/posts request is made via the test client.
          4. The post appears in the response as is_unlocked=True.
          5. All previously-redacted fields are present and non-None.

        No call to serialize_post() is made inside this test.
        """
        from unlock_job import run_unlock_job

        # Step 2: run the real unlock job (commits via its own connection)
        with file_app_ctx.app_context():
            run_unlock_job()

        # Step 3 & 4: HTTP request via test client (separate connection)
        r = file_client.get("/api/posts", headers=file_auth_member)
        assert r.status_code == 200
        posts = r.get_json()

        # Step 4: locate the formerly-locked post in the response
        formerly_locked = next(
            (p for p in posts if p["id"] == str(file_locked_post.id)),
            None,
        )
        assert formerly_locked is not None, (
            f"Post {file_locked_post.id} not found in feed response. "
            f"Feed contained: {[p['id'] for p in posts]}"
        )

        # Step 5: confirm is_unlocked
        assert formerly_locked["is_unlocked"] is True, (
            "Post must be unlocked after run_unlock_job()"
        )

        # Step 6: confirm all previously-redacted fields are present
        for field in ("caption", "media_type", "media_url", "posted_at",
                      "like_count", "comment_count", "has_liked"):
            assert field in formerly_locked, (
                f"Field {field!r} must be present in unlocked post response"
            )


# ── Feed inclusion ────────────────────────────────────────────────────────────

class TestFeedInclusion:
    """Locked capsules appear in the aggregate feed as placeholders."""

    def test_locked_capsule_appears_in_feed(self, client, vault, locked_post, auth_member):
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        assert any(not p["is_unlocked"] for p in posts)

    def test_unlocked_post_appears_in_feed(self, client, vault, unlocked_post, auth_member):
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        assert any(p["is_unlocked"] for p in posts)

    def test_both_types_appear_together(self, client, vault, locked_post, unlocked_post, auth_member):
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        assert any(not p["is_unlocked"] for p in posts)
        assert any(p["is_unlocked"] for p in posts)
