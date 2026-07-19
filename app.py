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
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///time_capsule.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'protected_media'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

db = SQLAlchemy(app)




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


def create_notification(user_id, type, message):
    notif = Notification(user_id=user_id, type=type, message=message)
    db.session.add(notif)
    # No commit here — caller commits


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
    author    — User row for post.author_id
    vault     — Vault row for post.vault_id
    liked_set — set of post_id integers the requesting user has liked
    """
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
        'is_unlocked':   post.is_unlocked,
        'posted_at':     post.posted_at.isoformat() if post.posted_at else None,
        'created_at':    post.created_at.isoformat(),
        'like_count':    post.like_count,
        'comment_count': post.comment_count,
        'has_liked':     post.id in liked_set,
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


# --- V1 ROUTES: VAULTS (3A) ---

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
        entry = {
            'id':           str(vault.id),
            'name':         vault.name,
            'created_by':   str(vault.created_by),
            'created_at':   vault.created_at.isoformat(),
            'member_count': member_count,
            'unread_count': unread,
            'user_role':    vm.role,
        }
        if vm.role == 'owner':
            entry['invite_code'] = vault.invite_code
        result.append(entry)
    return jsonify(result), 200


@app.route('/api/vaults', methods=['POST'])
@token_required
def create_vault():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Vault name is required'}), 400
    if len(name) > 50:
        return jsonify({'error': 'Vault name cannot exceed 50 characters'}), 400
    now = datetime.utcnow()
    vault = Vault(
        name=name,
        invite_code=generate_invite_code(),
        created_by=g.user_id,
        created_at=now,
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
    return jsonify({
        'id':           str(vault.id),
        'name':         vault.name,
        'invite_code':  vault.invite_code,
        'created_by':   str(vault.created_by),
        'created_at':   vault.created_at.isoformat(),
        'member_count': 1,
        'unread_count': 0,
        'user_role':    'owner',
    }), 201


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
                f'{owner.name} deleted {vault.name}'
            )
    # Hard-delete all content in dependency order
    post_ids = [p.id for p in Post.query.filter_by(vault_id=vault_id).all()]
    if post_ids:
        PostLike.query.filter(PostLike.post_id.in_(post_ids)).delete(synchronize_session=False)
        PostComment.query.filter(PostComment.post_id.in_(post_ids)).delete(synchronize_session=False)
    Post.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    VaultMember.query.filter_by(vault_id=vault_id).delete(synchronize_session=False)
    db.session.delete(vault)
    db.session.commit()
    return '', 204


@app.route('/api/vaults/<int:vault_id>', methods=['PUT'])
@token_required
def rename_vault(vault_id):
    vault = Vault.query.get(vault_id)
    if not vault:
        return jsonify({'error': 'Vault not found'}), 404
    result = require_vault_owner(vault_id)
    if isinstance(result, tuple):
        return result
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Vault name is required'}), 400
    if len(name) > 50:
        return jsonify({'error': 'Vault name cannot exceed 50 characters'}), 400
    vault.name = name
    db.session.commit()
    vm = result  # already the VaultMember from require_vault_owner
    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    return jsonify({
        'id':           str(vault.id),
        'name':         vault.name,
        'invite_code':  vault.invite_code,
        'created_by':   str(vault.created_by),
        'created_at':   vault.created_at.isoformat(),
        'member_count': member_count,
        'unread_count': 0,
        'user_role':    vm.role,
    }), 200


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
    db.session.delete(target)
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
    db.session.delete(vm)
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
    db.session.add(member)
    db.session.commit()
    member_count = VaultMember.query.filter_by(vault_id=vault.id).count()
    return jsonify({
        'vault': {
            'id':           str(vault.id),
            'name':         vault.name,
            'member_count': member_count,
            'user_role':    'member',
        },
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

# Maximum allowed base64 media_url size (bytes).  The base64 string for a
# 5 MB file is ~6.7 MB; this constant enforces that ceiling on the server.
# The frontend enforces the same 5 MB limit before encoding.
_MAX_MEDIA_URL_BYTES = 7 * 1024 * 1024  # 7 MB base64 ceiling for a 5 MB file


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

    posts = (
        Post.query
        .filter(
            Post.vault_id.in_(member_vault_ids),
            Post.is_unlocked == True,
        )
        .order_by(Post.posted_at.desc())
        .all()
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

    data = request.get_json() or {}

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
    media_url = data.get('media_url')

    # Media size guard: reject oversized base64 payloads before any other work.
    if media_url is not None and len(str(media_url)) > _MAX_MEDIA_URL_BYTES:
        return jsonify({'error': 'File too large. Maximum upload size is 5 MB.'}), 413

    # Caption validation.
    if media_type == 'text':
        if not caption or not str(caption).strip():
            return jsonify({'error': 'Caption is required for text posts'}), 400
    if caption and len(str(caption)) > 500:
        return jsonify({'error': 'Caption cannot exceed 500 characters'}), 400

    # media_url validation.
    if media_type == 'video':
        if not media_url or not str(media_url).startswith('data:video/'):
            return jsonify({'error': 'media_url must be a valid video data URI for video posts'}), 400
    elif media_type == 'image':
        if not media_url or not str(media_url).startswith('data:image/'):
            return jsonify({'error': 'media_url must be a valid image data URI for image posts'}), 400
    else:  # text
        if media_url is not None:
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
    if unlock_dt:
        # Time capsule post.
        post = Post(
            vault_id=vault_id,
            author_id=g.user_id,
            caption=caption,
            media_type=media_type,
            media_url=media_url,
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
            media_url=media_url,
            unlock_at=None,
            is_unlocked=True,
            posted_at=now,
            created_at=now,
        )

    db.session.add(post)
    db.session.commit()

    author = User.query.get(g.user_id)
    return jsonify(serialize_post(post, author, vault, set())), 201


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
    db.session.delete(post)
    db.session.commit()
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
    existing = PostLike.query.filter_by(post_id=post_id, user_id=g.user_id).first()
    if existing:
        return jsonify({'error': 'You have already liked this post'}), 400
    like = PostLike(post_id=post_id, user_id=g.user_id, created_at=datetime.utcnow())
    post.like_count += 1
    db.session.add(like)
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
            f'{commenter.name} commented on your post in {vault.name}.'
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- NEW V1 MODELS ---

class Vault(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(50), nullable=False)
    invite_code = db.Column(db.String(6), unique=True, nullable=False)
    created_by  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


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
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')
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
    data = request.get_json()
    
    # Validation: Ensure email and password are provided
    if not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email and password are required'}), 400

    # Check if user already exists
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'Email already registered'}), 400

    user = User(
        name=data.get('name'),
        email=data['email'],
        password_hash=generate_password_hash(data['password'])
    )
    
    db.session.add(user)
    db.session.commit()
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
    code = ''.join(random.choices(chars, k=6))
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
    data = request.get_json()
    
    if data.get('avatar'):
        user.avatar = data.get('avatar')

    user.first_name = data.get('firstName', user.first_name)
    user.last_name = data.get('lastName', user.last_name)
    user.name = f"{user.first_name} {user.last_name}".strip()
    
    db.session.commit()
    return jsonify({'message': 'Profile updated successfully'}), 200


@app.route('/api/notifications', methods=['GET'])
@token_required
def get_notifications():
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notifs = Notification.query.filter_by(user_id=g.user_id)\
        .order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify([{
        'id': n.id,
        'type': n.type,
        'message': n.message,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat()
    } for n in notifs]), 200

@app.route('/api/notifications/read/<int:notif_id>', methods=['POST'])
@token_required
def mark_notification_read(notif_id):
    if not hasattr(g, 'user_id'):
        return jsonify({'error': 'Unauthorized'}), 401
    notif = Notification.query.get_or_404(notif_id)
    if notif.user_id != g.user_id:
        return jsonify({'error': 'Forbidden'}), 403
    notif.is_read = True
    db.session.commit()
    return jsonify({'message': 'Marked read'}), 200

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
    data = request.get_json()
    user = User.query.get(g.user_id)
    if not check_password_hash(user.password_hash, data.get('current_password', '')):
        return jsonify({'error': 'Current password is incorrect'}), 400
    if len(data.get('new_password', '')) < 8:
        return jsonify({'error': 'New password must be at least 8 characters'}), 400
    user.password_hash = generate_password_hash(data['new_password'])
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
        for vault in owned_vaults:
            vault_post_ids = [
                p.id for p in Post.query.filter_by(vault_id=vault.id).all()
            ]
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
        db.session.delete(user)
        db.session.commit()
        return jsonify({'message': 'Account deleted'}), 200

    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Account deletion failed. Please try again.'}), 500

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=False, port=5000)