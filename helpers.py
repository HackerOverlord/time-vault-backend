"""Shared helpers: notifications, vault permission checks, serializers, upload validation."""
import re
import base64
import secrets
import string

from flask import request, jsonify, g

from extensions import db
from models import Notification, User, Vault, VaultMember

# Notification types from the old family-tree / memories system, which the
# current frontend no longer has screens for. Hidden from the notification list.
LEGACY_NOTIFICATION_TYPES = (
    'family_joined', 'family_left', 'member_added',
    'vault_sent', 'vault_received', 'vault_deleted',
)

def utc_iso(dt):
    """ISO-8601 with an explicit 'Z' for naive UTC datetimes, so browsers
    don't parse them as local time. None passes through."""
    return dt.isoformat() + 'Z' if dt else None

def create_notification(user_id, type, message, vault_id=None):
    notif = Notification(user_id=user_id, type=type, message=message,
                         vault_id=vault_id)
    db.session.add(notif)
    # No commit here — caller commits


def notify_vault_members(vault_id, type, message, exclude_user_ids=None):
    """Create notifications for all current members of a vault.

    Args:
        vault_id: the vault whose current members receive the notification.
        type: notification type string.
        message: human-readable notification message.
        exclude_user_ids: iterable of user_ids that should NOT receive the
            notification (typically the actor). Defaults to empty.

    Recipients are determined from VaultMember at the moment of the call,
    ensuring removed members never receive future vault notifications.
    No commit is performed here — caller is responsible for committing.
    """
    exclude = set(exclude_user_ids or [])
    members = VaultMember.query.filter_by(vault_id=vault_id).all()
    for m in members:
        if m.user_id not in exclude:
            create_notification(m.user_id, type, message, vault_id=vault_id)


# --- V1 HELPERS ---

def vault_forbidden(msg='Forbidden'):
    """Return a vault-scoped 403 JSON response without touching global error handlers."""
    return jsonify({'error': msg}), 403


def require_vault_member(vault_id):
    """Return the VaultMember row for g.user_id in vault_id, or a 403 tuple.
    Caller must check: result = require_vault_member(id); if isinstance(result, tuple): return result
    """
    vm = VaultMember.query.filter_by(vault_id=vault_id, user_id=g.user_id).first()
    if not vm:
        return vault_forbidden()
    return vm


def require_vault_owner(vault_id):
    """Return the VaultMember row if g.user_id is the owner, or a 403 tuple."""
    result = require_vault_member(vault_id)
    if isinstance(result, tuple):
        return result
    if result.role != 'owner':
        return vault_forbidden()
    return result


def generate_invite_token() -> str:
    """Generate a cryptographically secure URL-safe invite token.

    Uses secrets.token_urlsafe(32) which produces 43 URL-safe characters
    with ~256 bits of entropy — infeasible to enumerate.
    """
    return secrets.token_urlsafe(32)


