"""
r2.py — Cloudflare R2 object-storage helpers for Time Vault.

All media (post images/videos and user avatars) is stored in R2 after the
migration.  The bucket is private; media is served via the public R2 URL or
a future custom domain configured in Cloudflare.

Required environment variables
───────────────────────────────
  R2_ACCOUNT_ID        Cloudflare account ID (shown on the R2 overview page)
  R2_ACCESS_KEY_ID     R2 API token → Access Key ID
  R2_SECRET_ACCESS_KEY R2 API token → Secret Access Key
  R2_BUCKET_NAME       Bucket name, e.g. "time-vault-media"
  R2_PUBLIC_URL        Public base URL, e.g. "https://pub-<hash>.r2.dev"
                       (no trailing slash)

If any R2 variable is missing the helpers raise RuntimeError so the
startup check catches the misconfiguration early.
"""

import base64
import logging
import os
import re
import secrets

import boto3

log = logging.getLogger(__name__)
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# MIME → extension mapping used for object key generation
# ---------------------------------------------------------------------------
_MIME_TO_EXT: dict[str, str] = {
    'image/jpeg':      'jpg',
    'image/jpg':       'jpg',
    'image/png':       'png',
    'image/gif':       'gif',
    'image/webp':      'webp',
    'video/mp4':       'mp4',
    'video/webm':      'webm',
    'video/quicktime': 'mov',
    'video/ogg':       'ogv',
}

# Regex that matches the header of a data URI.
_DATA_URI_RE = re.compile(r'^data:([^;]+);base64,(.+)$', re.DOTALL)


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"R2 environment variable '{name}' is not set")
    return v


def _client():
    """Return a boto3 S3 client pointed at the Cloudflare R2 endpoint."""
    account_id = _env('R2_ACCOUNT_ID')
    return boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=_env('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=_env('R2_SECRET_ACCESS_KEY'),
        region_name='auto',
    )


def is_r2_url(value: str | None) -> bool:
    """Return True if *value* is already an R2 / HTTPS URL (not a data URI)."""
    return bool(value and value.startswith('https://'))


def _parse_data_uri(data_uri: str) -> tuple[str, bytes]:
    """Parse *data_uri* → (mime_type, raw_bytes).

    Raises ValueError for invalid or non-base64 data URIs.
    """
    m = _DATA_URI_RE.match(data_uri)
    if not m:
        raise ValueError('Invalid data URI format')
    mime_type = m.group(1).lower()
    try:
        raw_bytes = base64.b64decode(m.group(2))
    except Exception as exc:
        raise ValueError(f'Cannot decode base64 content: {exc}') from exc
    return mime_type, raw_bytes


def upload_media(data_uri: str, prefix: str) -> str:
    """Upload a base64 data URI to R2 and return its public HTTPS URL.

    Parameters
    ──────────
    data_uri   Full data URI, e.g. "data:image/jpeg;base64,/9j/4AAQ..."
    prefix     Object-key prefix, e.g. "posts" or "avatars"
               (no trailing slash)

    Returns
    ───────
    The permanent HTTPS URL of the uploaded object.

    Raises
    ──────
    ValueError      if the data URI is malformed.
    RuntimeError    if an R2 environment variable is missing.
    BotoCoreError / ClientError  on network or API failures.
    """
    mime_type, raw_bytes = _parse_data_uri(data_uri)
    ext = _MIME_TO_EXT.get(mime_type, 'bin')
    key = f'{prefix}/{secrets.token_hex(16)}.{ext}'

    bucket = _env('R2_BUCKET_NAME')
    client = _client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=raw_bytes,
        ContentType=mime_type,
    )

    public_url = _env('R2_PUBLIC_URL').rstrip('/')
    return f'{public_url}/{key}'


def upload_file_object(file_bytes: bytes, prefix: str, mime_type: str) -> str:
    """Upload raw bytes to R2 and return the public HTTPS URL.

    Unlike upload_media(), this accepts raw binary data directly —
    no base64 encoding/decoding required. Used for cover image uploads
    where the client sends multipart/form-data.

    Parameters
    ──────────
    file_bytes  Raw binary content of the file.
    prefix      Object-key prefix, e.g. "covers" (no trailing slash).
    mime_type   MIME type string, e.g. "image/jpeg".

    Returns
    ───────
    The permanent HTTPS URL of the uploaded object.
    """
    ext = _MIME_TO_EXT.get(mime_type, 'bin')
    key = f'{prefix}/{secrets.token_hex(16)}.{ext}'

    bucket = _env('R2_BUCKET_NAME')
    client = _client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=file_bytes,
        ContentType=mime_type,
    )

    public_url = _env('R2_PUBLIC_URL').rstrip('/')
    return f'{public_url}/{key}'


def upload_post_media(file_bytes: bytes, filename: str, mime_type: str) -> str:
    """Upload raw post-media bytes to R2 and return the public HTTPS URL.

    Thin wrapper around upload_file_object() with the "posts" prefix and
    a filename-derived extension fallback.  Avoids duplicating the vault-
    cover upload logic.

    Parameters
    ──────────
    file_bytes  Raw binary content.
    filename    Original filename from the upload (used only for ext fallback).
    mime_type   Declared MIME type, e.g. "image/jpeg".

    Returns
    ───────
    Permanent HTTPS URL of the uploaded object.
    """
    return upload_file_object(file_bytes, prefix='posts', mime_type=mime_type)


def object_key_from_url(url: str) -> str | None:
    """Extract the R2 object key from a public URL.

    e.g. "https://pub-xxx.r2.dev/posts/abc.jpg" → "posts/abc.jpg"
    Returns None if the URL cannot be parsed.
    """
    if not url:
        return None
    public_url = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')
    if public_url and url.startswith(public_url + '/'):
        return url[len(public_url) + 1:]
    # Fallback: extract the path after the third slash
    # e.g. https://host/posts/abc.jpg → posts/abc.jpg
    parts = url.split('/', 3)
    return parts[3] if len(parts) == 4 else None


def delete_object(url: str) -> None:
    """Delete the R2 object identified by its public URL.

    Best-effort: deletion failures are logged via the application logger but
    never re-raised.  A missed deletion leaves an orphaned object that can be
    identified and removed from a future maintenance pass.

    Parameters
    ──────────
    url   Public HTTPS URL returned by upload_media(), or None / empty.
          If the value is a base64 data URI (legacy row), no action is taken.
    """
    if not url or not is_r2_url(url):
        return  # Nothing to delete — either empty or a legacy base64 value

    key = object_key_from_url(url)
    if not key:
        return

    try:
        bucket = _env('R2_BUCKET_NAME')
        client = _client()
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001
        # Non-fatal: log the failure so orphaned objects can be diagnosed.
        log.exception('R2 delete_object failed for key %r', key)
