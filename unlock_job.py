"""
unlock_job.py — Time Vault scheduled job

Responsibilities:
  1. Unlock time-capsule posts whose unlock_at has passed.
  2. Send capsule_unlocked notifications to all vault members.
  3. Send the daily new_post digest at 8 PM UTC.

PythonAnywhere setup:
  Command: python3.12 /home/HackerOverlord/unlock_job.py
  Interval: Hourly
  Output: visible in the Tasks tab → "Recent output"
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app import (
    app, db,
    Post, Vault, VaultMember, User,
    Notification, DigestDelivery,
    create_notification,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relative_time(created_at, now):
    """Return a human-readable elapsed time string for capsule_unlocked messages.

    Thresholds (approved 3F spec):
      <1 day      → "recently"
      1–29 days   → "X day(s) ago"
      30–364 days → "X month(s) ago"
      365+ days   → "X year(s) ago"
    """
    days = max(0, (now - created_at).days)
    if days >= 365:
        n = days // 365
        return f'{n} year{"s" if n != 1 else ""} ago'
    if days >= 30:
        n = days // 30
        return f'{n} month{"s" if n != 1 else ""} ago'
    if days >= 1:
        return f'{days} day{"s" if days != 1 else ""} ago'
    return 'recently'


# ---------------------------------------------------------------------------
# Capsule unlock
# ---------------------------------------------------------------------------

def run_unlock_job():
    """Unlock all time-capsule posts whose unlock_at has passed.

    Each post is processed in its own transaction. A conditional UPDATE
    atomically claims the post — if another process already set
    is_unlocked=True, the rowcount is 0 and this process skips the post
    without creating duplicate notifications.
    """
    now = datetime.utcnow()
    due_posts = Post.query.filter(
        Post.is_unlocked == False,
        Post.unlock_at.isnot(None),
        Post.unlock_at <= now,
    ).all()

    if not due_posts:
        log.info('No capsules due for unlock.')
        return

    log.info(f'{len(due_posts)} capsule(s) due for unlock.')

    for post in due_posts:
        # Capture fields before the UPDATE — ORM object may be stale after
        # synchronize_session=False.
        post_id   = post.id
        vault_id  = post.vault_id
        unlock_at = post.unlock_at
        created_at = post.created_at

        try:
            # Atomic conditional UPDATE. Returns rowcount=1 if this process
            # claimed the post, 0 if another process already unlocked it.
            claimed = db.session.query(Post).filter(
                Post.id == post_id,
                Post.is_unlocked == False,
            ).update(
                {'is_unlocked': True, 'posted_at': unlock_at},
                synchronize_session=False,
            )

            if claimed == 0:
                # Another process claimed this post first.
                db.session.rollback()
                log.info(f'Post {post_id} already claimed by another process — skipped.')
                continue

            # Notify every vault member, including the post author.
            # PRD §9.7: recipients are "all vault members eligible to see the post".
            members = VaultMember.query.filter_by(vault_id=vault_id).all()
            vault   = Vault.query.get(vault_id)
            rel     = _relative_time(created_at, now)
            message = f'A memory from {rel} just unlocked in {vault.name}'

            for m in members:
                create_notification(m.user_id, 'capsule_unlocked', message)

            db.session.commit()
            log.info(f'Unlocked post {post_id} in vault {vault_id} '
                     f'({len(members)} notification(s) sent).')

        except Exception as exc:
            db.session.rollback()
            log.error(f'Failed to unlock post {post_id}: {exc}')
            # Continue — do not let one broken post prevent others.


# ---------------------------------------------------------------------------
# Daily new_post digest
# ---------------------------------------------------------------------------

def run_daily_digest():
    """Send the new_post digest to vault members.

    Called only when the UTC hour is 20 (8 PM UTC), per PRD §9.7.
    Deduplication uses DigestDelivery rows keyed on (user_id, vault_id,
    digest_date) — stable integer IDs, not message text.
    Notifications and DigestDelivery rows are committed once per vault.
    IntegrityError on the unique constraint rolls back the entire vault
    batch safely so no partial notifications are committed.
    """
    now          = datetime.utcnow()
    today_utc    = now.date()              # explicit UTC calendar date
    window_start = now - timedelta(hours=24)

    # All vault IDs with at least one unlocked post in the 24-hour window.
    active_vault_ids = [
        row[0] for row in (
            db.session.query(Post.vault_id)
            .filter(
                Post.is_unlocked == True,
                Post.posted_at >= window_start,
            )
            .distinct()
            .all()
        )
    ]

    if not active_vault_ids:
        log.info('Digest: no vaults with new posts in the last 24 hours.')
        return

    total_sent = 0

    for vault_id in active_vault_ids:
        try:
            vault = Vault.query.get(vault_id)
            if not vault:
                continue

            recent_posts = Post.query.filter(
                Post.vault_id == vault_id,
                Post.is_unlocked == True,
                Post.posted_at >= window_start,
            ).all()

            if not recent_posts:
                continue

            count = len(recent_posts)
            if count == 1:
                author  = User.query.get(recent_posts[0].author_id)
                message = f'{author.name} posted in {vault.name}'
            else:
                message = f'{count} new memories in {vault.name}'

            members = VaultMember.query.filter_by(vault_id=vault_id).all()
            sent_this_vault = 0

            for m in members:
                # Pre-check: has this user already received a digest for this
                # vault today? DigestDelivery uses stable integer IDs —
                # no string matching, vault renames do not affect deduplication.
                already = DigestDelivery.query.filter_by(
                    user_id=m.user_id,
                    vault_id=vault_id,
                    digest_date=today_utc,
                ).first()

                if already:
                    continue  # already delivered today for this vault

                create_notification(m.user_id, 'new_post', message)
                db.session.add(DigestDelivery(
                    user_id=m.user_id,
                    vault_id=vault_id,
                    digest_date=today_utc,
                ))
                sent_this_vault += 1

            # Commit notifications + DigestDelivery rows together.
            # IntegrityError on uq_digest_delivery rolls back the entire
            # vault batch — no notifications committed without their
            # corresponding delivery records, and no delivery records
            # committed without their notifications.
            db.session.commit()
            total_sent += sent_this_vault
            log.info(f'Digest: vault {vault_id} ("{vault.name}") — '
                     f'{sent_this_vault} notification(s) sent.')

        except IntegrityError as exc:
            db.session.rollback()
            log.error(f'Digest: IntegrityError for vault {vault_id} '
                      f'(concurrent delivery conflict) — rolled back: {exc}')
            # Continue to next vault.

        except Exception as exc:
            db.session.rollback()
            log.error(f'Digest: failed for vault {vault_id}: {exc}')
            # Continue to next vault.

    log.info(f'Digest complete: {total_sent} total notification(s) created.')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    log.info('Unlock job started.')

    with app.app_context():
        run_unlock_job()

        now = datetime.utcnow()
        if 20 <= now.hour < 21:
            log.info(f'Running daily digest (UTC hour {now.hour}).')
            run_daily_digest()
        else:
            log.info(f'Digest not due (UTC hour {now.hour}).')

    log.info('Unlock job complete.')
