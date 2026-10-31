"""Posts: feed, create (incl. time capsules), archive, delete, likes, comments, dashboard."""
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g
from sqlalchemy import func

from extensions import db
from models import Post, PostComment, PostLike, User, Vault, VaultMember
from security import token_required
from helpers import (
    _MAX_MEDIA_URL_BYTES,
    _MAX_POST_IMAGE_DECODED_BYTES,
    _MAX_POST_VIDEO_DECODED_BYTES,
    _POST_IMAGE_MIME_ALLOWLIST,
    _POST_VIDEO_MIME_ALLOWLIST,
    create_notification,
    notify_vault_members,
    require_vault_member,
    require_vault_owner,
    serialize_comment,
    serialize_post,
    utc_iso,
    validate_data_uri,
    validate_multipart_media,
    vault_forbidden,
)

from r2 import delete_object, is_r2_url, upload_media, upload_post_media

bp = Blueprint('posts', __name__)

# ── Pass 22: Legacy base64 migration utility ─────────────────────────────────
# This endpoint can be called to migrate a single post's base64 media_url to R2.
# Run as a one-off background job; never runs automatically.
@bp.route('/api/posts/<int:post_id>/migrate-media', methods=['POST'])
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


# --- V1 ROUTES: POSTS (3C) ---

_ALLOWED_MEDIA_TYPES = {'video', 'image', 'text'}

# Fields the frontend currently sends that do not exist in the V1 backend contract.
# Rejected explicitly so Milestone 4 wiring mistakes surface immediately.
_REJECTED_POST_FIELDS = {'group_id', 'recipient_ids'}


@bp.route('/api/posts', methods=['GET'])
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


@bp.route('/api/vaults/<int:vault_id>/posts', methods=['POST'])
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
        data       = request.get_json(silent=True) or {}
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
        # "Tomorrow" is judged in the poster's own timezone. The client sends
        # tz_offset_minutes = JS Date.getTimezoneOffset() (UTC minus local,
        # e.g. 240 for Toronto in summer). Missing/invalid → treat as UTC.
        try:
            tz_offset = int(data.get('tz_offset_minutes', 0))
        except (ValueError, TypeError):
            tz_offset = 0
        if not -840 <= tz_offset <= 720:
            tz_offset = 0
        local_today = (datetime.utcnow() - timedelta(minutes=tz_offset)).date()
        if unlock_dt.date() <= local_today:
            return jsonify({'error': 'Unlock date must be at least tomorrow'}), 400
        # A date-only unlock means midnight in the poster's timezone, stored as UTC.
        if 'T' not in str(unlock_at_str):
            unlock_dt = unlock_dt + timedelta(minutes=tz_offset)

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
@bp.route('/api/posts/<int:post_id>/archive', methods=['POST'])
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


@bp.route('/api/posts/<int:post_id>', methods=['DELETE'])
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

@bp.route('/api/posts/<int:post_id>/like', methods=['POST'])
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


@bp.route('/api/posts/<int:post_id>/like', methods=['DELETE'])
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


@bp.route('/api/posts/<int:post_id>/comments', methods=['GET'])
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


@bp.route('/api/posts/<int:post_id>/comments', methods=['POST'])
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
    data = request.get_json(silent=True) or {}
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


@bp.route('/api/comments/<int:comment_id>', methods=['DELETE'])
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

@bp.route('/api/dashboard', methods=['GET'])
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
            'unlock_at':        utc_iso(cap.unlock_at),
            'days_until_unlock': days_until_unlock,
        })

    return jsonify({'vaults': vault_cards, 'upcoming_capsules': upcoming}), 200
