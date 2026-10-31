"""Database models."""
from datetime import datetime

from sqlalchemy import UniqueConstraint

from extensions import db
from crypto import encrypt_content, decrypt_content

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
    # Shareable link token — secrets.token_urlsafe(32), 43 URL-safe chars
    invite_token = db.Column(db.String(64), unique=True, nullable=True)
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


class PasswordResetToken(db.Model):
    """Single-use hashed token for resetting a forgotten password."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    # SHA-256 hex digest of the plaintext token — plaintext never stored
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
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
