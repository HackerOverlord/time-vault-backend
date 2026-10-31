"""Vaults: CRUD, cover, members, invites, child-vault claims."""
import os
import hashlib
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g, current_app

from extensions import db
from models import ChildClaimToken, Post, PostComment, PostLike, User, Vault, VaultMember
from security import issue_token, token_required, user_from_token
from helpers import (
    create_notification,
    generate_invite_code,
    generate_invite_token,
    notify_vault_members,
    require_vault_member,
    require_vault_owner,
    serialize_vault,
)
from emailer import send_claim_invite_email
from r2 import delete_object, is_r2_url, upload_file_object

bp = Blueprint('vaults', __name__)

@bp.route('/api/vaults', methods=['GET'])
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


@bp.route('/api/vaults', methods=['POST'])
@token_required
def create_vault():
    import re as _re
    data = request.get_json(silent=True) or {}
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


@bp.route('/api/vaults/<int:vault_id>', methods=['DELETE'])
@token_required
def delete_vault_v1(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    confirm = (request.get_json(silent=True) or {}).get('confirm_name', '')
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
    if vault.cover_url:                       # cover art is R2-hosted too
        media_urls.append(vault.cover_url)
    if post_ids:
        PostLike.query.filter(PostLike.post_id.in_(post_ids)).delete(synchronize_session=False)
        PostComment.query.filter(PostComment.post_id.in_(post_ids)).delete(synchronize_session=False)
    Post.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    VaultMember.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    # Child-claim tokens hold a non-nullable FK to this vault.
    ChildClaimToken.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    db.session.delete(vault)
    db.session.commit()

    # Best-effort storage cleanup — the vault is already gone, so a storage
    # failure must not surface as a failed delete. Isolated per object.
    failed = 0
    for url in media_urls:
        try:
            delete_object(url)
        except Exception:
            failed += 1  # URL not logged — may contain a token
    if failed:
        current_app.logger.warning(
            "Vault deletion: %d/%d storage objects could not be removed",
            failed, len(media_urls),
        )
    return '', 204


@bp.route('/api/vaults/<int:vault_id>', methods=['PUT'])
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


@bp.route('/api/vaults/<int:vault_id>/cover', methods=['POST'])
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
@bp.route('/api/vaults/<int:vault_id>/claim-invite', methods=['POST'])
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
            current_app.logger.error('Failed to send claim invite email: %s', exc)
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
@bp.route('/api/claim-info', methods=['GET'])
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
@bp.route('/api/claim-vault', methods=['POST'])
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
            claimer = user_from_token(auth_header[7:])
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
        response['access_token'] = issue_token(claimer)

    return jsonify(response), 200


@bp.route('/api/vaults/<int:vault_id>/seen', methods=['POST'])
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

@bp.route('/api/vaults/<int:vault_id>/members', methods=['GET'])
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


@bp.route('/api/vaults/<int:vault_id>/members/<int:user_id>', methods=['DELETE'])
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


@bp.route('/api/vaults/<int:vault_id>/leave', methods=['DELETE'])
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


@bp.route('/api/vaults/join', methods=['POST'])
@token_required
def join_vault():
    data = request.get_json(silent=True) or {}
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


@bp.route('/api/vaults/<int:vault_id>/invite/regenerate', methods=['POST'])
@token_required
def regenerate_invite_code(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    vault.invite_code  = generate_invite_code()
    vault.invite_token = generate_invite_token()
    db.session.commit()
    return jsonify({
        'invite_code':  vault.invite_code,
        'invite_token': vault.invite_token,
    }), 200


# ── Pass 31: Shareable invite links ─────────────────────────────────────────

@bp.route('/api/invites/<string:token>', methods=['GET'])
@token_required
def resolve_invite(token):
    """Return public-safe vault info for an invite token (no side effects)."""
    vault = Vault.query.filter_by(invite_token=token).first()
    if not vault:
        return jsonify({'error': 'Invite not found or expired'}), 404
    creator = User.query.get(vault.created_by)
    already_member = VaultMember.query.filter_by(
        vault_id=vault.id, user_id=g.user_id
    ).first() is not None
    response = {
        'valid':          True,
        'vault_id':       str(vault.id),
        'vault_name':     vault.name,
        'vault_cover':    vault.cover_url,
        'accent_color':   vault.accent_color,
        'inviter_name':   creator.name if creator else 'Someone',
        'already_member': already_member,
    }
    # If already a member, include the serialized vault so the frontend
    # can navigate directly to it without a second API round-trip.
    if already_member:
        vm = VaultMember.query.filter_by(vault_id=vault.id, user_id=g.user_id).first()
        member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
        response['vault'] = serialize_vault(vault, vm, member_count)
    return jsonify(response), 200


@bp.route('/api/invites/<string:token>/join', methods=['POST'])
@token_required
def join_via_token(token):
    """Join a vault via shareable invite link. Idempotent — 200 if already member."""
    vault = Vault.query.filter_by(invite_token=token).first()
    if not vault:
        return jsonify({'error': 'Invite not found or expired'}), 404
    existing = VaultMember.query.filter_by(
        vault_id=vault.id, user_id=g.user_id
    ).first()
    if existing:
        member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
        return jsonify({
            'vault':          serialize_vault(vault, existing, member_count),
            'message':        f'You are already a member of {vault.name}',
            'already_member': True,
        }), 200
    now = datetime.utcnow()
    member = VaultMember(
        vault_id=vault.id, user_id=g.user_id,
        role='member', joined_at=now, invited_by=None, last_seen_at=now,
    )
    joiner = User.query.get(g.user_id)
    existing_members = VaultMember.query.filter_by(vault_id=vault.id).all()
    db.session.add(member)
    for em in existing_members:
        create_notification(
            em.user_id, 'member_joined',
            f'{joiner.name} joined {vault.name}.',
            vault_id=vault.id,
        )
    create_notification(
        g.user_id, 'member_joined',
        f'You joined {vault.name}.',
        vault_id=vault.id,
    )
    db.session.commit()
    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    new_vm = VaultMember.query.filter_by(vault_id=vault.id, user_id=g.user_id).first()
    return jsonify({
        'vault':          serialize_vault(vault, new_vm, member_count),
        'message':        f'You joined {vault.name}',
        'already_member': False,
    }), 201
