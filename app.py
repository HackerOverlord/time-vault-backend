from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, date
import os
import secrets
import hashlib
import base64
import random
import string
from datetime import timedelta
from sqlalchemy import or_, UniqueConstraint, func
import jwt                          
from functools import wraps  
from flask import g

app = Flask(__name__)

# --- INTEGRATION: THE BRIDGE ---
# Allows your v0 frontend to securely request data from this Flask backend
import re

CORS(app, supports_credentials=True, origins=[
    "http://localhost:3000",
    "https://time-vault-imhg.vercel.app",
    re.compile(r"https://time-vault-imhg.*\.vercel\.app"),
], allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])

# Add these configurations to ensure cookies are handled correctly over local dev
# Ensure this is also set for security
app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
)
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise ValueError("SECRET_KEY environment variable is not set")
app.config['SECRET_KEY'] = _secret_key

# HTTP-level upload ceiling — stops oversized requests at the Werkzeug layer
# before they reach application code, preventing unbounded memory use.
# Value: 50 MB video limit + 5 MB headroom for multipart framing overhead.
_MAX_CONTENT_LENGTH = 55 * 1024 * 1024   # 55 MB
app.config['MAX_CONTENT_LENGTH'] = _MAX_CONTENT_LENGTH
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///time_capsule.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'protected_media'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

# Werkzeug enforces this before the request body reaches application code.
# Any request exceeding 50 MB + overhead is rejected with 413 automatically.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 + 64 * 1024

db = SQLAlchemy(app)


@app.errorhandler(413)
def request_too_large(e):
    """Return a JSON 413 instead of Flask's default HTML page."""
    return jsonify({'error': 'Request too large. Maximum upload size is 50 MB for video and 5 MB for images.'}), 413




def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            g.user_id = data['user_id']  # ← use g instead of session
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# --- SECURITY & ENCRYPTION ---
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY environment variable is not set")

def encrypt_content(content):
    if not content: return b''
    key_bytes = ENCRYPTION_KEY.encode()
    content_bytes = content.encode()
    encrypted = bytearray()
    for i, byte in enumerate(content_bytes):
        encrypted.append(byte ^ key_bytes[i % len(key_bytes)])
    return base64.b64encode(encrypted)

def decrypt_content(encrypted_content):
    if not encrypted_content: return ''
    try:
        key_bytes = ENCRYPTION_KEY.encode()
        encrypted_bytes = base64.b64decode(encrypted_content)
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key_bytes[i % len(key_bytes)])
        return decrypted.decode()
    except Exception:
        return str(encrypted_content)
    

def generate_lineage_code():
    digits = ''.join(random.choices(string.digits, k=4))
    letters = ''.join(random.choices(string.ascii_uppercase, k=2))
    return f"FAM-{digits}-{letters}"

def ensure_user_has_family(user):
    if user.family_id is None:
        family = Family(lineage_code=generate_lineage_code())
        db.session.add(family)
        db.session.flush()  # so family.id is available before commit
        user.family_id = family.id
        db.session.commit()
    return user.family_id


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
            'unlock_at':     post.unlock_at.isoformat(),
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
        'unlock_at':     post.unlock_at.isoformat() if post.unlock_at else None,
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


# --- V1 ROUTES: VAULTS (3A) ---



# ── Email helper — Pass 21 ───────────────────────────────────────────────────
# Uses Python's smtplib so no additional library is required.
# All SMTP settings come from environment variables; email sending is a no-op
# when SMTP_HOST is unset (useful in development and CI).
#
# SMTP_HOST         — SMTP relay hostname (e.g. smtp.sendgrid.net)
# SMTP_PORT         — port (default 587)
# SMTP_USER         — login username
# SMTP_PASSWORD     — login password
# SMTP_FROM         — From address shown to recipients
# APP_BASE_URL      — public base URL, e.g. https://time-vault.vercel.app
#
# When TESTING=true the function is bypassed (tests mock it directly).

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


def _send_email(to_address: str, subject: str, body_text: str, body_html: str = '') -> None:
    """Send a transactional email via SMTP.

    Silently skips when SMTP_HOST is not configured (development mode).
    Raises on SMTP errors in production so callers can handle failures.
    """
    smtp_host = os.environ.get('SMTP_HOST', '')
    if not smtp_host:
        app.logger.debug('SMTP_HOST not set — skipping email to %s', to_address)
        return

    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pw   = os.environ.get('SMTP_PASSWORD', '')
    from_addr = os.environ.get('SMTP_FROM', smtp_user or 'noreply@example.com')

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = from_addr
    msg['To']      = to_address
    msg.attach(MIMEText(body_text, 'plain'))
    if body_html:
        msg.attach(MIMEText(body_html, 'html'))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pw)
        server.sendmail(from_addr, [to_address], msg.as_string())


def send_claim_invite_email(
    to_email: str,
    child_email: str,
    vault_name: str,
    creator_name: str,
    raw_token: str,
    expires_at,
) -> None:
    """Build and send the child-vault claim invitation email.

    The plaintext token is embedded in the claim URL only — it is never
    returned in API JSON responses (except when EXPOSE_CLAIM_TOKEN=true,
    which is for development and test environments only).
    """
    base_url   = os.environ.get('APP_BASE_URL', 'https://time-vault.vercel.app')
    claim_url  = f'{base_url}/?screen=claim&token={raw_token}'
    expiry_str = expires_at.strftime('%B %d, %Y') if hasattr(expires_at, 'strftime') else str(expires_at)

    subject    = f'{creator_name} created a Time Vault for you'
    body_text  = (
        f'Hi!\n\n'
        f'{creator_name} has created a Time Vault called "{vault_name}" for you.\n\n'
        f'Click the link below to claim your vault and access all the memories inside:\n'
        f'{claim_url}\n\n'
        f'This invitation expires on {expiry_str}.\n\n'
        f'— Time Vault'
    )
    body_html = (
        f'<p>Hi!</p>'
        f'<p><strong>{creator_name}</strong> has created a Time Vault called '
        f'<strong>\"{vault_name}\"</strong> for you.</p>'
        f'<p><a href="{claim_url}">Click here to claim your vault</a></p>'
        f'<p>Invitation expires: {expiry_str}</p>'
    )
    _send_email(to_email, subject, body_text, body_html)

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
        entry['invite_code'] = vault.invite_code
    return entry


@app.route('/api/vaults', methods=['GET'])
@token_required
def get_vaults():
    memberships = VaultMember.query.filter_by(user_id=g.user_id).all()
    result = []
    for vm in memberships:
        vault = Vault.query.get(vm.vault_id)
        if not vault:
            continue
        member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
        if vm.last_seen_at:
            unread = Post.query.filter(
                Post.vault_id == vault.id,
                Post.is_unlocked == True,
                Post.posted_at > vm.last_seen_at,
                Post.author_id != g.user_id
            ).count()
        else:
            unread = 0
        result.append(serialize_vault(vault, vm, member_count, unread))
    return jsonify(result), 200


@app.route('/api/vaults', methods=['POST'])
@token_required
def create_vault():
    import re as _re
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Vault name is required'}), 400
    if len(name) > 50:
        return jsonify({'error': 'Vault name cannot exceed 50 characters'}), 400

    vault_type  = data.get('vault_type', 'normal')
    child_email = (data.get('child_email') or '').strip().lower() or None

    if vault_type not in ('normal', 'child'):
        return jsonify({'error': "vault_type must be 'normal' or 'child'"}), 400

    if vault_type == 'child':
        if not child_email:
            return jsonify({'error': 'child_email is required for a child vault'}), 400
        # Basic email format check
        if not _re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', child_email):
            return jsonify({'error': 'child_email is not a valid email address'}), 400
        # Uniqueness: no other unclaimed child vault with this email
        existing = Vault.query.filter_by(
            vault_type='child', child_email=child_email, claimed_at=None
        ).first()
        if existing:
            return jsonify({'error': 'An unclaimed child vault with that email already exists'}), 409

    now = datetime.utcnow()
    vault = Vault(
        name=name,
        invite_code=generate_invite_code(),
        created_by=g.user_id,
        created_at=now,
        vault_type=vault_type,
        child_email=child_email if vault_type == 'child' else None,
    )
    db.session.add(vault)
    db.session.flush()  # vault.id available before commit
    owner = VaultMember(
        vault_id=vault.id,
        user_id=g.user_id,
        role='owner',
        joined_at=now,
        invited_by=None,
        last_seen_at=now,
    )
    db.session.add(owner)
    db.session.commit()
    return jsonify(serialize_vault(vault, owner, 1, 0)), 201


