"""
tests/backend/test_r2_uploads.py — Pass 22 R2 upload tests.

R2 is mocked throughout. No real files are uploaded.
Tests cover both the new multipart path and the legacy base64 path.
"""
import io
import sys
import os
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import app as A

db = A.db

FAKE_R2_URL = "https://pub.example.com/posts/abc123.jpg"

def _post_text(client, vault_id, auth, caption="hello", unlock_at=None):
    body = {"caption": caption, "media_type": "text"}
    if unlock_at:
        body["unlock_at"] = unlock_at
    return client.post(f"/api/vaults/{vault_id}/posts", json=body, headers=auth)

def _post_multipart(client, vault_id, auth, file_bytes, filename, mime, media_type, caption="test"):
    data = {
        "media":      (io.BytesIO(file_bytes), filename, mime),
        "media_type": media_type,
        "caption":    caption,
    }
    return client.post(f"/api/vaults/{vault_id}/posts",
                       data=data,
                       content_type="multipart/form-data",
                       headers=auth)

# Minimal magic-byte payloads that pass signature checks
JPEG_BYTES  = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * 100
PNG_BYTES   = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 100
# MP4: ftyp box — 4-byte size (0x00000018 = 24), then "ftyp"
MP4_BYTES   = bytes([0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70]) + b"mp42" + b"\x00" * 80
WEBM_BYTES  = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"\x00" * 100

# ── Text posts (unchanged by Pass 22) ────────────────────────────────────────

class TestTextPosts:
    def test_text_post_created_successfully(self, client, vault, auth_owner):
        r = _post_text(client, vault.id, auth_owner, "hello world")
        assert r.status_code == 201
        assert r.get_json()["media_type"] == "text"

    def test_text_post_has_no_media_url(self, client, vault, auth_owner):
        r = _post_text(client, vault.id, auth_owner)
        assert r.get_json().get("media_url") is None

    def test_text_post_does_not_call_r2(self, client, vault, auth_owner):
        with patch("app.upload_post_media") as mock_r2:
            _post_text(client, vault.id, auth_owner)
            mock_r2.assert_not_called()

    def test_empty_caption_text_post_returns_400(self, client, vault, auth_owner):
        r = _post_text(client, vault.id, auth_owner, caption="")
        assert r.status_code == 400


# ── Multipart image upload ─────────────────────────────────────────────────────

