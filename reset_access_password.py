"""Forgotten the Standalone Access Password AND all 3 security answers?
Run this script.

This is for Standalone mode ONLY (no login, single shared "access
password" set in Settings). The normal way to recover a forgotten
password is the "Forgot password?" link on the lock screen — answer any
ONE of your 3 security questions and set a new password right there, no
script needed. This script is the LAST resort, for when the password AND
all 3 security answers are gone: it talks to your local database file
directly (bypassing the app and its lock screen entirely) and simply
turns the access password back OFF, exactly as if you'd clicked "Disable
Password" in Settings (this also clears the saved security questions,
same as disabling normally does). The next time you open the app, it
opens straight to normal use, no unlock screen. Set a new password (and
new security questions) from Settings if you still want the lock screen.

It touches ONLY the one row that stores the password. Every property,
transaction, and setting is left completely untouched.

Requires only Python 3 (the "sqlite3" module is built in — nothing else
to install). It does NOT need the app's virtual environment.

USAGE
  Close the Rental Property Manager app first, then run:

      python reset_access_password.py

  If it can't find your database automatically, pass the path to it
  directly:

      python reset_access_password.py "C:\\path\\to\\rental_manager.db"
"""
import os
import sqlite3
import sys


def find_default_db_path():
    """Mirrors app.py's own logic for where the standalone database lives:
    the installed/packaged app keeps it under the current Windows user's
    AppData folder; a dev/portable checkout keeps it in ./data next to
    this script instead."""
    candidates = []

    appdata = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "RentalManager", "data", "rental_manager.db"))

    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "data", "rental_manager.db"))

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def main():
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
        if not os.path.exists(db_path):
            print(f"Can't find a database file at: {db_path}")
            sys.exit(1)
    else:
        db_path = find_default_db_path()
        if not db_path:
            print(
                "Couldn't automatically find rental_manager.db.\n"
                "Re-run this script with the full path to it, for example:\n"
                '  python reset_access_password.py "C:\\Users\\you\\AppData\\Local\\RentalManager\\data\\rental_manager.db"'
            )
            sys.exit(1)

    print(f"Using database: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
        )
        if not cur.fetchone():
            print("This database has no 'settings' table — nothing to reset.")
            sys.exit(1)

        cur.execute("SELECT value FROM settings WHERE key = 'access_password_hash'")
        row = cur.fetchone()
        if not row:
            print("No access password is currently set — there's nothing to reset.")
            sys.exit(0)

        # Mirrors exactly what clicking "Disable Password" in Settings does:
        # clears the password itself, the 3 security questions/answers, and
        # the failed-attempt/lockout bookkeeping — a clean slate.
        keys_to_clear = [
            "access_password_hash",
            "access_security_q1", "access_security_a1_hash",
            "access_security_q2", "access_security_a2_hash",
            "access_security_q3", "access_security_a3_hash",
            "access_password_failed_attempts", "access_password_lockout_until",
        ]
        cur.executemany(
            "DELETE FROM settings WHERE key = ?", [(k,) for k in keys_to_clear]
        )
        conn.commit()
        print(
            "Done. The access password and security questions have been removed.\n"
            "Restart the app — it will open directly with no unlock screen.\n"
            "You can set a new password (and new security questions) any time from Settings."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