def generate_invite_code():
    """Generate a unique 6-character uppercase alphanumeric invite code."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(10):
        code = ''.join(secrets.choice(chars) for _ in range(6))
        if not Vault.query.filter_by(invite_code=code).first():
            return code
    raise RuntimeError('Failed to generate unique invite code after 10 attempts')


def serialize_post(post, author, vault, liked_set):
    """Serialise a Post row to the canonical V1 API response shape.

    Locked time-capsule posts use a STRICT ALLOWLIST containing only the
    metadata required for the placeholder card.  The payload is built
    separately and returned early — we never build the full payload and
    then null-out fields, because that risks accidentally leaking new
    fields added in future.

    Locked payload fields (exhaustive allowlist):
      id, vault_id, vault_name, author_id, author_name, author_avatar,
      created_at, unlock_at, is_unlocked

    Unlocked payload adds:
      caption, media_type, media_url, posted_at,
      like_count, comment_count, has_liked
    """
    if not post.is_unlocked:
        # Strict minimum for placeholder rendering.
        # Media type, caption, counts, and like status are intentionally absent.
        return {
            'id':            str(post.id),
            'vault_id':      str(post.vault_id),
            'vault_name':    vault.name,
            'author_id':     str(post.author_id),
            'author_name':   author.name,
            'author_avatar': author.avatar,
            'created_at':    post.created_at.isoformat(),
            'unlock_at':     utc_iso(post.unlock_at),
            'is_unlocked':   False,
        'is_archived':   bool(post.is_archived),
        }

    return {
        'id':            str(post.id),
        'vault_id':      str(post.vault_id),
        'vault_name':    vault.name,
        'author_id':     str(post.author_id),
        'author_name':   author.name,
        'author_avatar': author.avatar,
        'caption':       post.caption,
        'media_type':    post.media_type,
        'media_url':     post.media_url,
        'unlock_at':     utc_iso(post.unlock_at),
        'is_unlocked':   True,
        'posted_at':     post.posted_at.isoformat() if post.posted_at else None,
        'created_at':    post.created_at.isoformat(),
        'like_count':    post.like_count,
        'comment_count': post.comment_count,
        'has_liked':     post.id in liked_set,
        'is_archived':   bool(post.is_archived),
    }


def serialize_comment(comment, author):
    return {
        'id':            str(comment.id),
        'author_id':     str(comment.author_id),
        'author_name':   author.name,
        'author_avatar': author.avatar,
        'body':          comment.body,
        'created_at':    comment.created_at.isoformat(),
    }


def require_json_object():
    """Parse the request body and return a dict, or None on any failure.

    Returns None (not an exception) when:
      - the body is absent or empty (get_json returns None)
      - the body is not valid JSON (get_json returns None via silent=True)
      - the JSON root value is not an object (list, string, number, null)

    Callers must check for None and return a 400 immediately:
      data = require_json_object()
      if data is None:
          return jsonify({'error': 'Request body must be a JSON object'}), 400
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return data


# ── Strict data-URI validator ────────────────────────────────────────────────
# MIME allowlists derived from what the frontend actually produces.
# SVG, HTML, JS, and arbitrary image/* are explicitly excluded.
_AVATAR_MIME_ALLOWLIST = {'image/jpeg', 'image/png', 'image/webp'}

# ── Upload size limits ───────────────────────────────────────────────────────
# Frontend enforces 5 MB raw for all media types (upload-modal MAX_FILE_BYTES).
# Base64 encoding expands raw bytes by factor 4/3 (~33%).
# Encoded-string ceiling = raw * 4/3, rounded up to nearest MB.
#
# Category             Frontend   Raw limit   Encoded ceiling   Decoded limit
# Avatar image         5 MB *     2 MB        ~2.7 MB           2 MB
# Post image           5 MB       5 MB        ~6.7 MB           5 MB
# Post video           5 MB       5 MB        ~6.7 MB           5 MB
#
# * Avatar limit is tighter on the backend because profile images
#   do not need full-resolution quality; 2 MB decoded is generous.
#
_MAX_MEDIA_URL_BYTES        = 7 * 1024 * 1024   # fast-path: max encoded string (all media)
_MAX_AVATAR_DECODED_BYTES   = 2 * 1024 * 1024   # 2 MB decoded for avatar images
_MAX_POST_IMAGE_DECODED_BYTES = 5  * 1024 * 1024  # 5 MB for post images
_MAX_POST_VIDEO_DECODED_BYTES = 50 * 1024 * 1024  # 50 MB for post videos
# Convenience alias — used by validate_data_uri default parameter
_MAX_DECODED_BYTES = _MAX_POST_IMAGE_DECODED_BYTES

_DATA_URI_RE = re.compile(
    r'^data:([a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*/[a-zA-Z0-9][a-zA-Z0-9!#$&\-^_]*);base64,(.+)$',
    re.DOTALL,
)

