"""Two-step verification: an emailed code, or an authenticator app (TOTP).

Login with 2FA on:   POST /api/login                -> {mfa_required, mfa_token, method}
                     POST /api/login/mfa            {mfa_token, code} -> {token}
                     POST /api/login/mfa/resend     {mfa_token} -> {mfa_token}   (email only)
Settings, email:     POST /api/mfa/enable           (emails a code)
                     POST /api/mfa/enable/confirm   {code}
Settings, app:       POST /api/mfa/totp/setup       -> {secret, otpauth_uri}
                     POST /api/mfa/totp/confirm     {code} -> {recovery_codes}
                     POST /api/mfa/recovery-codes   {password} -> {recovery_codes}
Either:              POST /api/mfa/disable          {password}
Sign-up prompt:      POST /api/me/mfa-prompt/dismiss

Authenticator users can sign in with a single-use recovery code instead of an
app code, and must give an app or recovery code to reset a forgotten password
(so someone who only controls their email can't take the account over).

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
from models import MfaChallenge, RecoveryCode, User
import totp
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


def _start_app_challenge(user, now=None):
    """Login attempt record for authenticator users: nothing is sent, but the
    row carries the wrong-guess counter and the hourly cap."""
    now = now or datetime.utcnow()
    recent = MfaChallenge.query.filter(
        MfaChallenge.user_id == user.id,
        MfaChallenge.purpose == 'login',
        MfaChallenge.created_at > now - timedelta(hours=1),
    ).count()
    if recent >= MAX_CODES_PER_HOUR:
        raise TooManyCodes()
    MfaChallenge.query.filter_by(user_id=user.id, purpose='login', used=False)\
        .update({'used': True}, synchronize_session=False)
    ch = MfaChallenge(user_id=user.id, purpose='login', code_hash='totp',
                      created_at=now, expires_at=now + CODE_LIFETIME)
    db.session.add(ch)
    db.session.commit()
    return ch


def begin_login(user):
    """Called by /api/login after a correct password for a 2FA user."""
    is_app = user.mfa_method == 'totp'
    try:
        ch = _start_app_challenge(user) if is_app else start_challenge(user, 'login')
    except TooManyCodes:
        msg = ('Too many sign-in attempts. Try again in an hour.' if is_app
               else 'Too many codes requested. Try again in an hour.')
        return jsonify({'error': msg}), 429
    except Exception as exc:
        current_app.logger.error(f'Sending login code failed: {exc}')
        return jsonify({'error': "We couldn't send your sign-in code. Try again shortly."}), 503
    body = {
        'mfa_required': True,
        'mfa_token': issue_mfa_token(user, ch.id),
        'method': 'totp' if is_app else 'email',
    }
    if not is_app:
        body['email_hint'] = mask_email(user.email)
    return jsonify(body), 200


# ── Authenticator app + recovery codes ───────────────────────────────────────

def check_app_or_recovery_code(user, code):
    """Return 'app', 'recovery' or None. Consumes the code on success: an app
    code can't be replayed, and a recovery code works once."""
    secret_enc = user.totp_secret_enc
    if not secret_enc:
        return None
    if totp.looks_like_recovery_code(code):
        h = totp.hash_recovery_code(code)
        row = RecoveryCode.query.filter_by(user_id=user.id, code_hash=h, used_at=None).first()
        if not row:
            return None
        claimed = RecoveryCode.query.filter_by(id=row.id, used_at=None)\
            .update({'used_at': datetime.utcnow()}, synchronize_session=False)
        db.session.commit()
        return 'recovery' if claimed else None
    step = totp.verify(totp.decrypt_secret(secret_enc), code, user.totp_last_step)
    if step is None:
        return None
    # Only advance if nobody else used this step first (two tabs, a replay).
    last = user.totp_last_step
    q = User.query.filter_by(id=user.id)
    q = q.filter(User.totp_last_step.is_(None)) if last is None else q.filter(User.totp_last_step == last)
    claimed = q.update({'totp_last_step': step}, synchronize_session=False)
    db.session.commit()
    db.session.refresh(user)
    return 'app' if claimed else None


def recovery_codes_left(user):
    if user.mfa_method != 'totp' or not user.mfa_enabled:
        return 0
    return RecoveryCode.query.filter_by(user_id=user.id, used_at=None).count()


def _issue_recovery_codes(user):
    RecoveryCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    codes = totp.new_recovery_codes()
    for c in codes:
        db.session.add(RecoveryCode(user_id=user.id, code_hash=totp.hash_recovery_code(c)))
    return codes


def _clear_app_settings(user):
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.totp_last_step = None
    RecoveryCode.query.filter_by(user_id=user.id).delete(synchronize_session=False)


