"""Two-step verification by email code.

Login with 2FA on:   POST /api/login                -> {mfa_required, mfa_token}
                     POST /api/login/mfa            {mfa_token, code} -> {token}
                     POST /api/login/mfa/resend     {mfa_token} -> {mfa_token}
Settings:            POST /api/mfa/enable           (emails a code)
                     POST /api/mfa/enable/confirm   {code}
                     POST /api/mfa/disable          {password}

Codes: 6 digits, single use, valid 10 minutes, 5 wrong tries per code, and at
most MAX_CODES_PER_HOUR codes per user per purpose (caps guessing and email
spam). Only an HMAC of each code is stored.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, g, current_app
from werkzeug.security import check_password_hash

from extensions import db
from models import MfaChallenge, User
from security import token_required, issue_token, issue_mfa_token, read_mfa_token
from emailer import send_verification_code_email

bp = Blueprint('mfa', __name__)

CODE_LIFETIME      = timedelta(minutes=10)
MAX_ATTEMPTS       = 5
MAX_CODES_PER_HOUR = 5
RESEND_COOLDOWN    = timedelta(seconds=30)


class TooManyCodes(Exception):
    pass


def _hash_code(challenge_id, code):
    key = current_app.config['SECRET_KEY'].encode()
    return hmac.new(key, f'{challenge_id}:{code}'.encode(), hashlib.sha256).hexdigest()


def mask_email(email):
    local, _, domain = email.partition('@')
    return (local[:1] + '•••' + '@' + domain) if domain else '•••'


def start_challenge(user, purpose, now=None):
    """Invalidate open codes for this purpose, create a new one, email it.

    Raises TooManyCodes if the hourly cap is reached, and lets email errors
    propagate (the caller turns them into a 503).
    """
    now = now or datetime.utcnow()
    recent = MfaChallenge.query.filter(
        MfaChallenge.user_id == user.id,
        MfaChallenge.purpose == purpose,
        MfaChallenge.created_at > now - timedelta(hours=1),
    ).count()
    if recent >= MAX_CODES_PER_HOUR:
        raise TooManyCodes()
    MfaChallenge.query.filter_by(user_id=user.id, purpose=purpose, used=False)\
        .update({'used': True}, synchronize_session=False)
    code = f'{secrets.randbelow(1_000_000):06d}'
    ch = MfaChallenge(user_id=user.id, purpose=purpose, code_hash='pending',
                      created_at=now, expires_at=now + CODE_LIFETIME)
    db.session.add(ch)
    db.session.flush()                       # need the id for the HMAC
    ch.code_hash = _hash_code(ch.id, code)
    db.session.commit()
    send_verification_code_email(user.email, user.first_name or user.name or 'there', code, purpose)
    return ch


def check_code(user, challenge_id, purpose, code, now=None):
    """Return 'ok', 'wrong' or 'expired'. Wrong guesses are counted; the code
    dies after MAX_ATTEMPTS."""
    now = now or datetime.utcnow()
    ch = MfaChallenge.query.filter_by(id=challenge_id, user_id=user.id, purpose=purpose).first()
    if not ch or ch.used or ch.expires_at < now or ch.attempts >= MAX_ATTEMPTS:
        return 'expired'
    code = ''.join(c for c in str(code or '') if c.isdigit())
    if len(code) == 6 and hmac.compare_digest(ch.code_hash, _hash_code(ch.id, code)):
        claimed = MfaChallenge.query.filter_by(id=ch.id, used=False)\
            .update({'used': True}, synchronize_session=False)
        db.session.commit()
        return 'ok' if claimed else 'expired'
    ch.attempts += 1
    if ch.attempts >= MAX_ATTEMPTS:
        ch.used = True
    db.session.commit()
    return 'expired' if ch.used else 'wrong'


def begin_login(user):
    """Called by /api/login after a correct password for a 2FA user."""
    try:
        ch = start_challenge(user, 'login')
    except TooManyCodes:
        return jsonify({'error': 'Too many codes requested. Try again in an hour.'}), 429
    except Exception as exc:
        current_app.logger.error(f'Sending login code failed: {exc}')
        return jsonify({'error': "We couldn't send your sign-in code. Try again shortly."}), 503
    return jsonify({
        'mfa_required': True,
        'mfa_token': issue_mfa_token(user, ch.id),
        'email_hint': mask_email(user.email),
    }), 200


# ── Login: second step ───────────────────────────────────────────────────────

@bp.route('/api/login/mfa', methods=['POST'])
def verify_login_code():
    data = request.get_json(silent=True) or {}
    pending = read_mfa_token(str(data.get('mfa_token') or ''))
    if not pending:
        return jsonify({'error': 'Your sign-in expired. Please sign in again.'}), 401
    user, cid = pending
    result = check_code(user, cid, 'login', data.get('code'))
    if result == 'ok':
        return jsonify({'message': 'Login successful', 'token': issue_token(user)}), 200
    if result == 'wrong':
        return jsonify({'error': "That code isn't right. Check the email and try again."}), 401
    return jsonify({'error': 'This code has expired. Send a new one.', 'code_expired': True}), 401


@bp.route('/api/login/mfa/resend', methods=['POST'])
def resend_login_code():
    data = request.get_json(silent=True) or {}
    pending = read_mfa_token(str(data.get('mfa_token') or ''))
    if not pending:
        return jsonify({'error': 'Your sign-in expired. Please sign in again.'}), 401
    user, cid = pending
    last = MfaChallenge.query.get(cid)
    if last and datetime.utcnow() - last.created_at < RESEND_COOLDOWN:
        return jsonify({'error': 'Please wait a few seconds before asking for another code.'}), 429
    return begin_login(user)


# ── Settings: turn 2-step verification on / off ──────────────────────────────

@bp.route('/api/mfa/enable', methods=['POST'])
@token_required
def enable_start():
    user = User.query.get(g.user_id)
    if user.mfa_enabled:
        return jsonify({'error': '2-step verification is already on.'}), 400
    try:
        start_challenge(user, 'enable')
    except TooManyCodes:
        return jsonify({'error': 'Too many codes requested. Try again in an hour.'}), 429
    except Exception as exc:
        current_app.logger.error(f'Sending enable code failed: {exc}')
        return jsonify({'error': "We couldn't send the code. Try again shortly."}), 503
    return jsonify({'message': 'Code sent', 'email_hint': mask_email(user.email)}), 200


@bp.route('/api/mfa/enable/confirm', methods=['POST'])
@token_required
def enable_confirm():
    user = User.query.get(g.user_id)
    data = request.get_json(silent=True) or {}
    ch = MfaChallenge.query.filter_by(user_id=user.id, purpose='enable', used=False)\
        .order_by(MfaChallenge.id.desc()).first()
    result = check_code(user, ch.id if ch else None, 'enable', data.get('code'))
    if result == 'ok':
        user.mfa_enabled = True
        db.session.commit()
        return jsonify({'message': '2-step verification is on', 'mfaEnabled': True}), 200
    if result == 'wrong':
        return jsonify({'error': "That code isn't right."}), 400
    return jsonify({'error': 'This code has expired. Send a new one.', 'code_expired': True}), 400


@bp.route('/api/mfa/disable', methods=['POST'])
@token_required
def disable():
    user = User.query.get(g.user_id)
    data = request.get_json(silent=True) or {}
    if not check_password_hash(user.password_hash, str(data.get('password') or '')):
        return jsonify({'error': 'Password is incorrect.'}), 400
    user.mfa_enabled = False
    MfaChallenge.query.filter_by(user_id=user.id, used=False)\
        .update({'used': True}, synchronize_session=False)
    db.session.commit()
    return jsonify({'message': '2-step verification is off', 'mfaEnabled': False}), 200