@app.route('/api/vaults/<int:vault_id>', methods=['DELETE'])
@token_required
def delete_vault_v1(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    confirm = (request.get_json() or {}).get('confirm_name', '')
    if confirm != vault.name:
        return jsonify({'error': 'Vault name confirmation does not match'}), 400
    # Notify non-owner members before deletion
    members = VaultMember.query.filter_by(vault_id=vault_id).all()
    owner = User.query.get(g.user_id)
    for m in members:
        if m.user_id != g.user_id:
            create_notification(
                m.user_id, 'vault_deleted',
                f'{owner.name} deleted {vault.name}',
                vault_id=vault.id,
            )
    # Hard-delete all content in dependency order
    vault_posts = Post.query.filter_by(vault_id=vault_id).all()
    post_ids = [p.id for p in vault_posts]
    media_urls = [p.media_url for p in vault_posts if p.media_url]  # collect before delete
    if post_ids:
        PostLike.query.filter(PostLike.post_id.in_(post_ids)).delete(synchronize_session=False)
        PostComment.query.filter(PostComment.post_id.in_(post_ids)).delete(synchronize_session=False)
    Post.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    VaultMember.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    db.session.delete(vault)
    db.session.commit()
    for url in media_urls:  # fire-and-forget R2 cleanup after commit
        delete_object(url)
    return '', 204


@app.route('/api/vaults/<int:vault_id>', methods=['PUT'])
@token_required
def update_vault(vault_id):
    """Update vault metadata. Owner only.

    Accepted fields (all optional; absent field is unchanged):
        name         str  max 50 chars
        description  str  max 160 chars; null/empty string clears it
        accent_color str  one of the allowed palette values
        cover_url    str  R2 URL from POST /cover; null clears it
    """
    _ACCENT_ALLOWED = {'blue', 'green', 'purple', 'orange', 'rose', 'slate'}

    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    vm = require_vault_owner(vault_id)
    if isinstance(vm, tuple):
        return vm

    data = request.get_json(silent=True) or {}

    # ── name ─────────────────────────────────────────────────────────────────
    if 'name' in data:
        name = (data['name'] or '').strip()
        if not name:
            return jsonify({'error': 'Vault name is required'}), 400
        if len(name) > 50:
            return jsonify({'error': 'Vault name cannot exceed 50 characters'}), 400
        vault.name = name

    # ── description ──────────────────────────────────────────────────────────
    if 'description' in data:
        desc = (data['description'] or '').strip()
        if len(desc) > 160:
            return jsonify({'error': 'Description cannot exceed 160 characters'}), 400
        vault.description = desc or None  # empty string stored as NULL

    # ── accent_color ─────────────────────────────────────────────────────────
    if 'accent_color' in data:
        color = data['accent_color']
        if color is not None and color not in _ACCENT_ALLOWED:
            return jsonify({'error': f'Invalid accent color. Allowed: {", ".join(sorted(_ACCENT_ALLOWED))}'}), 400
        vault.accent_color = color

    # ── cover_url ─────────────────────────────────────────────────────────────
    # Only R2 URLs or null accepted here; raw uploads use POST /cover.
    if 'cover_url' in data:
        new_cover = data['cover_url']
        if new_cover is not None and not is_r2_url(new_cover):
            return jsonify({'error': 'cover_url must be an R2 URL or null'}), 400
        old_cover = vault.cover_url
        vault.cover_url = new_cover
        if old_cover and is_r2_url(old_cover) and old_cover != new_cover:
            db.session.commit()
            delete_object(old_cover)
            member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
            return jsonify(serialize_vault(vault, vm, member_count)), 200

    db.session.commit()
    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    return jsonify(serialize_vault(vault, vm, member_count)), 200


@app.route('/api/vaults/<int:vault_id>/cover', methods=['POST'])
@token_required
def upload_vault_cover(vault_id):
    """Upload a vault cover image via multipart/form-data. Owner only.

    Field name: 'cover'
    Returns: { cover_url: <str> }
    """
    _COVER_MIMES = {'image/jpeg', 'image/png', 'image/webp'}
    _MAX_COVER_BYTES = 5 * 1024 * 1024  # 5 MB

    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    vm = require_vault_owner(vault_id)
    if isinstance(vm, tuple):
        return vm

    file = request.files.get('cover')
    if not file:
        return jsonify({'error': 'No file uploaded (field name: cover)'}), 400

    mime_type = file.mimetype or ''
    if mime_type not in _COVER_MIMES:
        return jsonify({'error': 'Unsupported file type. Allowed: jpeg, png, webp'}), 400

    file_bytes = file.read()
    if len(file_bytes) == 0:
        return jsonify({'error': 'Uploaded file is empty'}), 400
    if len(file_bytes) > _MAX_COVER_BYTES:
        return jsonify({'error': 'Cover image must be under 5 MB'}), 413

    try:
        new_url = upload_file_object(file_bytes, prefix='covers', mime_type=mime_type)
    except Exception as exc:
        return jsonify({'error': f'Upload failed: {exc}'}), 503

    old_cover = vault.cover_url
    vault.cover_url = new_url
    db.session.commit()

    if old_cover and is_r2_url(old_cover):
        delete_object(old_cover)

    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    return jsonify(serialize_vault(vault, vm, member_count)), 200


# ── Pass 21: Child vault claim-invite ────────────────────────────────────────
@app.route('/api/vaults/<int:vault_id>/claim-invite', methods=['POST'])
@token_required
def create_claim_invite(vault_id):
    """Generate a secure single-use claim token for a child vault.  Owner only."""
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    if vault.vault_type != 'child':
        return jsonify({'error': 'Only child vaults support claim invites'}), 400
    if vault.claimed_at:
        return jsonify({'error': 'Vault has already been claimed'}), 409
    vm = require_vault_owner(vault_id)
    if isinstance(vm, tuple):
        return vm

    # Invalidate all previous tokens for this vault
    ChildClaimToken.query.filter_by(vault_id=vault_id, used=False).update({'used': True})

    raw_token  = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.utcnow() + timedelta(days=30)

    token_row = ChildClaimToken(
        vault_id=vault_id,
        token_hash=token_hash,
        created_by=g.user_id,
        expires_at=expires_at,
    )
    db.session.add(token_row)
    db.session.commit()

    create_notification(
        g.user_id,
        'claim_invite_created',
        f'Claim invitation created for "{vault.name}". Link expires in 30 days.',
        vault_id=vault.id,
    )

    # Send the invitation email (no-op when SMTP_HOST is not configured)
    email_error = None
    if vault.child_email:
        try:
            creator = User.query.get(g.user_id)
            send_claim_invite_email(
                to_email=vault.child_email,
                child_email=vault.child_email,
                vault_name=vault.name,
                creator_name=creator.name if creator else 'Your family',
                raw_token=raw_token,
                expires_at=expires_at,
            )
        except Exception as exc:
            app.logger.error('Failed to send claim invite email: %s', exc)
            email_error = str(exc)

    # The plaintext token is NOT returned in normal responses.
    # It is embedded only in the email sent to child_email.
    # EXPOSE_CLAIM_TOKEN=true enables it for development / test environments.
    response: dict = {
        'expires_at':  expires_at.isoformat(),
        'vault_name':  vault.name,
        'child_email': vault.child_email,
        'sent':        email_error is None,
    }
    if os.environ.get('EXPOSE_CLAIM_TOKEN') == 'true':
        response['token'] = raw_token
    if email_error:
        response['email_error'] = email_error
    return jsonify(response), 201


# ── Pass 21: Claim info endpoint (token validation preview) ─────────────────
@app.route('/api/claim-info', methods=['GET'])
def get_claim_info():
    """Return vault info for a claim token without consuming it.
    Used by ClaimScreen to render the vault name and expiry before the user
    fills in the form.  Does NOT mark the token used.
    """
    raw_token = request.args.get('token', '').strip()
    if not raw_token:
        return jsonify({'error': 'token query param required'}), 400

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_row  = ChildClaimToken.query.filter_by(token_hash=token_hash).first()

    if not token_row:
        return jsonify({'error': 'Invalid claim token'}), 404
    if token_row.used:
        return jsonify({'error': 'Claim token has already been used'}), 410
    if token_row.expires_at < datetime.utcnow():
        return jsonify({'error': 'Claim token has expired'}), 410

    vault = Vault.query.get(token_row.vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    if vault.claimed_at:
        return jsonify({'error': 'Vault already claimed'}), 409

    creator = User.query.get(vault.created_by)
    return jsonify({
        'vault_name': vault.name,
        'created_by': creator.name if creator else 'Unknown',
        'expires_at': token_row.expires_at.isoformat(),
        'child_email': vault.child_email,
    }), 200


# ── Pass 21: Claim vault endpoint ────────────────────────────────────────────
@app.route('/api/claim-vault', methods=['POST'])
def claim_vault():
    """Claim a child vault using a one-time token.

    Input JSON:
        token        (required) — the raw claim token from the invite link
        display_name (optional) — name for new accounts
        password     (required when creating a new account)

    Flow:
        1. Validate token (exists, not used, not expired).
        2. If no authenticated user and child_email has no account → create one.
        3. If no authenticated user but account exists → require authentication.
        4. Email must match vault.child_email.
        5. Add claimer as vault member (role=owner).
        6. Mark vault as claimed.
        7. Notify parent and child.
    """
    import re as _re
    from werkzeug.security import generate_password_hash, check_password_hash

    data = request.get_json(silent=True) or {}
    raw_token = (data.get('token') or '').strip()
    if not raw_token:
        return jsonify({'error': 'token is required'}), 400

    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    token_row  = ChildClaimToken.query.filter_by(token_hash=token_hash).first()

    if not token_row:
        return jsonify({'error': 'Invalid claim token'}), 404
    if token_row.used:
        return jsonify({'error': 'Claim token has already been used'}), 410
    if token_row.expires_at < datetime.utcnow():
        return jsonify({'error': 'Claim token has expired'}), 410

    vault = Vault.query.get(token_row.vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    if vault.claimed_at:
        return jsonify({'error': 'Vault has already been claimed'}), 409

    child_email = (vault.child_email or '').lower()

    # Determine the claiming user
    # Option A: token provided in Authorization header → authenticated user
    auth_header = request.headers.get('Authorization', '')
    claimer = None
    if auth_header.startswith('Bearer '):
        try:
            import jwt as _jwt
            payload = _jwt.decode(
                auth_header[7:], app.config['SECRET_KEY'], algorithms=['HS256']
            )
            claimer = User.query.get(payload['user_id'])
        except Exception:
            pass

    if claimer:
        # Authenticated path: email must match
        if child_email and claimer.email.lower() != child_email:
            return jsonify({'error': 'Authenticated account email does not match invitation'}), 403
    else:
        # Unauthenticated path: create or locate the account
        if not child_email:
            return jsonify({'error': 'Cannot create account: child_email not set on vault'}), 400
        claimer = User.query.filter_by(email=child_email).first()
        if claimer:
            # Account exists but user is not authenticated
            password = data.get('password', '')
            if not check_password_hash(claimer.password_hash, password):
                return jsonify({'error': 'Password incorrect for existing account'}), 401
        else:
            # Create new account
            password     = data.get('password', '')
            display_name = (data.get('display_name') or '').strip()
            if not password or len(password) < 8:
                return jsonify({'error': 'Password must be at least 8 characters'}), 400
            if not display_name:
                return jsonify({'error': 'display_name is required for new accounts'}), 400
            claimer = User(
                name=display_name,
                email=child_email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(claimer)
            db.session.flush()  # get claimer.id

    now = datetime.utcnow()

    # Add claimer as owner (idempotent)
    existing_member = VaultMember.query.filter_by(
        vault_id=vault.id, user_id=claimer.id
    ).first()
    if not existing_member:
        db.session.add(VaultMember(
            vault_id=vault.id, user_id=claimer.id,
            role='owner', joined_at=now,
        ))

    # Mark vault as claimed
    vault.claimed_at         = now
    vault.claimed_by_user_id = claimer.id

    # Invalidate the token (single-use)
    token_row.used = True

    db.session.commit()

    # Notify parent
    parent = User.query.get(vault.created_by)
    if parent:
        create_notification(
            parent.id,
            'vault_claimed',
            f'{claimer.name} has claimed the vault "{vault.name}".',
            vault_id=vault.id,
        )
    # Notify child
    create_notification(
        claimer.id,
        'vault_claimed',
        f'Welcome! You now have access to "{vault.name}".',
        vault_id=vault.id,
    )

    # Return a JWT for newly created accounts
    response: dict = {'claimed': True, 'vault_id': str(vault.id)}
    if not auth_header.startswith('Bearer '):
        import jwt as _jwt
        token_str = _jwt.encode(
            {'user_id': claimer.id},
            app.config['SECRET_KEY'],
            algorithm='HS256',
        )
        response['access_token'] = token_str

    return jsonify(response), 200


# ── Pass 22: Legacy base64 migration utility ─────────────────────────────────
# This endpoint can be called to migrate a single post's base64 media_url to R2.
# Run as a one-off background job; never runs automatically.
@app.route('/api/posts/<int:post_id>/migrate-media', methods=['POST'])
@token_required
def migrate_post_media(post_id):
    """Migrate a single legacy base64 post to R2.  Vault-owner utility.

    Idempotent: if the post already has an R2 URL, returns immediately.
    Callers should implement their own rate-limiting and retry logic.
    """
    # Only vault owners can trigger migration of their vault's posts.
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    vm = require_vault_owner(post.vault_id)
    if isinstance(vm, tuple):
        return vm

    if not post.media_url:
        return jsonify({'migrated': False, 'reason': 'no media_url'}), 200
    if is_r2_url(post.media_url):
        return jsonify({'migrated': False, 'reason': 'already migrated'}), 200
    if not post.media_url.startswith('data:'):
        return jsonify({'migrated': False, 'reason': 'unrecognised URL scheme'}), 200

    try:
        new_url = upload_media(post.media_url, prefix='posts')
    except Exception as exc:
        return jsonify({'error': f'Migration failed: {exc}'}), 503

    post.media_url = new_url
    db.session.commit()
    return jsonify({'migrated': True, 'media_url': new_url}), 200


@app.route('/api/vaults/<int:vault_id>/seen', methods=['POST'])
@token_required
def mark_vault_seen(vault_id):
    if not Vault.query.get(vault_id):
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_member(vault_id)
    if isinstance(result, tuple):
        return result
    result.last_seen_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'last_seen_at': result.last_seen_at.isoformat()}), 200


# --- V1 ROUTES: MEMBERSHIP + INVITES (3B) ---

@app.route('/api/vaults/<int:vault_id>/members', methods=['GET'])
@token_required
def get_vault_members(vault_id):
    if not Vault.query.get(vault_id):
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_member(vault_id)
    if isinstance(result, tuple):
        return result
    members = VaultMember.query.filter_by(vault_id=vault_id).all()
    out = []
    for m in members:
        user = User.query.get(m.user_id)
        if not user:
            continue
        out.append({
            'user_id':   str(user.id),
            'name':      user.name,
            'avatar':    user.avatar if hasattr(user, 'avatar') else None,
            'role':      m.role,
            'joined_at': m.joined_at.isoformat(),
        })
    return jsonify(out), 200


@app.route('/api/vaults/<int:vault_id>/members/<int:user_id>', methods=['DELETE'])
@token_required
def remove_vault_member(vault_id, user_id):
    if not Vault.query.get(vault_id):
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    if user_id == g.user_id:
        return jsonify({'error': 'Vault owners cannot remove themselves. Delete the vault instead.'}), 400
    target = VaultMember.query.filter_by(vault_id=vault_id, user_id=user_id).first()
    if not target:
        return jsonify({'error': 'Member not found in this vault'}), 404
    vault_obj = Vault.query.get(vault_id)
    db.session.delete(target)
    db.session.flush()  # apply delete so removed user is no longer a member
    # Notify the removed user — vault_id intentionally null so they cannot
    # navigate into a vault they no longer have access to.
    create_notification(
        user_id,
        'member_removed',
        f'You were removed from {vault_obj.name}.',
        vault_id=None,
    )
    db.session.commit()
    return '', 204


@app.route('/api/vaults/<int:vault_id>/leave', methods=['DELETE'])
@token_required
def leave_vault(vault_id):
    if not Vault.query.get(vault_id):
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_member(vault_id)
    if isinstance(result, tuple):
        return result
    vm = result
    if vm.role == 'owner':
        return jsonify({'error': 'Vault owners cannot leave. Delete the vault instead.'}), 400
    vault_obj = Vault.query.get(vault_id)
    leaver    = User.query.get(g.user_id)
    db.session.delete(vm)
    # Notify remaining vault members (owner + others) that this member left.
    # At this point the VaultMember row is staged for deletion but not yet
    # flushed, so VaultMember.query still includes all members except vm
    # after we flush the delete.
    db.session.flush()  # apply the delete so leaver is no longer a member
    notify_vault_members(
        vault_id, 'member_left',
        f'{leaver.name} left {vault_obj.name}.',
        exclude_user_ids=[g.user_id],  # leaver does not notify themselves
    )
    db.session.commit()
    return '', 204


@app.route('/api/vaults/join', methods=['POST'])
@token_required
def join_vault():
    data = request.get_json() or {}
    raw = data.get('invite_code', '')
    # Normalise: strip whitespace, remove hyphens, uppercase
    normalised = raw.replace('-', '').replace(' ', '').upper()
    # Reject before querying unless exactly 6 alphanumeric characters
    if len(normalised) != 6 or not normalised.isalnum():
        return jsonify({'error': 'Invalid invite code'}), 400
    vault = Vault.query.filter_by(invite_code=normalised).first()
    if not vault:
        return jsonify({'error': 'Invalid invite code'}), 400
    existing = VaultMember.query.filter_by(
        vault_id=vault.id, user_id=g.user_id
    ).first()
    if existing:
        return jsonify({'error': 'You are already a member of this vault'}), 400
    now = datetime.utcnow()
    member = VaultMember(
        vault_id=vault.id,
        user_id=g.user_id,
        role='member',
        joined_at=now,
        invited_by=None,
        last_seen_at=now,
    )
    # Query existing members BEFORE adding the new member to avoid autoflush
    # including the joiner in the "someone joined" loop.
    joiner = User.query.get(g.user_id)
    existing_members = VaultMember.query.filter_by(vault_id=vault.id).all()
    db.session.add(member)
    # Notify existing vault members (owner + others) that someone joined.
    for em in existing_members:
        create_notification(
            em.user_id,
            'member_joined',
            f'{joiner.name} joined {vault.name}.',
            vault_id=vault.id,
        )
    # Confirmation notification for the joiner themselves.
    create_notification(
        g.user_id,
        'member_joined',
        f'You joined {vault.name}.',
        vault_id=vault.id,
    )
    db.session.commit()
    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    new_vm = VaultMember.query.filter_by(
        vault_id=vault.id, user_id=g.user_id
    ).first()
    return jsonify({
        'vault':   serialize_vault(vault, new_vm, member_count),
        'message': f'You joined {vault.name}',
    }), 201


@app.route('/api/vaults/<int:vault_id>/invite/regenerate', methods=['POST'])
@token_required
def regenerate_invite_code(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    vault.invite_code = generate_invite_code()
    db.session.commit()
    return jsonify({'invite_code': vault.invite_code}), 200


# --- V1 ROUTES: POSTS (3C) ---

_ALLOWED_MEDIA_TYPES = {'video', 'image', 'text'}

# Fields the frontend currently sends that do not exist in the V1 backend contract.
# Rejected explicitly so Milestone 4 wiring mistakes surface immediately.
_REJECTED_POST_FIELDS = {'group_id', 'recipient_ids'}


@app.route('/api/posts', methods=['GET'])
@token_required
def get_posts():
    vault_id_param = request.args.get('vault_id', type=int)

    if vault_id_param is not None:
        # Explicit vault filter: must exist, requester must be a member.
        if not Vault.query.get(vault_id_param):
            return jsonify({'error': 'Vault not found'}), 404
        result = require_vault_member(vault_id_param)
        if isinstance(result, tuple):
            return result
        # Scope to this single vault.
        member_vault_ids = [vault_id_param]
    else:
        member_vault_ids = [
            vm.vault_id
            for vm in VaultMember.query.filter_by(user_id=g.user_id).all()
        ]

    if not member_vault_ids:
        return jsonify([]), 200

    # Pass 23: archive + search
    show_archived = request.args.get("archived", "").lower() == "true"
    raw_q = (request.args.get("q") or "").strip()
    if len(raw_q) > 200:
        return jsonify({"error": "Search query must be 200 characters or fewer"}), 400
    search_q = raw_q
    _ord = db.case((Post.is_unlocked == True, Post.posted_at), else_=Post.created_at).desc()
    _base = [
        Post.vault_id.in_(member_vault_ids),
        Post.is_archived == (True if show_archived else False),
        db.or_(Post.is_unlocked == True, Post.unlock_at.isnot(None)),
    ]
    if search_q:
        _like = f"%{search_q}%"
        posts = (
            Post.query.join(User, Post.author_id == User.id)
            .join(Vault, Post.vault_id == Vault.id)
            .filter(*_base)
            .filter(db.or_(
                db.and_(Post.is_unlocked == True, Post.caption.ilike(_like)),
                Vault.name.ilike(_like), User.name.ilike(_like)
            ))
            .order_by(_ord).all()
        )
    else:
        posts = (
            Post.query
            .filter(*_base)
            .order_by(_ord).all()
        )

    if not posts:
        return jsonify([]), 200

    # Bulk liked-post lookup — one query regardless of feed size.
    post_ids = [p.id for p in posts]
    liked_set = {
        pl.post_id
        for pl in PostLike.query.filter(
            PostLike.post_id.in_(post_ids),
            PostLike.user_id == g.user_id,
        ).all()
    }

    # Author and vault rows — keyed by id to avoid per-post queries.
    author_ids = {p.author_id for p in posts}
    vault_ids  = {p.vault_id  for p in posts}
    authors = {u.id: u for u in User.query.filter(User.id.in_(author_ids)).all()}
    vaults  = {v.id: v for v in Vault.query.filter(Vault.id.in_(vault_ids)).all()}

    result = []
    for post in posts:
        author = authors.get(post.author_id)
        vault  = vaults.get(post.vault_id)
        if not author or not vault:
            continue
        result.append(serialize_post(post, author, vault, liked_set))

    return jsonify(result), 200


@app.route('/api/vaults/<int:vault_id>/posts', methods=['POST'])
@token_required
def create_post(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404

    result = require_vault_member(vault_id)
    if isinstance(result, tuple):
        return result

    # Accept both multipart/form-data (new binary flow) and application/json
    # (legacy base64 flow). Both paths produce the same internal variables.
    is_multipart = request.content_type and 'multipart' in request.content_type
    if is_multipart:
        data       = request.form
        media_file = request.files.get('media')
    else:
        data       = request.get_json() or {}
        media_file = None

    # Reject legacy frontend fields explicitly.
    rejected = _REJECTED_POST_FIELDS & set(data.keys())
    if rejected:
        return jsonify({
            'error': f'Unsupported field(s): {", ".join(sorted(rejected))}. '
                     f'Use POST /api/vaults/<id>/posts with vault_id in the URL.'
        }), 400

    media_type = (data.get('media_type') or '').strip()
    if media_type not in _ALLOWED_MEDIA_TYPES:
        return jsonify({'error': 'media_type must be one of: video, image, text'}), 400

    caption   = data.get('caption')
    # For multipart uploads media_url comes from the file; for JSON it is in the body.
    media_url = None if is_multipart else data.get('media_url')

    # Caption validation.
    if media_type == 'text':
        if not caption or not str(caption).strip():
            return jsonify({'error': 'Caption is required for text posts'}), 400
    if caption and len(str(caption)) > 500:
        return jsonify({'error': 'Caption cannot exceed 500 characters'}), 400

    # Media validation — two paths:
    #   Multipart: file is in request.files["media"] as binary bytes.
    #   JSON (legacy): media_url is a base64 data URI validated by validate_data_uri.
    if media_type in ('image', 'video'):
        if is_multipart:
            max_bytes = (_MAX_POST_IMAGE_DECODED_BYTES if media_type == 'image'
                         else _MAX_POST_VIDEO_DECODED_BYTES)
            try:
                mime_type, file_bytes = validate_multipart_media(
                    media_file, media_type, max_bytes
                )
            except OverflowError as exc:
                return jsonify({'error': str(exc)}), 413
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            try:
                media_url = upload_post_media(
                    file_bytes,
                    filename=media_file.filename or '',
                    mime_type=mime_type,
                )
            except Exception as exc:
                return jsonify({'error': f'Media upload failed: {exc}'}), 503
        else:
            # JSON / legacy base64 path.
            if not media_url:
                return jsonify({'error':
                    f'media_url is required for {media_type} posts'}), 400
            if len(str(media_url)) > _MAX_MEDIA_URL_BYTES:
                return jsonify({'error': 'File too large. Maximum upload size is 5 MB.'}), 413
            allowlist  = (_POST_IMAGE_MIME_ALLOWLIST if media_type == 'image'
                          else _POST_VIDEO_MIME_ALLOWLIST)
            max_bytes  = (_MAX_POST_IMAGE_DECODED_BYTES if media_type == 'image'
                          else _MAX_POST_VIDEO_DECODED_BYTES)
            try:
                validate_data_uri(str(media_url), allowlist,
                                  max_decoded_bytes=max_bytes)
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
    else:  # text
        if is_multipart and media_file:
            return jsonify({'error': 'media file must not be provided for text posts'}), 400
        if not is_multipart and media_url is not None:
            return jsonify({'error': 'media_url must be null or absent for text posts'}), 400

    # unlock_at validation.
    unlock_at_str = data.get('unlock_at')
    unlock_dt = None
    if unlock_at_str:
        try:
            unlock_dt = datetime.fromisoformat(str(unlock_at_str))
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid unlock date format'}), 400
        if unlock_dt.date() <= date.today():
            return jsonify({'error': 'Unlock date must be at least tomorrow'}), 400

    now = datetime.utcnow()

    # For multipart uploads, media_url is already the R2 URL (set above).
    # For JSON/legacy uploads, base64 data URIs are uploaded here.
    # Existing R2 URLs (e.g. from a retry) pass through unchanged.
    stored_media_url = media_url
    if not is_multipart and media_url and not is_r2_url(media_url):
        try:
            stored_media_url = upload_media(media_url, prefix='posts')
        except Exception as exc:
            return jsonify({'error': f'Media upload failed: {exc}'}), 503

    if unlock_dt:
        # Time capsule post.
        post = Post(
            vault_id=vault_id,
            author_id=g.user_id,
            caption=caption,
            media_type=media_type,
            media_url=stored_media_url,
            unlock_at=unlock_dt,
            is_unlocked=False,
            posted_at=None,
            created_at=now,
        )
    else:
        # Immediate post.
        post = Post(
            vault_id=vault_id,
            author_id=g.user_id,
            caption=caption,
            media_type=media_type,
            media_url=stored_media_url,
            unlock_at=None,
            is_unlocked=True,
            posted_at=now,
            created_at=now,
        )

    db.session.add(post)
    db.session.commit()

    # Notify other vault members about the new post / capsule.
    # Notifications are created in a separate transaction so a notification
    # failure never rolls back the successfully created post.
    try:
        author_user = User.query.get(g.user_id)
        if post.is_unlocked:
            # Unlocked post — describe the media type
            media_label = {'image': 'photo', 'video': 'video'}.get(
                post.media_type, 'text memory'
            )
            notif_message = (
                f'{author_user.name} shared a new {media_label} in {vault.name}.'
            )
            notif_type = 'new_post'
        else:
            # Locked time-capsule — do NOT reveal caption or media
            notif_message = (
                f'{author_user.name} added a Time Capsule to {vault.name}.'
            )
            notif_type = 'new_post'
        notify_vault_members(
            vault_id, notif_type, notif_message,
            exclude_user_ids=[g.user_id],
        )
        db.session.commit()
    except Exception:
        db.session.rollback()  # notifications failed; post is already committed

    author = User.query.get(g.user_id)
    return jsonify(serialize_post(post, author, vault, set())), 201


# ── Pass 23: Archive/unarchive ──────────────────────────────────────────────
@app.route('/api/posts/<int:post_id>/archive', methods=['POST'])
@token_required
def archive_post(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    vm = VaultMember.query.filter_by(vault_id=post.vault_id, user_id=g.user_id).first()
    if post.author_id != g.user_id and (not vm or vm.role != 'owner'):
        return jsonify({'error': 'Only the post author or vault owner can archive this post'}), 403
    data = request.get_json(silent=True) or {}
    flag = data.get('archived')
    if not isinstance(flag, bool):
        return jsonify({'error': '"archived" must be a boolean'}), 400
    post.is_archived = flag
    db.session.commit()
    author = User.query.get(post.author_id)
    vault  = Vault.query.get(post.vault_id)
    liked  = {pl.post_id for pl in PostLike.query.filter_by(user_id=g.user_id).all()}
    return jsonify(serialize_post(post, author, vault, liked)), 200


@app.route('/api/posts/<int:post_id>', methods=['DELETE'])
@token_required
def delete_post(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404

    # Confirm vault membership before revealing any post details.
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    vm = result

    if post.author_id != g.user_id and vm.role != 'owner':
        return vault_forbidden('You do not have permission to delete this post')

    # Hard-delete dependents before the post itself.
    PostLike.query.filter_by(post_id=post_id).delete(synchronize_session=False)
    PostComment.query.filter_by(post_id=post_id).delete(synchronize_session=False)
    media_to_delete = post.media_url  # capture before delete
    db.session.delete(post)
    db.session.commit()
    delete_object(media_to_delete)  # fire-and-forget; never blocks commit
    return '', 204


# --- V1 ROUTES: LIKES + COMMENTS (3D) ---

@app.route('/api/posts/<int:post_id>/like', methods=['POST'])
@token_required
def like_post(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    if not post.is_unlocked:
        return jsonify({'error': 'Cannot like a locked time capsule'}), 403
    existing = PostLike.query.filter_by(post_id=post_id, user_id=g.user_id).first()
    if existing:
        return jsonify({'error': 'You have already liked this post'}), 400
    like = PostLike(post_id=post_id, user_id=g.user_id, created_at=datetime.utcnow())
    post.like_count += 1
    db.session.add(like)
    # Notify post author — but not if they liked their own post.
    if g.user_id != post.author_id:
        liker = User.query.get(g.user_id)
        vault = Vault.query.get(post.vault_id)
        create_notification(
            post.author_id,
            'post_liked',
            f'{liker.name} liked your memory in {vault.name}.',
            vault_id=post.vault_id,
        )
    db.session.commit()
    return jsonify({'like_count': post.like_count}), 201


@app.route('/api/posts/<int:post_id>/like', methods=['DELETE'])
@token_required
def unlike_post(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    if not post.is_unlocked:
        return jsonify({'error': 'Cannot unlike a locked time capsule'}), 403
    like = PostLike.query.filter_by(post_id=post_id, user_id=g.user_id).first()
    if not like:
        return jsonify({'error': 'You have not liked this post'}), 400
    db.session.delete(like)
    post.like_count = max(0, post.like_count - 1)
    db.session.commit()
    return jsonify({'like_count': post.like_count}), 200


@app.route('/api/posts/<int:post_id>/comments', methods=['GET'])
@token_required
def get_comments(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    if not post.is_unlocked:
        return jsonify({'error': 'Cannot view comments on a locked time capsule'}), 403
    comments = (
        PostComment.query
        .filter_by(post_id=post_id)
        .order_by(PostComment.created_at.asc())
        .all()
    )
    author_ids = {c.author_id for c in comments}
    authors = {u.id: u for u in User.query.filter(User.id.in_(author_ids)).all()}
    return jsonify([
        serialize_comment(c, authors[c.author_id])
        for c in comments
        if c.author_id in authors
    ]), 200


@app.route('/api/posts/<int:post_id>/comments', methods=['POST'])
@token_required
def create_comment(post_id):
    post = Post.query.get(post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    if not post.is_unlocked:
        return jsonify({'error': 'Cannot comment on a locked time capsule'}), 403
    data = request.get_json() or {}
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Comment body is required'}), 400
    if len(body) > 500:
        return jsonify({'error': 'Comment cannot exceed 500 characters'}), 400
    now = datetime.utcnow()
    comment = PostComment(post_id=post_id, author_id=g.user_id, body=body, created_at=now)
    post.comment_count += 1
    db.session.add(comment)
    if g.user_id != post.author_id:
        commenter = User.query.get(g.user_id)
        vault = Vault.query.get(post.vault_id)
        create_notification(
            post.author_id,
            'comment_received',
            f'{commenter.name} commented on your post in {vault.name}.',
            vault_id=post.vault_id,
        )
    db.session.commit()
    author = User.query.get(g.user_id)
    return jsonify(serialize_comment(comment, author)), 201


@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
@token_required
def delete_comment(comment_id):
    comment = PostComment.query.get(comment_id)
    if not comment:
        return jsonify({'error': 'Comment not found'}), 404
    post = Post.query.get(comment.post_id)
    if not post:
        return jsonify({'error': 'Post not found'}), 404
    result = require_vault_member(post.vault_id)
    if isinstance(result, tuple):
        return result
    vm = result
    if comment.author_id != g.user_id and vm.role != 'owner':
        return vault_forbidden('You do not have permission to delete this comment')
    db.session.delete(comment)
    post.comment_count = max(0, post.comment_count - 1)
    db.session.commit()
    return '', 204


# --- V1 ROUTES: DASHBOARD (3E) ---

@app.route('/api/dashboard', methods=['GET'])
@token_required
def get_dashboard():
    # ── Vault memberships for this user ───────────────────────────────────────
    memberships = VaultMember.query.filter_by(user_id=g.user_id).all()
    if not memberships:
        return jsonify({'vaults': [], 'upcoming_capsules': []}), 200

    vault_ids = [vm.vault_id for vm in memberships]
    vm_by_vault = {vm.vault_id: vm for vm in memberships}

    # ── Load vault rows ────────────────────────────────────────────────────────
    vaults = {v.id: v for v in Vault.query.filter(Vault.id.in_(vault_ids)).all()}

    # ── member_count: one grouped query instead of N separate .count() calls ──
    count_rows = (
        db.session.query(VaultMember.vault_id, func.count(VaultMember.id))
        .filter(VaultMember.vault_id.in_(vault_ids))
        .group_by(VaultMember.vault_id)
        .all()
    )
    member_counts = {vault_id: count for vault_id, count in count_rows}

    # ── Build vault card list ─────────────────────────────────────────────────
    vault_cards = []
    for vm in memberships:
        vault = vaults.get(vm.vault_id)
        if not vault:
            continue

        # Unread count: posts in this vault that appeared after the user's
        # last visit, are currently visible, and were authored by someone else.
        # NOTE: excluding own posts (Post.author_id != g.user_id) is an
        # implementation decision — PRD/TDD only specify "posts since last visit".
        # Rationale: a user cannot "unread" content they authored themselves.
        # This exclusion was explicitly approved during the unread-count
        # reconciliation and must remain consistent across all unread queries.
        if vm.last_seen_at is None:
            unread = 0
        else:
            unread = Post.query.filter(
                Post.vault_id == vm.vault_id,
                Post.is_unlocked == True,
                Post.posted_at > vm.last_seen_at,
                Post.author_id != g.user_id,  # implementation decision — see above
            ).count()

        vault_cards.append({
            'id':           str(vault.id),
            'name':         vault.name,
            'member_count': member_counts.get(vm.vault_id, 0),
            'unread_count': unread,
            'user_role':    vm.role,
        })

    # ── Upcoming capsules: only the requesting user's own pending capsules ─────
    capsules = (
        Post.query
        .filter(
            Post.author_id == g.user_id,
            Post.is_unlocked == False,
            Post.unlock_at.isnot(None),  # defensive: capsules must have an unlock date
        )
        .order_by(Post.unlock_at.asc())
        .all()
    )

    # Cache vault names for capsule entries to avoid repeated queries.
    vault_cache = dict(vaults)  # already loaded above
    for cap in capsules:
        if cap.vault_id not in vault_cache:
            v = Vault.query.get(cap.vault_id)
            if v:
                vault_cache[cap.vault_id] = v

    now = datetime.utcnow()
    upcoming = []
    for cap in capsules:
        vault = vault_cache.get(cap.vault_id)
        if not vault:
            continue
        days_until_unlock = max(0, (cap.unlock_at - now).days)
        upcoming.append({
            'post_id':          str(cap.id),
            'vault_id':         str(cap.vault_id),
            'vault_name':       vault.name,
            'unlock_at':        cap.unlock_at.isoformat(),
            'days_until_unlock': days_until_unlock,
        })

    return jsonify({'vaults': vault_cards, 'upcoming_capsules': upcoming}), 200


def get_mime_type(filename):
    if not filename:
        return 'data:application/octet-stream'
    ext = filename.lower().split('.')[-1]
    mime_types = {
        'pdf': 'data:application/pdf',
        'jpg': 'data:image/jpeg',
        'jpeg': 'data:image/jpeg',
        'png': 'data:image/png',
        'gif': 'data:image/gif',
        'txt': 'data:text/plain',
        'doc': 'data:application/msword',
    }
    return mime_types.get(ext, 'data:application/octet-stream')

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(60), nullable=True)
    last_name = db.Column(db.String(60), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    last_login = db.Column(db.DateTime)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=True)
    avatar = db.Column(db.Text, nullable=True)  # stores base64


class Family(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    lineage_code = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class InviteCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)



class FamilyMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    family_id = db.Column(db.Integer, db.ForeignKey('family.id'), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    suffix = db.Column(db.String(10))
    email = db.Column(db.String(120))
    description = db.Column(db.Text)
    photo = db.Column(db.Text)  # Stores the base64 string from profilePreview
    parent_id = db.Column(db.Integer, db.ForeignKey('family_member.id'), nullable=True)
    
    # Store milestones as a JSON blob or a separate table
    milestones = db.Column(db.JSON)
    
    bio_attachments = db.Column(db.JSON)

class Memory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    content_encrypted = db.Column(db.LargeBinary, nullable=False)
    media_content = db.Column(db.LargeBinary, nullable=True)  # ADD THIS LINE
    media_type = db.Column(db.String(50), nullable=True)  # ADD THIS LINE - 'video' or 'audio'
    attachments = db.Column(db.JSON, nullable=True)
    release_date = db.Column(db.DateTime, nullable=False)
    is_released = db.Column(db.Boolean, default=False)
    is_draft = db.Column(db.Boolean, default=False)
    recipient_email = db.Column(db.String(120), nullable=True)
    hidden_from_sender = db.Column(db.Boolean, default=False)
    hidden_from_recipient = db.Column(db.Boolean, default=False)

    @property
    def content(self): return decrypt_content(self.content_encrypted)
    @content.setter
    def content(self, value): self.content_encrypted = encrypt_content(value)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # 'vault_sent', 'vault_received', 'vault_deleted'
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    # Pass 26.1: nullable vault reference for frontend action routing
    vault_id = db.Column(db.Integer, db.ForeignKey('vault.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- NEW V1 MODELS ---

class Vault(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(50), nullable=False)
    invite_code  = db.Column(db.String(6), unique=True, nullable=False)
    created_by   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Pass 18: vault identity fields
    description  = db.Column(db.String(160), nullable=True)
    accent_color = db.Column(db.String(20),  nullable=True)
    cover_url    = db.Column(db.String(500), nullable=True)
    # Pass 21: child vault fields
    vault_type           = db.Column(db.String(20), nullable=False, default='normal')  # 'normal' | 'child'
    child_email          = db.Column(db.String(120), nullable=True)
    claimed_at           = db.Column(db.DateTime, nullable=True)
    claimed_by_user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)


class VaultMember(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    vault_id     = db.Column(db.Integer, db.ForeignKey('vault.id'), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role         = db.Column(db.String(20), nullable=False, default='member')
    joined_at    = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    invited_by   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint('vault_id', 'user_id', name='uq_vault_member'),
    )


class ChildClaimToken(db.Model):
    """Single-use hashed token for a child to claim a vault."""
    id         = db.Column(db.Integer, primary_key=True)
    vault_id   = db.Column(db.Integer, db.ForeignKey('vault.id'), nullable=False)
    # SHA-256 hex digest of the plaintext token — plaintext never stored
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, nullable=False, default=False)


class Post(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    vault_id      = db.Column(db.Integer, db.ForeignKey('vault.id'), nullable=False)
    author_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    caption       = db.Column(db.Text, nullable=True)
    media_type    = db.Column(db.String(20), nullable=False)
    media_url     = db.Column(db.Text, nullable=True)
    unlock_at     = db.Column(db.DateTime, nullable=True)
    is_unlocked   = db.Column(db.Boolean, nullable=False, default=True)
    posted_at     = db.Column(db.DateTime, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    like_count    = db.Column(db.Integer, nullable=False, default=0)
    comment_count = db.Column(db.Integer, nullable=False, default=0)
    is_archived   = db.Column(db.Boolean, nullable=False, default=False)


class PostLike(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    post_id    = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint('post_id', 'user_id', name='uq_post_like'),
    )


class PostComment(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    post_id    = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    author_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    body       = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class DigestDelivery(db.Model):
    """Records that a new_post digest was delivered to a user for a specific
    vault on a specific UTC calendar date. Used by unlock_job.py to prevent
    duplicate digest notifications within the same UTC day.
    """
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    vault_id    = db.Column(db.Integer, db.ForeignKey('vault.id'), nullable=False)
    digest_date = db.Column(db.Date, nullable=False)

    __table_args__ = (
        UniqueConstraint('user_id', 'vault_id', 'digest_date',
                         name='uq_digest_delivery'),
    )


# --- API ROUTES FOR V0 ---


@app.route('/api/family-members', methods=['POST'])
@token_required
def add_member():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    email_val = data.get('email')
    

    user = User.query.get(g.user_id)
    ensure_user_has_family(user)

    new_member = FamilyMember(
        user_id=g.user_id,
        family_id=user.family_id,
        name=data.get('name'),
        first_name=data.get('firstName'),
        last_name=data.get('lastName'),
        suffix=data.get('suffix'),
        photo=data.get('photo'),
        parent_id=data.get('parentId'),
        milestones=data.get('milestones'),
        bio_attachments=data.get('bioAttachments'),
        email=email_val,
        description=data.get('description', ''),
    )
    
    db.session.add(new_member)
    db.session.commit()
    create_notification(g.user_id, 'member_added',
        f"Family member \"{new_member.first_name or new_member.name}\" was added successfully.")
    db.session.commit()
    
    # === VERIFY DATABASE LINK BEFORE RETURNING RESPONSE ===
    is_user_registered = User.query.filter_by(email=email_val).first() is not None if email_val else False
    
    return jsonify({
        "id": new_member.id,
        "name": new_member.name,
        "firstName": new_member.first_name,
        "lastName": new_member.last_name,
        "suffix": new_member.suffix,
        "email": new_member.email,
        "linkedAccount": new_member.email if is_user_registered else None,  # Fix here
        "photo": new_member.photo,
        "parentId": new_member.parent_id,
        "milestones": new_member.milestones,
        "bioAttachments": new_member.bio_attachments
    }), 201


@app.route('/api/family-members/<int:member_id>', methods=['DELETE'])
@token_required
def delete_member(member_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    member = FamilyMember.query.get_or_404(member_id)



    user = User.query.get(g.user_id)
    if member.family_id != user.family_id:
        return jsonify({'error': 'Forbidden'}), 403

    try:
        # Disconnect any children so they don't have a 'ghost' parent
        children = FamilyMember.query.filter_by(parent_id=member_id).all()
        for child in children:
            child.parent_id = None
        
        db.session.delete(member)
        db.session.commit()
        return jsonify({"message": "Member deleted successfully"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route('/api/stats', methods=['GET'])
@token_required
def get_dashboard_stats():
    """Feeds data to the v0 Dashboard cards"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    u_id = g.user_id
    user = User.query.get(u_id)
    return jsonify({
        "totalMemories": Memory.query.filter_by(user_id=u_id, hidden_from_sender=False).count(),
        "lockedVaults": Memory.query.filter_by(user_id=u_id, is_released=False, hidden_from_sender=False).count(),
        "familyMembers": FamilyMember.query.filter_by(family_id=user.family_id).count() if user.family_id else 0
    })

@app.route('/api/family-members', methods=['GET'])
@token_required
def get_family_members():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Not logged in'}), 401
    
    user = User.query.get(g.user_id)
    members = FamilyMember.query.filter_by(family_id=user.family_id).all() if user.family_id else []
    result = []
    
    for member in members:
        # Safe string splitting to prevent crashes for single names (e.g., "Mom")
        name_parts = member.name.split() if member.name else []
        first_name = name_parts[0] if len(name_parts) > 0 else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""


        is_user_registered = User.query.filter_by(email=member.email).first() is not None if member.email else False

        result.append({
            "id": member.id,
            "name": member.name,
            "firstName": first_name,
            "lastName": last_name,
            "suffix": member.suffix,
            "email": member.email,
            "linkedAccount": member.email if is_user_registered else None,
            "description": member.description,
            
            "photo": member.photo,
            "parentId": member.parent_id,
            "milestones": member.milestones or [],
            "bioAttachments": member.bio_attachments or []
        })
        
    return jsonify(result), 200

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    email    = str(data.get('email')    or '').strip().lower()
    password = str(data.get('password') or '')
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    user = User.query.filter_by(email=email).first()
    if user and check_password_hash(user.password_hash, password):
        token = jwt.encode({
            'user_id': user.id,
            'exp': datetime.utcnow() + timedelta(days=7)
        }, app.config['SECRET_KEY'], algorithm='HS256')
        return jsonify({"message": "Login successful", "token": token}), 200
    return jsonify({"error": "Access Denied"}), 401

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    email    = str(data.get('email')    or '').strip().lower()
    password = str(data.get('password') or '')
    name     = str(data.get('name')     or '').strip()
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    if len(email) > 120:
        return jsonify({'error': 'Email is too long'}), 400
    # Basic email syntax: must contain exactly one @, with non-empty local and domain.
    _email_parts = email.split('@')
    if len(_email_parts) != 2 or not _email_parts[0] or not _email_parts[1] or '.' not in _email_parts[1]:
        return jsonify({'error': 'Invalid email address'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    if len(password) > 128:
        return jsonify({'error': 'Password is too long'}), 400
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if len(name) > 120:
        return jsonify({'error': 'Name is too long'}), 400
    # Check if user already exists
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 400
    user = User(
        name=name,
        email=email,
        password_hash=generate_password_hash(password)
    )
    
    db.session.add(user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Most likely a uniqueness race on email; return the same message
        # as the pre-check to avoid leaking which field caused the conflict.
        return jsonify({'error': 'Email already registered'}), 409
    token = jwt.encode({
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=7)
    }, app.config['SECRET_KEY'], algorithm='HS256')
    return jsonify({'message': 'User created', 'token': token}), 201

@app.route('/api/memories', methods=['POST'])
@token_required
def create_memory():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json()
    
    # Clean up the ISO string so python parses it perfectly
    date_str = data.get('releaseDate').replace('Z', '')
    parsed_date = datetime.fromisoformat(date_str)

    # Convert media from base64 string to binary if it exists
    media_content = None
    media_type = None

    if data.get('mediaContent'):
        import base64
        # Extract the base64 part and media type
        media_data = data.get('mediaContent')
        if media_data.startswith('data:'):
            # Format: data:video/webm;base64,xxxxx
            media_type = media_data.split(':')[1].split(';')[0]  # Extract 'video/webm'
            media_content = base64.b64decode(media_data.split(',')[1])

    # Create a new memory
    # Encrypt the content before storing
    import base64
    content_to_encrypt = data.get('content', '')
    key_bytes = ENCRYPTION_KEY.encode()
    content_bytes = content_to_encrypt.encode()
    encrypted_bytes = bytearray()
    for i, byte in enumerate(content_bytes):
        encrypted_bytes.append(byte ^ key_bytes[i % len(key_bytes)])
    encrypted_content = base64.b64encode(encrypted_bytes).decode()

    new_memory = Memory(
        user_id=g.user_id,
        title=data.get('title'),
        content_encrypted=encrypted_content.encode(),  # Store encrypted as binary
        media_content=media_content,
        media_type=media_type,
        release_date=parsed_date,
        is_released=False,
        is_draft=data.get('is_draft', False),
        recipient_email=data.get('recipient')
    )

    attachments_list = []
    if data.get('attachments'):
        import base64
        for attachment in data.get('attachments'):
            if isinstance(attachment, dict):
                image_data = attachment.get('data')
                image_name = attachment.get('name')
            else:
                image_data = attachment
                image_name = None
                
            if image_data:
                if isinstance(image_data, str) and image_data.startswith('data:'):
                    image_data = image_data.split(',')[1]
                
                attachments_list.append({
                    'name': image_name,
                    'data': image_data
                })

    new_memory.attachments = attachments_list if attachments_list else None
    
    db.session.add(new_memory)
    db.session.flush()  # get new_memory.id before commit

    if not new_memory.is_draft and new_memory.recipient_email:
        # Notify sender
        create_notification(g.user_id, 'vault_sent',
            f"You sent \"{new_memory.title}\" to {new_memory.recipient_email}.")
        # Notify recipient if they have an account
        recipient_user = User.query.filter_by(email=new_memory.recipient_email).first()
        if recipient_user:
            create_notification(recipient_user.id, 'vault_received',
                f"You received a new vault: \"{new_memory.title}\".")

    db.session.commit()
    return jsonify({
    'message': 'Vault created successfully',
    'is_draft': new_memory.is_draft,
    'has_recipient': bool(new_memory.recipient_email)
    }), 201

@app.route('/api/memories/shared', methods=['GET'])
@token_required
def get_shared_memories():
    """Fetches all capsules addressed to the currently logged-in family recipient account"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    current_user = User.query.get(g.user_id)
    if not current_user:
        return jsonify({'error': 'User profile not found'}), 404
        
    now = datetime.now()

    # REMOVED the '<= now' constraint so the recipient can see the countdown!
    shared_memories = Memory.query.filter(
    Memory.recipient_email == current_user.email,
    Memory.is_draft == False,
    Memory.hidden_from_recipient == False
    ).all()
    
    result = []
    for m in shared_memories:
        # Convert binary media to base64 if it exists
        media_content = None
        if m.media_content:
            import base64
            media_content = 'data:' + m.media_type + ';base64,' + base64.b64encode(m.media_content).decode()
        
        image_content = None
        if m.attachments:
            import base64
        
        result.append({
            "id": m.id,
            "title": m.title,
            "content": m.content if m.release_date <= now else "",
            "mediaContent": media_content,
            "attachments": [],  # Add attachments handling
            "hasImage": False,
            "hasVideo": m.media_type == 'video/webm' if m.media_type else False,
            "hasAudio": m.media_type == 'audio/webm' if m.media_type else False,
            "release_date": m.release_date.isoformat(),
            "status": "released" if m.release_date <= now else "locked",
            "is_draft": False,
            "recipient": m.recipient_email,
            "sender": User.query.get(m.user_id).name if User.query.get(m.user_id) else "Unknown",

        })

    return jsonify(result), 200

@app.route('/api/heartbeat', methods=['POST'])
@token_required
def heartbeat():
    """Updates last_login to prevent Dead Man's Switch trigger"""
    if 'user_id' in session:
        user = User.query.get(g.user_id)
        user.last_login = datetime.utcnow()
        db.session.commit()
        return jsonify({'status': 'active'}), 200
    return jsonify({'status': 'guest'}), 200


@app.route('/api/memories', methods=['GET'])
@token_required
def get_memories():
    """Returns the list of time-locked vaults for the dashboard"""
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    u_id = g.user_id
    memories = Memory.query.filter_by(user_id=u_id, hidden_from_sender=False).all()
    
    return jsonify([{
        "id": m.id,
        "title": m.title,
        "release_date": m.release_date.isoformat(),
        "status": "released" if m.is_released else "locked",
        "is_draft": m.is_draft 
    } for m in memories])




@app.route('/api/get-my-code', methods=['GET'])
@token_required
def get_my_code():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    user = User.query.get(g.user_id)
    ensure_user_has_family(user)
    family = Family.query.get(user.family_id)

    chars = string.ascii_uppercase + string.digits
    # Use secrets.choice (CSPRNG) instead of random.choices (PRNG)
    code = ''.join(secrets.choice(chars) for _ in range(6))
    formatted_code = f"{code[:3]}-{code[3:]}"

    invite = InviteCode(
        code=formatted_code,
        family_id=family.id,
        created_by=user.id,
        expires_at=datetime.utcnow() + timedelta(hours=24)
    )
    db.session.add(invite)
    db.session.commit()

    return jsonify({
        "invite_code": formatted_code,
    }), 200


@app.route('/api/join-family', methods=['POST'])
@token_required
def join_family():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    code = (data.get('invite_code') or '').strip().upper()


    invite = InviteCode.query.filter_by(code=code, used=False).first()
    if not invite:
        return jsonify({'message': 'Invalid or expired invite code.'}), 400
    if invite.expires_at < datetime.utcnow():
        return jsonify({'message': 'This invite code has expired.'}), 400

    user = User.query.get(g.user_id)

    if invite.created_by == user.id:
        return jsonify({'message': "You can't join your own family via invite."}), 400

    user.family_id = invite.family_id
    invite.used = True
    db.session.commit()
    create_notification(g.user_id, 'family_joined',
        f"You successfully joined a family using an invite code.")
    # Also notify the invite creator
    create_notification(invite.created_by, 'member_joined',
        f"{user.name} joined your family using an invite code.")
    db.session.commit()

    return jsonify({'message': 'Successfully joined family!'}), 200



@app.route('/api/family-members/<int:member_id>', methods=['PUT'])
@token_required
def update_member(member_id):

    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401

    member = FamilyMember.query.get_or_404(member_id)
    data = request.get_json()
    
    user = User.query.get(g.user_id)
    # AFTER
    if member.family_id != user.family_id and member.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    
    member.first_name = data.get('firstName', member.first_name)
    member.last_name = data.get('lastName', member.last_name)
    member.suffix = data.get('suffix', member.suffix)
    member.name = data.get('name', member.name)
    incoming_email = data.get('linkedAccount') or data.get('email')
    
    if incoming_email is not None:
        member.email = incoming_email

    member.photo = data.get('photo', member.photo)
    member.milestones = data.get('milestones', member.milestones)
    member.bio_attachments = data.get('bioAttachments', member.bio_attachments)

    if 'description' in data:
        member.description = data.get('description')
    
    
    db.session.commit()
    
    # Return the full object so the frontend state updates correctly
    return jsonify({
        "id": member.id,
        "name": member.name,
        "firstName": member.first_name,
        "lastName": member.last_name,
        "suffix": member.suffix,
        "email": member.email,
        "linkedAccount": member.email if (member.email and hasattr(member, 'is_linked') and member.is_linked) else None,
        "description": member.description,
        "photo": member.photo,
        "parentId": member.parent_id,
        "milestones": member.milestones,
        "bioAttachments": member.bio_attachments
    }), 200


@app.route('/api/memories/<int:memory_id>', methods=['PUT'])
@token_required
def update_memory(memory_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    memory = Memory.query.get_or_404(memory_id)
    
    if memory.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    
    # Update fields
    memory.title = data.get('title', memory.title)
    memory.content_encrypted = encrypt_content(data.get('content', ''))
    memory.release_date = datetime.fromisoformat(data.get('releaseDate').replace('Z', ''))
    memory.is_draft = data.get('is_draft', memory.is_draft)
    memory.recipient_email = data.get('recipient')
    

    # Handle media content
    if data.get('mediaContent'):
        media_data = data.get('mediaContent')
        if ',' in media_data:
            header, b64data = media_data.split(',', 1)
            mime = header.split(':')[1].split(';')[0]
            memory.media_content = base64.b64decode(b64data)
            memory.media_type = mime

    # Handle attachments similarly to POST
    if data.get('attachments'):
        attachments_list = []
        for attachment in data.get('attachments'):
            if isinstance(attachment, dict):
                attachments_list.append({
                    'name': attachment.get('name'),
                    'data': attachment.get('data').split(',')[1] if 'data:' in attachment.get('data', '') else attachment.get('data')
                })
        memory.attachments = attachments_list if attachments_list else None
    
    db.session.commit()
    return jsonify({'message': 'Vault updated successfully'}), 200

@app.route('/api/me', methods=['GET'])
@token_required
def get_current_user():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Not logged in'}), 401
    

    user = User.query.get(g.user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
        
    return jsonify({
    "id":        str(user.id),
    "name":      user.name,
    "firstName": user.first_name or user.name.split()[0],
    "lastName":  user.last_name or (user.name.split()[1] if len(user.name.split()) > 1 else ''),
    "email":     user.email,
    "avatar":    user.avatar or None
}), 200


@app.route('/api/check-email', methods=['POST'])
def check_email():
    data = request.get_json()
    email = data.get('email')
    
    if not email:
        return jsonify({"exists": False}), 400

    # This checks your User database table
    user = User.query.filter_by(email=email).first()
    
    return jsonify({"exists": user is not None})


@app.route('/api/memories/<int:memory_id>', methods=['GET'])
@token_required
def get_single_memory(memory_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
        
    memory = Memory.query.get_or_404(memory_id)
    current_user = User.query.get(g.user_id)
    
    # Allow access if user is owner OR recipient
    is_owner = memory.user_id == g.user_id
    is_recipient = current_user and memory.recipient_email == current_user.email
    
    if not is_owner and not is_recipient:
        return jsonify({'error': 'Forbidden'}), 403
    
    # Convert binary media to base64 if it exists
    media_content = None
    if memory.media_content:
        import base64
        media_content = 'data:' + memory.media_type + ';base64,' + base64.b64encode(memory.media_content).decode()
        
    attachments = []
    if memory.attachments:
        for att in memory.attachments:
            attachments.append({
                'name': att.get('name'),
                'content': get_mime_type(att.get('name')) + ';base64,' + att.get('data')
            })
    
    return jsonify({
        "id": memory.id,
        "title": memory.title,
        "content": memory.content,
        "mediaContent": media_content,  # ADD THIS
        "attachments": attachments,
        "hasImage": len(attachments) > 0,
        "hasVideo": memory.media_type == 'video/webm' if memory.media_type else False, 
        "hasAudio": memory.media_type == 'audio/webm' if memory.media_type else False,
        "release_date": memory.release_date.isoformat(),
        "status": "released" if memory.is_released else "locked",
        "is_draft": memory.is_draft
    }), 200


@app.route('/api/me', methods=['PUT'])
@token_required
def update_current_user():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    
    user = User.query.get(g.user_id)
    data = require_json_object()
    if data is None:
        return jsonify({'error': 'Request body must be a JSON object'}), 400

    old_avatar = None
    if 'avatar' in data:
        raw_avatar = data['avatar']
        if raw_avatar is None:
            # Explicit null clears the avatar (intentional feature)
            old_avatar = user.avatar
            user.avatar = None
        elif not isinstance(raw_avatar, str):
            return jsonify({'error': 'Avatar must be a string'}), 400
        elif is_r2_url(raw_avatar):
            # Already an R2 URL — pass through (no re-upload needed)
            user.avatar = raw_avatar
        else:
            # Validate MIME against strict allowlist before uploading
            try:
                validate_data_uri(
                    raw_avatar,
                    _AVATAR_MIME_ALLOWLIST,
                    max_decoded_bytes=_MAX_AVATAR_DECODED_BYTES,
                )
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            try:
                uploaded_url = upload_media(raw_avatar, prefix='avatars')
            except Exception as exc:
                return jsonify({'error': f'Avatar upload failed: {exc}'}), 503
            old_avatar = user.avatar  # will be deleted after commit
            user.avatar = uploaded_url

    # Name fields: must be strings if provided; omitted fields preserve current value
    first_raw = data.get('firstName', None)
    last_raw  = data.get('lastName',  None)
    if first_raw is not None and not isinstance(first_raw, str):
        return jsonify({'error': 'firstName must be a string'}), 400
    if last_raw is not None and not isinstance(last_raw, str):
        return jsonify({'error': 'lastName must be a string'}), 400
    first = (first_raw or '').strip() if first_raw is not None else (user.first_name or '')
    last  = (last_raw  or '').strip() if last_raw  is not None else (user.last_name  or '')
    if len(first) > 60 or len(last) > 60:
        return jsonify({'error': 'Name fields cannot exceed 60 characters'}), 400
    user.first_name = first
    user.last_name  = last
    user.name = f"{first} {last}".strip()
    if not user.name:
        return jsonify({'error': 'Name cannot be empty'}), 400

    db.session.commit()
    if old_avatar and is_r2_url(old_avatar):
        delete_object(old_avatar)  # fire-and-forget; only delete confirmed R2 URLs
    return jsonify({'message': 'Profile updated successfully'}), 200


@app.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notifs = Notification.query.filter_by(user_id=g.user_id)\
        .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': str(n.id),
        'type': n.type,
        'message': n.message,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat(),
        'vault_id': str(n.vault_id) if n.vault_id else None,
    } for n in notifs]), 200

@app.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
@token_required
def mark_notification_read(notif_id):
    n = Notification.query.get(notif_id)
    if not n:
        return jsonify({'error': 'Notification not found'}), 404
    if n.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    n.is_read = True
    db.session.commit()
    return jsonify({'id': n.id, 'is_read': True}), 200


@app.route('/api/notifications/read-all', methods=['POST'])
@token_required
def mark_all_read():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    Notification.query.filter_by(user_id=g.user_id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'message': 'All marked read'}), 200

@app.route('/api/leave-family', methods=['POST'])
@token_required
def leave_family():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    user = User.query.get(g.user_id)
    if not user.family_id:
        return jsonify({'error': 'Not in a family'}), 400
    
    # Give them their own new solo family
    old_family_id = user.family_id
    new_family = Family(lineage_code=generate_lineage_code())
    db.session.add(new_family)
    db.session.flush()
    user.family_id = new_family.id

    # Move user's own family members to the new family
    FamilyMember.query.filter_by(
        family_id=old_family_id,
        user_id=user.id
    ).update({'family_id': new_family.id})
    
    create_notification(g.user_id, 'family_left',
        "You've left the shared family. Your data remains intact.")
    db.session.commit()
    return jsonify({'message': 'Left family successfully'}), 200


@app.route('/api/family-status', methods=['GET'])
@token_required
def family_status():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    user = User.query.get(g.user_id)
    # Count how many users share this family_id
    member_count = User.query.filter_by(family_id=user.family_id).count()
    return jsonify({'in_shared_family': member_count > 1}), 200

@app.route('/api/change-password', methods=['POST'])
@token_required
def change_password():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = require_json_object()
    if data is None:
        return jsonify({'error': 'Request body must be a JSON object'}), 400
    user = User.query.get(g.user_id)
    current = data.get('current_password')
    new_pwd = data.get('new_password')
    if not isinstance(current, str) or not isinstance(new_pwd, str):
        return jsonify({'error': 'current_password and new_password must be strings'}), 400
    if not check_password_hash(user.password_hash, current):
        return jsonify({'error': 'Current password is incorrect'}), 400
    if len(new_pwd) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    if len(new_pwd) > 128:
        return jsonify({'error': 'Password is too long'}), 400
    user.password_hash = generate_password_hash(new_pwd)
    db.session.commit()
    return jsonify({'message': 'Password updated successfully'}), 200

@app.route('/api/delete-account', methods=['DELETE'])
@token_required
def delete_account():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    user = User.query.get(g.user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    if not check_password_hash(user.password_hash, data.get('password', '')):
        return jsonify({'error': 'Incorrect password'}), 400

    # Revision 1 (product decision): block deletion if the user owns any vault
    # that has other members. Deleting their account must not silently destroy
    # other members' shared content. The user should remove all members from
    # their vaults first (via the vault management screen) before deleting.
    # Vaults where the user is the sole member are deleted cleanly below.
    owned_vaults = Vault.query.filter_by(created_by=user.id).all()
    for vault in owned_vaults:
        other_members = VaultMember.query.filter(
            VaultMember.vault_id == vault.id,
            VaultMember.user_id != user.id,
        ).count()
        if other_members > 0:
            return jsonify({
                'error': (
                    f'You own "{vault.name}" which has other members. '
                    'Remove all members from your vaults before deleting your account.'
                )
            }), 400

    try:
        # ── Collect all post_ids that will be deleted (owned vaults + user's own posts) ──
        # Used to distinguish surviving posts whose counters need updating.
        owned_vault_ids = [v.id for v in owned_vaults]
        # Collect post_ids that will be deleted so counter updates skip them.
        owned_vault_post_ids = (
            [p.id for p in Post.query.filter(Post.vault_id.in_(owned_vault_ids)).all()]
            if owned_vault_ids else []
        )
        user_post_ids = [
            p.id for p in Post.query.filter_by(author_id=user.id).all()
        ]
        all_deleted_post_ids = set(owned_vault_post_ids) | set(user_post_ids)

        # ── Phase 1: delete solo-owned vaults and their content ──────────────────
        # (At this point every owned vault has no other members — checked above.)
        r2_urls_to_delete: list[str] = []  # collected before DB deletes
        for vault in owned_vaults:
            vault_posts = Post.query.filter_by(vault_id=vault.id).all()
            vault_post_ids = [p.id for p in vault_posts]
            r2_urls_to_delete += [p.media_url for p in vault_posts if p.media_url]
            if vault_post_ids:
                PostLike.query.filter(
                    PostLike.post_id.in_(vault_post_ids)
                ).delete(synchronize_session=False)
                PostComment.query.filter(
                    PostComment.post_id.in_(vault_post_ids)
                ).delete(synchronize_session=False)
            Post.query.filter_by(vault_id=vault.id).delete(synchronize_session=False)
            VaultMember.query.filter_by(vault_id=vault.id).delete(synchronize_session=False)
            DigestDelivery.query.filter_by(vault_id=vault.id).delete(synchronize_session=False)
            db.session.delete(vault)

        # ── Phase 2: user's participation in other vaults ────────────────────────

        # 2a. Delete likes the user placed on other users' posts that SURVIVE.
        # Revision 2: update like_count on surviving posts before bulk delete.
        # Posts that will survive and lose a like from this user.
        # Counters on deleted posts (in all_deleted_post_ids) don't matter.
        surviving_liked_posts = (
            Post.query
            .join(PostLike, PostLike.post_id == Post.id)
            .filter(
                PostLike.user_id == user.id,
                Post.id.notin_(all_deleted_post_ids),
            )
            .all()
        ) if all_deleted_post_ids else (
            Post.query
            .join(PostLike, PostLike.post_id == Post.id)
            .filter(PostLike.user_id == user.id)
            .all()
        )
        for p in surviving_liked_posts:
            p.like_count = max(0, p.like_count - 1)

        PostLike.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # 2b. Delete comments the user made on other users' posts that SURVIVE.
        # Revision 2: update comment_count on surviving posts before bulk delete.
        # Posts that will survive and lose comments from this user.
        surviving_commented_posts = (
            Post.query
            .join(PostComment, PostComment.post_id == Post.id)
            .filter(
                PostComment.author_id == user.id,
                Post.id.notin_(all_deleted_post_ids),
            )
            .distinct()
            .all()
        ) if all_deleted_post_ids else (
            Post.query
            .join(PostComment, PostComment.post_id == Post.id)
            .filter(PostComment.author_id == user.id)
            .distinct()
            .all()
        )
        for p in surviving_commented_posts:
            # Each surviving post may have multiple comments from this user;
            # count them to decrement correctly.
            user_comment_count = PostComment.query.filter_by(
                post_id=p.id, author_id=user.id
            ).count()
            p.comment_count = max(0, p.comment_count - user_comment_count)

        PostComment.query.filter_by(author_id=user.id).delete(synchronize_session=False)

        # 2c. Delete user's own posts (in other vaults) and their likes/comments.
        user_posts = Post.query.filter_by(author_id=user.id).all()
        r2_urls_to_delete += [p.media_url for p in user_posts if p.media_url]
        if user_post_ids:
            PostLike.query.filter(
                PostLike.post_id.in_(user_post_ids)
            ).delete(synchronize_session=False)
            PostComment.query.filter(
                PostComment.post_id.in_(user_post_ids)
            ).delete(synchronize_session=False)
        Post.query.filter_by(author_id=user.id).delete(synchronize_session=False)

        # 2d. Remove remaining vault memberships, digest records, and notifications.
        VaultMember.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        DigestDelivery.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        Notification.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # ── Phase 3: legacy cleanup ───────────────────────────────────────────────
        Memory.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        InviteCode.query.filter_by(created_by=user.id).delete(synchronize_session=False)
        if user.family_id is not None:
            FamilyMember.query.filter_by(
                family_id=user.family_id
            ).delete(synchronize_session=False)

        # ── Phase 4: delete the user record ──────────────────────────────────────
        if user.avatar:
            r2_urls_to_delete.append(user.avatar)
        db.session.delete(user)
        db.session.commit()
        for url in r2_urls_to_delete:  # fire-and-forget R2 cleanup after commit
            delete_object(url)
        return jsonify({'message': 'Account deleted'}), 200

    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Account deletion failed. Please try again.'}), 500

# R2 helpers: imported unconditionally.  If boto3 is missing or the r2
# module cannot be loaded, the application refuses to start rather than
# silently disabling media storage.  Install with:
#   pip3.12 install --user boto3
from r2 import upload_media, upload_file_object, upload_post_media, delete_object, is_r2_url

# ── Startup: create tables + safe additive column migrations ────────────────
#
# db.create_all() creates tables that do not yet exist but does NOT add new
# columns to existing tables.  The _run_migrations() function below handles
# the additive ALTER TABLE statements that are needed when upgrading an
# existing database (e.g. from pre-Pass-18 deployments).
#
# Each ALTER TABLE is wrapped in a try/except so that:
#   • the migration is idempotent — safe to run on every app restart;
#   • if the column already exists (OperationalError "duplicate column")
#     the error is silently swallowed;
#   • user data is never touched.
#
def _run_migrations(conn):
    """Execute safe additive ALTER TABLE statements.

    Works with both raw sqlite3.Connection objects (used in tests)
    and SQLAlchemy Connection objects (used at application startup).

    Only the "duplicate column" condition is silently ignored — every
    other exception (locked database, disk full, malformed SQL, permission
    error) is logged and re-raised so that a broken migration is never
    hidden from operators.
    """
    import sqlite3 as _sqlite3
    try:
        from sqlalchemy import text as _sa_text
    except ImportError:
        _sa_text = None

    _is_sa = not isinstance(conn, _sqlite3.Connection)

    _NEW_VAULT_COLUMNS = [
        # (column_name, DDL type)
        ("description",          "VARCHAR(160)"),
        ("accent_color",         "VARCHAR(20)"),
        ("cover_url",            "VARCHAR(500)"),
        # Pass 21: child vault fields
        ("vault_type",           "VARCHAR(20) NOT NULL DEFAULT 'normal'"),
        ("child_email",          "VARCHAR(120)"),
        ("claimed_at",           "DATETIME"),
        ("claimed_by_user_id",   "INTEGER"),
    ]
    for col, col_type in _NEW_VAULT_COLUMNS:
        ddl = f"ALTER TABLE vault ADD COLUMN {col} {col_type}"
        try:
            if _is_sa and _sa_text:
                conn.execute(_sa_text(ddl))
                conn.commit()
            else:
                conn.execute(ddl)
                conn.commit()
        except Exception as exc:
            # SQLite raises OperationalError with "duplicate column name"
            # when the column already exists.  That specific error is the
            # expected idempotency condition — ignore it and move on.
            # Every other error (locked DB, disk full, bad SQL, etc.)
            # must be surfaced so operators can act on it.
            msg = str(exc).lower()
            if "duplicate column" in msg:
                try:
                    conn.rollback()
                except Exception:
                    pass  # rollback failure after a no-op is safe to ignore
            else:
                app.logger.error(
                    "Migration failed for column %r: %s", col, exc
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    for _col, _ddl in [("is_archived", "BOOLEAN NOT NULL DEFAULT 0")]:
        _stmt = f"ALTER TABLE post ADD COLUMN {_col} {_ddl}"
        try:
            if _is_sa and _sa_text: conn.execute(_sa_text(_stmt)); conn.commit()
            else: conn.execute(_stmt); conn.commit()
        except Exception as _exc:
            if "duplicate column" in str(_exc).lower():
                try: conn.rollback()
                except Exception: pass
            else:
                app.logger.error("Post migration %r: %s", _col, _exc)
                try: conn.rollback()
                except Exception: pass
                raise

    # Pass 26.1: vault_id on notification (nullable, idempotent)
    _notif_stmt = "ALTER TABLE notification ADD COLUMN vault_id INTEGER"
    try:
        if _is_sa and _sa_text: conn.execute(_sa_text(_notif_stmt)); conn.commit()
        else: conn.execute(_notif_stmt); conn.commit()
    except Exception as _ne:
        if "duplicate column" in str(_ne).lower():
            try: conn.rollback()
            except Exception: pass
        else:
            app.logger.error("Notification migration vault_id: %s", _ne)
            try: conn.rollback()
            except Exception: pass
            raise


with app.app_context():
    db.create_all()          # create tables that do not exist yet
    with db.engine.connect() as _conn:
        _run_migrations(_conn)  # add new columns to existing tables


if __name__ == '__main__':
    app.run(debug=False, port=5000)