# ── MIME / extension / signature table — single source of truth ──────────────
# video/ogg omitted: OGG signature cannot reliably distinguish video streams.
_MEDIA_SPEC: dict[str, dict] = {
    'image/jpeg':     {'extensions': {'.jpg', '.jpeg'}, 'signatures': [(0, b'\xff\xd8\xff')]},
    'image/png':      {'extensions': {'.png'},          'signatures': [(0, b'\x89PNG\r\n\x1a\n')]},
    'image/gif':      {'extensions': {'.gif'},          'signatures': [(0, b'GIF87a'), (0, b'GIF89a')]},
    # WebP needs BOTH offsets to match (RIFF header + WEBP marker)
    'image/webp':     {'extensions': {'.webp'},         'signatures': [(0, b'RIFF'), (8, b'WEBP')]},
    'video/mp4':      {'extensions': {'.mp4'},          'signatures': [(4, b'ftyp')]},
    'video/quicktime':{'extensions': {'.mov'},          'signatures': [(4, b'ftyp')]},
    'video/webm':     {'extensions': {'.webm'},         'signatures': [(0, b'\x1aE\xdf\xa3')]},
}
# These MIME types require ALL listed signature pairs to match (not just any one).
_SIGNATURES_ALL_REQUIRED = {'image/webp'}
# Legacy lookup used by _check_signature and validate_data_uri
_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    mime: spec['signatures'] for mime, spec in _MEDIA_SPEC.items()
}
# Derived allowlists — replace the old hardcoded sets
_POST_IMAGE_MIME_ALLOWLIST: frozenset[str] = frozenset(
    m for m in _MEDIA_SPEC if m.startswith('image/')
)
_POST_VIDEO_MIME_ALLOWLIST: frozenset[str] = frozenset(
    m for m in _MEDIA_SPEC if m.startswith('video/')
)

def _check_signature(mime_type: str, raw_bytes: bytes) -> bool:
    """Return True if raw_bytes match the magic-byte signature for mime_type.
    GIF / JPEG / PNG / MP4 / WebM: any listed pair matches.
    WebP (_SIGNATURES_ALL_REQUIRED): every listed pair must match.
    Returns True for MIME types not in _SIGNATURES (no check registered).
    """
    sigs = _SIGNATURES.get(mime_type)
    if not sigs:
        return True
    if mime_type in _SIGNATURES_ALL_REQUIRED:
        return all(
            len(raw_bytes) >= off + len(exp) and raw_bytes[off:off + len(exp)] == exp
            for off, exp in sigs
        )
    for offset, expected in sigs:
        end = offset + len(expected)
        if len(raw_bytes) >= end and raw_bytes[offset:end] == expected:
            return True
    return False


def validate_multipart_media(media_file, media_type: str, max_bytes: int):
    """Validate a multipart file upload with bounded read and signature check.

    Checks: MIME in allowlist → extension valid for MIME → bounded read
    (max_bytes+1 to detect oversize) → non-empty → within limit → magic bytes.

    Returns (mime_type: str, file_bytes: bytes) on success.
    Raises ValueError on format/extension/signature errors.
    Raises OverflowError when file exceeds max_bytes.
    """
    import os as _os
    if not media_file:
        raise ValueError('No media file supplied')

    mime_type = (media_file.mimetype or '').lower().strip()
    allowlist = (_POST_IMAGE_MIME_ALLOWLIST if media_type == 'image'
                 else _POST_VIDEO_MIME_ALLOWLIST)
    if not mime_type:
        raise ValueError('Media file has no MIME type')
    if mime_type not in allowlist:
        raise ValueError(
            f'Unsupported MIME type "{mime_type}". '
            f'Allowed: {", ".join(sorted(allowlist))}'
        )

    raw_filename = (media_file.filename or '').strip()
    if not raw_filename:
        raise ValueError('Filename is required')
    _, raw_ext = _os.path.splitext(raw_filename)
    ext = raw_ext.lower()
    if not ext:
        raise ValueError('Filename must include an extension (e.g. .jpg, .mp4)')
    allowed_exts = _MEDIA_SPEC[mime_type]['extensions']
    if ext not in allowed_exts:
        raise ValueError(
            f'Extension "{ext}" is not valid for MIME type "{mime_type}". '
            f'Expected: {", ".join(sorted(allowed_exts))}'
        )

    # Bounded read: read at most max_bytes+1 bytes.
    # This avoids loading a 2 GB file into memory before checking size.
    file_bytes = media_file.read(max_bytes + 1)

    if len(file_bytes) == 0:
        raise ValueError('Uploaded file is empty')
    if len(file_bytes) > max_bytes:
        raise OverflowError(
            f'File exceeds the {max_bytes // (1024 * 1024)} MB size limit'
        )
    if not _check_signature(mime_type, file_bytes):
        raise ValueError(
            f'File content does not match the declared type "{mime_type}". '
            'Ensure you are uploading the correct file.'
        )
    return mime_type, file_bytes


