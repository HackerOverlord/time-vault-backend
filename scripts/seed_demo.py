"""
scripts/seed_demo.py — create a realistic demo account for showing Time Vault.

Creates one demo login plus three "family member" accounts (which can't log
in), three vaults with covers, photo and text posts spread over the past
months, likes, comments, and two time capsules (one opening soon, one later).

Usage (from the backend folder), with an inbox you control so
"Forgot password?" works for the demo login:
    python3.12 scripts/seed_demo.py --email you+demo@gmail.com            # create
    python3.12 scripts/seed_demo.py --email you+demo@gmail.com --reset    # recreate
    python3.12 scripts/seed_demo.py --email you+demo@gmail.com --delete   # remove

The demo password is generated randomly and printed once. Forgot it? Use
"Forgot password?" in the app (the +demo address goes to your inbox).

Images are generated locally (no stock photos, no copyright issues) and
uploaded to R2. Needs Pillow:  pip3.12 install --user pillow
"""
import argparse
import io
import math
import os
import random
import secrets
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)                       # so .env and instance/ resolve
sys.path.insert(0, ROOT)
os.environ.setdefault('RUN_JOBS_ON_REQUEST', '0')

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    sys.exit('Pillow is required:  pip3.12 install --user pillow')

import app as A                                      # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

# Supporting cast: unusable passwords, example.com addresses (never emailed).
CAST = [
    ('Maya',  'Kakar', 'maya.demo@example.com'),
    ('Omar',  'Kakar', 'omar.demo@example.com'),
    ('Laila', 'Kakar', 'laila.demo@example.com'),
]
DEMO_DOMAIN_MARK = '.demo@example.com'
rng = random.Random(2026)


# ── Image generation ─────────────────────────────────────────────────────────

def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _sky(draw, w, h, top, bottom, horizon=1.0):
    for y in range(int(h * horizon)):
        draw.line([(0, y), (w, y)], fill=_lerp(top, bottom, y / (h * horizon)))

def _ridge(draw, w, h, base, amp, color, seed, step=8):
    r = random.Random(seed)
    phase = [r.uniform(0, 6.28) for _ in range(3)]
    pts = [(0, h)]
    for x in range(0, w + step, step):
        y = base + amp * (0.55 * math.sin(x / 210 + phase[0]) +
                          0.30 * math.sin(x / 95 + phase[1]) +
                          0.15 * math.sin(x / 41 + phase[2]))
        pts.append((x, y))
    pts.append((w, h))
    draw.polygon(pts, fill=color)

def _finish(img, blur=0.6, grain=6):
    img = img.filter(ImageFilter.GaussianBlur(blur))
    px = img.load()
    w, h = img.size
    r = random.Random(w * h)
    for _ in range(w * h // 6):                       # light film grain
        x, y = r.randrange(w), r.randrange(h)
        c = px[x, y]
        d = r.randint(-grain, grain)
        px[x, y] = tuple(max(0, min(255, v + d)) for v in c)
    return img

def scene_lake_sunset(w=1350, h=1080):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (255, 170, 110), (255, 220, 170), 0.62)
    d.ellipse([w*0.58, h*0.38, w*0.70, h*0.38 + w*0.12], fill=(255, 236, 200))
    _ridge(d, w, h, h*0.50, 40, (120, 80, 110), 1)
    _ridge(d, w, h, h*0.56, 25, (80, 55, 90), 2)
    for y in range(int(h*0.62), h):                    # water
        d.line([(0, y), (w, y)], fill=_lerp((230, 150, 120), (60, 50, 80), (y - h*0.62) / (h*0.38)))
    for i in range(14):                                # sun glints
        y = h*0.64 + i*22; x = w*0.64 + rng.uniform(-30, 30)
        d.line([(x - 60 + i*3, y), (x + 60 - i*3, y)], fill=(255, 220, 180), width=3)
    _ridge(d, w, h, h*0.86, 18, (35, 30, 45), 3)       # dock-side trees
    return _finish(img)

def scene_mountains(w=1350, h=1080):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (120, 170, 220), (210, 230, 245))
    _ridge(d, w, h, h*0.38, 90, (150, 165, 190), 4)
    _ridge(d, w, h, h*0.50, 70, (100, 120, 150), 5)
    _ridge(d, w, h, h*0.66, 45, (60, 95, 80), 6)
    _ridge(d, w, h, h*0.80, 30, (40, 70, 55), 7)
    return _finish(img)

