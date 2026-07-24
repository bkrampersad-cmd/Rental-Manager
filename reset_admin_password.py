"""Locked out of Network (server) mode with no other Admin available?
Run this script.

Normally, if one Admin forgets their password, ANY other Admin can fix it
in two clicks: Settings -> Users -> edit that person -> type a new
password. That's the first thing to try, and it needs nothing outside
the app.

This script is only for the harder case: every single Admin account is
locked out (forgotten password, or there's only ever been one Admin).
It connects straight to the Postgres database this install uses
(reading the same connection details from config.json that the app
itself uses) and resets one user's password directly, bypassing the
app and its login screen entirely.

Requires the same Python environment the app itself runs in (needs
psycopg2 and werkzeug already installed — see requirements-server.txt).
Run it on/near the server, or anywhere with network access to the
Postgres database.

USAGE
  python reset_admin_password.py
  python reset_admin_password.py --config "C:\\path\\to\\config.json"

It will list existing users, ask which one to reset, and prompt you to
type a new password (hidden as you type, and typed twice to confirm).
"""
import argparse
import getpass
import json
import os
import sys


def find_default_config_path():
    """Mirrors app.py's own logic for where config.json lives."""
    candidates = []

    appdata = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "RentalManager", "data", "config.json"))

    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "data", "config.json"))

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Path to config.json (auto-detected if omitted)")
    parser.add_argument("--username", help="Username to reset (skips the picker)")
    args = parser.parse_args()

    config_path = args.config or find_default_config_path()
    if not config_path or not os.path.exists(config_path):
        print(
            "Couldn't automatically find config.json.\n"
            "Re-run with --config pointing at it, for example:\n"
            '  python reset_admin_password.py --config "C:\\Users\\you\\AppData\\Local\\RentalManager\\data\\config.json"'
        )
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    database_url = config.get("database_url")
    if config.get("mode") != "server" or not database_url:
        print(
            "This install isn't running in Network (server) mode according to "
            f"{config_path} — there's no Postgres database to reset a user in.\n"
            "(Standalone mode has no user accounts at all; if you meant to "
            "reset the Standalone Access Password instead, use "
            "reset_access_password.py.)"
        )
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        print(
            "The 'psycopg2' package isn't installed in this Python environment.\n"
            "Run this script using the same virtual environment / Python install "
            "the app itself uses (pip install -r requirements-server.txt)."
        )
        sys.exit(1)

    try:
        from werkzeug.security import generate_password_hash
    except ImportError:
        print(
            "The 'werkzeug' package isn't installed in this Python environment.\n"
            "Run this script using the same virtual environment / Python install "
            "the app itself uses (pip install -r requirements.txt)."
        )
        sys.exit(1)

    conn = psycopg2.connect(database_url)
    try:
        conn.autocommit = False
        cur = conn.cursor()

        username = args.username
        if not username:
            cur.execute("SELECT username, role, active FROM users ORDER BY username")
            users = cur.fetchall()
            if not users:
                print("No users found in this database.")
                sys.exit(1)
            print("\nExisting users:")
            for u, role, active in users:
                status = "active" if active else "DISABLED"
                print(f"  {u}  ({role}, {status})")
            username = input("\nUsername to reset: ").strip()

        cur.execute("SELECT id, role FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
        if not row:
            print(f"No user named '{username}' found.")
            sys.exit(1)
        user_id, role = row

        password = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm new password: ")
        if not password or len(password) < 4:
            print("Password should be at least 4 characters. Nothing was changed.")
            sys.exit(1)
        if password != confirm:
            print("Passwords didn't match. Nothing was changed.")
            sys.exit(1)

        new_hash = generate_password_hash(password)
        cur.execute(
            "UPDATE users SET password_hash = %s, active = TRUE WHERE id = %s",
            (new_hash, user_id),
        )
        conn.commit()
        print(f"\nDone. {username}'s password has been reset and the account is active.")
        if role != "admin":
            print(
                f"Note: {username}'s role is '{role}', not Admin. If you need Admin "
                "access, re-run this script for an existing Admin username instead, "
                "or ask an existing Admin to change this user's role in Settings -> Users."
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
