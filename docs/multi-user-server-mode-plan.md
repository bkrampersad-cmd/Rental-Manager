# Scope: Standalone + Internal Server Mode

This is the plan for adding a second install mode to Rental Property Manager
— a shared, multi-user "server mode" — while keeping the existing
single-user standalone experience completely unchanged for anyone who
doesn't need it. Public web/internet access is explicitly **out of scope**
for this phase (see "Explicitly deferred" at the end).

**Status: All three phases are complete.** Config system, database
abstraction, migrations, accounts/roles, the first-run setup wizard, and
server hardening (Waitress + Postgres-flavored backup) are all built and
verified — including against a real local Postgres instance and inside the
actual packaged .exe, in both standalone and server mode. See "What's
actually built" below for specifics, and `docs/deployment-guide.md` for how
to actually stand up and package a server-mode install.

## The two modes, at a glance

| | Standalone (today) | Server mode (new) |
|---|---|---|
| Database | SQLite file | PostgreSQL |
| Login | None — opens straight to the app | Required — Admin / Editor / Viewer roles |
| Who uses it | One person, one computer | A team, over the internal network |
| Runs via | `RentalManager.exe` / `run.bat` | Same app, run continuously on a server machine |
| Data location | `%LOCALAPPDATA%\RentalManager` on that PC | Central Postgres database |

One codebase, one installer. The mode is chosen once, the first time the
app is launched, via a short setup screen — not by installing different
software.

## How mode selection works

The Inno Setup installer doesn't change. The first time `RentalManager.exe`
runs and no configuration file exists yet, instead of loading the
dashboard, the browser shows a one-time setup screen: **"Just me on this
computer"** or **"Shared server for a team."**

- Choosing standalone writes a small config file and behaves exactly as the
  app does today — SQLite, no login, opens straight to the dashboard from
  then on.
- Choosing server mode asks for the Postgres connection details (host,
  port, database name, credentials) and has you create the first Admin
  account, then writes that configuration and takes you to a login screen.

This keeps the installer itself simple and low-risk, and puts the more
detailed data-entry (connection strings, admin account creation) in the
Flask app itself, where it's easy to build and test properly rather than in
Inno Setup's more limited scripting.

## Architecture changes

**Configuration layer.** A small config file (JSON) in the data directory
holds `mode` (`standalone` or `server`), the database connection string
(when applicable), and a signing key for sessions. `app.py` reads this once
at startup to decide how to wire everything else up.

**Database abstraction.** The app already goes through SQLAlchemy rather
than raw SQLite calls, so switching the connection string to Postgres in
server mode is mechanically simple. The one new dependency is a Postgres
driver (`psycopg2-binary`), loaded only when server mode is active so
standalone installs don't carry the extra weight.

**Schema migrations.** Up to now, the app has just called
`db.create_all()`, which only adds new tables — it can't alter existing
ones. Adding a Users table and a couple of audit columns is a good moment
to introduce proper migrations (Flask-Migrate / Alembic) so future schema
changes can be rolled out to existing installs safely, in both modes.