def scene_beach(w=1080, h=1350):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (90, 170, 230), (190, 225, 245), 0.48)
    for y in range(int(h*0.48), int(h*0.70)):
        d.line([(0, y), (w, y)], fill=_lerp((40, 140, 190), (90, 200, 210), (y - h*0.48) / (h*0.22)))
    for y in range(int(h*0.70), h):
        d.line([(0, y), (w, y)], fill=_lerp((235, 215, 175), (215, 185, 140), (y - h*0.70) / (h*0.30)))
    for i in range(4):                                 # foam
        y = h*0.69 + i*8
        d.arc([-200, y - 30, w + 200, y + 30], 0, 180, fill=(245, 250, 250), width=4)
    d.ellipse([w*0.15, h*0.12, w*0.27, h*0.12 + w*0.12], fill=(255, 250, 225))
    return _finish(img)

def scene_autumn(w=1350, h=1080):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (235, 210, 170), (245, 230, 200))
    palette = [(200, 90, 40), (225, 140, 50), (180, 60, 40), (235, 180, 70), (150, 70, 40)]
    r = random.Random(8)
    for _ in range(140):                               # canopy blobs
        x, y, s = r.uniform(-50, w), r.uniform(-50, h*0.7), r.uniform(60, 160)
        d.ellipse([x, y, x + s, y + s*0.8], fill=r.choice(palette))
    for x in (w*0.18, w*0.47, w*0.79):                 # trunks
        d.rectangle([x, h*0.45, x + 28, h], fill=(70, 45, 35))
    for y in range(int(h*0.82), h):
        d.line([(0, y), (w, y)], fill=_lerp((190, 110, 50), (140, 80, 40), (y - h*0.82) / (h*0.18)))
    return _finish(img, blur=1.2)

def scene_city_night(w=1080, h=1350):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (15, 20, 50), (60, 50, 100))
    r = random.Random(9)
    x = 0
    while x < w:
        bw, bh = r.randint(70, 160), r.randint(int(h*0.25), int(h*0.65))
        d.rectangle([x, h - bh, x + bw, h], fill=(25, 25, 40))
        for wy in range(h - bh + 20, h - 20, 34):
            for wx in range(x + 12, x + bw - 12, 26):
                if r.random() < 0.45:
                    d.rectangle([wx, wy, wx + 12, wy + 18], fill=r.choice([(255, 210, 120), (255, 235, 180), (190, 210, 255)]))
        x += bw + r.randint(4, 14)
    return _finish(img, blur=0.8)

def scene_kitchen_warm(w=1080, h=1350):
    """Soft bokeh — a warm, out-of-focus kitchen-light mood shot."""
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    _sky(d, w, h, (90, 50, 30), (40, 20, 15))
    r = random.Random(10)
    for _ in range(45):
        x, y, s = r.uniform(0, w), r.uniform(0, h), r.uniform(60, 220)
        d.ellipse([x, y, x + s, y + s], fill=r.choice([(255, 190, 110), (240, 150, 80), (255, 220, 160), (200, 110, 60)]))
    return _finish(img.filter(ImageFilter.GaussianBlur(28)), blur=0)

def cover(colors, w=1500, h=500):
    img = Image.new('RGB', (w, h)); d = ImageDraw.Draw(img)
    for x in range(w):
        d.line([(x, 0), (x, h)], fill=_lerp(colors[0], colors[1], x / w))
    _ridge(d, w, h, h*0.62, 40, _lerp(colors[1], (20, 20, 30), 0.5), 11)
    return _finish(img)

def to_r2(img, prefix):
    """Upload a PIL image to R2 as JPEG; falls back to a data URI."""
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=85, optimize=True)
    data = buf.getvalue()
    try:
        from r2 import upload_file_object
        return upload_file_object(data, prefix=prefix, mime_type='image/jpeg')
    except Exception as exc:
        print(f'  ! R2 upload failed ({exc}); storing inline instead')
        import base64
        return 'data:image/jpeg;base64,' + base64.b64encode(data).decode()


# ── Demo data ────────────────────────────────────────────────────────────────

def demo_users(email):
    q = A.User.query.filter((A.User.email == email) | (A.User.email.like(f'%{DEMO_DOMAIN_MARK}')))
    return q.all()

