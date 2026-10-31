"""Accounts: login, register, profile, password change/reset, account deletion."""
import os
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g, current_app
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db
from models import (
    ChildClaimToken,
    DigestDelivery,
    FamilyMember,
    InviteCode,
    MfaChallenge,
    Memory,
    RecoveryCode,
    Notification,
    PasswordResetToken,
    Post,
    PostComment,
    PostLike,
    User,
    Vault,
    VaultMember,
)
from routes.mfa import check_app_or_recovery_code, recovery_codes_left
from security import (
    LOGIN_MAX_FAILURES,
    _client_ip,
    _login_failures,
    _recent_failures,
    issue_token,
    token_required,
)
from helpers import (
    _AVATAR_MIME_ALLOWLIST,
    _MAX_AVATAR_DECODED_BYTES,
    require_json_object,
    validate_data_uri,
)
from emailer import _send_email
from r2 import delete_object, is_r2_url, upload_media

bp = Blueprint('auth', __name__)

@bp.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    email    = str(data.get('email')    or '').strip().lower()
    password = str(data.get('password') or '')
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    key = (_client_ip(), email)
    now = datetime.utcnow()
    if len(_recent_failures(key, now)) >= LOGIN_MAX_FAILURES:
        return jsonify({'error': 'Too many failed attempts. Try again in 15 minutes.'}), 429
    user = User.query.filter_by(email=email).first()
    if user and check_password_hash(user.password_hash, password):
        _login_failures.pop(key, None)
        if user.mfa_enabled:
            from routes.mfa import begin_login   # second step: emailed code
            return begin_login(user)
        return jsonify({"message": "Login successful", "token": issue_token(user)}), 200
    _login_failures.setdefault(key, []).append(now)
    return jsonify({"error": "Access Denied"}), 401

@bp.route('/api/register', methods=['POST'])
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
    return jsonify({'message': 'User created', 'token': issue_token(user)}), 201

@bp.route('/api/me', methods=['GET'])
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
    "avatar":    user.avatar or None,
    "mfaEnabled": bool(user.mfa_enabled),
    "mfaMethod": user.mfa_method if user.mfa_enabled else None,
    "mfaPromptDismissed": bool(user.mfa_prompt_dismissed),
    "recoveryCodesLeft": recovery_codes_left(user),
}), 200


@bp.route('/api/me', methods=['PUT'])
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

# ── Forgot / reset password ──────────────────────────────────────────────────
# Flow: POST /api/forgot-password {email} → always the same 200 response (no
# account enumeration). If the account exists, a 30-minute single-use link is
# emailed. POST /api/reset-password {token, new_password} sets the password,
# which also revokes every existing login token (password fingerprint).
RESET_TOKEN_LIFETIME = timedelta(minutes=30)
RESET_MAX_REQUESTS   = 3                  # per (IP, email) ...
RESET_WINDOW         = timedelta(hours=1)  # ... per hour
_reset_requests = {}
_FORGOT_RESPONSE = {'message': 'If an account exists for that email, a reset link has been sent.'}

def _hash_reset_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()

