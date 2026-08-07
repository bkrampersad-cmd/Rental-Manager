<div align="center">

# 🏠 Rental Property Manager

**A private, local-first app for tracking rental income and expenses across all your properties.**

No subscription. No ads. No cloud account required. Your data stays on your computer unless you choose otherwise.

![License](https://img.shields.io/badge/license-proprietary-blueviolet)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-informational)
![Modes](https://img.shields.io/badge/modes-Standalone%20%7C%20Network-success)
![Status](https://img.shields.io/badge/status-active-brightgreen)

</div>

---

## 📸 Screenshots

<table>
<tr>
<td width="50%"><img src="static/img/help/dashboard-mockup.svg" alt="Dashboard"></td>
<td width="50%"><img src="static/img/help/transactions-mockup.svg" alt="Transactions"></td>
</tr>
<tr>
<td align="center"><sub><b>Dashboard</b> — income, expenses, and cash flow at a glance</sub></td>
<td align="center"><sub><b>Transactions</b> — full ledger with inline property/unit editing</sub></td>
</tr>
<tr>
<td width="50%"><img src="static/img/help/import-mockup.svg" alt="Import"></td>
<td width="50%"><img src="static/img/help/tax-documents-mockup.svg" alt="Tax Documents"></td>
</tr>
<tr>
<td align="center"><sub><b>Import</b> — CSV, Excel, or PDF bank statements with column mapping</sub></td>
<td align="center"><sub><b>Tax Documents</b> — Schedule E-organized report, ready for your CPA</sub></td>
</tr>
</table>

---

## ⚡ Quick Install Guide

Want the fastest path to running this app? **[📄 Rental Property Manager — Install Guide (PDF)](docs/RentalManagerInstallGuide.pdf)** has a one-page Quick Install Guide right on its cover, plus a full walkthrough of every setup screen for both Standalone and Network mode. The short version:

1. Download `RentalManagerSetup.exe` and run it (no admin rights needed).
2. Launch the app — your browser opens automatically.
3. On first launch, choose **"Just me on this computer"** (Standalone) or **"Shared server for a team"** (Network mode).
4. Start adding properties.

See [Setup](#-setup) below for running from source instead, or the PDF guide for the complete picture.

---

## ✨ Features

### 🏘️ Properties & Accounting
- **Multi-property tracking** with a property switcher in the top bar that instantly filters every screen — one property or "All Properties" combined.
- **Bank accounts & units per property**, with bank-style **reconciliation** against a statement balance.
- **Rental-focused chart of accounts** (Rent Income, Late Fees, Mortgage Interest, Repairs & Maintenance, Property Management Fees, Capital Improvements, and more) — fully customizable, with a one-click **Restore Default Categories**.

### 💳 Transactions & Import
- Manual entry, or bulk **import from CSV, Excel, or PDF** bank statements — full column mapping, row/column exclusion, and automatic duplicate detection on re-import.
- **Import from QuickBooks (.iif) or Quicken (.qif)** — moving from either platform brings its categories and payees straight in, auto-matched to your existing properties by name.
- Scanned/photographed PDF statements are read via **OCR** (the app tells you plainly when OCR was used, so you know to double-check).
- **Import auto-categorization rules** — teach the importer a payee once, and future imports categorize themselves.
- **Email Auto-Import (Windows + Outlook)** — watches specific Outlook folders for unread bank statement attachments and imports them automatically; confidently-matched statements commit straight to the database (with a Dashboard **Undo**), anything less certain is left as a **Needs Review** item on the Import tab instead of being guessed at.
- **Recurring transactions** — set up rent, mortgage payments, or subscriptions once; they generate on schedule automatically.

### 🧾 Tax & Reporting
- **Tax Documents** — a Schedule E-organized report per property, plus a vendor/1099 report that flags anyone paid over the federal reporting threshold.
- **Mileage tracker** — log property-related trips (optionally tagged to a specific unit); deductions calculate at the current IRS standard mileage rate and flow into your tax report automatically.
- **Property comparison** — cap rate, cash-on-cash return, income, expenses, and net cash flow, side by side for any subset of properties you pick.
- **Exports** to CSV, Excel (formatted, color-coded), or a polished letterhead-branded PDF — and a **Print** button on Dashboard/Transactions/Compare for a clean printed page.
- **Migrate to QuickBooks or Quicken** — export your full transaction history as a QuickBooks Desktop IIF (with matching accounts and a Class per property), a Quicken QIF (categories + property preserved), or a generic bank CSV for QuickBooks Online's manual upload.

### 🏡 Tenants & Documents
- **Tenants & leases** — names, unit assignments, lease dates, and monthly rent per property.
- **Property documents** — store leases, insurance policies, and other files against a property, with expiration-date tracking.
- **Dashboard alerts** — proactive nudges for unreconciled accounts, quiet properties, expiring leases/documents, and statements from Email Auto-Import waiting for review.
- **Global search** — one search box finds properties, transactions, and tenants at once.

### 🎨 Branding & Customization
- **Business branding** — your name, address, contact info, and logo replace the generic look throughout the app and on every export.
- **Custom colors** for income/expense/accent, a custom **currency symbol**, and a configurable **fiscal year start month**.
- **Report footer text** — a custom line printed at the bottom of every PDF/Excel export.

### 🔒 Security & Data Control
- **Local-first by default** — Standalone mode keeps everything in one SQLite file on your computer, no login, no account.
- **Optional Access Password** (Standalone) — a single shared password with a lock screen at startup and a one-click **Lock Now** button.
- **3 security questions for recovery** — forget the password, answer any one of your three questions to set a new one, no email or support desk needed.
- **Optional lockout** — after a configurable number of wrong guesses (password or security answers), the app locks out further attempts for 15 minutes.
- **Danger Zone reset options**, gated behind a recent Full Backup and (in Network mode) Admin role — **Reset Data Only** or a complete **Full Factory Reset**, each requiring you to type `DELETE` to confirm.
- **Backup & Restore** — export just your business profile (branding, no data) or a full backup (everything) as a `.zip`, with every backup logged so the reset gate always knows how current it is.
- **Automatic Backups** — optional Weekly or Every-30-Days schedule that writes a Full Backup straight into a `Backup` folder next to the app, keeping up to 30 files (oldest deleted first) and restorable with one click.
- **Accounts & roles (Network mode)** — Admin / Editor / Viewer logins, with transactions recording who entered them.

---

## 🖥️ Standalone vs. Network Mode

One codebase, one installer — which mode you get is a **setup-screen choice**, not a different piece of software.

| | Standalone | Network (Server) |
|---|---|---|
| **Best for** | One person, one computer | A small team sharing data |
| **Database** | Local SQLite file | Shared PostgreSQL |
| **Login** | None (optional Access Password) | Required — Admin/Editor/Viewer roles |
| **Setup** | Nothing extra | Needs a reachable Postgres instance |

See [`docs/deployment-guide.md`](docs/deployment-guide.md) for the full Network mode walkthrough (Postgres setup, keeping it running unattended, firewall rules).

---

## 📝 A note on bank linking

True live bank sync (like Plaid, which powers apps like DoorLoop and Rocket Money) requires your own developer account, API keys, and ideally a hosted server, since it involves handling other people's banking credentials — a significant step up from a local app. This app instead supports **CSV import**, covering the same end goal (getting bank transactions in) without any external account or security exposure. If you later set up a Plaid developer account, `importer.py` is a natural place to add a live-sync data source alongside the CSV path.

## 📝 A note on PDF import and OCR

Digital PDF statements (the normal kind your bank lets you download, where you can select/copy the text) import out of the box. A scanned or photographed statement needs OCR, which requires [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) — a well-known, free, open-source OCR engine. By default this is a separate system-level install on the machine running the app; if it isn't installed, the app says so plainly instead of failing silently. If you're building your own installer to hand out, you can optionally bundle Tesseract right into it instead (see "BUILDING TESSERACT OCR INTO THE APP ITSELF" in `BUILD INSTRUCTIONS.txt`) so it works for whoever you give it to with nothing extra for them to install. OCR is never perfectly accurate — always check extracted rows against the original statement before importing.

---

## 🚀 Setup

Requires Python 3.9+.

**Windows:** double-click `run.bat` (creates a virtual environment, installs dependencies, and starts the app).

**Mac/Linux:** run `./run.sh` from a terminal.

**Manual setup (any OS):**

```
python -m venv venv
venv\Scripts\activate      (Windows)   or   source venv/bin/activate   (Mac/Linux)
pip install -r requirements.txt
python launcher.py
```

This opens **http://localhost:5000** automatically. The first time it runs with no existing configuration, you'll see a one-time setup screen — choose "Just me on this computer" for the normal single-user experience (see `docs/deployment-guide.md` for "Shared server for a team" instead).

If you already had this app running before this setup screen existed, you won't lose anything — the next launch shows that one screen once, and choosing "Just me on this computer" picks right back up with your existing properties and transactions intact.

Your data lives in `data/rental_manager.db` when run this way. Back this file up periodically (or add it to a cloud-synced folder) — it's the only thing you need to preserve.

## 🔄 Moving to a new machine / setting up a second install

Because each installed copy keeps its own database on that machine, there's no automatic syncing between installs. **Settings → Backup & Restore** gives you two ways to move things over:

- **Business Profile Only** exports your name, address, contact info, colors, and logo as a small `.zip` — no properties or transactions. Handy for quickly re-applying your branding to a fresh install, or handing a pre-branded starting point to someone else.
- **Full Backup (Profile + Data)** exports everything as a `.zip`. Importing a full backup **replaces all current data** in the install you import it into — meant for migrating to a new computer or restoring after a problem, not merging two active installs. Standalone mode backs up by copying the SQLite file directly; Network mode backs up to a portable JSON format (including user accounts) instead. The two formats aren't interchangeable — each correctly rejects the other's file.

## 📦 Building an installer (.exe) to hand out

One installer covers both modes — which one a given install runs as is decided by the setup screen the person sees on first launch. If you're setting up a shared team install, see `docs/deployment-guide.md` for the full walkthrough. The build steps themselves are the same either way:

1. Install Python 3.9+ if it isn't already on that machine (only needed for the build).
2. Install [Inno Setup](https://jrsoftware.org/isdl.php) (free) — either Inno Setup 6 or 7 both work.
3. From the project folder, run **`build_exe.bat`**. This creates a virtual environment, installs dependencies plus PyInstaller, and freezes the app into `dist\RentalManager\RentalManager.exe`.
4. Run **`build_installer.bat`**. This compiles `installer.iss` with Inno Setup into `installer_output\RentalManagerSetup.exe`, which also bundles the password-recovery scripts (see below).

Hand `RentalManagerSetup.exe` to anyone. Running it installs the app to their own user folder (no admin rights needed), adds a Start Menu entry and an optional desktop shortcut, and includes an uninstaller.

In Standalone mode, each installed copy keeps its own data in `%LOCALAPPDATA%\RentalManager\data\rental_manager.db`. In Network mode, everyone connecting to the same PostgreSQL database shares the same data.

Note: nothing needs to be cleared out before building. The `data/` folder in this project is **never** bundled into the exe — each installed copy creates its own empty database the first time it runs, so your local test data never ends up in what you hand out.

## 🔑 If a password is forgotten

- **Network mode:** any other Admin can reset a locked-out user's password in two clicks via Settings → Users. If every Admin is locked out at once, `reset_admin_password.py` (included with the app) resets one directly against the Postgres database.
- **Standalone mode:** click **Forgot password?** on the lock screen and answer any one of your 3 security questions. If you've also forgotten all 3 answers, `reset_access_password.py` (included with the app) clears the password and security questions directly from the database file as a last resort.

## 📜 License

Proprietary — free to use, not to modify or redistribute. See [`LICENSE.md`](LICENSE.md) (or `LICENSE`), or Settings → About → View License inside the app.

## 📁 Project structure

```
app.py                    Flask app, API routes, setup wizard, login, role enforcement
models.py                 Database models (properties, transactions, categories, settings, users)
config.py                 Standalone-vs-server mode config (see docs/multi-user-server-mode-plan.md)
importer.py               CSV/Excel/PDF statement parsing, OCR fallback, & column auto-detection
email_monitor.py          Outlook MAPI/COM helpers for Email Auto-Import (Windows + Outlook only)
exports.py                CSV / Excel / PDF generation, incl. business letterhead branding
launcher.py               Entry point — standalone (dev server + browser) or server mode (Waitress)
migrations/               Database schema migrations (Flask-Migrate/Alembic) — applied automatically on startup
templates/index.html      Main single-page UI
templates/setup.html      First-run setup wizard (mode choice, Postgres connection, first admin)
templates/login.html      Login page (server mode only)
templates/unlock.html     Access Password lock screen (standalone mode only)
static/css/style.css      Styling (theme colors driven by CSS variables)
static/js/app.js          Frontend logic, including lightweight canvas charts and role-aware UI
data/                     SQLite database + config.json (created on first run)
LICENSE / LICENSE.md      Proprietary license, free use/no modification
reset_access_password.py  Last-resort standalone password/security-question recovery tool
reset_admin_password.py   Last-resort Network mode admin password recovery tool
requirements.txt          Core dependencies (used by run.bat/.sh — always installs cleanly)
requirements-server.txt   PostgreSQL driver, only needed for server mode / building the installer
build_exe.bat             Freezes the app into a standalone .exe with PyInstaller
build_installer.bat       Compiles the Inno Setup installer
installer.iss             Inno Setup script (installer contents/shortcuts)
docs/                     Planning docs, deployment-guide.md, and the Install Guide PDF
```

---

<div align="center">
<sub>✨ Created by Beeran Rampersad · Built with the assistance of Claude AI</sub>
</div>
