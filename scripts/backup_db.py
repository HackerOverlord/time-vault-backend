"""
scripts/backup_db.py — back up or restore the Time Vault database.

From the backend folder:
    python3.12 scripts/backup_db.py                    # back up now
    python3.12 scripts/backup_db.py --list             # show backups
    python3.12 scripts/backup_db.py --restore FILE     # restore (stop the app first)

The app also makes one automatic backup a day (see backup.py).
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault('RUN_JOBS_ON_REQUEST', '0')

import app as A                     # noqa: E402
import backup                       # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--list', action='store_true', help='list existing backups')
    g.add_argument('--restore', metavar='FILE', help='restore this .db.gz backup')
    args = ap.parse_args()

    with A.app.app_context():
        db_path = A.db.engine.url.database
    dest = backup.backup_dir_for(db_path)

    if args.list:
        files = backup.list_backups(dest)
        for f in files:
            print(f'{f.name}  {f.stat().st_size / 1024:.0f} KB')
        print(f'{len(files)} backup(s) in {dest}')
        return

    if args.restore:
        answer = input(f'Replace {db_path} with {args.restore}? Stop the web app first. Type "restore": ')
        if answer.strip() != 'restore':
            sys.exit('Cancelled.')
        safety = backup.backup_database(db_path, dest)   # keep the current state too
        backup.restore_database(args.restore, db_path)
        print(f'Restored. The previous database was saved as {safety.name}. Reload the web app.')
        return

    path = backup.backup_database(db_path, dest)
    print(f'Backup written: {path} ({path.stat().st_size / 1024:.0f} KB)')


if __name__ == '__main__':
    main()
