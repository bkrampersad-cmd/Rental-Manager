# Deployment Guide — Server Mode

This covers setting up **Server Mode** — a shared install a team accesses over
your internal network — and the final steps to package and hand out the app.
If you just want the single-user desktop app, skip straight to "Packaging &
handing out the app" below; none of the Postgres/server steps apply to you.

## 1. Set up PostgreSQL

Server mode needs a PostgreSQL database (version 12+) reachable from the
machine that will run Rental Property Manager. If you don't already have
one:

1. Install PostgreSQL on a server machine (or use an existing instance) —
   see [postgresql.org/download](https://www.postgresql.org/download/).
2. Create a database and a user for the app, e.g.:
   ```sql
   CREATE DATABASE rental_manager;
   CREATE USER rental_app WITH PASSWORD 'choose-a-strong-password';
   GRANT ALL PRIVILEGES ON DATABASE rental_manager TO rental_app;
   ```
3. Make sure the server accepts connections from the machine(s) that will
   run the app — this usually means setting `listen_addresses` in
   `postgresql.conf` and adding a line to `pg_hba.conf` for your internal
   network's IP range (e.g. `192.168.1.0/24`), then restarting PostgreSQL.
4. Confirm you can reach it before running the wizard — from the machine
   that will run the app: `psql -h <host> -p 5432 -U rental_app -d rental_manager`.

## 2. Install the app and run the setup wizard

Install `RentalManagerSetup.exe` on the machine that will run the shared
install (see "Packaging" below for how that installer is built). The first
time it launches, instead of opening the dashboard it shows a one-time setup
screen:

- **"Just me on this computer"** — standalone/SQLite, no login. Not what you
  want here.
- **"Shared server for a team"** — choose this. Enter the PostgreSQL host,
  port, database name, username, and password from step 1. The app tests
  the connection before continuing. Once verified, create the first Admin
  account (username + password) — this is the account you'll use to log in
  and add everyone else afterward.

After saving, the app asks you to **restart it once** — this is because it
needs to reconnect using the new database rather than the one it started
with. Close and reopen it; you'll land on a login screen.

## 3. Add your team

Log in as the Admin account you just created, go to **Settings → Users**,
and add an account for each person, choosing a role for each:

- **Admin** — manages user accounts, plus everything an Editor can do.
- **Editor** — full read/write on properties, transactions, categories,
  imports, and exports.
- **Viewer** — read-only: can see everything and export CSV/Excel/PDF, but
  can't add, edit, delete, or import.

Give each person their own login rather than sharing one account — this is
what makes "who entered this transaction" (visible on transactions created
in server mode) meaningful, and lets you deactivate one person's access
without affecting anyone else.

## 4. Keep it running unattended

In server mode the app runs under **Waitress**, a production-ready server —
not the "close this window to stop" behavior of the single-user desktop
app. Running it as a normal desktop shortcut works, but it stops if that
window is closed or the machine reboots. To keep it running unattended:

**Option A — Windows Task Scheduler (simplest, built into Windows)**

1. Open Task Scheduler → Create Task.
2. General tab: name it, check "Run whether user is logged on or not."
3. Triggers tab: "At startup."
4. Actions tab: "Start a program" → point it at the installed
   `RentalManager.exe`.
5. Settings tab: consider unchecking "Stop the task if it runs longer than"
   (it's meant to run indefinitely).

**Option B — NSSM (runs it as an actual Windows service)**

[NSSM](https://nssm.cc/) ("the Non-Sucking Service Manager") wraps any
executable as a proper Windows service, so it starts before any user logs
in and Windows restarts it automatically if it crashes.

```
nssm install RentalManager "C:\Path\To\RentalManager.exe"
nssm start RentalManager
```

Either approach keeps the app listening on the port shown in its startup
log (5000 by default — set the `RENTAL_MANAGER_PORT` environment variable
before starting it to change that).

## 5. Firewall / network access

Server mode binds to all network interfaces (`0.0.0.0`) so other computers
can reach it, but Windows Firewall may still block inbound connections to
that port by default. On the server machine:

1. Open Windows Defender Firewall → Advanced Settings → Inbound Rules →
   New Rule.
2. Port → TCP → the port the app is running on (5000 by default).
3. Allow the connection, and scope it to your internal network profile
   (Private/Domain) — not Public.

Everyone on the team then browses to `http://<server-machine-name-or-ip>:5000`.

**A note on scope:** this covers your internal network only. Making this
reachable from the public internet needs additional hardening — HTTPS/TLS,
a properly hosted (not just internally-networked) Postgres instance, and
tighter session security — that's intentionally not part of what's built
here. Treat this as an internal-network tool, not an internet-facing one,
unless you take those extra steps separately.

## 6. Backing up a server-mode install

Server mode's **Settings → Backup & Restore → Full Backup** exports a
`.zip` containing a `backup.json` (every property, transaction, category,
user, and setting) plus your logo files — a database-agnostic format,
unlike the standalone SQLite-file backup. Restoring it **replaces all
current data** in whatever install you import it into, including user
accounts, so treat it the same way you'd treat a database restore anywhere
else: something to do deliberately, not casually. It's still worth doing
regularly, in addition to whatever backup routine you already have for the
Postgres server itself (e.g. `pg_dump`) — the two aren't a replacement for
each other, they're complementary.

---

# Packaging & handing out the app

This is the same process whether you're building the single-user desktop
version or the version people will use to connect to a shared server —
it's one installer either way; the setup wizard is what decides which mode
a given install runs in.

1. Install Python 3.9+ on the Windows machine you're building on (only
   needed for the build, not for the people you hand the app to).
2. Install [Inno Setup](https://jrsoftware.org/isdl.php) (free).
3. From the project folder, run **`build_exe.bat`**. This creates a virtual
   environment and installs both `requirements.txt` and
   `requirements-server.txt` (so the one .exe it produces supports both
   modes), then freezes everything into `dist\RentalManager\RentalManager.exe`.
   If installing `requirements-server.txt` fails on this machine with a
   "pg_config not found" or "Microsoft Visual C++ 14.0 required" error, the
   build machine's Python version is too new for the current
   psycopg2-binary release — use Python 3.11 or 3.12 to build with, or
   check PyPI for a newer psycopg2-binary release that's added a wheel for
   your version.
4. Run **`build_installer.bat`**. This compiles `installer.iss` into
   `installer_output\RentalManagerSetup.exe`.

Hand `RentalManagerSetup.exe` to anyone — running it installs the app, adds
a Start Menu entry and optional desktop shortcut, no admin rights or Python
required on their end. What happens when they first launch it depends on
what they choose in the one-time setup screen:

- **Single user, own computer:** choose "Just me on this computer" — works
  exactly like the original single-user version, private SQLite database,
  no login.
- **Shared team install:** choose "Shared server for a team" and follow
  steps 1–5 above.

Re-run both build scripts any time you change the app to produce an
updated installer.