**Import cache fix.** The CSV import wizard currently holds preview data in
an in-memory dictionary on the server process. That's fine for a single
process, but would silently break under a multi-worker production server
(a preview created on one worker, committed against another, "session
expired" for no visible reason). Moving that cache into the database
(a small, self-expiring table) fixes this properly and benefits standalone
mode too, not just server mode.

## Authentication & roles

A `User` table (username/email, hashed password via Werkzeug's existing
password hashing, role, active flag). Login/logout and session handling via
Flask-Login. Three roles, per your call:

- **Admin** — manages user accounts, plus everything an Editor can do.
- **Editor** — full read/write on properties, transactions, categories,
  imports, and exports.
- **Viewer** — read-only. Can see the dashboard, transactions, and
  comparisons, and can still export CSV/Excel/PDF, but cannot add, edit,
  delete, or import.

In standalone mode, none of this is enforced — there's no login screen,
and every request behaves as it does today. The permission checks only
activate when `mode == server`.

As a light complement to multi-user access, transactions (and possibly
properties) gain a `created_by` field so it's visible who entered what —
useful for a shared team environment, not required for it to function.

## Server runtime

Flask's built-in development server isn't meant for concurrent production
traffic. For server mode, the app runs under **Waitress** — a pure-Python
WSGI server that runs natively on Windows with no extra system
dependencies, handling multiple simultaneous requests via threads rather
than needing a Unix-only server like gunicorn. `launcher.py` gets a server
variant that starts Waitress and stays running (no auto-opening a browser,
since it's meant to be left running unattended) rather than the
"start and open my browser" behavior standalone mode uses.

Keeping it running unattended (through reboots, without a logged-in
Windows session) is a Windows Task Scheduler or NSSM (a small, well-known
tool for running arbitrary programs as a Windows service) configuration —
documented as part of the deployment guide, not something the app itself
needs to manage.

## Backup & restore in server mode

The current full-backup feature works by zipping up the live SQLite file
directly — simple and exact, but SQLite-specific. Postgres needs a
different approach: a portable export that walks every table (properties,
categories, transactions, settings, users) into JSON, zipped together, with
the reverse process for import. Slightly more code, but it also means
server-mode backups aren't tied to a specific database engine, which is a
nice side benefit if the underlying database ever changes.

## What's actually built

**Phase 1 — Foundation. ✅ Done.** Config system and mode detection,
database abstraction, import-cache fix, migrations framework. No visible
change to current standalone behavior — this phase was entirely about
making the codebase ready for what comes next. Specifics:

- `config.py` — a small `config.json` in the data directory records `mode`
  (`standalone`/`server`), the database connection string, and a
  session-signing key. Standalone installs never need to touch this; it's
  created automatically with sensible defaults the first time the app runs.
- `app.py` now builds its `SQLALCHEMY_DATABASE_URI` from that config rather
  than a hardcoded SQLite path — pointing it at Postgres instead is a
  one-line config change, not a code change. Verified against a real local
  Postgres instance: schema creation, properties, transactions, dashboard
  queries, CSV import, and PDF/Excel exports all work identically to SQLite.
- Flask-Migrate/Alembic is wired in with two migrations: `0001_baseline`
  (captures exactly today's schema) and `0002_import_sessions` (the new
  table below). Existing databases — including the one already on this
  machine — are detected automatically and stamped at the baseline before
  upgrading, so nobody needs to run a migration command by hand. Verified
  this against a real copy of the live database on this machine: schema
  updated, all existing data preserved.
- The CSV import wizard's preview cache moved from an in-memory dictionary
  to a small, self-expiring database table (`import_sessions`), fixing a
  latent bug that would otherwise surface under a multi-process production
  server.
- Also caught and fixed two real packaging bugs while testing this against
  an actual frozen build: `build_exe.bat` had an invalid PyInstaller flag
  that would have failed the build outright, and the migration scripts
  needed an explicit `--hidden-import` plus being added to the bundle —
  both are fixed now and verified working in a packaged executable.

**Phase 2 — Accounts. ✅ Done.**

- `models.py` gained a `User` model (username, email, password hash via
  Werkzeug's hashing, role, active flag) and `Transaction.created_by`, both
  covered by migration `0003_users`.
- Flask-Login handles sessions (`login_user`/`logout_user`/`current_user`),
  wired in `app.py` with a `user_loader`. A `require_role(*roles)` decorator
  wraps every write endpoint — and is a complete no-op in standalone mode,
  so single-user installs are provably unaffected.
- The first-run setup wizard (`/setup`, `templates/setup.html`) offers
  "Just me on this computer" vs. "Shared server for a team." Choosing
  server mode tests the Postgres connection live before accepting it,
  then collects the first Admin account.
- **The chicken-and-egg problem** (a running process can't swap its live
  database engine mid-request) is solved by having the wizard write the
  new config plus the *hashed* admin credentials to `config.json` under
  `pending_admin_*` keys, and asking for a restart. The next startup is
  already connected to the new database; `_create_pending_admin_if_needed()`
  in `app.py` creates that admin row once migrations have run, then clears
  the pending fields so it never re-runs. Verified end-to-end against a
  real local Postgres instance, including inside the frozen .exe.
- Existing (pre-wizard) standalone installs are grandfathered in
  automatically — `config.py` detects a config file that predates the
  `setup_complete` key and marks it complete, so nobody who was already
  running this app gets sent through a wizard unexpectedly.
- Three roles (Admin, Editor, Viewer) enforced server-side on every write
  route, plus a role-aware frontend (`Settings → Users`, hidden buttons for
  Viewer accounts, a session/logout indicator in the top bar) — all inert
  in standalone mode.

**Phase 3 — Server hardening. ✅ Done.**

- `launcher.py` now branches on mode: standalone behavior (dev server +
  auto-opened browser) is untouched; server mode runs under **Waitress**
  bound to `0.0.0.0`, with no browser auto-open, meant to be left running
  (see the deployment guide for Task Scheduler/NSSM).
- Server-mode backup/restore (`/api/backup/full/*`) uses a portable JSON
  format (`backup.json` inside the zip) instead of a raw SQLite file copy,
  walking every table generically via SQLAlchemy's column introspection.
  Restoring resets Postgres's auto-increment sequences afterward so newly
  created rows don't collide with restored IDs — verified by creating a
  row immediately after a restore. Standalone mode's original SQLite
  file-swap backup is untouched; each format correctly rejects the other's
  file with a clear error instead of silently corrupting anything.
- `docs/deployment-guide.md` covers setting up Postgres, running the setup
  wizard, adding a team, keeping the app running unattended (Task
  Scheduler/NSSM), firewall guidance, and the packaging steps.
- Packaging is one PyInstaller build/installer that supports both modes —
  `requirements.txt` (Flask-Login and Waitress added) covers everything
  standalone needs and is all `run.bat`/`run.sh` install, so a local
  preview never depends on a working Postgres driver install.
  `requirements-server.txt` holds just `psycopg2-binary`; `build_exe.bat`
  installs both so the one .exe it produces supports server mode too.
  Verified by actually freezing the app on Linux and running it end-to-end
  in both standalone and Postgres-backed server mode, including login,
  role enforcement, and backup/restore inside the frozen executable.

## Explicitly deferred

Public internet access — HTTPS/TLS, a managed/hosted Postgres instance,
hardened session security, rate limiting, and so on — is intentionally not
part of this scope. The work above is what makes that a deployment change
rather than a rewrite when you're ready for it, but building it out is a
separate, later decision.