def delete_demo(email):
    users = demo_users(email)
    if not users:
        print('No demo data found.')
        return
    ids = [u.id for u in users]
    vaults = A.Vault.query.filter(A.Vault.created_by.in_(ids)).all()
    urls = []
    for v in vaults:
        posts = A.Post.query.filter_by(vault_id=v.id).all()
        pids = [p.id for p in posts]
        urls += [p.media_url for p in posts if p.media_url] + ([v.cover_url] if v.cover_url else [])
        if pids:
            A.PostLike.query.filter(A.PostLike.post_id.in_(pids)).delete(synchronize_session=False)
            A.PostComment.query.filter(A.PostComment.post_id.in_(pids)).delete(synchronize_session=False)
        A.Post.query.filter_by(vault_id=v.id).delete(synchronize_session=False)
        A.VaultMember.query.filter_by(vault_id=v.id).delete(synchronize_session=False)
        A.DigestDelivery.query.filter_by(vault_id=v.id).delete(synchronize_session=False)
        A.Notification.query.filter_by(vault_id=v.id).delete(synchronize_session=False)
        A.db.session.delete(v)
    for model, col in ((A.VaultMember, 'user_id'), (A.Notification, 'user_id'),
                       (A.DigestDelivery, 'user_id'), (A.PostLike, 'user_id'),
                       (A.PasswordResetToken, 'user_id')):
        model.query.filter(getattr(model, col).in_(ids)).delete(synchronize_session=False)
    for u in users:
        A.db.session.delete(u)
    A.db.session.commit()
    try:
        from r2 import delete_object, is_r2_url
        for u in urls:
            if is_r2_url(u):
                delete_object(u)
    except Exception as exc:
        print(f'  ! Could not delete some R2 objects: {exc}')
    print(f'Deleted {len(users)} demo users and {len(vaults)} vaults.')


def make_user(first, last, email, password=None):
    u = A.User(first_name=first, last_name=last, name=f'{first} {last}', email=email,
               password_hash=generate_password_hash(password or secrets.token_urlsafe(32)))
    A.db.session.add(u)
    A.db.session.flush()
    return u


def make_vault(owner, name, description, accent, cover_colors, members, created):
    print(f'  vault: {name}')
    v = A.Vault(name=name, description=description, accent_color=accent,
                invite_code=A.generate_invite_code(), created_by=owner.id,
                created_at=created, cover_url=to_r2(cover(cover_colors), 'covers'))
    A.db.session.add(v)
    A.db.session.flush()
    A.db.session.add(A.VaultMember(vault_id=v.id, user_id=owner.id, role='owner',
                                   joined_at=created, last_seen_at=datetime.utcnow()))
    for i, m in enumerate(members):
        A.db.session.add(A.VaultMember(vault_id=v.id, user_id=m.id, role='member',
                                       joined_at=created + timedelta(days=i + 1), invited_by=owner.id))
    return v


def make_post(vault, author, when, caption, image=None, likers=(), comments=()):
    media_type = 'image' if image is not None else 'text'
    p = A.Post(vault_id=vault.id, author_id=author.id, caption=caption,
               media_type=media_type, media_url=to_r2(image, 'posts') if image is not None else None,
               is_unlocked=True, posted_at=when, created_at=when)
    A.db.session.add(p)
    A.db.session.flush()
    for u in likers:
        A.db.session.add(A.PostLike(post_id=p.id, user_id=u.id, created_at=when + timedelta(hours=2)))
    for i, (u, body) in enumerate(comments):
        A.db.session.add(A.PostComment(post_id=p.id, author_id=u.id, body=body,
                                       created_at=when + timedelta(hours=3 + i)))
    p.like_count, p.comment_count = len(likers), len(comments)
    return p


def make_capsule(vault, author, created, unlock_at, caption):
    p = A.Post(vault_id=vault.id, author_id=author.id, caption=caption, media_type='text',
               media_url=None, unlock_at=unlock_at, is_unlocked=False,
               posted_at=None, created_at=created)
    A.db.session.add(p)
    return p


