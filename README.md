# Time Vault — Backend API

[![CI](https://github.com/HackerOverlord/time-vault-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/HackerOverlord/time-vault-backend/actions/workflows/ci.yml)

Flask REST API for **[Time Vault](https://time-vault-imhg.vercel.app)**, a private memory-sharing app where families post photos, videos and notes into shared vaults, and can seal posts as **time capsules** that only open on a chosen date.

Frontend: [HackerOverlord/time-vault](https://github.com/HackerOverlord/time-vault) (Next.js on Vercel)

## Features

- **Vaults:** shared spaces with an owner and members. Each has a name, description, color and cover image.
- **Invites:** single-use invite links that you can regenerate (regenerating invalidates the old link).
- **Posts:** text, photo and video, with likes, comments, archiving and search.
- **Time capsules:** posts locked until an unlock date. While locked, the API returns a redacted placeholder with no caption and no media URL, to everyone including the author.
- **Child vaults:** a vault created for a child's email that the child can claim later with an emailed link.
- **Notifications:** member joins and leaves, capsule unlocks, and a daily digest of new posts.

## Security

| Area | Implementation |
|---|---|
| Authentication | JWT (HS256, 7-day expiry) sent as `Authorization: Bearer`. Each token carries a fingerprint of the password hash, so changing the password or deleting the account revokes every existing token. |
| Passwords | Werkzeug salted hashes. Login locks for 15 minutes after 5 failures per (IP, email). |
| Authorization | Every vault, post and comment route checks vault membership or ownership server-side. |
| Encryption at rest | Legacy memory text uses Fernet (AES-128-CBC + HMAC), with the key derived from `ENCRYPTION_KEY`. |
| Uploads | Size limits plus magic-byte checks on the decoded data. Media is stored in Cloudflare R2 under random 128-bit keys. |
| HTTP | Strict CORS allow-list, `nosniff`, `X-Frame-Options: DENY`, a restrictive CSP and HSTS on every response. |

## Stack

Python 3.12 · Flask · Flask-SQLAlchemy (SQLite) · PyJWT · cryptography · boto3 (Cloudflare R2) · pytest
Hosted on PythonAnywhere.

## Project structure

```
app.py              App setup: config, CORS, security headers, job hook, startup migrations
routes/
  auth.py           Login, register, profile, password change/reset, account deletion
  vaults.py         Vaults, covers, members, invites, child-vault claims
  posts.py          Feed, posts and time capsules, archive, likes, comments
  notifications.py  Notification list and read state
security.py         JWT issue/verify, @token_required, login lockout
crypto.py           Fernet encryption at rest
helpers.py          Permission checks, serializers, upload validation, notifications
emailer.py          SMTP email
models.py           SQLAlchemy models
r2.py               Cloudflare R2 storage
unlock_job.py       Capsule unlocking and the daily digest
scripts/seed_demo.py  Demo data
scripts/backup_db.py  Database backup / restore
backup.py           Backup logic (also run daily by the app)
tests/backend/      pytest suite
```

## Running locally

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env        # then fill in the values
python app.py              # http://localhost:5000
```

Run the tests (no network, R2 or email needed):

```bash
pip install pytest
pytest tests
```

## Capsule unlocking and the daily digest

`unlock_job.py` unlocks capsules whose date has passed, notifies vault members, and sends the daily digest after 20:00 UTC.

PythonAnywhere's free plan has no scheduled tasks, so **the API runs the job itself**, at most once a minute, triggered by incoming requests. Each capsule is claimed with an atomic conditional `UPDATE`, so concurrent workers never unlock it twice or send duplicate notifications. The digest is deduplicated per user, vault and day.

With a real scheduler, set `RUN_JOBS_ON_REQUEST=0` and run the job directly:

```
cd /home/<user>/time-vault-backend && python3.12 unlock_job.py
```

## Demo data

`scripts/seed_demo.py` creates a ready-to-show demo account: three vaults with covers, photo and text posts over the past months, likes, comments, and locked time capsules. The images are generated locally, so there are no stock photos.

```bash
python3.12 scripts/seed_demo.py --email you+demo@gmail.com            # create; prints the demo login once
python3.12 scripts/seed_demo.py --email you+demo@gmail.com --reset    # wipe demo data and recreate
python3.12 scripts/seed_demo.py --email you+demo@gmail.com --delete   # remove demo data
```

## Database backups

Once a day, on the first request after 24 hours, the app writes a consistent, integrity-checked, gzip-compressed copy of the SQLite database to `instance/backups/` and keeps the newest 14. It uses SQLite's online backup API, so it's safe while the app is serving requests.

```bash
python3.12 scripts/backup_db.py                    # back up now (e.g. before a risky change)
python3.12 scripts/backup_db.py --list             # list backups
python3.12 scripts/backup_db.py --restore FILE     # restore (stop the web app first)
```

Backups stay on the server and protect against bad migrations or accidental deletes. Download one occasionally for an off-server copy. They are never uploaded to R2, because the bucket is public.

## Configuration

See [`env.example`](env.example). Required: `SECRET_KEY`, `ENCRYPTION_KEY`, and the `R2_*` variables. Optional: `SMTP_*` (email), `APP_BASE_URL`, `DATABASE_URL`.

Never commit `.env`, the database, or anything under `protected_media/`; they are git-ignored.
