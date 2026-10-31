"""Encryption at rest for legacy Memory content (Fernet, key from ENCRYPTION_KEY)."""
import os
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

# --- SECURITY & ENCRYPTION ---
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY environment variable is not set")

# Derive a valid Fernet key (32 url-safe base64 bytes) from ENCRYPTION_KEY,
# so any sufficiently random string in .env works.
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(ENCRYPTION_KEY.encode()).digest()))
_FERNET_PREFIX = b'fernet:'

def encrypt_content(content):
    if not content: return b''
    return _FERNET_PREFIX + _fernet.encrypt(content.encode())

def decrypt_content(encrypted_content):
    if not encrypted_content: return ''
    if isinstance(encrypted_content, str):
        encrypted_content = encrypted_content.encode()
    if encrypted_content.startswith(_FERNET_PREFIX):
        try:
            return _fernet.decrypt(encrypted_content[len(_FERNET_PREFIX):]).decode()
        except InvalidToken:
            return ''
    # Legacy rows: repeating-key XOR (kept read-only for backwards compatibility).
    try:
        key_bytes = ENCRYPTION_KEY.encode()
        encrypted_bytes = base64.b64decode(encrypted_content)
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_bytes):
            decrypted.append(byte ^ key_bytes[i % len(key_bytes)])
        return decrypted.decode()
    except Exception:
        return str(encrypted_content)