def _check_app_login(user, cid, code):
    """Second step for authenticator users, with the wrong-guess counter
    kept on the login attempt row."""
    now = datetime.utcnow()
    ch = MfaChallenge.query.filter_by(id=cid, user_id=user.id, purpose='login').first()
    if not ch or ch.used or ch.expires_at < now or ch.attempts >= MAX_ATTEMPTS:
        return 'expired', None
    how = check_app_or_recovery_code(user, code)
    if how:
        MfaChallenge.query.filter_by(id=ch.id).update({'used': True}, synchronize_session=False)
        db.session.commit()
        return 'ok', how
    ch = MfaChallenge.query.get(cid)
    ch.attempts += 1
    if ch.attempts >= MAX_ATTEMPTS:
        ch.used = True
    db.session.commit()
    return ('expired' if ch.used else 'wrong'), None


# ── Login: second step ───────────────────────────────────────────────────────

@bp.route('/api/login/mfa', methods=['POST'])
def verify_login_code():
    data = request.get_json(silent=True) or {}
    pending = read_mfa_token(str(data.get('mfa_token') or ''))
    if not pending:
        return jsonify({'error': 'Your sign-in expired. Please sign in again.'}), 401
    user, cid = pending
    if user.mfa_method == 'totp':
        result, how = _check_app_login(user, cid, data.get('code'))
        if result == 'ok':
            body = {'message': 'Login successful', 'token': issue_token(user)}
            if how == 'recovery':
                body['recovery_codes_left'] = recovery_codes_left(user)
            return jsonify(body), 200
        if result == 'wrong':
            return jsonify({'error': "That code isn't right. Check your authenticator app and try again."}), 401
        return jsonify({'error': 'Too many tries. Please sign in again.', 'code_expired': True}), 401
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
    if user.mfa_method == 'totp':
        return jsonify({'error': 'Codes come from your authenticator app, not email.'}), 400
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
        user.mfa_method = 'email'
        _clear_app_settings(user)
        db.session.commit()
        return jsonify({'message': '2-step verification is on', 'mfaEnabled': True,
                        'mfaMethod': 'email'}), 200
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
    user.mfa_method = 'email'
    _clear_app_settings(user)
    MfaChallenge.query.filter_by(user_id=user.id, used=False)\
        .update({'used': True}, synchronize_session=False)
    db.session.commit()
    return jsonify({'message': '2-step verification is off', 'mfaEnabled': False}), 200


@bp.route('/api/mfa/totp/setup', methods=['POST'])
@token_required
def totp_setup():
    """Make a new secret for the QR code. Nothing changes until it's confirmed."""
    user = User.query.get(g.user_id)
    if user.mfa_enabled:
        return jsonify({'error': 'Turn off 2-step verification first, then choose a new method.'}), 400
    secret = totp.new_secret()
    user.totp_pending_secret_enc = totp.encrypt_secret(secret)
    db.session.commit()
    return jsonify({'secret': secret,
                    'otpauth_uri': totp.provisioning_uri(secret, user.email)}), 200


@bp.route('/api/mfa/totp/confirm', methods=['POST'])
@token_required
def totp_confirm():
    user = User.query.get(g.user_id)
    if user.mfa_enabled:
        return jsonify({'error': '2-step verification is already on.'}), 400
    if not user.totp_pending_secret_enc:
        return jsonify({'error': 'Start the set-up again.', 'code_expired': True}), 400
    data = request.get_json(silent=True) or {}
    secret = totp.decrypt_secret(user.totp_pending_secret_enc)
    step = totp.verify(secret, data.get('code'), None)
    if step is None:
        return jsonify({'error': "That code isn't right. Make sure your phone's clock is set automatically."}), 400
    user.totp_secret_enc = user.totp_pending_secret_enc
    user.totp_pending_secret_enc = None
    user.totp_last_step = step
    user.mfa_method = 'totp'
    user.mfa_enabled = True
    MfaChallenge.query.filter_by(user_id=user.id, used=False)\
        .update({'used': True}, synchronize_session=False)
    codes = _issue_recovery_codes(user)
    db.session.commit()
    return jsonify({'message': '2-step verification is on', 'mfaEnabled': True,
                    'mfaMethod': 'totp', 'recovery_codes': codes}), 200


@bp.route('/api/mfa/recovery-codes', methods=['POST'])
@token_required
def regenerate_recovery_codes():
    user = User.query.get(g.user_id)
    data = request.get_json(silent=True) or {}
    if not check_password_hash(user.password_hash, str(data.get('password') or '')):
        return jsonify({'error': 'Password is incorrect.'}), 400
    if not (user.mfa_enabled and user.mfa_method == 'totp'):
        return jsonify({'error': 'Recovery codes are only used with an authenticator app.'}), 400
    codes = _issue_recovery_codes(user)
    db.session.commit()
    return jsonify({'recovery_codes': codes}), 200


@bp.route('/api/me/mfa-prompt/dismiss', methods=['POST'])
@token_required
def dismiss_prompt():
    user = User.query.get(g.user_id)
    user.mfa_prompt_dismissed = True
    db.session.commit()
    return jsonify({'mfaPromptDismissed': True}), 200