def seed(email):
    if demo_users(email):
        sys.exit('Demo data already exists. Use --reset to recreate it.')

    now = datetime.utcnow()
    ago = lambda days, hour=17: (now - timedelta(days=days)).replace(hour=hour, minute=rng.randint(0, 59), second=0, microsecond=0)
    password = secrets.token_urlsafe(9)

    me = make_user('Alex', 'Kakar', email, password)
    maya, omar, laila = (make_user(*c) for c in CAST)
    if hasattr(me, 'last_login'):
        me.last_login = now

    print('Generating images and uploading…')
    trips = make_vault(me, 'Family Trips', 'Every road trip, cottage weekend and airport selfie.',
                       'blue', ((60, 110, 200), (120, 190, 230)), [maya, omar, laila], ago(240))
    kitchen = make_vault(me, "Grandma's Kitchen", 'Recipes, stories and Sunday dinners.',
                         'orange', ((200, 110, 60), (240, 180, 100)), [maya, laila], ago(200))
    grad = make_vault(me, 'Class of 2026', 'Notes to our future selves.',
                      'purple', ((90, 60, 160), (170, 120, 220)), [omar], ago(120))

    print('Creating posts…')
    make_post(trips, maya, ago(210), 'Cottage sunset on the last night. Nobody wanted to leave the dock.',
              scene_lake_sunset(), likers=[me, omar, laila],
              comments=[(omar, 'Best week of the summer'), (me, 'Same time next year?')])
    make_post(trips, me, ago(150), 'Made it to the lookout before the clouds rolled in.',
              scene_mountains(), likers=[maya, laila])
    make_post(trips, laila, ago(95), 'Beach day!! Omar lost his sandals again.',
              scene_beach(), likers=[me, maya, omar],
              comments=[(omar, 'They were stolen, I swear'), (maya, 'Sure they were')])
    make_post(trips, omar, ago(40), 'Fall colours on the drive up north.',
              scene_autumn(), likers=[me])
    make_post(trips, me, ago(6), 'Toronto skyline from the hotel window. One more night here.',
              scene_city_night(), likers=[maya, laila, omar])

    make_post(kitchen, maya, ago(180, 12),
              "Grandma's lentil soup: red lentils, one onion, two carrots, cumin, lemon at the end. "
              "She never measured anything, so neither will we.",
              likers=[me, laila], comments=[(laila, 'Adding this to the family cookbook')])
    make_post(kitchen, laila, ago(60, 19), 'Sunday dinner glow. Nine people, one table, zero leftovers.',
              scene_kitchen_warm(), likers=[me, maya])
    make_post(kitchen, me, ago(12, 20), 'Tried the soup recipe. Not as good as hers, but close.',
              likers=[maya], comments=[(maya, 'Needs more lemon')])

    make_post(grad, omar, ago(100, 14), 'First day of final year. Writing this so future us remembers how nervous we were.',
              likers=[me])
    make_post(grad, me, ago(30, 22), 'Capstone demo went well. Sleeping for a week.', likers=[omar],
              comments=[(omar, 'Earned it')])

    print('Sealing time capsules…')
    make_capsule(grad, me, ago(25), (now + timedelta(days=3)).replace(hour=4, minute=0, second=0, microsecond=0),
                 'Open on results day: whatever happened, we tried our best.')
    make_capsule(grad, omar, ago(20), (now + timedelta(days=240)).replace(hour=4, minute=0, second=0, microsecond=0),
                 'Read this at graduation.')
    make_capsule(trips, maya, ago(10), (now + timedelta(days=60)).replace(hour=4, minute=0, second=0, microsecond=0),
                 'Guesses for where we go next summer. No peeking.')

    for vault in (trips, kitchen, grad):
        A.create_notification(me.id, 'member_joined', f'{maya.first_name if vault is not grad else omar.first_name} joined {vault.name}.',
                              vault_id=vault.id)
    A.db.session.commit()

    print('\nDemo account ready')
    print(f'  Email:    {email}')
    print(f'  Password: {password}')
    print('  (shown once; use "Forgot password?" in the app if you lose it)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--email', required=True,
                    help='login email for the demo account (use an inbox you control)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--reset', action='store_true', help='delete existing demo data, then recreate')
    g.add_argument('--delete', action='store_true', help='delete demo data only')
    args = ap.parse_args()
    email = args.email.strip().lower()

    with A.app.app_context():
        if args.delete or args.reset:
            delete_demo(email)
        if not args.delete:
            seed(email)


if __name__ == '__main__':
    main()