def send_password_reset_email(to_email, name, raw_token):
    import html
    base_url  = os.environ.get('APP_BASE_URL', 'https://time-vault-imhg.vercel.app').rstrip('/')
    reset_url = f'{base_url}/?screen=reset&token={raw_token}'
    minutes   = int(RESET_TOKEN_LIFETIME.total_seconds() // 60)
    subject   = 'Reset your Time Vault password'
    body_text = (
        f'Hi {name},\n\n'
        f'Someone asked to reset the password for your Time Vault account.\n'
        f'Use this link within {minutes} minutes:\n{reset_url}\n\n'
        f"If this wasn't you, ignore this email. Your password won't change.\n\n"
        f'— Time Vault'
    )
    body_html = (
        f'<p>Hi {html.escape(name)},</p>'
        f'<p>Someone asked to reset the password for your Time Vault account.</p>'
        f'<p><a href="{html.escape(reset_url)}">Reset your password</a> '
        f'(link expires in {minutes} minutes).</p>'
        f"<p>If this wasn't you, ignore this email. Your password won't change.</p>"
    )
    _send_email(to_email, subject, body_text, body_html)

@bp.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data  = request.get_json(silent=True) or {}
    email = str(data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    # Rate limit silently: over the limit still gets the generic response.
    key = (_client_ip(), email)
    now = datetime.utcnow()
    recent = [t for t in _reset_requests.get(key, []) if now - t < RESET_WINDOW]
    if len(recent) >= RESET_MAX_REQUESTS:
        _reset_requests[key] = recent
        return jsonify(_FORGOT_RESPONSE), 200
    _reset_requests[key] = recent + [now]

    user = User.query.filter_by(email=email).first()
    if user:
        # Only the newest link works.
        PasswordResetToken.query.filter_by(user_id=user.id, used=False)\
            .update({'used': True}, synchronize_session=False)
        raw = secrets.token_urlsafe(32)
        db.session.add(PasswordResetToken(
            user_id=user.id, token_hash=_hash_reset_token(raw),
            created_at=now, expires_at=now + RESET_TOKEN_LIFETIME,
        ))
        db.session.commit()
        try:
            send_password_reset_email(user.email, user.name or 'there', raw)
        except Exception as exc:
            current_app.logger.error(f'Password reset email failed: {exc}')
    return jsonify(_FORGOT_RESPONSE), 200

# Wrong authenticator codes per reset link (in memory; the link is burned at
# the limit, and links only live 30 minutes anyway).
_reset_code_failures = {}
RESET_CODE_MAX_FAILURES = 5


@bp.route('/api/reset-password', methods=['POST'])
def reset_password():
    data    = request.get_json(silent=True) or {}
    raw     = str(data.get('token') or '')
    new_pwd = data.get('new_password')
    if not raw or not isinstance(new_pwd, str):
        return jsonify({'error': 'Token and new password are required'}), 400
    if len(new_pwd) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    if len(new_pwd) > 128:
        return jsonify({'error': 'Password is too long'}), 400

    row = PasswordResetToken.query.filter_by(token_hash=_hash_reset_token(raw)).first()
    if not row or row.used or row.expires_at < datetime.utcnow():
        return jsonify({'error': 'This reset link is invalid or has expired. Request a new one.'}), 400
    user = User.query.get(row.user_id)
    if not user:
        return jsonify({'error': 'This reset link is invalid or has expired. Request a new one.'}), 400

    # Authenticator users must also prove they have the app (or a recovery
    # code), so control of the email inbox alone can't take the account.
    if user.mfa_enabled and user.mfa_method == 'totp':
        code = str(data.get('code') or '').strip()
        if not code:
            return jsonify({'error': 'Enter a code from your authenticator app or a recovery code.',
                            'mfa_code_required': True}), 403
        if not check_app_or_recovery_code(user, code):
            fails = _reset_code_failures.get(row.id, 0) + 1
            _reset_code_failures[row.id] = fails
            if fails >= RESET_CODE_MAX_FAILURES:
                _reset_code_failures.pop(row.id, None)
                PasswordResetToken.query.filter_by(id=row.id)\
                    .update({'used': True}, synchronize_session=False)
                db.session.commit()
                return jsonify({'error': 'Too many wrong codes. Request a new reset link.'}), 400
            return jsonify({'error': "That code isn't right.", 'mfa_code_required': True}), 403
        _reset_code_failures.pop(row.id, None)

    # Claim the token atomically so a link can't be used twice concurrently.
    claimed = PasswordResetToken.query.filter_by(id=row.id, used=False)\
        .update({'used': True}, synchronize_session=False)
    if not claimed:
        db.session.rollback()
        return jsonify({'error': 'This reset link is invalid or has expired. Request a new one.'}), 400
    user.password_hash = generate_password_hash(new_pwd)
    db.session.commit()

    # Clear any login lockout for this account so the user can sign in now.
    for k in [k for k in _login_failures if k[1] == user.email]:
        _login_failures.pop(k, None)
    return jsonify({'message': 'Password updated. You can now sign in.'}), 200


@bp.route('/api/change-password', methods=['POST'])
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

@bp.route('/api/delete-account', methods=['DELETE'])
@token_required
def delete_account():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True) or {}
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
            # Vault cover art is R2-hosted too — collect before the row is gone.
            if vault.cover_url:
                r2_urls_to_delete.append(vault.cover_url)
            # Child-claim tokens reference vault_id (non-nullable) — must go
            # before the vault row or they become orphaned.
            ChildClaimToken.query.filter_by(
                vault_id=vault.id
            ).delete(synchronize_session=False)
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
        PasswordResetToken.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        MfaChallenge.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        RecoveryCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)

        # ── Phase 3: legacy cleanup ───────────────────────────────────────────────
        # Tokens this user created for vaults they do NOT own (created_by is a
        # non-nullable FK to user, so these would orphan on user deletion).
        ChildClaimToken.query.filter_by(
            created_by=user.id
        ).delete(synchronize_session=False)

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

        # ── Best-effort storage cleanup ──────────────────────────────────────
        # The DB commit above is the point of no return: the account IS gone.
        # A storage failure must never surface as a failed deletion, or the
        # client keeps its session token for an account that no longer exists.
        # Each object is isolated so one failure cannot skip the rest.
        failed = 0
        for url in r2_urls_to_delete:
            try:
                delete_object(url)
            except Exception:
                failed += 1  # URL itself is not logged — may contain a token
        if failed:
            current_app.logger.warning(
                "Account deletion: %d/%d storage objects could not be removed",
                failed, len(r2_urls_to_delete),
            )
        return jsonify({'message': 'Account deleted'}), 200

    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Account deletion failed. Please try again.'}), 500
