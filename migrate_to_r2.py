"""
migrate_to_r2.py — One-time migration: base64 media in SQLite → Cloudflare R2.

Usage (PythonAnywhere Bash):
    python3.12 /home/HackerOverlord/migrate_to_r2.py

The script is idempotent and resumable:
  • It skips rows whose media_url already starts with "https://" (already migrated).
  • It commits after each row, so interruption loses at most one item's work.
  • Re-running after interruption continues from where it left off.
  • Errors on individual rows are logged and skipped; the script keeps going.

Both tables are migrated:
  1. Post.media_url    — post images and videos
  2. User.avatar       — profile photos

Prerequisites:
  • All five R2 environment variables must be set (see r2.py).
  • boto3 must be installed: pip3.12 install --user boto3
  • This file and app.py must be in the same directory.
"""

import logging
import sys
import os

# Ensure the directory containing app.py and r2.py is on sys.path.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

from app import app, db, Post, User            # noqa: E402
from r2 import upload_media, is_r2_url         # noqa: E402


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
_total   = 0
_ok      = 0
_skipped = 0
_failed  = 0


def _migrate_posts() -> None:
    global _total, _ok, _skipped, _failed
    posts = Post.query.filter(Post.media_url.isnot(None)).all()
    log.info(f'Posts with media_url: {len(posts)}')
    for post in posts:
        _total += 1
        if not post.media_url:
            _skipped += 1
            continue
        if is_r2_url(post.media_url):
            log.debug(f'Post {post.id}: already migrated — skipping')
            _skipped += 1
            continue
        # Only migrate data URIs; skip anything else unusual.
        if not post.media_url.startswith('data:'):
            log.warning(f'Post {post.id}: unexpected media_url format — skipping')
            _skipped += 1
            continue
        try:
            url = upload_media(post.media_url, prefix='posts')
            post.media_url = url
            db.session.commit()
            log.info(f'Post {post.id}: migrated → {url}')
            _ok += 1
        except Exception as exc:
            db.session.rollback()
            log.error(f'Post {post.id}: FAILED — {exc}')
            _failed += 1


def _migrate_avatars() -> None:
    global _total, _ok, _skipped, _failed
    users = User.query.filter(User.avatar.isnot(None)).all()
    log.info(f'Users with avatar: {len(users)}')
    for user in users:
        _total += 1
        if not user.avatar:
            _skipped += 1
            continue
        if is_r2_url(user.avatar):
            log.debug(f'User {user.id}: avatar already migrated — skipping')
            _skipped += 1
            continue
        if not user.avatar.startswith('data:'):
            log.warning(f'User {user.id}: unexpected avatar format — skipping')
            _skipped += 1
            continue
        try:
            url = upload_media(user.avatar, prefix='avatars')
            user.avatar = url
            db.session.commit()
            log.info(f'User {user.id}: avatar migrated → {url}')
            _ok += 1
        except Exception as exc:
            db.session.rollback()
            log.error(f'User {user.id}: avatar FAILED — {exc}')
            _failed += 1


def main() -> None:
    with app.app_context():
        log.info('=== Time Vault → Cloudflare R2 migration ===')
        _migrate_posts()
        _migrate_avatars()
        log.info(
            f'=== Done. total={_total} ok={_ok} '
            f'skipped={_skipped} failed={_failed} ==='
        )
        if _failed:
            log.warning('Some items failed. Re-run this script to retry.')
            sys.exit(1)


if __name__ == '__main__':
    main()