class TestMultipartImageUpload:
    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_jpeg_upload_returns_r2_url(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        assert r.status_code == 201
        assert r.get_json()["media_url"] == FAKE_R2_URL

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_png_upload_succeeds(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            PNG_BYTES, "photo.png", "image/png", "image")
        assert r.status_code == 201

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_r2_url_stored_in_db(self, mock_r2, app_ctx, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        post_id = r.get_json()["id"]
        with app_ctx.app_context():
            p = A.Post.query.get(int(post_id))
            assert p.media_url == FAKE_R2_URL

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_base64_never_stored_in_db(self, mock_r2, app_ctx, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        post_id = r.get_json()["id"]
        with app_ctx.app_context():
            p = A.Post.query.get(int(post_id))
            assert p.media_url is not None
            assert not p.media_url.startswith("data:")

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_upload_post_media_called_with_correct_args(self, mock_r2, client, vault, auth_owner):
        _post_multipart(client, vault.id, auth_owner,
                        JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        mock_r2.assert_called_once()
        args = mock_r2.call_args
        assert args[1]["mime_type"] == "image/jpeg" or args[0][2] == "image/jpeg"

    def test_image_upload_without_media_file_returns_400(self, client, vault, auth_owner):
        r = client.post(f"/api/vaults/{vault.id}/posts",
                        data={"media_type": "image", "caption": "no file"},
                        content_type="multipart/form-data",
                        headers=auth_owner)
        assert r.status_code == 400

    def test_invalid_image_mime_returns_400(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            b"fake tiff data", "photo.tiff", "image/tiff", "image")
        assert r.status_code == 400

    def test_empty_file_returns_400(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            b"", "photo.jpg", "image/jpeg", "image")
        assert r.status_code == 400

    @patch("app.upload_post_media", side_effect=Exception("R2 unavailable"))
    def test_r2_failure_returns_503(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        assert r.status_code == 503
        assert "upload" in r.get_json()["error"].lower()

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_r2_failure_leaves_no_orphan_post(self, mock_r2, app_ctx, client, vault, auth_owner):
        """If R2 fails, no Post row is created."""
        mock_r2.side_effect = Exception("R2 down")
        with app_ctx.app_context():
            before = A.Post.query.filter_by(vault_id=vault.id).count()
        _post_multipart(client, vault.id, auth_owner,
                        JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        with app_ctx.app_context():
            after = A.Post.query.filter_by(vault_id=vault.id).count()
        assert after == before


# ── Multipart video upload ─────────────────────────────────────────────────────

class TestMultipartVideoUpload:
    @patch("app.upload_post_media", return_value="https://pub.example.com/posts/vid.mp4")
    def test_mp4_upload_returns_r2_url(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            MP4_BYTES, "clip.mp4", "video/mp4", "video")
        assert r.status_code == 201
        assert "mp4" in r.get_json()["media_url"]

    @patch("app.upload_post_media", return_value="https://pub.example.com/posts/vid.webm")
    def test_webm_upload_succeeds(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            WEBM_BYTES, "clip.webm", "video/webm", "video")
        assert r.status_code == 201

    def test_invalid_video_mime_returns_400(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            b"avi data", "clip.avi", "video/avi", "video")
        assert r.status_code == 400


# ── Legacy base64 path (backward compatibility) ───────────────────────────────

class TestLegacyBase64Path:
    """The existing JSON / base64 upload path must remain functional."""

    def _b64_jpeg(self):
        """Build a minimal valid base64 data URI for a JPEG."""
        import base64
        return "data:image/jpeg;base64," + base64.b64encode(JPEG_BYTES).decode()

    @patch("app.upload_media", return_value=FAKE_R2_URL)
    def test_legacy_base64_image_upload_succeeds(self, mock_upload, client, vault, auth_owner):
        r = client.post(f"/api/vaults/{vault.id}/posts",
                        json={"caption": "legacy", "media_type": "image",
                              "media_url": self._b64_jpeg()},
                        headers=auth_owner)
        assert r.status_code == 201

    @patch("app.upload_media", return_value=FAKE_R2_URL)
    def test_legacy_base64_stores_r2_url_not_base64(self, mock_upload, app_ctx, client, vault, auth_owner):
        r = client.post(f"/api/vaults/{vault.id}/posts",
                        json={"caption": "legacy", "media_type": "image",
                              "media_url": self._b64_jpeg()},
                        headers=auth_owner)
        post_id = r.get_json()["id"]
        with app_ctx.app_context():
            p = A.Post.query.get(int(post_id))
            assert not (p.media_url or "").startswith("data:")

    def test_existing_post_with_base64_still_serializes(self, app_ctx, client, users, vault, auth_member):
        """Posts already in the DB with base64 media_url must still be returned."""
        owner, _, _ = users
        with app_ctx.app_context():
            now = datetime.utcnow()
            p = A.Post(vault_id=vault.id, author_id=owner.id,
                       caption="legacy", media_type="image",
                       media_url="data:image/jpeg;base64,/9j/AAAA",
                       is_unlocked=True, posted_at=now, created_at=now)
            db.session.add(p)
            db.session.commit()
        r = client.get("/api/posts", headers=auth_member)
        posts = r.get_json()
        legacy = next((p for p in posts if p.get("caption") == "legacy"), None)
        assert legacy is not None
        assert legacy["media_url"].startswith("data:")  # still returned as-is

    @patch("app.upload_media", return_value=FAKE_R2_URL)
    def test_existing_r2_url_passes_through_unchanged(self, mock_upload, app_ctx, client, users, vault, auth_owner):
        """If media_url is already an R2 URL, upload_media must not be called."""
        r = client.post(f"/api/vaults/{vault.id}/posts",
                        json={"caption": "r2post", "media_type": "image",
                              "media_url": FAKE_R2_URL},
                        headers=auth_owner)
        # R2 URLs skip re-upload
        mock_upload.assert_not_called()


# ── Migration utility ─────────────────────────────────────────────────────────

class TestMigrationUtility:
    @patch("app.upload_media", return_value=FAKE_R2_URL)
    def test_migrate_base64_post_to_r2(self, mock_upload, app_ctx, client, users, vault, auth_owner):
        owner, _, _ = users
        with app_ctx.app_context():
            now = datetime.utcnow()
            p = A.Post(vault_id=vault.id, author_id=owner.id,
                       caption="migrate me", media_type="image",
                       media_url="data:image/jpeg;base64,/9j/AAAA",
                       is_unlocked=True, posted_at=now, created_at=now)
            db.session.add(p); db.session.commit()
            post_id = p.id

        r = client.post(f"/api/posts/{post_id}/migrate-media",
                        headers=auth_owner)
        assert r.status_code == 200
        data = r.get_json()
        assert data["migrated"] is True
        assert data["media_url"] == FAKE_R2_URL

    @patch("app.upload_media", return_value=FAKE_R2_URL)
    def test_migrate_already_r2_post_is_noop(self, mock_upload, app_ctx, client, users, vault, auth_owner):
        owner, _, _ = users
        with app_ctx.app_context():
            now = datetime.utcnow()
            p = A.Post(vault_id=vault.id, author_id=owner.id,
                       caption="already r2", media_type="image",
                       media_url=FAKE_R2_URL,
                       is_unlocked=True, posted_at=now, created_at=now)
            db.session.add(p); db.session.commit()
            post_id = p.id

        r = client.post(f"/api/posts/{post_id}/migrate-media",
                        headers=auth_owner)
        assert r.status_code == 200
        assert r.get_json()["migrated"] is False
        mock_upload.assert_not_called()

    def test_migrate_non_member_returns_403(self, app_ctx, client, users, vault, auth_outsider):
        owner, _, _ = users
        with app_ctx.app_context():
            now = datetime.utcnow()
            p = A.Post(vault_id=vault.id, author_id=owner.id,
                       caption="x", media_type="image",
                       media_url="data:image/jpeg;base64,AAAA",
                       is_unlocked=True, posted_at=now, created_at=now)
            db.session.add(p); db.session.commit()
            post_id = p.id
        r = client.post(f"/api/posts/{post_id}/migrate-media",
                        headers=auth_outsider)
        assert r.status_code in (403, 404)


# ── Size limits ────────────────────────────────────────────────────────────────

class TestSizeLimits:
    def test_oversized_image_returns_413(self, client, vault, auth_owner):
        # Exceed 5 MB limit for images
        big_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * (5 * 1024 * 1024 + 1)
        r = _post_multipart(client, vault.id, auth_owner,
                            big_bytes, "big.jpg", "image/jpeg", "image")
        assert r.status_code == 413, f"Expected 413, got {r.status_code}: {r.get_json()}"

    def test_oversized_image_r2_not_called(self, client, vault, auth_owner):
        big_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * (5 * 1024 * 1024 + 1)
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            _post_multipart(client, vault.id, auth_owner,
                            big_bytes, "big.jpg", "image/jpeg", "image")
            mock_r2.assert_not_called()

    def test_oversized_image_no_post_created(self, app_ctx, client, vault, auth_owner):
        big_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * (5 * 1024 * 1024 + 1)
        with app_ctx.app_context():
            before = A.Post.query.filter_by(vault_id=vault.id).count()
        _post_multipart(client, vault.id, auth_owner,
                        big_bytes, "big.jpg", "image/jpeg", "image")
        with app_ctx.app_context():
            assert A.Post.query.filter_by(vault_id=vault.id).count() == before

    @pytest.mark.patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_boundary_size_image_accepted(self, client, vault, auth_owner):
        # Exactly 5 MB — should pass
        boundary_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * (5 * 1024 * 1024 - 4)
        with unittest.mock.patch("app.upload_post_media", return_value=FAKE_R2_URL):
            r = _post_multipart(client, vault.id, auth_owner,
                                boundary_bytes, "ok.jpg", "image/jpeg", "image")
        assert r.status_code == 201

    def test_oversized_video_returns_413(self, client, vault, auth_owner):
        # Exceed 50 MB limit for videos
        # Build a minimal valid MP4-like header then pad
        mp4_header = bytes([0x00, 0x00, 0x00, 0x18, 0x66, 0x74, 0x79, 0x70]) + b"mp42" + b"\x00" * 8
        big_video = mp4_header + b"\x00" * (50 * 1024 * 1024 + 1)
        r = _post_multipart(client, vault.id, auth_owner,
                            big_video, "big.mp4", "video/mp4", "video")
        assert r.status_code == 413


# ── MIME + extension validation ────────────────────────────────────────────────

class TestMimeValidation:
    def test_missing_mime_type_returns_400(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "", "image")
        assert r.status_code == 400

    def test_executable_mime_type_rejected(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            b"\x4D\x5A\x00\x00", "evil.exe", "application/octet-stream", "image")
        assert r.status_code == 400

    def test_text_mime_rejected_for_image(self, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            b"hello", "note.txt", "text/plain", "image")
        assert r.status_code == 400


# ── Migration endpoint (now at /api/posts/<id>/migrate-media) ─────────────────

class TestMigrationEndpointRename:
    """The migration endpoint must be at /api/posts/, not /api/admin/."""

    def _setup_legacy_post(self, app_ctx, users, vault):
        owner, _, _ = users
        with app_ctx.app_context():
            now = datetime.utcnow()
            p = A.Post(vault_id=vault.id, author_id=owner.id,
                       caption="x", media_type="image",
                       media_url="data:image/jpeg;base64,/9j/AAAA",
                       is_unlocked=True, posted_at=now, created_at=now)
            A.db.session.add(p); A.db.session.commit()
            return p.id

    def test_migration_not_under_admin_route(self, app_ctx, client, users, vault, auth_owner):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        r = client.post(f"/api/admin/posts/{post_id}/migrate-media", headers=auth_owner)
        assert r.status_code == 404, "Old /api/admin/ route must be gone"

    def test_migration_at_correct_route(self, app_ctx, client, users, vault, auth_owner):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        with unittest.mock.patch("app.upload_media", return_value=FAKE_R2_URL):
            r = client.post(f"/api/posts/{post_id}/migrate-media", headers=auth_owner)
        assert r.status_code == 200

    def test_migration_non_owner_member_returns_403(self, app_ctx, client, users, vault, auth_member):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{post_id}/migrate-media", headers=auth_member)
        assert r.status_code == 403

    def test_migration_outsider_returns_403(self, app_ctx, client, users, vault, auth_outsider):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{post_id}/migrate-media", headers=auth_outsider)
        assert r.status_code in (403, 404)

    def test_migration_unauthenticated_returns_401(self, app_ctx, client, users, vault):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        r = client.post(f"/api/posts/{post_id}/migrate-media")
        assert r.status_code == 401

    def test_migration_r2_failure_leaves_original_url(self, app_ctx, client, users, vault, auth_owner):
        post_id = self._setup_legacy_post(app_ctx, users, vault)
        with unittest.mock.patch("app.upload_media", side_effect=Exception("R2 down")):
            r = client.post(f"/api/posts/{post_id}/migrate-media", headers=auth_owner)
        assert r.status_code == 503
        with app_ctx.app_context():
            p = A.Post.query.get(post_id)
            assert p.media_url.startswith("data:")  # original preserved


# ── Pass 22.2: byte-signature and extension validation ─────────────────────────

class TestByteSignatureValidation:
    """Every multipart upload must pass magic-byte verification before R2 is called."""

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_valid_jpeg_bytes_accepted(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.jpg", "image/jpeg", "image")
        assert r.status_code == 201

    def test_random_bytes_with_jpeg_mime_rejected(self, client, vault, auth_owner):
        """Fake JPEG: MIME says image/jpeg but bytes are random junk."""
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                b"\x00\x01\x02\x03" * 20, "photo.jpg", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_png_bytes_with_jpeg_mime_rejected(self, client, vault, auth_owner):
        """PNG content declared as JPEG — signature mismatch."""
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                PNG_BYTES, "photo.jpg", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_jpeg_bytes_submitted_as_video_rejected(self, client, vault, auth_owner):
        """Image bytes with video/mp4 MIME — cross-type rejection."""
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                JPEG_BYTES, "clip.mp4", "video/mp4", "video")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_mp4_bytes_submitted_as_image_rejected(self, client, vault, auth_owner):
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                MP4_BYTES, "photo.jpg", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_valid_png_bytes_accepted(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            PNG_BYTES, "photo.png", "image/png", "image")
        assert r.status_code == 201

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_valid_mp4_bytes_accepted(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            MP4_BYTES, "clip.mp4", "video/mp4", "video")
        assert r.status_code == 201

    @patch("app.upload_post_media", return_value=FAKE_R2_URL)
    def test_valid_webm_bytes_accepted(self, mock_r2, client, vault, auth_owner):
        r = _post_multipart(client, vault.id, auth_owner,
                            WEBM_BYTES, "clip.webm", "video/webm", "video")
        assert r.status_code == 201


class TestExtensionMimeAgreement:
    """Extension must match the MIME type exactly per _MEDIA_SPEC."""

    def test_exe_extension_with_image_mime_rejected(self, client, vault, auth_owner):
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                JPEG_BYTES, "evil.exe", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_png_extension_with_jpeg_mime_rejected(self, client, vault, auth_owner):
        """Extension .png but MIME image/jpeg — mismatch."""
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                JPEG_BYTES, "photo.png", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_missing_extension_rejected(self, client, vault, auth_owner):
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            r = _post_multipart(client, vault.id, auth_owner,
                                JPEG_BYTES, "photonoext", "image/jpeg", "image")
            mock_r2.assert_not_called()
        assert r.status_code == 400

    def test_uppercase_extension_normalised_and_accepted(self, client, vault, auth_owner):
        """JPG → .jpg after lower() normalisation — should be accepted."""
        with unittest.mock.patch("app.upload_post_media", return_value=FAKE_R2_URL):
            r = _post_multipart(client, vault.id, auth_owner,
                                JPEG_BYTES, "photo.JPG", "image/jpeg", "image")
        assert r.status_code == 201

    def test_no_r2_call_on_extension_mismatch(self, client, vault, auth_owner):
        with unittest.mock.patch("app.upload_post_media") as mock_r2:
            _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.png", "image/jpeg", "image")
            mock_r2.assert_not_called()

    def test_no_orphan_post_on_extension_mismatch(self, app_ctx, client, vault, auth_owner):
        with app_ctx.app_context():
            before = A.Post.query.filter_by(vault_id=vault.id).count()
        with unittest.mock.patch("app.upload_post_media"):
            _post_multipart(client, vault.id, auth_owner,
                            JPEG_BYTES, "photo.png", "image/jpeg", "image")
        with app_ctx.app_context():
            assert A.Post.query.filter_by(vault_id=vault.id).count() == before


# ── MAX_CONTENT_LENGTH 413 handler ─────────────────────────────────────────────

class TestContentLengthHandler:
    """Flask MAX_CONTENT_LENGTH triggers a 413; verify JSON response."""

    def test_413_response_is_json_with_error_key(self, app_ctx, client, vault, auth_owner):
        """Simulate a request that exceeds MAX_CONTENT_LENGTH."""
        import flask
        import app as A
        # Temporarily lower the limit so the test doesn't need a 55 MB payload
        original = A.app.config.get('MAX_CONTENT_LENGTH')
        A.app.config['MAX_CONTENT_LENGTH'] = 10  # 10 bytes
        try:
            # Send a text post with a long body that exceeds 10 bytes
            r = client.post(
                f"/api/vaults/{vault.id}/posts",
                json={"caption": "X" * 100, "media_type": "text"},
                headers=auth_owner,
            )
            assert r.status_code == 413
            body = r.get_json()
            assert body is not None, "Response must be JSON"
            assert "error" in body
        finally:
            if original is not None:
                A.app.config['MAX_CONTENT_LENGTH'] = original
            else:
                A.app.config.pop('MAX_CONTENT_LENGTH', None)

    def test_normal_text_post_not_affected_by_limit(self, client, vault, auth_owner):
        """Normal posts must not be rejected by MAX_CONTENT_LENGTH."""
        r = client.post(
            f"/api/vaults/{vault.id}/posts",
            json={"caption": "hello", "media_type": "text"},
            headers=auth_owner,
        )
        assert r.status_code == 201
