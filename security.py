"""Authentication: JWT issue/verify, the token_required decorator, and login rate limiting."""
import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps

import jwt
from flask import request, jsonify, g, current_app

from models import User

# ── JWT helpers ──────────────────────────────────────────────────────────────
# Each token carries a fingerprint of the user's current password hash ('pwv').
# Changing the password changes the fingerprint, so every token issued before
# the change stops working. Deleted users fail the lookup. No DB column needed.
TOKEN_LIFETIME = timedelta(days=7)

def _password_fingerprint(user):
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]

def issue_token(user):
    return jwt.encode({
        'user_id': user.id,
        'pwv': _password_fingerprint(user),
        'exp': datetime.utcnow() + TOKEN_LIFETIME,
    }, current_app.config['SECRET_KEY'], algorithm='HS256')

def user_from_token(token):
    """Return the User for a valid, current token, else None.
    Raises jwt.ExpiredSignatureError for expired tokens."""
    data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
    user = User.query.get(data.get('user_id'))
    if not user or not secrets.compare_digest(str(data.get('pwv', '')), _password_fingerprint(user)):
        return None
    return user

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Unauthorized'}), 401
        try:
            user = user_from_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Unauthorized'}), 401
        if user is None:
            return jsonify({'error': 'Session expired. Please log in again.'}), 401
        g.user_id = user.id
        return f(*args, **kwargs)
    return decorated

# ── Login rate limiting ──────────────────────────────────────────────────────
# In-memory, per worker process: max LOGIN_MAX_FAILURES failed attempts per
# (client IP, email) within LOGIN_WINDOW. Successful login clears the counter.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = timedelta(minutes=15)
_login_failures = {}

def _client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    return forwarded.split(',')[0].strip() if forwarded else (request.remote_addr or '')

def _recent_failures(key, now):
    attempts = [t for t in _login_failures.get(key, []) if now - t < LOGIN_WINDOW]
    if attempts:
        _login_failures[key] = attempts
    else:
        _login_failures.pop(key, None)
    return attempts