def validate_data_uri(value, mime_allowlist, max_decoded_bytes=_MAX_DECODED_BYTES):
    """Validate a data URI against an explicit MIME allowlist.

    Returns (mime_type, raw_bytes) on success.
    Raises ValueError with a user-safe message on any failure.
    Checks:
      1. Value is a string.
      2. Matches data:<mime>;base64,<payload> structure.
      3. MIME type is in the caller-supplied allowlist.
      4. Base64 payload decodes successfully (strict validation).
      5. Decoded payload is non-empty.
      6. Decoded byte length is within max_decoded_bytes.
      7. File signature (magic bytes) matches declared MIME type.
    The decoded bytes are produced once and reused for steps 5-7.
    """
    if not isinstance(value, str):
        raise ValueError('Media value must be a string')
    m = _DATA_URI_RE.match(value)
    if not m:
        raise ValueError('Media must be a valid base64 data URI (data:<mime>;base64,<data>)')
    mime_type = m.group(1).lower()
    if mime_type not in mime_allowlist:
        raise ValueError(
            f'Unsupported media type "{mime_type}". '
            f'Allowed: {", ".join(sorted(mime_allowlist))}'
        )
    b64_data = m.group(2)
    if not b64_data.strip():
        raise ValueError('Media payload is empty')
    try:
        raw_bytes = base64.b64decode(b64_data, validate=True)
    except Exception:
        raise ValueError('Media contains invalid base64 encoding')
    if len(raw_bytes) == 0:
        raise ValueError('Media payload decodes to empty content')
    if len(raw_bytes) > max_decoded_bytes:
        raise ValueError(
            f'Media exceeds the {max_decoded_bytes // (1024*1024)} MB size limit'
        )
    # File-signature check — uses already-decoded bytes, no second decode.
    if not _check_signature(mime_type, raw_bytes):
        raise ValueError(
            f'File content does not match the declared type "{mime_type}". '
            'Please ensure you are uploading the correct file type.'
        )
    return mime_type, raw_bytes

# ── Vault serializer ──────────────────────────────────────────────────────────
def _claimed_by_summary(user_id):
    """Return a safe public summary of the user who claimed a child vault."""
    if not user_id:
        return None
    user = User.query.get(user_id)
    if not user:
        return None
    return {
        'id':           str(user.id),
        'display_name': user.name,
        'avatar':       user.avatar,
    }


def serialize_vault(vault, vm, member_count, unread=0):
    """Return the canonical vault dict for API responses.

    Centralised so every vault endpoint returns the same shape.
    accent_color defaults to 'blue' when not set.
    """
    entry = {
        'id':           str(vault.id),
        'name':         vault.name,
        'created_by':   str(vault.created_by),
        'created_at':   vault.created_at.isoformat(),
        'member_count': member_count,
        'unread_count': unread,
        'user_role':    vm.role,
        'description':  vault.description or None,
        'accent_color': vault.accent_color or 'blue',
        'cover_url':    vault.cover_url or None,
        # Pass 21: child vault metadata
        'vault_type':          vault.vault_type or 'normal',
        'child_email':         vault.child_email or None,
        'claimed_at':          vault.claimed_at.isoformat() if vault.claimed_at else None,
        'claimed_by_user_id':  str(vault.claimed_by_user_id) if vault.claimed_by_user_id else None,
        # Safe claimed-user summary for dashboard display
        'claimed_by':          _claimed_by_summary(vault.claimed_by_user_id),
    }
    if vm.role == 'owner':
        entry['invite_code']  = vault.invite_code
        # Lazy-generate invite_token if vault pre-dates this feature
        if not vault.invite_token:
            vault.invite_token = generate_invite_token()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()  # concurrent write — next call will retry
        entry['invite_token'] = vault.invite_token
    return entry
