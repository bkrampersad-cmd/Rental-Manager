"""Rental Property Manager — local Flask app.

Run with:  python app.py
Then open: http://localhost:5000

Packaging note: when frozen into a standalone .exe with PyInstaller,
templates/static are read from the bundled (read-only) resource dir, while
the SQLite database lives in a writable per-user data folder so it survives
app updates/reinstalls. See launcher.py and BUILD.md for the packaging flow.
"""
import functools
import io
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from flask import Flask, jsonify, request, render_template, send_file, abort, redirect, url_for, session
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from flask_migrate import Migrate, upgrade as migrate_upgrade, stamp as migrate_stamp
from sqlalchemy import inspect as sa_inspect, text as sa_text
from werkzeug.security import generate_password_hash, check_password_hash

from models import (
    db,
    Property,
    Category,
    Transaction,
    Setting,
    ImportSession,
    User,
    PropertyAccount,
    ACCOUNT_TYPES,
    PropertyUnit,
    AccountReconciliation,
    RecurringTransaction,
    RECURRING_FREQUENCIES,
    ImportRule,
    MileageLog,
    Tenant,
    PropertyDocument,
    DOCUMENT_TYPES,
    BackupLog,
    EmailImportRule,
    EmailImportBatch,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_VIEWER,
    ROLES,
    seed_categories,
    restore_default_categories,
    seed_settings,
    DEFAULT_SETTINGS,
    current_standard_mileage_rate,
    SCHEDULE_E_LINES,
    SCHEDULE_E_NOT_DEDUCTIBLE,
    SCHEDULE_E_CAPITALIZE,
)
from importer import sniff_file, build_transactions, get_ocr_status
import exports as export_lib
import tax_report as tax_lib
import email_monitor
from config import (
    load_config, save_config, get_database_url, is_server_mode,
    build_postgres_url, test_database_connection, reset_config,
)

IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    # PyInstaller extracts bundled data files (templates/static) here.
    RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # Writable, per-user location that survives reinstalls/updates.
    APPDATA = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or os.path.expanduser("~")
    DATA_DIR = os.path.join(APPDATA, "RentalManager", "data")
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(RESOURCE_DIR, "data")

DB_PATH = os.path.join(DATA_DIR, "rental_manager.db")

# Where scheduled automatic backups are written — literally "a Backup folder
# in the app's own directory" (next to RentalManager.exe / app.py), not
# tucked away in DATA_DIR, so it's easy to find in Explorer. RESOURCE_DIR is
# writable here even in a packaged install: PyInstaller's --onedir layout
# puts it under the user's own LocalAppData\Programs (see installer.iss —
# PrivilegesRequired=lowest), not the real (UAC-protected) Program Files.
# Uninstalling only removes files the installer itself put there, so this
# folder and its contents are left behind rather than deleted.
BACKUP_DIR = os.path.join(RESOURCE_DIR, "Backup")

# Config decides SQLite-standalone vs. Postgres-server before the Flask app
# or database engine exist. Absent any config.json, this is a no-op that
# reproduces exactly today's standalone behavior.
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
APP_CONFIG = load_config(DATA_DIR)
APP_MODE = APP_CONFIG["mode"]

app = Flask(
    __name__,
    template_folder=os.path.join(RESOURCE_DIR, "templates"),
    static_folder=os.path.join(RESOURCE_DIR, "static"),
)
app.config["SQLALCHEMY_DATABASE_URI"] = get_database_url(APP_CONFIG, DB_PATH)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB upload cap
app.config["SECRET_KEY"] = APP_CONFIG["secret_key"]
app.config["RENTAL_MANAGER_MODE"] = APP_MODE

db.init_app(app)

# Migration scripts are application code (like templates/static), not user
# data, so they live next to app.py / inside the bundled resource dir rather
# than in the per-user data directory.
MIGRATIONS_DIR = os.path.join(RESOURCE_DIR, "migrations")
migrate = Migrate(app, db, directory=MIGRATIONS_DIR)

BASELINE_REVISION = "0001_baseline"

MORTGAGE_PRINCIPAL_INTEREST = {"Mortgage Interest", "Mortgage Principal"}

# ---------------------------------------------------------------------------
# Login (server mode only — standalone mode never touches any of this)
# ---------------------------------------------------------------------------

login_manager = LoginManager()
login_manager.login_view = "login_page"
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def require_role(*roles):
    """Restrict a write endpoint to the given roles — but ONLY in server
    mode. In standalone mode there's no login at all, so every request is
    allowed through exactly as it always has been; this decorator is a
    complete no-op there."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_server_mode(APP_CONFIG):
                return fn(*args, **kwargs)
            if not current_user.is_authenticated:
                return jsonify({"error": "Authentication required"}), 401
            if current_user.role not in roles:
                return jsonify({"error": "You don't have permission to do that"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _current_username():
    """Best-effort attribution for created_by — None in standalone mode."""
    if is_server_mode(APP_CONFIG) and current_user.is_authenticated:
        return current_user.username
    return None


# Paths reachable before the first-run wizard is complete, and (once
# complete) without being logged in, in server mode.
_SETUP_OPEN_PREFIXES = ("/api/setup", "/static")
_LOGIN_OPEN_PREFIXES = ("/api/login", "/static")
_UNLOCK_OPEN_PREFIXES = ("/api/unlock", "/static")


def _standalone_password_enabled():
    return bool(_settings_map().get("access_password_hash"))


@app.before_request
def _enforce_access():
    path = request.path

    if not APP_CONFIG.get("setup_complete"):
        if path == "/setup" or path.startswith(_SETUP_OPEN_PREFIXES):
            return None
        return redirect("/setup")

    # Setup is done — /setup has no further purpose.
    if path == "/setup" or path.startswith("/api/setup"):
        abort(404)

    if not is_server_mode(APP_CONFIG):
        # Standalone: no user accounts, login/logout pages don't apply — but
        # an optional single shared "access password" (Settings) can still
        # gate the whole app behind an /unlock screen, entirely separate
        # from Network mode's per-user login.
        if path in ("/login",) or path.startswith("/api/login"):
            return redirect("/")
        if _standalone_password_enabled():
            if path == "/unlock" or path.startswith(_UNLOCK_OPEN_PREFIXES) or path == "/api/lock":
                return None
            if not session.get("standalone_unlocked"):
                if path.startswith("/api/"):
                    return jsonify({"error": "Locked"}), 401
                return redirect("/unlock")
        elif path == "/unlock" or path.startswith("/api/unlock"):
            abort(404)
        return None

    # Server mode from here on: everything requires an authenticated session
    # except the login page/API itself and static assets.
    if path == "/login" or path.startswith(_LOGIN_OPEN_PREFIXES):
        return None
    if path == "/unlock" or path.startswith("/api/unlock"):
        abort(404)  # Access Password is a Standalone-only feature.
    if not current_user.is_authenticated:
        if path.startswith("/api/"):
            return jsonify({"error": "Authentication required"}), 401
        return redirect(url_for("login_page"))
    return None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with app.app_context():
        _ensure_schema_up_to_date()
        seed_categories()
        seed_settings()
        _create_pending_admin_if_needed()


def _create_pending_admin_if_needed():
    """Resolves the setup wizard's server-mode chicken-and-egg problem.

    When the wizard writes a server-mode config, the *running* process is
    still connected to the old (standalone) engine — you can't swap a live
    SQLAlchemy engine out from under a process. So the wizard stores the new
    admin account (already hashed) in config.json and asks for a restart.
    The next time the process starts, it's already pointed at the new
    database (migrated by _ensure_schema_up_to_date() just above), so this
    is where that pending account actually gets created — once, then the
    pending fields are cleared so it never runs again.
    """
    pending_username = APP_CONFIG.get("pending_admin_username")
    pending_hash = APP_CONFIG.get("pending_admin_password_hash")
    if not pending_username or not pending_hash:
        return
    if not User.query.filter_by(username=pending_username).first():
        db.session.add(User(
            username=pending_username,
            email=APP_CONFIG.get("pending_admin_email") or None,
            password_hash=pending_hash,
            role=ROLE_ADMIN,
            active=True,
        ))
        db.session.commit()
    APP_CONFIG["pending_admin_username"] = None
    APP_CONFIG["pending_admin_email"] = None
    APP_CONFIG["pending_admin_password_hash"] = None
    save_config(DATA_DIR, APP_CONFIG)


def _ensure_schema_up_to_date():
    """Bring the database schema up to the latest migration.

    Brand-new databases: Alembic builds the schema from scratch by running
    every migration in order.

    Existing databases from before migrations existed (created via the old
    db.create_all() approach — including anyone's current standalone
    install): there's no alembic_version table yet, but the tables already
    match the baseline migration, so we stamp it as already applied instead
    of re-running it, then upgrade from there. This all happens
    automatically; nobody needs to run a migration command by hand.
    """
    inspector = sa_inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    if "properties" in existing_tables and "alembic_version" not in existing_tables:
        migrate_stamp(revision=BASELINE_REVISION)

    migrate_upgrade()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_bounds(args):
    start = args.get("start")
    end = args.get("end")
    return start, end


def _query_transactions(property_id=None, start=None, end=None, type_=None, category_id=None, search=None, unit_id=None):
    q = Transaction.query
    if property_id and str(property_id).lower() != "all":
        q = q.filter(Transaction.property_id == int(property_id))
    if start:
        q = q.filter(Transaction.date >= start)
    if end:
        q = q.filter(Transaction.date <= end)
    if type_ and type_ != "all":
        q = q.filter(Transaction.type == type_)
    if category_id:
        q = q.filter(Transaction.category_id == int(category_id))
    if unit_id:
        q = q.filter(Transaction.unit_id == int(unit_id))
    if search:
        like = f"%{search}%"
        q = q.filter(Transaction.payee.ilike(like))
    return q.order_by(Transaction.date.desc()).all()


def _txn_dict_with_property(t):
    d = t.to_dict()
    d["property_name"] = t.property.name if t.property else ""
    linked_log = MileageLog.query.filter_by(transaction_id=t.id).first()
    d["mileage_miles"] = linked_log.miles if linked_log else None
    return d


def _settings_map():
    result = dict(DEFAULT_SETTINGS)
    for r in Setting.query.all():
        result[r.key] = r.value
    return result


def _set_setting(key, value):
    row = db.session.get(Setting, key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def _business_info():
    s = _settings_map()
    address_parts = [s.get("business_address"), s.get("business_city"), s.get("business_state"), s.get("business_zip")]
    return {
        "name": s.get("business_name") or "",
        "address_line": ", ".join(p for p in address_parts if p),
        "phone": s.get("business_phone") or "",
        "email": s.get("business_email") or "",
        "website": s.get("business_website") or "",
        "tax_id": s.get("business_tax_id") or "",
        "footer": s.get("report_footer") or "",
    }


def _logo_bytes(variant="print"):
    if _settings_map().get("has_logo") != "1":
        return None
    path = os.path.join(DATA_DIR, f"logo_{variant}.png")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _fiscal_year_bounds(today, start_month):
    """Return (start_date, today) for the fiscal year containing `today`,
    given a 1-12 starting month (1 = plain calendar year)."""
    try:
        start_month = int(start_month or 1)
    except (TypeError, ValueError):
        start_month = 1
    start_month = min(max(start_month, 1), 12)
    if start_month == 1:
        return today.replace(month=1, day=1), today
    if today.month >= start_month:
        start = today.replace(month=start_month, day=1)
    else:
        start = today.replace(year=today.year - 1, month=start_month, day=1)
    return start, today


def _default_period():
    today = date.today()
    fy_start_month = _settings_map().get("fiscal_year_start_month", "1")
    start, end = _fiscal_year_bounds(today, fy_start_month)
    return start.isoformat(), end.isoformat()


def _compute_property_metrics(prop, start, end):
    txns = _query_transactions(property_id=prop.id, start=start, end=end)
    income = sum(t.amount for t in txns if t.type == "income")
    expenses = sum(t.amount for t in txns if t.type == "expense")
    operating_expenses = sum(
        t.amount for t in txns
        if t.type == "expense" and (not t.category or t.category.name not in MORTGAGE_PRINCIPAL_INTEREST)
    )
    net = income - expenses
    noi = income - operating_expenses

    # Annualize based on the number of months covered, for cap-rate comparability.
    months = _months_between(start, end)
    annualization = 12.0 / months if months else 1.0

    cap_rate = None
    if prop.current_value and prop.current_value > 0:
        cap_rate = (noi * annualization / prop.current_value) * 100

    coc_return = None
    cash_basis = prop.down_payment if prop.down_payment else prop.purchase_price
    if cash_basis and cash_basis > 0:
        coc_return = (net * annualization / cash_basis) * 100

    return {
        "property_id": prop.id,
        "name": prop.name,
        "income": income,
        "expenses": expenses,
        "net": net,
        "noi": noi,
        "cap_rate": cap_rate,
        "coc_return": coc_return,
        "transaction_count": len(txns),
    }


def _months_between(start, end):
    if not start or not end:
        return 12
    try:
        s = datetime.fromisoformat(start).date()
        e = datetime.fromisoformat(end).date()
    except ValueError:
        return 12
    delta = relativedelta(e, s)
    months = delta.years * 12 + delta.months + 1
    return max(months, 1)


def _monthly_series(property_id, start, end):
    txns = _query_transactions(property_id=property_id, start=start, end=end)
    buckets = {}
    for t in txns:
        month = t.date[:7]  # yyyy-mm
        b = buckets.setdefault(month, {"income": 0.0, "expense": 0.0})
        b[t.type] += t.amount
    months = sorted(buckets.keys())
    return [{"month": m, "income": buckets[m]["income"], "expense": buckets[m]["expense"]} for m in months]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# First-run setup wizard
# ---------------------------------------------------------------------------

@app.route("/setup")
def setup_page():
    return render_template("setup.html")


@app.route("/api/setup/standalone", methods=["POST"])
def setup_standalone():
    APP_CONFIG["mode"] = "standalone"
    APP_CONFIG["setup_complete"] = True
    save_config(DATA_DIR, APP_CONFIG)
    init_db()
    return jsonify({"ok": True})


@app.route("/api/setup/server/test", methods=["POST"])
def setup_server_test():
    data = request.get_json(force=True)
    url = build_postgres_url(
        data.get("host"), data.get("port"), data.get("dbname"),
        data.get("user"), data.get("password"),
    )
    ok, error = test_database_connection(url)
    if not ok:
        return jsonify({"ok": False, "error": f"Could not connect: {error}"}), 400
    return jsonify({"ok": True})


@app.route("/api/setup/server/complete", methods=["POST"])
def setup_server_complete():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password should be at least 8 characters"}), 400

    # Re-verify — the connection details are the source of truth we're about
    # to persist, so don't trust a test that happened moments earlier.
    url = build_postgres_url(data.get("host"), data.get("port"), data.get("dbname"), data.get("user"), data.get("password"))
    ok, error = test_database_connection(url)
    if not ok:
        return jsonify({"error": f"Could not connect: {error}"}), 400

    APP_CONFIG["mode"] = "server"
    APP_CONFIG["database_url"] = url
    APP_CONFIG["setup_complete"] = True
    APP_CONFIG["pending_admin_username"] = username
    APP_CONFIG["pending_admin_email"] = (data.get("email") or "").strip() or None
    APP_CONFIG["pending_admin_password_hash"] = generate_password_hash(password)
    save_config(DATA_DIR, APP_CONFIG)

    return jsonify({"ok": True, "restart_required": True})


# ---------------------------------------------------------------------------
# Login / logout (server mode only)
# ---------------------------------------------------------------------------

@app.route("/login")
def login_page():
    if not is_server_mode(APP_CONFIG):
        return redirect("/")
    if current_user.is_authenticated:
        return redirect("/")
    business = _business_info()
    logo_url = "/api/business/logo?variant=app" if _settings_map().get("has_logo") == "1" else None
    return render_template("login.html", business_name=business["name"], logo_url=logo_url)


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = User.query.filter_by(username=username).first()
    if not user or not user.active or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid username or password"}), 401
    login_user(user)
    return jsonify({"ok": True, "user": user.to_dict()})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/session", methods=["GET"])
def api_session():
    server_mode = is_server_mode(APP_CONFIG)
    if server_mode and current_user.is_authenticated:
        return jsonify({
            "mode": "server",
            "authenticated": True,
            "username": current_user.username,
            "role": current_user.role,
        })
    return jsonify({
        "mode": "server" if server_mode else "standalone",
        "authenticated": not server_mode,
        "username": None,
        "role": None,
        "standalone_password_enabled": (not server_mode) and _standalone_password_enabled(),
    })


# ---------------------------------------------------------------------------
# Access Password (Standalone mode only) — an optional single shared
# password that gates the whole app behind an /unlock screen. This is
# deliberately much simpler than Network mode's per-user login: one
# password, no username, no roles — Standalone is still meant for one
# person, this just adds a lock screen for a shared/laptop scenario.
#
# Recovery: since there's no email/username to send a reset link to, the
# password is backed by 3 security questions (answer any ONE of the three
# correctly to set a new password) — set up at the same time the password
# is first turned on, since that's the only chance to capture a recovery
# path before it's needed. If both the password AND all 3 answers are
# forgotten, the only way back in is direct access to the database file
# (see the User Manual) — there's no email/support-desk reset for a
# single-user desktop app.
# ---------------------------------------------------------------------------

# Offered when choosing the 3 security questions — deliberately generic,
# stable-over-time facts (not "favorite" anything, which people's answers to
# tend to drift on) so an answer given once stays correct for good.
SECURITY_QUESTION_CHOICES = [
    "What was the name of your first pet?",
    "What city were you born in?",
    "What was the make and model of your first car?",
    "What is your mother's maiden name?",
    "What was the name of your elementary school?",
    "What street did you grow up on?",
    "What was your childhood nickname?",
    "Who was your childhood best friend?",
]

# How many consecutive wrong guesses (wrong password OR wrong security
# answer — they share one counter, since both are ways of guessing your way
# in) trigger a temporary lockout, and how long that lockout lasts. The
# attempt count is configurable (Settings, optional, blank = off); the
# cooldown length is not — 15 minutes is enough to make guessing
# impractical without being a real burden on someone who mistyped.
ACCESS_LOCKOUT_COOLDOWN_MINUTES = 15


def _normalize_security_answer(answer):
    """Security-question answers are matched case/whitespace-insensitively
    — "Fluffy", "fluffy ", and "FLUFFY" should all count as the same
    answer, since people aren't consistent about how they'd type it a
    second time."""
    return (answer or "").strip().lower()


def _access_lockout_status():
    """Returns (locked, minutes_remaining) — minutes_remaining is None
    unless locked is True."""
    until_iso = _settings_map().get("access_password_lockout_until")
    if not until_iso:
        return False, None
    try:
        until = datetime.fromisoformat(until_iso)
    except ValueError:
        return False, None
    remaining = (until - datetime.utcnow()).total_seconds() / 60
    if remaining <= 0:
        return False, None
    return True, remaining


def _record_access_attempt_result(success):
    """Call after every password or security-answer attempt. A success
    always clears any accumulated count/lockout. A failure only starts
    counting toward a lockout if the optional threshold (Settings) is
    actually set — most installs will leave this off."""
    if success:
        _set_setting("access_password_failed_attempts", "0")
        _set_setting("access_password_lockout_until", "")
        return
    threshold_raw = _settings_map().get("access_password_lockout_attempts")
    try:
        threshold = int(threshold_raw) if threshold_raw else 0
    except ValueError:
        threshold = 0
    if threshold <= 0:
        return  # feature is off
    attempts = int(_settings_map().get("access_password_failed_attempts") or 0) + 1
    if attempts >= threshold:
        lockout_until = datetime.utcnow() + timedelta(minutes=ACCESS_LOCKOUT_COOLDOWN_MINUTES)
        _set_setting("access_password_lockout_until", lockout_until.isoformat())
        _set_setting("access_password_failed_attempts", "0")  # fresh count once the cooldown ends
    else:
        _set_setting("access_password_failed_attempts", str(attempts))


@app.route("/unlock")
def unlock_page():
    if is_server_mode(APP_CONFIG) or not _standalone_password_enabled():
        return redirect("/")
    if session.get("standalone_unlocked"):
        return redirect("/")
    business = _business_info()
    logo_url = "/api/business/logo?variant=app" if _settings_map().get("has_logo") == "1" else None
    return render_template("unlock.html", business_name=business["name"], logo_url=logo_url)


@app.route("/api/unlock", methods=["POST"])
def api_unlock():
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Not applicable in Network mode"}), 400
    stored_hash = _settings_map().get("access_password_hash")
    if not stored_hash:
        session["standalone_unlocked"] = True
        return jsonify({"ok": True})

    locked, minutes_remaining = _access_lockout_status()
    if locked:
        return jsonify({"error": f"Too many attempts. Try again in about {round(minutes_remaining)} minute(s)."}), 423

    password = (request.get_json(force=True) or {}).get("password") or ""
    if not check_password_hash(stored_hash, password):
        _record_access_attempt_result(success=False)
        return jsonify({"error": "Incorrect password"}), 401
    _record_access_attempt_result(success=True)
    session["standalone_unlocked"] = True
    return jsonify({"ok": True})


@app.route("/api/lock", methods=["POST"])
def api_lock():
    session.pop("standalone_unlocked", None)
    return jsonify({"ok": True})


@app.route("/api/unlock/questions", methods=["GET"])
def get_unlock_questions():
    """Powers the "Forgot password?" link on the unlock screen — lists the
    3 configured questions (never the answers) so the person can pick which
    one(s) to try. available: False means this install was never set up
    with security questions (or they were cleared) — no in-app recovery is
    possible."""
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Not applicable in Network mode"}), 400
    locked, minutes_remaining = _access_lockout_status()
    if locked:
        return jsonify({"locked": True, "minutes_remaining": round(minutes_remaining, 1)}), 423
    settings_map = _settings_map()
    questions = [settings_map.get(f"access_security_q{i}") for i in (1, 2, 3)]
    if not all(questions):
        return jsonify({"available": False})
    return jsonify({"available": True, "questions": questions})


@app.route("/api/unlock/recover", methods=["POST"])
def recover_unlock():
    """Answer any ONE of the 3 security questions correctly to set a brand
    new access password, bypassing the forgotten one entirely."""
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Not applicable in Network mode"}), 400

    locked, minutes_remaining = _access_lockout_status()
    if locked:
        return jsonify({"error": f"Too many attempts. Try again in about {round(minutes_remaining)} minute(s)."}), 423

    settings_map = _settings_map()
    answer_hashes = [settings_map.get(f"access_security_a{i}_hash") for i in (1, 2, 3)]
    if not all(answer_hashes):
        return jsonify({
            "error": "No security questions are set up for this install — direct database access is "
                     "required to recover. See the User Manual (Settings section)."
        }), 400

    data = request.get_json(force=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 4:
        return jsonify({"error": "Password should be at least 4 characters"}), 400

    answers = data.get("answers") or {}
    matched = False
    for i in (1, 2, 3):
        raw_answer = answers.get(str(i))
        if not raw_answer:
            continue
        if check_password_hash(answer_hashes[i - 1], _normalize_security_answer(raw_answer)):
            matched = True
            break

    if not matched:
        _record_access_attempt_result(success=False)
        return jsonify({"error": "None of the answers matched."}), 401

    _set_setting("access_password_hash", generate_password_hash(new_password))
    _record_access_attempt_result(success=True)
    session["standalone_unlocked"] = True
    return jsonify({"ok": True})


@app.route("/api/settings/access-password", methods=["POST"])
def set_access_password():
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Access Password is only available in Standalone mode"}), 400
    data = request.get_json(force=True) or {}
    new_password = data.get("new_password") or ""
    if len(new_password) < 4:
        return jsonify({"error": "Password should be at least 4 characters"}), 400

    existing_hash = _settings_map().get("access_password_hash")
    first_time_setup = not existing_hash

    if existing_hash:
        current_password = data.get("current_password") or ""
        if not check_password_hash(existing_hash, current_password):
            return jsonify({"error": "Current password is incorrect"}), 401

    # Security questions are required the very first time the password is
    # turned on (it's the only recovery path there'll ever be for this
    # install) — optional to also update on a later password change.
    questions_payload = data.get("security_questions")
    if first_time_setup or questions_payload is not None:
        if not isinstance(questions_payload, list) or len(questions_payload) != 3:
            return jsonify({"error": "Please choose 3 security questions and answer all of them."}), 400
        normalized = []
        seen = set()
        for item in questions_payload:
            question = ((item or {}).get("question") or "").strip()
            answer = _normalize_security_answer((item or {}).get("answer"))
            if not question or not answer:
                return jsonify({"error": "Please choose 3 security questions and answer all of them."}), 400
            if question in seen:
                return jsonify({"error": "Please choose 3 different security questions."}), 400
            seen.add(question)
            normalized.append((question, answer))
        for i, (question, answer) in enumerate(normalized, start=1):
            _set_setting(f"access_security_q{i}", question)
            _set_setting(f"access_security_a{i}_hash", generate_password_hash(answer))

    _set_setting("access_password_hash", generate_password_hash(new_password))
    _record_access_attempt_result(success=True)  # a fresh password clears any lockout in progress
    session["standalone_unlocked"] = True  # don't lock the person out of the change they just made
    return jsonify({"ok": True})


@app.route("/api/settings/access-password/disable", methods=["POST"])
def disable_access_password():
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Access Password is only available in Standalone mode"}), 400
    existing_hash = _settings_map().get("access_password_hash")
    if not existing_hash:
        return jsonify({"ok": True})  # already disabled — nothing to do
    data = request.get_json(force=True) or {}
    current_password = data.get("current_password") or ""
    if not check_password_hash(existing_hash, current_password):
        return jsonify({"error": "Current password is incorrect"}), 401
    for key in (
        "access_password_hash",
        "access_security_q1", "access_security_a1_hash",
        "access_security_q2", "access_security_a2_hash",
        "access_security_q3", "access_security_a3_hash",
        "access_password_failed_attempts", "access_password_lockout_until",
    ):
        row = db.session.get(Setting, key)
        if row:
            db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/settings/access-password/question-choices", methods=["GET"])
def get_security_question_choices():
    if is_server_mode(APP_CONFIG):
        return jsonify({"error": "Not applicable in Network mode"}), 400
    return jsonify({"choices": SECURITY_QUESTION_CHOICES})


# ---------------------------------------------------------------------------
# User management (server mode, Admin only)
# ---------------------------------------------------------------------------

@app.route("/api/users", methods=["GET"])
@require_role(ROLE_ADMIN)
def list_users():
    users = User.query.order_by(User.username).all()
    return jsonify([u.to_dict() for u in users])


@app.route("/api/users", methods=["POST"])
@require_role(ROLE_ADMIN)
def create_user():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role") or ROLE_EDITOR
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password should be at least 8 characters"}), 400
    if role not in ROLES:
        return jsonify({"error": "Invalid role"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "That username is already taken"}), 400
    user = User(
        username=username,
        email=(data.get("email") or "").strip() or None,
        password_hash=generate_password_hash(password),
        role=role,
        active=True,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201


@app.route("/api/users/<int:user_id>", methods=["PUT"])
@require_role(ROLE_ADMIN)
def update_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    data = request.get_json(force=True)

    if "role" in data:
        if data["role"] not in ROLES:
            return jsonify({"error": "Invalid role"}), 400
        if user.id == current_user.id and data["role"] != ROLE_ADMIN:
            other_admins = User.query.filter(User.role == ROLE_ADMIN, User.id != user.id).count()
            if other_admins == 0:
                return jsonify({"error": "You can't remove the last remaining Admin"}), 400
        user.role = data["role"]

    if "active" in data:
        if user.id == current_user.id and not data["active"]:
            return jsonify({"error": "You can't deactivate your own account"}), 400
        user.active = bool(data["active"])

    if "email" in data:
        user.email = (data.get("email") or "").strip() or None

    if data.get("password"):
        if len(data["password"]) < 8:
            return jsonify({"error": "Password should be at least 8 characters"}), 400
        user.password_hash = generate_password_hash(data["password"])

    db.session.commit()
    return jsonify(user.to_dict())


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN)
def delete_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        return jsonify({"error": "You can't delete your own account"}), 400
    if user.role == ROLE_ADMIN and User.query.filter_by(role=ROLE_ADMIN).count() <= 1:
        return jsonify({"error": "You can't delete the last remaining Admin"}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

@app.route("/api/properties", methods=["GET"])
def list_properties():
    include_archived = request.args.get("include_archived") == "1"
    q = Property.query
    if not include_archived:
        q = q.filter_by(archived=False)
    props = q.order_by(Property.name).all()
    return jsonify([p.to_dict() for p in props])


@app.route("/api/properties", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_property():
    data = request.get_json(force=True)
    if not data.get("name"):
        return jsonify({"error": "Property name is required"}), 400
    p = Property(
        name=data["name"],
        address=data.get("address"),
        city=data.get("city"),
        state=data.get("state"),
        zip_code=data.get("zip_code"),
        purchase_date=data.get("purchase_date"),
        purchase_price=data.get("purchase_price") or 0,
        down_payment=data.get("down_payment") or 0,
        current_value=data.get("current_value") or 0,
        land_value=data.get("land_value") or 0,
        placed_in_service_date=data.get("placed_in_service_date"),
        monthly_rent_target=data.get("monthly_rent_target") or 0,
        mortgage_balance=data.get("mortgage_balance") or 0,
        monthly_mortgage_payment=data.get("monthly_mortgage_payment") or 0,
        units=data.get("units") or 1,
        notes=data.get("notes"),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201


@app.route("/api/properties/<int:prop_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_property(prop_id):
    p = Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    for field in ["name", "address", "city", "state", "zip_code", "purchase_date", "purchase_price",
                  "down_payment", "current_value", "land_value", "placed_in_service_date",
                  "monthly_rent_target", "mortgage_balance",
                  "monthly_mortgage_payment", "units", "notes", "archived"]:
        if field in data:
            setattr(p, field, data[field])
    db.session.commit()
    return jsonify(p.to_dict())


@app.route("/api/properties/<int:prop_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_property(prop_id):
    p = Property.query.get_or_404(prop_id)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Property bank accounts — used to prefill/suggest the Account field on
# transactions for that property, and to label things clearly when several
# properties' activity gets bundled into one statement.
# ---------------------------------------------------------------------------

@app.route("/api/properties/<int:prop_id>/accounts", methods=["GET"])
def list_property_accounts(prop_id):
    Property.query.get_or_404(prop_id)
    accounts = PropertyAccount.query.filter_by(property_id=prop_id).order_by(PropertyAccount.id).all()
    return jsonify([a.to_dict() for a in accounts])


@app.route("/api/properties/<int:prop_id>/accounts", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_property_account(prop_id):
    Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    account_type = data.get("account_type") or "checking"
    if account_type not in ACCOUNT_TYPES:
        return jsonify({"error": "account_type must be checking, savings, or other"}), 400
    acct = PropertyAccount(
        property_id=prop_id,
        account_type=account_type,
        account_number=(data.get("account_number") or "").strip() or None,
        nickname=(data.get("nickname") or "").strip() or None,
        starting_balance=data.get("starting_balance") or 0,
    )
    db.session.add(acct)
    db.session.commit()
    return jsonify(acct.to_dict()), 201


@app.route("/api/property-accounts/<int:account_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_property_account(account_id):
    acct = db.session.get(PropertyAccount, account_id) or abort(404)
    data = request.get_json(force=True)
    if "account_type" in data:
        if data["account_type"] not in ACCOUNT_TYPES:
            return jsonify({"error": "account_type must be checking, savings, or other"}), 400
        acct.account_type = data["account_type"]
    if "account_number" in data:
        acct.account_number = (data.get("account_number") or "").strip() or None
    if "nickname" in data:
        acct.nickname = (data.get("nickname") or "").strip() or None
    if "starting_balance" in data:
        acct.starting_balance = float(data.get("starting_balance") or 0)
    db.session.commit()
    return jsonify(acct.to_dict())


@app.route("/api/property-accounts/<int:account_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_property_account(account_id):
    acct = db.session.get(PropertyAccount, account_id) or abort(404)
    db.session.delete(acct)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Account reconciliation — compares this app's running balance for a bank
# account (starting_balance + everything tagged with that account's label)
# against what the bank statement actually shows as of a chosen date, and
# reports the discrepancy. Matching transactions to an account is by label
# string (the same string the Account field on transactions is filled with)
# since that's how transactions have always been tied to an account here —
# renaming an account's nickname later means older transactions keep their
# old label text and won't match going forward, same caveat as elsewhere.
# ---------------------------------------------------------------------------

def _account_computed_balance(acct, as_of=None):
    q = Transaction.query.filter(
        Transaction.property_id == acct.property_id,
        Transaction.account == acct.label(),
    )
    if as_of:
        q = q.filter(Transaction.date <= as_of)
    total = acct.starting_balance or 0
    for t in q.all():
        total += t.amount if t.type == "income" else -t.amount
    return total


@app.route("/api/property-accounts/<int:account_id>/balance", methods=["GET"])
def get_account_balance(account_id):
    acct = db.session.get(PropertyAccount, account_id) or abort(404)
    as_of = request.args.get("as_of")
    balance = _account_computed_balance(acct, as_of)
    return jsonify({
        "account_id": acct.id, "as_of": as_of, "starting_balance": acct.starting_balance,
        "computed_balance": round(balance, 2),
    })


@app.route("/api/property-accounts/<int:account_id>/reconcile", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def reconcile_account(account_id):
    acct = db.session.get(PropertyAccount, account_id) or abort(404)
    data = request.get_json(force=True)
    reconcile_date = data.get("reconcile_date") or date.today().isoformat()
    try:
        statement_balance = float(data.get("statement_balance"))
    except (TypeError, ValueError):
        return jsonify({"error": "statement_balance is required and must be a number"}), 400

    computed = _account_computed_balance(acct, as_of=reconcile_date)
    discrepancy = round(statement_balance - computed, 2)

    rec = AccountReconciliation(
        property_account_id=acct.id, reconcile_date=reconcile_date,
        statement_balance=statement_balance, computed_balance=round(computed, 2),
        discrepancy=discrepancy,
    )
    db.session.add(rec)
    db.session.commit()
    return jsonify(rec.to_dict()), 201


@app.route("/api/property-accounts/<int:account_id>/reconciliations", methods=["GET"])
def list_account_reconciliations(account_id):
    db.session.get(PropertyAccount, account_id) or abort(404)
    recs = (AccountReconciliation.query.filter_by(property_account_id=account_id)
            .order_by(AccountReconciliation.reconcile_date.desc(), AccountReconciliation.id.desc()).all())
    return jsonify([r.to_dict() for r in recs])


@app.route("/api/reconciliations/<int:rec_id>/create-adjustment", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_reconciliation_adjustment(rec_id):
    rec = db.session.get(AccountReconciliation, rec_id) or abort(404)
    if rec.adjustment_transaction_id:
        return jsonify({"error": "An adjustment transaction was already created for this reconciliation"}), 400
    if abs(rec.discrepancy) < 0.005:
        return jsonify({"error": "No discrepancy to adjust"}), 400

    acct = db.session.get(PropertyAccount, rec.property_account_id) or abort(404)
    txn = Transaction(
        property_id=acct.property_id,
        category_id=None,  # left uncategorized on purpose — see is_adjustment handling in tax_report.py
        date=rec.reconcile_date,
        type="income" if rec.discrepancy > 0 else "expense",
        payee="Balance Adjustment",
        amount=abs(rec.discrepancy),
        account=acct.label(),
        notes=f"Reconciliation adjustment — bank statement ${rec.statement_balance:,.2f} vs "
              f"app-computed ${rec.computed_balance:,.2f} as of {rec.reconcile_date}.",
        source="manual",
        is_adjustment=True,
    )
    db.session.add(txn)
    db.session.flush()
    rec.adjustment_transaction_id = txn.id
    db.session.commit()
    return jsonify({"reconciliation": rec.to_dict(), "transaction": _txn_dict_with_property(txn)}), 201


# ---------------------------------------------------------------------------
# Property units — named units within a multi-unit property (e.g. "Unit A",
# "2B"), optionally tagged on transactions. Distinct from Property.units,
# which is just a headline count used in cap-rate math.
# ---------------------------------------------------------------------------

@app.route("/api/properties/<int:prop_id>/units", methods=["GET"])
def list_property_units(prop_id):
    prop = Property.query.get_or_404(prop_id)
    existing = PropertyUnit.query.filter_by(property_id=prop_id).order_by(PropertyUnit.id).all()

    # Lazy auto-seed: the first time anyone asks for this property's units,
    # if it declares more than one unit but has no named-unit records yet,
    # generate "Unit 1".."Unit N" so there's zero required setup — every one
    # of these is a normal row afterward and can be freely renamed/removed.
    if not existing and prop.units and prop.units > 1:
        for i in range(1, prop.units + 1):
            db.session.add(PropertyUnit(property_id=prop_id, name=f"Unit {i}"))
        db.session.commit()
        existing = PropertyUnit.query.filter_by(property_id=prop_id).order_by(PropertyUnit.id).all()

    return jsonify([u.to_dict() for u in existing])


@app.route("/api/properties/<int:prop_id>/units", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_property_unit(prop_id):
    Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Unit name is required"}), 400
    unit = PropertyUnit(property_id=prop_id, name=name)
    db.session.add(unit)
    db.session.commit()
    return jsonify(unit.to_dict()), 201


@app.route("/api/property-units/<int:unit_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_property_unit(unit_id):
    unit = db.session.get(PropertyUnit, unit_id) or abort(404)
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Unit name is required"}), 400
    unit.name = name
    db.session.commit()
    return jsonify(unit.to_dict())


@app.route("/api/property-units/<int:unit_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_property_unit(unit_id):
    unit = db.session.get(PropertyUnit, unit_id) or abort(404)
    # Untag any transactions pointing at this unit first — they keep their
    # data, they just show no unit afterward (matches what the UI tells the
    # user before they confirm the delete). Without this, Postgres would
    # reject the delete outright with a foreign key violation.
    Transaction.query.filter_by(unit_id=unit_id).update({"unit_id": None})
    db.session.delete(unit)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/properties/<int:prop_id>/units/copy-from", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def copy_property_units(prop_id):
    """Replaces this property's unit list with a copy of another property's
    unit names — for a long, already-typed-out unit list (a 20-unit
    building, say) that a similar property also needs, instead of retyping
    it. Existing units on THIS property are removed first (any transactions
    tagged to them are untagged, not deleted, same as a normal unit delete)."""
    Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    source_id = data.get("source_property_id")
    if not source_id:
        return jsonify({"error": "source_property_id is required"}), 400
    source = Property.query.get_or_404(int(source_id))
    if source.id == prop_id:
        return jsonify({"error": "Choose a different property to copy from"}), 400

    source_units = PropertyUnit.query.filter_by(property_id=source.id).order_by(PropertyUnit.id).all()
    if not source_units:
        return jsonify({"error": f"{source.name} doesn't have any units to copy"}), 400

    existing = PropertyUnit.query.filter_by(property_id=prop_id).all()
    for u in existing:
        Transaction.query.filter_by(unit_id=u.id).update({"unit_id": None})
        db.session.delete(u)

    for u in source_units:
        db.session.add(PropertyUnit(property_id=prop_id, name=u.name))
    db.session.commit()

    new_units = PropertyUnit.query.filter_by(property_id=prop_id).order_by(PropertyUnit.id).all()
    return jsonify([u.to_dict() for u in new_units])


# ---------------------------------------------------------------------------
# Tenants — basic tenant/lease info per property (not a full tenant portal:
# no payments/messaging/screening, just who's where and when a lease is up).
# ---------------------------------------------------------------------------

@app.route("/api/tenants", methods=["GET"])
def list_all_tenants():
    """Backs the top-level Tenants tab, which (like Recurring) respects the
    property switcher including "All Properties" — the per-property route
    below only ever covers one property, which isn't enough for that view."""
    property_id = request.args.get("property_id")
    prop_by_id = {p.id: p for p in Property.query.all()}
    q = Tenant.query
    if property_id and str(property_id).lower() != "all":
        q = q.filter_by(property_id=int(property_id))
    tenants = q.order_by(Tenant.id).all()
    result = []
    for t in tenants:
        d = t.to_dict()
        d["property_name"] = prop_by_id[t.property_id].name if t.property_id in prop_by_id else ""
        result.append(d)
    return jsonify(result)


@app.route("/api/properties/<int:prop_id>/tenants", methods=["GET"])
def list_tenants(prop_id):
    Property.query.get_or_404(prop_id)
    tenants = Tenant.query.filter_by(property_id=prop_id).order_by(Tenant.id).all()
    return jsonify([t.to_dict() for t in tenants])


@app.route("/api/properties/<int:prop_id>/tenants", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_tenant(prop_id):
    Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Tenant name is required"}), 400
    t = Tenant(
        property_id=prop_id,
        unit_id=data.get("unit_id") or None,
        name=name,
        email=data.get("email"),
        phone=data.get("phone"),
        lease_start=data.get("lease_start"),
        lease_end=data.get("lease_end"),
        monthly_rent=data.get("monthly_rent") or 0,
        security_deposit=data.get("security_deposit") or 0,
        active=data.get("active", True),
        notes=data.get("notes"),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@app.route("/api/tenants/<int:tenant_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_tenant(tenant_id):
    t = db.session.get(Tenant, tenant_id) or abort(404)
    data = request.get_json(force=True)
    for field in ["unit_id", "email", "phone", "lease_start", "lease_end",
                  "monthly_rent", "security_deposit", "active", "notes"]:
        if field in data:
            setattr(t, field, data[field])
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Tenant name is required"}), 400
        t.name = name
    db.session.commit()
    return jsonify(t.to_dict())


@app.route("/api/tenants/<int:tenant_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_tenant(tenant_id):
    t = db.session.get(Tenant, tenant_id) or abort(404)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@app.route("/api/categories", methods=["GET"])
def list_categories():
    cats = Category.query.order_by(Category.type, Category.name).all()
    return jsonify([c.to_dict() for c in cats])


@app.route("/api/categories", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_category():
    data = request.get_json(force=True)
    if not data.get("name") or data.get("type") not in ("income", "expense"):
        return jsonify({"error": "name and type ('income'|'expense') required"}), 400
    c = Category(name=data["name"], type=data["type"], is_default=False)
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201


VALID_SCHEDULE_E_LINES = {key for key, _, _ in SCHEDULE_E_LINES} | {
    SCHEDULE_E_NOT_DEDUCTIBLE, SCHEDULE_E_CAPITALIZE,
}


@app.route("/api/categories/<int:cat_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_category(cat_id):
    c = Category.query.get_or_404(cat_id)
    data = request.get_json(force=True)
    if "name" in data:
        if not data["name"]:
            return jsonify({"error": "Category name is required"}), 400
        c.name = data["name"]
    if "schedule_e_line" in data:
        val = data["schedule_e_line"] or None
        if val is not None and val not in VALID_SCHEDULE_E_LINES:
            return jsonify({"error": f"Unknown schedule_e_line '{val}'"}), 400
        c.schedule_e_line = val
    db.session.commit()
    return jsonify(c.to_dict())


@app.route("/api/categories/<int:cat_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_category(cat_id):
    c = Category.query.get_or_404(cat_id)
    db.session.delete(c)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/categories/restore-defaults", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def restore_default_categories_route():
    added = restore_default_categories()
    return jsonify({"added": added, "count": len(added)})


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@app.route("/api/transactions", methods=["GET"])
def list_transactions():
    # Lazily catch up any recurring-transaction occurrences due through
    # today before listing, the same "generate on demand" pattern used
    # elsewhere (e.g. a property's default units) — see _generate_due_recurring.
    _generate_due_recurring(request.args.get("property_id"))
    txns = _query_transactions(
        property_id=request.args.get("property_id"),
        start=request.args.get("start"),
        end=request.args.get("end"),
        type_=request.args.get("type"),
        category_id=request.args.get("category_id"),
        search=request.args.get("search"),
        unit_id=request.args.get("unit_id"),
    )
    return jsonify([_txn_dict_with_property(t) for t in txns])


@app.route("/api/transactions", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_transaction():
    data = request.get_json(force=True)
    required = ["property_id", "date", "type"]
    for f in required:
        if data.get(f) in (None, ""):
            return jsonify({"error": f"'{f}' is required"}), 400
    if data["type"] not in ("income", "expense"):
        return jsonify({"error": "type must be 'income' or 'expense'"}), 400

    # "Distance" mode on the Auto/Travel category: the amount is computed
    # from miles x the current standard mileage rate rather than typed in
    # directly, and this transaction is linked to a MileageLog trip so it
    # also shows up on the Mileage tab. Entering a plain dollar Amount
    # instead (mileage_miles absent) just makes this an ordinary travel
    # expense, per the Amount-vs-Distance choice in the transaction form.
    mileage_miles = data.get("mileage_miles")
    rate_used = None
    if mileage_miles not in (None, ""):
        try:
            mileage_miles = float(mileage_miles)
        except (TypeError, ValueError):
            return jsonify({"error": "mileage_miles must be a number"}), 400
        if mileage_miles <= 0:
            return jsonify({"error": "mileage_miles must be greater than 0"}), 400
        rate_used = float(_settings_map().get("standard_mileage_rate") or 0)
        amount = round(mileage_miles * rate_used, 2)
    else:
        if data.get("amount") in (None, ""):
            return jsonify({"error": "'amount' is required"}), 400
        amount = abs(float(data["amount"]))

    t = Transaction(
        property_id=int(data["property_id"]),
        category_id=data.get("category_id") or None,
        unit_id=data.get("unit_id") or None,
        date=data["date"],
        type=data["type"],
        payee=data.get("payee"),
        amount=amount,
        account=data.get("account"),
        notes=data.get("notes"),
        source="mileage" if mileage_miles else "manual",
        created_by=_current_username(),
    )
    db.session.add(t)
    db.session.flush()  # assigns t.id, without needing a second round trip

    if mileage_miles:
        db.session.add(MileageLog(
            property_id=t.property_id, unit_id=t.unit_id, date=t.date, purpose=t.payee or "Mileage",
            miles=mileage_miles, rate_used=rate_used, transaction_id=t.id,
        ))

    db.session.commit()
    return jsonify(_txn_dict_with_property(t)), 201


@app.route("/api/transactions/<int:txn_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_transaction(txn_id):
    t = Transaction.query.get_or_404(txn_id)
    data = request.get_json(force=True)
    for field in ["property_id", "category_id", "unit_id", "date", "type", "payee", "account", "notes"]:
        if field in data:
            setattr(t, field, data[field])

    existing_log = MileageLog.query.filter_by(transaction_id=t.id).first()
    mileage_miles = data.get("mileage_miles")

    if mileage_miles not in (None, ""):
        try:
            mileage_miles = float(mileage_miles)
        except (TypeError, ValueError):
            return jsonify({"error": "mileage_miles must be a number"}), 400
        if mileage_miles <= 0:
            return jsonify({"error": "mileage_miles must be greater than 0"}), 400
        if existing_log:
            rate_used = existing_log.rate_used
            existing_log.miles = mileage_miles
            existing_log.date = t.date
            existing_log.purpose = t.payee or "Mileage"
            existing_log.unit_id = t.unit_id
        else:
            rate_used = float(_settings_map().get("standard_mileage_rate") or 0)
            db.session.add(MileageLog(
                property_id=t.property_id, unit_id=t.unit_id, date=t.date, purpose=t.payee or "Mileage",
                miles=mileage_miles, rate_used=rate_used, transaction_id=t.id,
            ))
        t.amount = round(mileage_miles * rate_used, 2)
        t.source = "mileage"
    elif "amount" in data:
        # An explicit dollar Amount was submitted — this is no longer
        # tracked as mileage even if it was before ("if you enter an amount
        # it will just assume travel, not mileage").
        t.amount = abs(float(data["amount"]))
        if existing_log:
            db.session.delete(existing_log)
        if t.source == "mileage":
            t.source = "manual"

    db.session.commit()
    return jsonify(_txn_dict_with_property(t))


@app.route("/api/transactions/<int:txn_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_transaction(txn_id):
    t = Transaction.query.get_or_404(txn_id)
    _delete_receipt_file(t.receipt_filename)
    # A mileage-based transaction and its MileageLog trip were created
    # together as one unit (see create_transaction/update_transaction) — if
    # the transaction goes, the trip it represents should go with it rather
    # than being left behind as an orphaned, un-linked entry that would then
    # silently start counting again in the tax report's mileage deduction.
    linked_log = MileageLog.query.filter_by(transaction_id=t.id).first()
    if linked_log:
        db.session.delete(linked_log)
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Recurring transactions — a template for something that repeats on a
# schedule (rent income, a fixed mortgage/insurance payment) so it doesn't
# have to be re-entered by hand every period. Occurrences are generated
# lazily: whenever the dashboard, transactions list, or the recurring rules
# themselves are looked at, any occurrence due through today gets turned
# into a real Transaction row and next_due_date advances — the same
# "generate on demand" pattern already used for auto-seeding a property's
# default units, so no background scheduler process is needed.
# ---------------------------------------------------------------------------

def _advance_recurring_date(d, frequency):
    if frequency == "weekly":
        return d + timedelta(days=7)
    if frequency == "yearly":
        return d + relativedelta(years=1)
    return d + relativedelta(months=1)  # monthly is the default/fallback


def _generate_due_recurring(property_id=None):
    """Creates a Transaction for every occurrence due through today for
    each active recurring rule (optionally scoped to one property), then
    commits. Returns how many transactions were generated."""
    today_iso = date.today().isoformat()
    q = RecurringTransaction.query.filter_by(active=True)
    if property_id and str(property_id).lower() != "all":
        q = q.filter_by(property_id=int(property_id))
    generated = 0
    changed = False
    for r in q.all():
        guard = 0
        while r.next_due_date <= today_iso and guard < 500:
            if r.end_date and r.next_due_date > r.end_date:
                break
            guard += 1
            db.session.add(Transaction(
                property_id=r.property_id,
                category_id=r.category_id,
                unit_id=r.unit_id,
                date=r.next_due_date,
                type=r.type,
                payee=r.payee,
                amount=r.amount,
                account=r.account,
                notes=r.notes,
                source="recurring",
                created_by=_current_username(),
            ))
            generated += 1
            changed = True
            r.next_due_date = _advance_recurring_date(
                datetime.fromisoformat(r.next_due_date).date(), r.frequency
            ).isoformat()
        if r.end_date and r.next_due_date > r.end_date and r.active:
            r.active = False
            changed = True
    if changed:
        db.session.commit()
    return generated


@app.route("/api/recurring", methods=["GET"])
def list_recurring():
    property_id = request.args.get("property_id")
    _generate_due_recurring(property_id)
    q = RecurringTransaction.query
    if property_id and str(property_id).lower() != "all":
        q = q.filter_by(property_id=int(property_id))
    rules = q.order_by(RecurringTransaction.next_due_date).all()
    return jsonify([r.to_dict() for r in rules])


@app.route("/api/recurring", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_recurring():
    data = request.get_json(force=True)
    if not data.get("property_id"):
        return jsonify({"error": "property_id is required"}), 400
    if not data.get("amount"):
        return jsonify({"error": "amount is required"}), 400
    if not data.get("start_date"):
        return jsonify({"error": "start_date is required"}), 400
    frequency = data.get("frequency") or "monthly"
    if frequency not in RECURRING_FREQUENCIES:
        return jsonify({"error": f"frequency must be one of {RECURRING_FREQUENCIES}"}), 400
    r = RecurringTransaction(
        property_id=data["property_id"],
        category_id=data.get("category_id") or None,
        unit_id=data.get("unit_id") or None,
        type=data.get("type") or "expense",
        payee=data.get("payee"),
        amount=abs(float(data["amount"])),
        account=data.get("account"),
        notes=data.get("notes"),
        frequency=frequency,
        start_date=data["start_date"],
        end_date=data.get("end_date") or None,
        next_due_date=data["start_date"],
        active=True,
    )
    db.session.add(r)
    db.session.commit()
    return jsonify(r.to_dict()), 201


@app.route("/api/recurring/<int:rec_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_recurring(rec_id):
    r = db.session.get(RecurringTransaction, rec_id) or abort(404)
    data = request.get_json(force=True)
    if "frequency" in data and data["frequency"] not in RECURRING_FREQUENCIES:
        return jsonify({"error": f"frequency must be one of {RECURRING_FREQUENCIES}"}), 400
    # If nothing has been generated yet, editing start_date should move
    # next_due_date along with it rather than leaving it pointing at a date
    # that no longer matches what was just typed.
    if "start_date" in data and r.next_due_date == r.start_date:
        r.next_due_date = data["start_date"]
    for field in ["category_id", "unit_id", "type", "payee", "account", "notes",
                  "frequency", "start_date", "end_date", "active"]:
        if field in data:
            setattr(r, field, data[field])
    if "amount" in data:
        r.amount = abs(float(data["amount"]))
    db.session.commit()
    return jsonify(r.to_dict())


@app.route("/api/recurring/<int:rec_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_recurring(rec_id):
    r = db.session.get(RecurringTransaction, rec_id) or abort(404)
    db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/recurring/generate", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def generate_recurring_now():
    data = request.get_json(silent=True) or {}
    generated = _generate_due_recurring(data.get("property_id"))
    return jsonify({"generated": generated})


# ---------------------------------------------------------------------------
# Transaction receipts — an optional photo/scan of a receipt attached to a
# transaction, so it's on hand if the IRS or a CPA ever asks for backup.
# Stored as plain files under DATA_DIR/receipts/ (not in the database, same
# reasoning as the business logo) with just the filename recorded on the
# transaction row.
# ---------------------------------------------------------------------------

ALLOWED_RECEIPT_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "pdf"}
RECEIPT_MIME_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "pdf": "application/pdf",
}


def _receipts_dir():
    path = os.path.join(DATA_DIR, "receipts")
    os.makedirs(path, exist_ok=True)
    return path


def _receipt_path(filename):
    return os.path.join(_receipts_dir(), filename)


def _delete_receipt_file(filename):
    if not filename:
        return
    path = _receipt_path(filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _zip_write_receipts(zf):
    """Bundles every attached receipt file into a full backup zip under a
    receipts/ prefix — without this, restoring a backup would leave
    transactions pointing at receipt_filename values that don't exist
    anywhere, since only the DB rows (not the files) live in the database."""
    receipts_dir = _receipts_dir()
    for fname in os.listdir(receipts_dir):
        path = os.path.join(receipts_dir, fname)
        if os.path.isfile(path):
            zf.write(path, f"receipts/{fname}")


def _zip_restore_receipts(zf):
    """Replaces the receipts/ folder wholesale with whatever's in the
    backup zip (clearing anything not present in it), mirroring how the
    logo files are fully replaced/removed on restore rather than merged."""
    receipts_dir = _receipts_dir()
    for fname in os.listdir(receipts_dir):
        path = os.path.join(receipts_dir, fname)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    for name in zf.namelist():
        if name.startswith("receipts/") and not name.endswith("/"):
            dest = os.path.join(receipts_dir, os.path.basename(name))
            with open(dest, "wb") as f:
                f.write(zf.read(name))


@app.route("/api/transactions/<int:txn_id>/receipt", methods=["GET"])
def get_transaction_receipt(txn_id):
    t = Transaction.query.get_or_404(txn_id)
    if not t.receipt_filename:
        abort(404)
    path = _receipt_path(t.receipt_filename)
    if not os.path.exists(path):
        abort(404)
    ext = t.receipt_filename.rsplit(".", 1)[-1].lower()
    mimetype = RECEIPT_MIME_TYPES.get(ext, "application/octet-stream")
    # Inline (not as_attachment) so it opens/previews right in the browser —
    # from there, the user's own browser Print (Ctrl+P) handles printing it,
    # no custom print layout needed for an arbitrary photo or PDF.
    return send_file(path, mimetype=mimetype, as_attachment=False)


@app.route("/api/transactions/<int:txn_id>/receipt", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def upload_transaction_receipt(txn_id):
    t = Transaction.query.get_or_404(txn_id)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        return jsonify({"error": "Unsupported file type. Use a PNG, JPG, GIF, BMP, or PDF."}), 400

    _delete_receipt_file(t.receipt_filename)  # replace, don't accumulate, if one was already attached
    stored_name = f"txn_{txn_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file.save(_receipt_path(stored_name))
    t.receipt_filename = stored_name
    db.session.commit()
    return jsonify(_txn_dict_with_property(t))


@app.route("/api/transactions/<int:txn_id>/receipt", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_transaction_receipt(txn_id):
    t = Transaction.query.get_or_404(txn_id)
    _delete_receipt_file(t.receipt_filename)
    t.receipt_filename = None
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Property documents — a general file attached to a property (lease,
# insurance policy, inspection report, etc.), not tied to any one
# transaction. Same on-disk storage pattern as receipts, under a separate
# DATA_DIR/documents/ folder.
# ---------------------------------------------------------------------------

ALLOWED_DOCUMENT_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "pdf", "doc", "docx", "txt"}
DOCUMENT_MIME_TYPES = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "bmp": "image/bmp", "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}


def _documents_dir():
    path = os.path.join(DATA_DIR, "documents")
    os.makedirs(path, exist_ok=True)
    return path


def _document_path(filename):
    return os.path.join(_documents_dir(), filename)


def _delete_document_file(filename):
    if not filename:
        return
    path = _document_path(filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _zip_write_documents(zf):
    docs_dir = _documents_dir()
    for fname in os.listdir(docs_dir):
        path = os.path.join(docs_dir, fname)
        if os.path.isfile(path):
            zf.write(path, f"documents/{fname}")


def _zip_restore_documents(zf):
    docs_dir = _documents_dir()
    for fname in os.listdir(docs_dir):
        path = os.path.join(docs_dir, fname)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    for name in zf.namelist():
        if name.startswith("documents/") and not name.endswith("/"):
            dest = os.path.join(docs_dir, os.path.basename(name))
            with open(dest, "wb") as f:
                f.write(zf.read(name))


@app.route("/api/documents", methods=["GET"])
def list_all_documents():
    """Backs the top-level Documents tab — same "All Properties or one"
    behavior as list_all_tenants/list_all_mileage above."""
    property_id = request.args.get("property_id")
    prop_by_id = {p.id: p for p in Property.query.all()}
    q = PropertyDocument.query
    if property_id and str(property_id).lower() != "all":
        q = q.filter_by(property_id=int(property_id))
    docs = q.order_by(PropertyDocument.uploaded_at.desc()).all()
    result = []
    for d in docs:
        item = d.to_dict()
        item["property_name"] = prop_by_id[d.property_id].name if d.property_id in prop_by_id else ""
        result.append(item)
    return jsonify(result)


@app.route("/api/properties/<int:prop_id>/documents", methods=["GET"])
def list_property_documents(prop_id):
    Property.query.get_or_404(prop_id)
    docs = PropertyDocument.query.filter_by(property_id=prop_id).order_by(
        PropertyDocument.uploaded_at.desc()
    ).all()
    return jsonify([d.to_dict() for d in docs])


@app.route("/api/properties/<int:prop_id>/documents", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def upload_property_document(prop_id):
    Property.query.get_or_404(prop_id)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        return jsonify({"error": "Unsupported file type. Use PNG, JPG, GIF, BMP, PDF, DOC, DOCX, or TXT."}), 400
    doc_type = request.form.get("doc_type") or "other"
    if doc_type not in DOCUMENT_TYPES:
        doc_type = "other"

    stored_name = f"prop_{prop_id}_{uuid.uuid4().hex[:8]}.{ext}"
    file.save(_document_path(stored_name))
    doc = PropertyDocument(
        property_id=prop_id,
        filename=stored_name,
        original_filename=file.filename,
        doc_type=doc_type,
        expiration_date=request.form.get("expiration_date") or None,
        notes=request.form.get("notes") or None,
    )
    db.session.add(doc)
    db.session.commit()
    return jsonify(doc.to_dict()), 201


@app.route("/api/documents/<int:doc_id>", methods=["GET"])
def get_property_document(doc_id):
    d = db.session.get(PropertyDocument, doc_id) or abort(404)
    path = _document_path(d.filename)
    if not os.path.exists(path):
        abort(404)
    ext = d.filename.rsplit(".", 1)[-1].lower()
    mimetype = DOCUMENT_MIME_TYPES.get(ext, "application/octet-stream")
    return send_file(path, mimetype=mimetype, as_attachment=False,
                      download_name=d.original_filename or d.filename)


@app.route("/api/documents/<int:doc_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_property_document(doc_id):
    d = db.session.get(PropertyDocument, doc_id) or abort(404)
    data = request.get_json(force=True)
    if "doc_type" in data and data["doc_type"] in DOCUMENT_TYPES:
        d.doc_type = data["doc_type"]
    if "expiration_date" in data:
        d.expiration_date = data["expiration_date"] or None
    if "notes" in data:
        d.notes = data["notes"]
    db.session.commit()
    return jsonify(d.to_dict())


@app.route("/api/documents/<int:doc_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_property_document(doc_id):
    d = db.session.get(PropertyDocument, doc_id) or abort(404)
    _delete_document_file(d.filename)
    db.session.delete(d)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Import auto-categorization rules — "if payee/description contains X,
# suggest category Y" for future imports. Only ever a suggestion applied
# during import_commit() when nothing else already resolved a category for
# that row (an explicitly mapped Category column always wins); never
# touches transactions entered by hand.
# ---------------------------------------------------------------------------

@app.route("/api/import-rules", methods=["GET"])
def list_import_rules():
    rules = ImportRule.query.order_by(ImportRule.id).all()
    return jsonify([r.to_dict() for r in rules])


@app.route("/api/import-rules", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_import_rule():
    data = request.get_json(force=True)
    match_text = (data.get("match_text") or "").strip()
    if not match_text:
        return jsonify({"error": "match_text is required"}), 400
    if not data.get("category_id"):
        return jsonify({"error": "category_id is required"}), 400
    r = ImportRule(match_text=match_text, category_id=data["category_id"])
    db.session.add(r)
    db.session.commit()
    return jsonify(r.to_dict()), 201


@app.route("/api/import-rules/<int:rule_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_import_rule(rule_id):
    r = db.session.get(ImportRule, rule_id) or abort(404)
    data = request.get_json(force=True)
    if "match_text" in data:
        match_text = (data.get("match_text") or "").strip()
        if not match_text:
            return jsonify({"error": "match_text is required"}), 400
        r.match_text = match_text
    if "category_id" in data:
        r.category_id = data["category_id"]
    db.session.commit()
    return jsonify(r.to_dict())


@app.route("/api/import-rules/<int:rule_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_import_rule(rule_id):
    r = db.session.get(ImportRule, rule_id) or abort(404)
    db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True})


def _apply_import_rules(text, rules_cache):
    """Returns a category_id if `text` (payee/description, any case) contains
    any rule's match_text, else None. `rules_cache` is the full ImportRule
    list fetched once per import rather than re-queried per row."""
    if not text:
        return None
    lower = text.lower()
    for r in rules_cache:
        if r.match_text.lower() in lower:
            return r.category_id
    return None


# ---------------------------------------------------------------------------
# Import (bank / CSV statement)
# ---------------------------------------------------------------------------

IMPORT_PREVIEW_LIMIT = 300  # rows shown/selectable in the UI; beyond this, all rows still import
IMPORT_SESSION_TTL = timedelta(hours=2)  # abandoned manual uploads get cleaned up after this long


def _prune_stale_import_sessions():
    # Only prunes "manual" sessions (an upload someone started and never
    # finished) — an email-sourced pending-review session (source="email")
    # is meant to sit and wait on the Dashboard "needs review" alert until
    # someone actually gets to it, which could be days, so it's deliberately
    # exempt from this time-based cleanup. It only ever goes away by being
    # committed or explicitly dismissed.
    cutoff = datetime.utcnow() - IMPORT_SESSION_TTL
    ImportSession.query.filter(
        ImportSession.created_at < cutoff, ImportSession.source != "email"
    ).delete(synchronize_session=False)
    db.session.commit()


@app.route("/api/import/ocr-status", methods=["GET"])
def import_ocr_status():
    """Lets Settings/Import show whether scanned-PDF OCR is actually
    working right now, and whether it's using the tesseract-bin\\ bundle
    or a separate system-wide Tesseract install — see get_ocr_status() in
    importer.py for what it actually checks."""
    return jsonify(get_ocr_status())


@app.route("/api/import/preview", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def import_preview():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in ("csv", "xlsx", "xlsm", "pdf", "iif", "qif"):
        return jsonify({"error": "Unsupported file type. Use a .csv, .xlsx, .pdf, .iif, or .qif file."}), 400

    raw = file.read()
    try:
        result = sniff_file(file.filename, raw)
    except Exception as exc:  # noqa: BLE001 - surface a friendly message instead of a 500
        return jsonify({"error": f"Could not read that file: {exc}"}), 400

    if not result["headers"]:
        warnings = result.get("warnings", [])
        message = " ".join(warnings) if warnings else "Could not find any usable data in that file."
        return jsonify({"error": message, "warnings": warnings}), 400

    token = uuid.uuid4().hex

    _prune_stale_import_sessions()
    db.session.add(ImportSession(
        token=token, rows_json=json.dumps(result["rows"]),
        headers_json=json.dumps(result["headers"]), guess_json=json.dumps(result["guess"]),
    ))
    db.session.commit()

    return jsonify({
        "token": token,
        "headers": result["headers"],
        "preview_rows": result["rows"][:IMPORT_PREVIEW_LIMIT],
        "preview_limit": IMPORT_PREVIEW_LIMIT,
        "row_count": result.get("row_count", len(result["rows"])),
        "guess": result["guess"],
        "warnings": result.get("warnings", []),
    })


def _commit_parsed_rows(parsed, property_id, default_income_cat=None, default_expense_cat=None,
                         account_label="Imported", import_batch_id=None, created_by=None):
    """Shared commit logic behind both the manual Import tab
    (POST /api/import/commit) and the Outlook email auto-import monitor
    (email_monitor.py). Takes rows already run through build_transactions()
    and inserts Transaction rows for a single property, de-duping via
    import_hash, resolving Property/Category name-column overrides (how a
    bundled multi-sub-account statement routes different rows to different
    properties/categories), applying ImportRule category guesses, and
    finally auto-confirming an AccountReconciliation for every (property,
    account label) touched, dated to the latest row seen for it.

    `import_batch_id`, when given, tags every inserted Transaction so an
    auto-import batch can later be identified and undone as a unit — see
    EmailImportBatch. Manual imports leave this as None.

    Does NOT commit the session — the caller commits (together with
    whatever else needs to happen in the same transaction, e.g. deleting
    the ImportSession row)."""
    property_id = int(property_id)

    # Case-insensitive lookup tables for resolving mapped Property/Category
    # name columns against what's already in the database.
    property_by_name = {p.name.strip().lower(): p.id for p in Property.query.all()}
    categories = Category.query.all()
    category_by_name = {(c.name.strip().lower(), c.type): c.id for c in categories}

    # Deduping is scoped per-property (loaded lazily, one query per property
    # actually touched) rather than one flat set for the whole import. A
    # bundled multi-property statement/ledger — which is exactly what a
    # QuickBooks/Quicken migration file usually is — routes different rows
    # to different resolved properties via the Property-name override below,
    # so checking every row against only the caller's single default
    # property_id would miss real duplicates on rows that resolved to a
    # different property, letting a re-import silently double them up.
    # Keeping it per-property (rather than one global set) still protects
    # against two different properties coincidentally having a genuinely
    # distinct transaction that happens to hash the same.
    _hash_cache = {}

    def _hashes_for(pid):
        if pid not in _hash_cache:
            _hash_cache[pid] = {
                h for (h,) in db.session.query(Transaction.import_hash)
                .filter(Transaction.property_id == pid, Transaction.import_hash.isnot(None))
                .all()
            }
        return _hash_cache[pid]

    import_rules = ImportRule.query.all()

    inserted = 0
    skipped_dupes = 0
    property_unmatched = 0
    category_unmatched = 0
    rule_applied = 0

    # A bank statement is authoritative up through a specific date — even a
    # row that turns out to be a duplicate still confirms the account's
    # activity is accurate up to that row's date, so this tracks the latest
    # row date per (property, account label) across every row in the
    # import, not just newly-inserted ones. After the loop, each account
    # touched gets an implicit reconciliation dated to that latest date,
    # which resets the "hasn't been reconciled in N days" dashboard alert
    # without requiring a separate manual Reconcile pass on data that was
    # just confirmed against the bank's own export.
    account_max_date = {}

    for row in parsed:
        resolved_property_id = property_id
        property_matched = True
        if "property_name" in row and row["property_name"]:
            match = property_by_name.get(row["property_name"].strip().lower())
            if match:
                resolved_property_id = match
            else:
                property_matched = False

        is_dupe = row["import_hash"] in _hashes_for(resolved_property_id)
        if not property_matched and not is_dupe:
            property_unmatched += 1

        resolved_account = row.get("account") or account_label
        date_key = (resolved_property_id, resolved_account)
        if row["date"] > account_max_date.get(date_key, ""):
            account_max_date[date_key] = row["date"]

        if is_dupe:
            skipped_dupes += 1
            continue

        default_cat = default_income_cat if row["type"] == "income" else default_expense_cat
        resolved_category_id = default_cat or None
        if "category_name" in row and row["category_name"]:
            match = category_by_name.get((row["category_name"].strip().lower(), row["type"]))
            if match:
                resolved_category_id = match
            else:
                category_unmatched += 1

        # An import rule only ever fills in a category that's still empty —
        # an explicitly mapped Category column (above) always takes
        # priority, so a rule can never override what the statement/mapping
        # itself already said.
        if not resolved_category_id and import_rules:
            rule_match = _apply_import_rules(row.get("payee"), import_rules)
            if rule_match:
                resolved_category_id = rule_match
                rule_applied += 1

        resolved_notes = row.get("notes") or None

        t = Transaction(
            property_id=resolved_property_id,
            category_id=resolved_category_id,
            date=row["date"],
            type=row["type"],
            payee=row["payee"],
            amount=row["amount"],
            account=resolved_account,
            notes=resolved_notes,
            source="import",
            import_hash=row["import_hash"],
            import_batch_id=import_batch_id,
            created_by=created_by,
        )
        db.session.add(t)
        _hashes_for(resolved_property_id).add(row["import_hash"])
        inserted += 1

    reconciled_accounts = 0
    accounts_by_property = {}
    for (pid, acct_label), max_date in account_max_date.items():
        if pid not in accounts_by_property:
            accounts_by_property[pid] = PropertyAccount.query.filter_by(property_id=pid).all()
        acct = next((a for a in accounts_by_property[pid] if a.label() == acct_label), None)
        if not acct:
            continue
        computed = round(_account_computed_balance(acct, as_of=max_date), 2)
        db.session.add(AccountReconciliation(
            property_account_id=acct.id, reconcile_date=max_date,
            statement_balance=computed, computed_balance=computed, discrepancy=0.0,
            import_batch_id=import_batch_id,
        ))
        reconciled_accounts += 1

    return {
        "inserted": inserted,
        "skipped_duplicates": skipped_dupes,
        "property_unmatched": property_unmatched,
        "category_unmatched": category_unmatched,
        "total_parsed": len(parsed),
        "rule_applied": rule_applied,
        "accounts_reconciled": reconciled_accounts,
    }


@app.route("/api/import/commit", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def import_commit():
    data = request.get_json(force=True)
    token = data.get("token")
    mapping = data.get("mapping", {})
    property_id = data.get("property_id")
    default_income_cat = data.get("default_income_category_id")
    default_expense_cat = data.get("default_expense_category_id")
    account_label = data.get("account_label") or "Imported"
    excluded_indices = set(data.get("excluded_row_indices") or [])

    session_row = db.session.get(ImportSession, token) if token else None
    if not session_row:
        return jsonify({"error": "Import session expired, please re-upload the file"}), 400
    if not property_id:
        return jsonify({"error": "property_id is required"}), 400

    all_rows = json.loads(session_row.rows_json)
    rows = [r for i, r in enumerate(all_rows) if i not in excluded_indices]
    parsed = build_transactions(rows, mapping)

    stats = _commit_parsed_rows(
        parsed, property_id, default_income_cat, default_expense_cat,
        account_label, created_by=_current_username(),
    )

    db.session.delete(session_row)
    db.session.commit()

    return jsonify({
        **stats,
        "excluded_by_user": len(excluded_indices),
        "unparseable": len(rows) - stats["total_parsed"],
    })


@app.route("/api/import/session/<token>", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def get_import_session(token):
    """Lets the Import tab resume a pending session it didn't just create
    itself — specifically, one the email monitor left for review (see
    ImportSession.source) — without the original file. Returns the same
    shape /api/import/preview does, so the existing preview/mapping UI can
    be reused unchanged; the only difference is where the token came from."""
    session_row = db.session.get(ImportSession, token)
    if not session_row:
        return jsonify({"error": "That import session no longer exists"}), 404
    rows = json.loads(session_row.rows_json)
    headers = json.loads(session_row.headers_json) if session_row.headers_json else []
    guess = json.loads(session_row.guess_json) if session_row.guess_json else {}
    return jsonify({
        "token": token,
        "headers": headers,
        "preview_rows": rows[:IMPORT_PREVIEW_LIMIT],
        "preview_limit": IMPORT_PREVIEW_LIMIT,
        "row_count": len(rows),
        "guess": guess,
        "warnings": [],
        "source": session_row.source,
        "email_subject": session_row.email_subject,
        "email_sender": session_row.email_sender,
        "email_folder": session_row.email_folder,
        "suggested_property_id": session_row.suggested_property_id,
    })


@app.route("/api/import/session/<token>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def dismiss_import_session(token):
    """Dismisses a pending review item (e.g. from the Dashboard "needs
    review" alert) without importing it — for a statement that turned out
    not to need entering after all, or that was already handled by hand."""
    session_row = db.session.get(ImportSession, token)
    if session_row:
        db.session.delete(session_row)
        db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Email Auto-Import — Outlook MAPI folder monitor (Settings > Email
# Auto-Import). email_monitor.py holds all the Outlook COM/MAPI plumbing and
# knows nothing about Flask/the database; everything here decides what to DO
# with what it finds — auto-commit confidently-matched rows straight to the
# database (tagged with an EmailImportBatch so they can be undone as a unit),
# or fall back to a normal pending ImportSession for a human to finish
# through the Import tab, exactly like Kevin described: never guess, always
# leave a safe, visible trail when the monitor can't be sure.
# ---------------------------------------------------------------------------

EMAIL_IMPORT_MIN_POLL_MINUTES = 5
EMAIL_IMPORT_MAX_POLL_MINUTES = 24 * 60


def _email_import_settings():
    s = _settings_map()
    try:
        minutes = int(s.get("email_import_poll_minutes") or 15)
    except (TypeError, ValueError):
        minutes = 15
    minutes = max(EMAIL_IMPORT_MIN_POLL_MINUTES, min(EMAIL_IMPORT_MAX_POLL_MINUTES, minutes))
    return {
        "enabled": s.get("email_import_enabled") == "1",
        "poll_minutes": minutes,
        "outlook_available": email_monitor.OUTLOOK_AVAILABLE,
    }


@app.route("/api/email-import/settings", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def get_email_import_settings():
    return jsonify(_email_import_settings())


@app.route("/api/email-import/settings", methods=["POST"])
@require_role(ROLE_ADMIN)
def update_email_import_settings():
    data = request.get_json(force=True) or {}
    if "enabled" in data:
        _set_setting("email_import_enabled", "1" if data.get("enabled") else "0")
    if "poll_minutes" in data:
        try:
            minutes = int(data.get("poll_minutes"))
        except (TypeError, ValueError):
            return jsonify({"error": "poll_minutes must be a number"}), 400
        minutes = max(EMAIL_IMPORT_MIN_POLL_MINUTES, min(EMAIL_IMPORT_MAX_POLL_MINUTES, minutes))
        _set_setting("email_import_poll_minutes", str(minutes))
    return jsonify(_email_import_settings())


@app.route("/api/email-import/folders", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def list_email_import_folders():
    """Lets the Settings UI offer a folder picker instead of making someone
    type an Outlook folder path by hand. Requires Outlook to be open on
    this machine right now — same requirement as the monitor itself."""
    try:
        return jsonify({"folders": email_monitor.list_folders()})
    except email_monitor.OutlookUnavailable as exc:
        return jsonify({"error": str(exc)}), 503


@app.route("/api/email-import/rules", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def list_email_import_rules():
    rules = EmailImportRule.query.order_by(EmailImportRule.folder_path, EmailImportRule.kind).all()
    return jsonify([r.to_dict() for r in rules])


@app.route("/api/email-import/rules", methods=["POST"])
@require_role(ROLE_ADMIN)
def create_email_import_rule():
    data = request.get_json(force=True) or {}
    kind = data.get("kind")
    folder_path = (data.get("folder_path") or "").strip()
    match_value = (data.get("match_value") or "").strip() or None
    property_id = data.get("property_id")
    account_label = (data.get("account_label") or "").strip() or "Imported"

    if kind not in ("folder", "subaccount"):
        return jsonify({"error": "kind must be 'folder' or 'subaccount'"}), 400
    if not folder_path:
        return jsonify({"error": "folder_path is required"}), 400
    if kind == "subaccount" and not match_value:
        return jsonify({"error": "match_value is required for a sub-account rule"}), 400
    if not property_id or not db.session.get(Property, int(property_id)):
        return jsonify({"error": "A valid property_id is required"}), 400

    rule = EmailImportRule(
        kind=kind, folder_path=folder_path, match_value=match_value,
        property_id=int(property_id), account_label=account_label, enabled=True,
    )
    db.session.add(rule)
    db.session.commit()
    return jsonify(rule.to_dict())


@app.route("/api/email-import/rules/<int:rule_id>", methods=["PUT"])
@require_role(ROLE_ADMIN)
def update_email_import_rule(rule_id):
    rule = db.session.get(EmailImportRule, rule_id)
    if not rule:
        return jsonify({"error": "Rule not found"}), 404
    data = request.get_json(force=True) or {}

    if "kind" in data:
        if data["kind"] not in ("folder", "subaccount"):
            return jsonify({"error": "kind must be 'folder' or 'subaccount'"}), 400
        rule.kind = data["kind"]
    if "folder_path" in data:
        folder_path = (data.get("folder_path") or "").strip()
        if not folder_path:
            return jsonify({"error": "folder_path is required"}), 400
        rule.folder_path = folder_path
    if "match_value" in data:
        rule.match_value = (data.get("match_value") or "").strip() or None
    if "property_id" in data:
        if not data["property_id"] or not db.session.get(Property, int(data["property_id"])):
            return jsonify({"error": "A valid property_id is required"}), 400
        rule.property_id = int(data["property_id"])
    if "account_label" in data:
        rule.account_label = (data.get("account_label") or "").strip() or "Imported"
    if "enabled" in data:
        rule.enabled = bool(data["enabled"])

    if rule.kind == "subaccount" and not rule.match_value:
        return jsonify({"error": "match_value is required for a sub-account rule"}), 400

    db.session.commit()
    return jsonify(rule.to_dict())


@app.route("/api/email-import/rules/<int:rule_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN)
def delete_email_import_rule(rule_id):
    rule = db.session.get(EmailImportRule, rule_id)
    if rule:
        db.session.delete(rule)
        db.session.commit()
    return jsonify({"ok": True})


def _guess_is_usable(guess):
    """The minimum a person would need before the Import tab even shows a
    preview: a date column, a description column, and either an amount
    column or a debit/credit pair. Anything less and the email monitor
    leaves this as a pending-review session rather than auto-parsing rows
    it can't confidently line up — same as a person would have to map
    columns by hand in that case."""
    if not guess:
        return False
    has_amount = (
        guess.get("amount_col") is not None
        or guess.get("debit_col") is not None
        or guess.get("credit_col") is not None
    )
    return guess.get("date_col") is not None and guess.get("description_col") is not None and has_amount


def _create_pending_email_session(folder_path, subject, sender, rows, headers, guess, suggested_property_id=None):
    _prune_stale_import_sessions()
    token = uuid.uuid4().hex
    db.session.add(ImportSession(
        token=token, rows_json=json.dumps(rows),
        headers_json=json.dumps(headers), guess_json=json.dumps(guess or {}),
        source="email", email_subject=(subject or "")[:500], email_sender=(sender or "")[:255],
        email_folder=folder_path, suggested_property_id=suggested_property_id,
    ))
    db.session.commit()
    return token


def _process_email_item(item, filename, raw, folder_path, rules_for_folder):
    """Decides what to do with one unread email's statement attachment:
    auto-commit it (tagged to an EmailImportBatch) if every row resolves
    confidently against `rules_for_folder`, otherwise leave it as a pending
    ImportSession for the Import tab. Always marks the email read once
    handled either way — but only once handled, so a crash partway through
    leaves it unread and retried on the next poll instead of silently lost."""
    subject, sender = email_monitor.message_metadata(item)

    try:
        result = sniff_file(filename, raw)
    except Exception:
        app.logger.exception("Email auto-import: could not read attachment %r", filename)
        _create_pending_email_session(folder_path, subject, sender, [], [], {})
        email_monitor.mark_read(item)
        return

    headers = result.get("headers") or []
    rows = result.get("rows") or []
    guess = result.get("guess") or {}

    if not headers or not rows or not _guess_is_usable(guess):
        _create_pending_email_session(folder_path, subject, sender, rows, headers, guess)
        email_monitor.mark_read(item)
        return

    parsed = build_transactions(rows, guess)
    if not parsed:
        _create_pending_email_session(folder_path, subject, sender, rows, headers, guess)
        email_monitor.mark_read(item)
        return

    folder_rule = next((r for r in rules_for_folder if r.kind == "folder"), None)
    subaccount_rules = {
        r.match_value.strip().lower(): r
        for r in rules_for_folder if r.kind == "subaccount" and r.match_value
    }

    groups = {}  # property_id -> {"rule": EmailImportRule, "rows": [...]}
    if folder_rule:
        groups[folder_rule.property_id] = {"rule": folder_rule, "rows": parsed}
    elif subaccount_rules:
        for row in parsed:
            key = (row.get("account") or "").strip().lower()
            rule = subaccount_rules.get(key)
            if not rule:
                groups = None
                break
            groups.setdefault(rule.property_id, {"rule": rule, "rows": []})["rows"].append(row)
    else:
        groups = None

    if not groups:
        # No rule covers this folder at all, or (for a bundled sub-account
        # statement) at least one row's account wasn't mapped — the whole
        # email goes to review rather than importing some rows and not
        # others, which would be a confusing half-done state.
        _create_pending_email_session(folder_path, subject, sender, rows, headers, guess)
        email_monitor.mark_read(item)
        return

    batch = EmailImportBatch(
        property_id=next(iter(groups)) if len(groups) == 1 else None,
        subject=(subject or "")[:500], sender=(sender or "")[:255], source_folder=folder_path,
    )
    db.session.add(batch)
    db.session.flush()  # assign batch.id for _commit_parsed_rows to tag rows with

    total_inserted = 0
    for property_id, group in groups.items():
        rule = group["rule"]
        stats = _commit_parsed_rows(
            group["rows"], property_id, account_label=(rule.account_label or "Imported"),
            import_batch_id=batch.id,
        )
        total_inserted += stats["inserted"]
    batch.inserted_count = total_inserted
    db.session.commit()
    email_monitor.mark_read(item)


def _poll_email_import_once():
    """One full pass: connects to Outlook, checks every folder that has at
    least one enabled EmailImportRule, and processes every unread item in
    it with a supported attachment. Any failure (Outlook not open, a folder
    that no longer exists, a single bad email) is caught and logged rather
    than raised, so one bad folder or a closed Outlook never stops the rest
    of the pass or crashes the scheduler — exactly the same
    try/except-and-move-on approach already used for scheduled auto-backups."""
    rules = EmailImportRule.query.filter_by(enabled=True).all()
    if not rules:
        return

    rules_by_folder = {}
    for r in rules:
        rules_by_folder.setdefault(r.folder_path, []).append(r)

    ns = email_monitor.outlook_namespace()  # raises OutlookUnavailable if not reachable

    for folder_path, folder_rules in rules_by_folder.items():
        try:
            folder = email_monitor.resolve_folder(ns, folder_path)
        except ValueError:
            app.logger.warning("Email auto-import: folder no longer found: %r", folder_path)
            continue
        try:
            for item, filename, raw in email_monitor.unread_items_with_attachment(folder):
                try:
                    _process_email_item(item, filename, raw, folder_path, folder_rules)
                except Exception:
                    app.logger.exception(
                        "Email auto-import: failed processing an item in %r", folder_path
                    )
        except email_monitor.OutlookUnavailable:
            raise
        except Exception:
            app.logger.exception("Email auto-import: failed reading folder %r", folder_path)


_email_scheduler_started = False


def _email_scheduler_loop():
    time.sleep(20)  # let the app finish booting/migrating first
    while True:
        next_sleep_seconds = 300  # fallback if reading settings itself fails
        try:
            with app.app_context():
                settings = _email_import_settings()
                next_sleep_seconds = settings["poll_minutes"] * 60
                if settings["enabled"]:
                    _poll_email_import_once()
        except email_monitor.OutlookUnavailable:
            pass  # Outlook isn't open right now — perfectly normal, try again next time
        except Exception:
            app.logger.exception("Scheduled email auto-import check failed")
        time.sleep(next_sleep_seconds)


def _start_email_scheduler():
    global _email_scheduler_started
    if _email_scheduler_started:
        return
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _email_scheduler_started = True
    threading.Thread(target=_email_scheduler_loop, daemon=True).start()


_start_email_scheduler()


@app.route("/api/email-import/poll-now", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def poll_email_import_now():
    """Manual "Check now" button in Settings — runs one poll pass
    immediately instead of waiting for the scheduled interval, mainly so a
    newly-added rule can be tried right away."""
    try:
        _poll_email_import_once()
    except email_monitor.OutlookUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"ok": True})


@app.route("/api/email-import/pending", methods=["GET"])
def list_pending_email_imports():
    """Powers the Dashboard "N statements need review" alert — every
    ImportSession the email monitor left behind because it couldn't
    confidently route it on its own."""
    rows = ImportSession.query.filter_by(source="email").order_by(ImportSession.created_at.asc()).all()
    return jsonify([{
        "token": r.token,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "email_subject": r.email_subject,
        "email_sender": r.email_sender,
        "email_folder": r.email_folder,
        "suggested_property_id": r.suggested_property_id,
    } for r in rows])


@app.route("/api/email-import/batches", methods=["GET"])
def list_email_import_batches():
    """Recent auto-import batches — used for the Dashboard's "Auto-imported
    N transactions — Undo" notification (only the most recent undoable one
    is shown; see EmailImportBatch.is_undoable)."""
    limit = min(int(request.args.get("limit", 10)), 50)
    rows = EmailImportBatch.query.order_by(EmailImportBatch.created_at.desc()).limit(limit).all()
    return jsonify([b.to_dict() for b in rows])


@app.route("/api/email-import/undo/<int:batch_id>", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def undo_email_import_batch(batch_id):
    batch = db.session.get(EmailImportBatch, batch_id)
    if not batch:
        return jsonify({"error": "Batch not found"}), 404
    if not batch.is_undoable():
        return jsonify({
            "error": "This import can no longer be undone — either it already was, or new data "
                     "has been added since it ran."
        }), 400

    AccountReconciliation.query.filter_by(import_batch_id=batch.id).delete(synchronize_session=False)
    Transaction.query.filter_by(import_batch_id=batch.id).delete(synchronize_session=False)
    batch.undone = True
    batch.undone_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Dashboard / reports
# ---------------------------------------------------------------------------

@app.route("/api/dashboard", methods=["GET"])
def dashboard():
    property_id = request.args.get("property_id", "all")
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        start, end = _default_period()

    # Lazily catch up any recurring-transaction occurrences due through
    # today — the Dashboard is the first thing most sessions load, so this
    # is the most reliable place to make sure "today's" numbers already
    # include anything a recurring rule owes.
    _generate_due_recurring(property_id)

    txns = _query_transactions(property_id=property_id, start=start, end=end)
    income = sum(t.amount for t in txns if t.type == "income")
    expenses = sum(t.amount for t in txns if t.type == "expense")

    by_category = {}
    for t in txns:
        if t.type != "expense":
            continue
        name = t.category.name if t.category else "Uncategorized"
        by_category[name] = by_category.get(name, 0) + t.amount
    expense_breakdown = sorted(
        [{"category": k, "amount": v} for k, v in by_category.items()],
        key=lambda x: -x["amount"],
    )

    series_property_id = None if property_id == "all" else int(property_id)
    series = _monthly_series(series_property_id, start, end)

    recent = sorted(txns, key=lambda t: t.date, reverse=True)[:10]

    # Optional per-unit break-out — only meaningful (and only computed) when
    # a single property is selected and it actually has units defined. This
    # is purely additive/opt-in: the dashboard totals above are unchanged
    # either way, and the frontend only shows this when the user asks for it.
    by_unit = None
    if series_property_id:
        units = PropertyUnit.query.filter_by(property_id=series_property_id).order_by(PropertyUnit.id).all()
        if units:
            buckets = {u.id: {"unit_id": u.id, "unit_name": u.name, "income": 0.0, "expenses": 0.0} for u in units}
            buckets[None] = {"unit_id": None, "unit_name": "Unassigned", "income": 0.0, "expenses": 0.0}
            for t in txns:
                b = buckets.get(t.unit_id, buckets[None])
                if t.type == "income":
                    b["income"] += t.amount
                else:
                    b["expenses"] += t.amount
            by_unit = [
                {**b, "net": b["income"] - b["expenses"]}
                for b in buckets.values()
                if b["unit_id"] is not None or b["income"] or b["expenses"]
            ]

    # Cap rate / cash-on-cash return — only meaningful for a single selected
    # property (blending several properties' purchase prices/values into one
    # ratio wouldn't mean anything), reusing the exact same math already
    # used on the Comparison report so the two screens never disagree.
    property_metrics = None
    if series_property_id:
        prop = db.session.get(Property, series_property_id)
        if prop:
            property_metrics = _compute_property_metrics(prop, start, end)

    return jsonify({
        "start": start,
        "end": end,
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "transaction_count": len(txns),
        "expense_breakdown": expense_breakdown,
        "monthly_series": series,
        "recent_transactions": [_txn_dict_with_property(t) for t in recent],
        "by_unit": by_unit,
        "property_metrics": property_metrics,
    })


@app.route("/api/reports/comparison", methods=["GET"])
def reports_comparison():
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        start, end = _default_period()

    ids_param = request.args.get("property_ids")
    if ids_param:
        ids = [int(x) for x in ids_param.split(",") if x]
        props = Property.query.filter(Property.id.in_(ids)).all()
    else:
        props = Property.query.filter_by(archived=False).all()

    rows = [_compute_property_metrics(p, start, end) for p in props]
    rows.sort(key=lambda r: -r["net"])
    return jsonify({"start": start, "end": end, "rows": rows})


# ---------------------------------------------------------------------------
# Dashboard alerts — small, computed-on-the-fly heads-up items using data
# that already exists elsewhere (reconciliations, transactions, tenants,
# documents). Nothing here is stored; it's recalculated every time the
# dashboard is loaded, so it can never go stale or need its own cleanup.
# ---------------------------------------------------------------------------

ALERT_UNRECONCILED_DAYS = 60
ALERT_EXPIRING_SOON_DAYS = 60


@app.route("/api/dashboard/alerts", methods=["GET"])
def dashboard_alerts():
    today = date.today()
    alerts = []

    active_props = Property.query.filter_by(archived=False).all()
    prop_by_id = {p.id: p for p in active_props}

    # 1) Bank accounts that haven't been reconciled in a while — only for
    # accounts that actually have transaction activity, so a brand-new
    # account you haven't started using yet doesn't nag you on day one.
    for acct in PropertyAccount.query.filter(PropertyAccount.property_id.in_(list(prop_by_id.keys()))).all():
        has_activity = Transaction.query.filter_by(account=acct.label()).first() is not None
        if not has_activity:
            continue
        latest = (
            AccountReconciliation.query.filter_by(property_account_id=acct.id)
            .order_by(AccountReconciliation.reconcile_date.desc()).first()
        )
        last_date = _parse_iso_date(latest.reconcile_date) if latest else None
        if not last_date or (today - last_date).days >= ALERT_UNRECONCILED_DAYS:
            days_text = f"{(today - last_date).days} days" if last_date else "never"
            alerts.append({
                "severity": "warning",
                "type": "unreconciled_account",
                "property_id": acct.property_id,
                "property_name": prop_by_id[acct.property_id].name,
                "account_id": acct.id,
                "message": f"\"{acct.label()}\" hasn't been reconciled in {days_text} "
                           f"— on {prop_by_id[acct.property_id].name}.",
            })

    # 2) Active properties with no transactions logged this month — only
    # for properties that have SOME history already, so a property you just
    # added (and haven't entered anything for yet) isn't flagged immediately.
    month_start = today.replace(day=1).isoformat()
    for p in active_props:
        has_any = Transaction.query.filter_by(property_id=p.id).first() is not None
        if not has_any:
            continue
        has_this_month = Transaction.query.filter(
            Transaction.property_id == p.id, Transaction.date >= month_start
        ).first() is not None
        if not has_this_month:
            alerts.append({
                "severity": "info",
                "property_id": p.id,
                "property_name": p.name,
                "message": f"No transactions logged yet this month for {p.name}.",
            })

    # 3) Leases expiring soon.
    cutoff = (today + timedelta(days=ALERT_EXPIRING_SOON_DAYS)).isoformat()
    today_iso = today.isoformat()
    for t in Tenant.query.filter_by(active=True).filter(
        Tenant.lease_end.isnot(None), Tenant.lease_end >= today_iso, Tenant.lease_end <= cutoff
    ).all():
        prop = prop_by_id.get(t.property_id)
        if not prop:
            continue
        alerts.append({
            "severity": "warning",
            "property_id": t.property_id,
            "property_name": prop.name,
            "message": f"{t.name}'s lease at {prop.name} ends {t.lease_end}.",
        })

    # 4) Documents (e.g. insurance policies) expiring soon.
    for d in PropertyDocument.query.filter(
        PropertyDocument.expiration_date.isnot(None),
        PropertyDocument.expiration_date >= today_iso,
        PropertyDocument.expiration_date <= cutoff,
    ).all():
        prop = prop_by_id.get(d.property_id)
        if not prop:
            continue
        alerts.append({
            "severity": "warning",
            "property_id": d.property_id,
            "property_name": prop.name,
            "message": f"{(d.original_filename or d.filename)} ({d.doc_type}) at {prop.name} "
                       f"expires {d.expiration_date}.",
        })

    # 5) Statements the Email Auto-Import monitor found but couldn't
    # confidently route on its own — each one is sitting as a pending
    # ImportSession, ready to be finished in the Import tab (see
    # GET /api/import/session/<token> and /api/email-import/pending).
    pending_email_count = ImportSession.query.filter_by(source="email").count()
    if pending_email_count:
        alerts.append({
            "severity": "warning",
            "type": "email_import_review",
            "property_id": None,
            "message": (
                f"{pending_email_count} bank statement"
                f"{'s' if pending_email_count != 1 else ''} from email need{'s' if pending_email_count == 1 else ''} review "
                "before they can be imported."
            ),
        })

    # 6) The most recent Outlook auto-import batch, for as long as it's
    # still safe to undo (see EmailImportBatch.is_undoable) — disappears on
    # its own the moment any newer transaction is added, exactly like Kevin
    # asked for, no separate "dismiss" needed for it to go away naturally.
    latest_batch = (
        EmailImportBatch.query.filter_by(undone=False)
        .order_by(EmailImportBatch.created_at.desc()).first()
    )
    if latest_batch and latest_batch.is_undoable():
        b = latest_batch.to_dict()
        where = f" for {b['property_name']}" if b["property_name"] else ""
        alerts.append({
            "severity": "info",
            "type": "email_import_undo",
            "property_id": latest_batch.property_id,
            "batch_id": latest_batch.id,
            "message": f"Auto-imported {b['inserted_count']} transaction"
                       f"{'s' if b['inserted_count'] != 1 else ''}{where} from email.",
        })

    return jsonify(alerts)


def _parse_iso_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Global search — across properties, transactions (payee/notes), categories,
# and tenants, for jumping straight to something instead of hunting through
# tabs/filters by hand.
# ---------------------------------------------------------------------------

SEARCH_RESULT_LIMIT_PER_TYPE = 15


@app.route("/api/search", methods=["GET"])
def global_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"query": q, "results": []})
    like = f"%{q}%"
    results = []

    for p in Property.query.filter(Property.name.ilike(like)).limit(SEARCH_RESULT_LIMIT_PER_TYPE).all():
        results.append({"type": "property", "id": p.id, "property_id": p.id,
                         "title": p.name, "subtitle": p.address or ""})

    for t in (Transaction.query.filter(
        db.or_(Transaction.payee.ilike(like), Transaction.notes.ilike(like))
    ).order_by(Transaction.date.desc()).limit(SEARCH_RESULT_LIMIT_PER_TYPE).all()):
        results.append({
            "type": "transaction", "id": t.id, "property_id": t.property_id,
            "title": t.payee or "(no payee)",
            "subtitle": f"{t.date} · {_money_text(t.amount)} · {t.property.name if t.property else ''}",
        })

    for c in Category.query.filter(Category.name.ilike(like)).limit(SEARCH_RESULT_LIMIT_PER_TYPE).all():
        results.append({"type": "category", "id": c.id, "property_id": None,
                         "title": c.name, "subtitle": c.type})

    for t in Tenant.query.filter(Tenant.name.ilike(like)).limit(SEARCH_RESULT_LIMIT_PER_TYPE).all():
        prop = db.session.get(Property, t.property_id)
        results.append({"type": "tenant", "id": t.id, "property_id": t.property_id,
                         "title": t.name, "subtitle": prop.name if prop else ""})

    return jsonify({"query": q, "results": results})


def _money_text(amount):
    return f"${amount:,.2f}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Never sent to the browser as-is, and never settable through the generic
# POST /api/settings below — each has its own dedicated endpoint that
# validates/hashes it properly instead. Security-question ANSWERS are
# hashed and hidden the same way the password is; the QUESTIONS themselves
# are plain text and fine to show (so Settings can display what's
# currently configured) — see access_security_q1/2/3 below, deliberately
# NOT in this set. The attempt-counter/lockout-timestamp rows are internal
# bookkeeping, not something a person sets directly, so they're hidden too.
SENSITIVE_SETTING_KEYS = {
    "access_password_hash",
    "access_security_a1_hash", "access_security_a2_hash", "access_security_a3_hash",
    "access_password_failed_attempts", "access_password_lockout_until",
}


@app.route("/api/settings", methods=["GET"])
def get_settings():
    s = _settings_map()
    s["access_password_enabled"] = bool(s.get("access_password_hash"))
    s["access_security_questions_configured"] = bool(
        s.get("access_security_q1") and s.get("access_security_q2") and s.get("access_security_q3")
    )
    # Read-only, derived — not stored as Settings rows themselves.
    s["backup_folder_path"] = BACKUP_DIR
    last_auto = _last_auto_backup_at()
    s["backup_auto_last_run_at"] = last_auto.isoformat() if last_auto else None
    for key in SENSITIVE_SETTING_KEYS:
        s.pop(key, None)
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
@require_role(ROLE_ADMIN)
def update_settings():
    data = request.get_json(force=True)
    for key, value in data.items():
        if key in SENSITIVE_SETTING_KEYS:
            continue
        if key == "backup_auto_frequency" and value not in ("", "weekly", "30days"):
            continue
        if key == "backup_auto_retention":
            value = str(_clamp_backup_retention(value))
        _set_setting(key, value)
    return jsonify({"ok": True})


@app.route("/api/settings/current-mileage-rate", methods=["GET"])
def get_current_mileage_rate():
    effective_date, rate = current_standard_mileage_rate(date.today().isoformat())
    return jsonify({"rate": rate, "effective_date": effective_date})


ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp"}

# Predetermined output sizes for each place the logo is used. Uploaded images
# are auto-resized (never upscaled) to fit within these boxes, so one upload
# looks right everywhere: small and crisp in the app header, larger and
# print-quality on PDF/Excel letterheads.
LOGO_VARIANTS = {
    "app": (240, 80),
    "print": (700, 260),
}


def _logo_variant_path(variant):
    return os.path.join(DATA_DIR, f"logo_{variant}.png")


def _clear_logo_files():
    for variant in LOGO_VARIANTS:
        path = _logo_variant_path(variant)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@app.route("/api/business/logo", methods=["GET"])
def get_logo():
    variant = request.args.get("variant", "app")
    if variant not in LOGO_VARIANTS:
        variant = "app"
    if _settings_map().get("has_logo") != "1":
        abort(404)
    path = _logo_variant_path(variant)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/api/business/logo", methods=["POST"])
@require_role(ROLE_ADMIN)
def upload_logo():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return jsonify({"error": "Unsupported image type. Use PNG, JPG, BMP, or GIF."}), 400

    try:
        from PIL import Image as PILImage
        img = PILImage.open(file.stream)
        img.load()
    except Exception:
        return jsonify({"error": "Could not read that image file. Try a different PNG/JPG/BMP file."}), 400

    if img.mode not in ("RGBA", "LA"):
        img = img.convert("RGBA")

    os.makedirs(DATA_DIR, exist_ok=True)
    _clear_logo_files()
    for variant, box in LOGO_VARIANTS.items():
        variant_img = img.copy()
        variant_img.thumbnail(box, PILImage.LANCZOS)
        variant_img.save(_logo_variant_path(variant), "PNG")

    _set_setting("has_logo", "1")
    return jsonify({"ok": True})


@app.route("/api/business/logo", methods=["DELETE"])
@require_role(ROLE_ADMIN)
def delete_logo():
    _clear_logo_files()
    _set_setting("has_logo", "0")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

@app.route("/api/export/transactions.<fmt>", methods=["GET"])
def export_transactions(fmt):
    property_id = request.args.get("property_id", "all")
    start = request.args.get("start")
    end = request.args.get("end")
    type_ = request.args.get("type", "all")
    category_id = request.args.get("category_id")
    unit_id = request.args.get("unit_id")

    txns = _query_transactions(property_id=property_id, start=start, end=end, type_=type_,
                                category_id=category_id, unit_id=unit_id)
    txn_dicts = [_txn_dict_with_property(t) for t in txns]

    if property_id != "all":
        prop = db.session.get(Property, int(property_id))
        prop_name = prop.name if prop else "Property"
    else:
        prop_name = "All Properties"
    period_label = f"{start or 'Beginning'} to {end or 'Today'}"

    settings_map = _settings_map()
    income_color = settings_map.get("income_color", DEFAULT_SETTINGS["income_color"])
    expense_color = settings_map.get("expense_color", DEFAULT_SETTINGS["expense_color"])
    business = _business_info()

    fname_base = f"transactions_{prop_name.replace(' ', '_')}_{date.today().isoformat()}"

    if fmt == "csv":
        content = export_lib.transactions_to_csv(txn_dicts)
        return send_file(io.BytesIO(content), mimetype="text/csv", as_attachment=True,
                          download_name=f"{fname_base}.csv")
    elif fmt == "xlsx":
        content = export_lib.transactions_to_excel(txn_dicts, business=business, logo_bytes=_logo_bytes())
        return send_file(io.BytesIO(content),
                          mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          as_attachment=True, download_name=f"{fname_base}.xlsx")
    elif fmt == "pdf":
        content = export_lib.transactions_to_pdf(txn_dicts, property_name=prop_name, period_label=period_label,
                                                   income_color=income_color, expense_color=expense_color,
                                                   business=business, logo_bytes=_logo_bytes())
        return send_file(io.BytesIO(content), mimetype="application/pdf", as_attachment=True,
                          download_name=f"{fname_base}.pdf")
    else:
        abort(400)


@app.route("/api/export/comparison.<fmt>", methods=["GET"])
def export_comparison(fmt):
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        start, end = _default_period()

    ids_param = request.args.get("property_ids")
    if ids_param:
        ids = [int(x) for x in ids_param.split(",") if x]
        props = Property.query.filter(Property.id.in_(ids)).all()
    else:
        props = Property.query.filter_by(archived=False).all()

    rows = [_compute_property_metrics(p, start, end) for p in props]
    rows.sort(key=lambda r: -r["net"])
    period_label = f"{start} to {end}"

    settings_map = _settings_map()
    income_color = settings_map.get("income_color", DEFAULT_SETTINGS["income_color"])
    expense_color = settings_map.get("expense_color", DEFAULT_SETTINGS["expense_color"])
    business = _business_info()

    fname_base = f"property_comparison_{date.today().isoformat()}"

    if fmt == "csv":
        content = export_lib.comparison_to_csv(rows)
        return send_file(io.BytesIO(content), mimetype="text/csv", as_attachment=True,
                          download_name=f"{fname_base}.csv")
    elif fmt == "xlsx":
        content = export_lib.comparison_to_excel(rows, period_label=period_label, business=business,
                                                  logo_bytes=_logo_bytes())
        return send_file(io.BytesIO(content),
                          mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          as_attachment=True, download_name=f"{fname_base}.xlsx")
    elif fmt == "pdf":
        content = export_lib.comparison_to_pdf(rows, period_label=period_label,
                                                income_color=income_color, expense_color=expense_color,
                                                business=business, logo_bytes=_logo_bytes())
        return send_file(io.BytesIO(content), mimetype="application/pdf", as_attachment=True,
                          download_name=f"{fname_base}.pdf")
    else:
        abort(400)


@app.route("/api/export/quickbooks.<fmt>", methods=["GET"])
def export_quickbooks(fmt):
    """One-time/occasional migration export — brings transaction history
    (with categories, and property tagged as a QuickBooks Class / Quicken
    sub-category) into QuickBooks Desktop (.iif), Quicken (.qif), or a
    generic 3-column bank CSV that QuickBooks Online's manual upload and
    Quicken's CSV importer both accept. Unlike /api/export/transactions.<fmt>
    above, this deliberately has no type/category/unit filters — a
    migration should bring everything, not just what happens to be on
    screen — but still respects a property and date-range choice since not
    everyone migrating wants every property or all history at once."""
    if fmt not in ("iif", "qif", "csv"):
        abort(400)

    property_id = request.args.get("property_id", "all")
    start = request.args.get("start")
    end = request.args.get("end")

    txns = _query_transactions(property_id=property_id, start=start, end=end)
    txn_dicts = [_txn_dict_with_property(t) for t in txns]

    if property_id != "all":
        prop = db.session.get(Property, int(property_id))
        prop_slug = (prop.name if prop else "property").replace(" ", "_")
    else:
        prop_slug = "all_properties"
    fname_base = f"{prop_slug}_{date.today().isoformat()}"

    if fmt == "iif":
        content = export_lib.transactions_to_iif(txn_dicts)
        return send_file(io.BytesIO(content), mimetype="application/octet-stream", as_attachment=True,
                          download_name=f"quickbooks_{fname_base}.iif")
    elif fmt == "qif":
        content = export_lib.transactions_to_qif(txn_dicts)
        return send_file(io.BytesIO(content), mimetype="application/qif", as_attachment=True,
                          download_name=f"quicken_{fname_base}.qif")
    else:  # csv
        content = export_lib.transactions_to_qb_csv(txn_dicts)
        return send_file(io.BytesIO(content), mimetype="text/csv", as_attachment=True,
                          download_name=f"bank_transactions_{fname_base}.csv")


# ---------------------------------------------------------------------------
# Mileage tracker — logs business trips per property for the IRS standard
# mileage rate deduction, which feeds into the Tax Documents report's Auto
# and Travel line (Schedule E line 6) alongside any transactions already
# tagged to that category. Each trip snapshots the rate in effect when it
# was logged (see MileageLog.rate_used in models.py) rather than looking the
# rate up live, since the IRS rate itself changes — sometimes more than
# once within a single tax year.
# ---------------------------------------------------------------------------

@app.route("/api/mileage", methods=["GET"])
def list_all_mileage():
    """Backs the top-level Mileage tab — same "All Properties or one"
    behavior as list_all_tenants/list_all_documents above."""
    property_id = request.args.get("property_id")
    year = request.args.get("year")
    prop_by_id = {p.id: p for p in Property.query.all()}
    q = MileageLog.query
    if property_id and str(property_id).lower() != "all":
        q = q.filter_by(property_id=int(property_id))
    if year:
        q = q.filter(MileageLog.date.like(f"{int(year)}-%"))
    logs = q.order_by(MileageLog.date.desc()).all()
    result = []
    for m in logs:
        d = m.to_dict()
        d["property_name"] = prop_by_id[m.property_id].name if m.property_id in prop_by_id else ""
        result.append(d)
    return jsonify(result)


@app.route("/api/properties/<int:prop_id>/mileage", methods=["GET"])
def list_mileage(prop_id):
    Property.query.get_or_404(prop_id)
    year = request.args.get("year")
    q = MileageLog.query.filter_by(property_id=prop_id)
    if year:
        q = q.filter(MileageLog.date.like(f"{int(year)}-%"))
    logs = q.order_by(MileageLog.date.desc()).all()
    return jsonify([m.to_dict() for m in logs])


@app.route("/api/properties/<int:prop_id>/mileage", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def create_mileage(prop_id):
    Property.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    if not data.get("date"):
        return jsonify({"error": "date is required"}), 400
    try:
        miles = float(data.get("miles") or 0)
    except (TypeError, ValueError):
        miles = 0
    if miles <= 0:
        return jsonify({"error": "miles must be greater than 0"}), 400
    rate = float(_settings_map().get("standard_mileage_rate") or 0)
    m = MileageLog(
        property_id=prop_id,
        unit_id=data.get("unit_id") or None,
        date=data["date"],
        purpose=data.get("purpose"),
        miles=miles,
        rate_used=rate,
    )
    db.session.add(m)
    db.session.commit()
    return jsonify(m.to_dict()), 201


@app.route("/api/mileage/<int:mileage_id>", methods=["PUT"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def update_mileage(mileage_id):
    m = db.session.get(MileageLog, mileage_id) or abort(404)
    data = request.get_json(force=True)
    if "date" in data:
        m.date = data["date"]
    if "purpose" in data:
        m.purpose = data["purpose"]
    if "unit_id" in data:
        m.unit_id = data["unit_id"] or None
    if "miles" in data:
        try:
            miles = float(data["miles"])
        except (TypeError, ValueError):
            return jsonify({"error": "miles must be a number"}), 400
        if miles <= 0:
            return jsonify({"error": "miles must be greater than 0"}), 400
        m.miles = miles
    db.session.commit()
    return jsonify(m.to_dict())


@app.route("/api/mileage/<int:mileage_id>", methods=["DELETE"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def delete_mileage(mileage_id):
    m = db.session.get(MileageLog, mileage_id) or abort(404)
    db.session.delete(m)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Tax Documents — Schedule E-style packet for a chosen calendar tax year,
# ready to hand to a CPA. Always computed fresh from current transactions/
# categories rather than stored as a separate mutable record, so it can
# never drift out of sync with the underlying data.
# ---------------------------------------------------------------------------

def _tax_report_properties(property_ids_param):
    if property_ids_param:
        ids = [int(x) for x in property_ids_param.split(",") if x]
        return Property.query.filter(Property.id.in_(ids)).order_by(Property.name).all()
    return Property.query.filter_by(archived=False).order_by(Property.name).all()


@app.route("/api/tax/years", methods=["GET"])
def tax_years():
    """Calendar years that have at least one transaction, for the year
    picker — newest first, always includes the current year even with no
    data yet so a first-time user isn't looking at an empty dropdown."""
    rows = db.session.query(Transaction.date).all()
    years = {int(d[:4]) for (d,) in rows if d and len(d) >= 4 and d[:4].isdigit()}
    years.add(date.today().year)
    return jsonify(sorted(years, reverse=True))


@app.route("/api/tax/report", methods=["GET"])
def tax_report_preview():
    try:
        year = int(request.args.get("year") or date.today().year)
    except ValueError:
        return jsonify({"error": "Invalid year"}), 400
    include_detail = request.args.get("detail") == "1"
    props = _tax_report_properties(request.args.get("property_ids"))
    if not props:
        return jsonify({"error": "No properties to report on"}), 400
    packet = tax_lib.build_tax_packet(props, year, include_detail=include_detail)
    return jsonify(packet)


@app.route("/api/tax/vendors", methods=["GET"])
def tax_vendor_report():
    """Vendor/1099 helper: totals every expense payee across the selected
    properties for a tax year and flags anyone at or above the 1099-NEC/MISC
    reporting threshold — informational only, this never files or generates
    anything, it just tells you who to look at."""
    try:
        year = int(request.args.get("year") or date.today().year)
    except ValueError:
        return jsonify({"error": "Invalid year"}), 400
    props = _tax_report_properties(request.args.get("property_ids"))
    if not props:
        return jsonify({"error": "No properties to report on"}), 400
    threshold = float(_settings_map().get("form_1099_threshold") or 2000)
    report = tax_lib.build_vendor_report(props, year, threshold)
    return jsonify(report)


@app.route("/api/tax/report.pdf", methods=["GET"])
def tax_report_pdf():
    try:
        year = int(request.args.get("year") or date.today().year)
    except ValueError:
        return jsonify({"error": "Invalid year"}), 400
    include_detail = request.args.get("detail") == "1"
    props = _tax_report_properties(request.args.get("property_ids"))
    if not props:
        return jsonify({"error": "No properties to report on"}), 400
    packet = tax_lib.build_tax_packet(props, year, include_detail=include_detail)
    business = _business_info()
    content = export_lib.tax_packet_to_pdf(packet, business=business, logo_bytes=_logo_bytes(),
                                            include_detail=include_detail)
    fname = f"tax_documents_{year}_{date.today().isoformat()}.pdf"
    return send_file(io.BytesIO(content), mimetype="application/pdf", as_attachment=True,
                      download_name=fname)


# ---------------------------------------------------------------------------
# License
# ---------------------------------------------------------------------------

@app.route("/LICENSE")
def license_file():
    path = os.path.join(RESOURCE_DIR, "LICENSE")
    if not os.path.exists(path):
        return "License file not found.", 404
    return send_file(path, mimetype="text/plain")


# ---------------------------------------------------------------------------
# Backup & restore
#
# "Profile" backups move just the business branding (name, address, colors,
# logo, etc.) so a fresh install can look like yours without carrying over
# any properties/transactions. "Full" backups move everything by zipping up
# the live SQLite database file itself — simplest way to get a byte-perfect
# copy with zero risk of subtly re-encoding the data.
# ---------------------------------------------------------------------------

LOGO_FILENAMES = [f"logo_{v}.png" for v in LOGO_VARIANTS]

# Server-mode (Postgres) backups can't just zip up a database file the way
# SQLite ones do, so instead they walk every table into plain JSON — a
# format that doesn't care what database engine is underneath, which also
# means a server-mode backup could in principle be restored into a
# differently-hosted Postgres instance later.
SERVER_BACKUP_FORMAT = "rental_manager_server_backup_v1"
SERVER_BACKUP_TABLES = [
    ("properties", Property),
    ("categories", Category),
    ("property_accounts", PropertyAccount),
    ("property_units", PropertyUnit),
    ("transactions", Transaction),
    ("account_reconciliations", AccountReconciliation),
    ("recurring_transactions", RecurringTransaction),
    ("import_rules", ImportRule),
    ("mileage_logs", MileageLog),
    ("tenants", Tenant),
    ("property_documents", PropertyDocument),
    ("users", User),
]


def _row_to_plain_dict(row):
    d = {}
    for col in sa_inspect(row).mapper.column_attrs:
        val = getattr(row, col.key)
        if isinstance(val, (datetime, date)):
            val = val.isoformat()
        d[col.key] = val
    return d


def _export_server_backup_json():
    payload = {
        "format": SERVER_BACKUP_FORMAT,
        "exported_at": datetime.utcnow().isoformat(),
        "settings": _settings_map(),
    }
    for key, model in SERVER_BACKUP_TABLES:
        payload[key] = [_row_to_plain_dict(r) for r in model.query.all()]
    return payload


def _reset_pg_sequence(table_name, pk_column="id"):
    if not str(db.engine.url).startswith("postgresql"):
        return
    db.session.execute(sa_text(
        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_column}'), "
        f"COALESCE((SELECT MAX({pk_column}) FROM {table_name}), 1), true)"
    ))


def _import_server_backup_json(payload):
    if payload.get("format") != SERVER_BACKUP_FORMAT:
        raise ValueError("That doesn't look like a server-mode backup file")

    # Clear in FK-safe order (children before parents), then reload
    # everything from the backup.
    AccountReconciliation.query.delete()
    RecurringTransaction.query.delete()
    ImportRule.query.delete()
    MileageLog.query.delete()
    Tenant.query.delete()
    PropertyDocument.query.delete()
    Transaction.query.delete()
    PropertyAccount.query.delete()
    PropertyUnit.query.delete()
    Category.query.delete()
    Property.query.delete()
    User.query.delete()
    db.session.commit()

    for key, model in SERVER_BACKUP_TABLES:
        for row in payload.get(key) or []:
            db.session.add(model(**row))
    db.session.commit()

    for table_name in ("properties", "categories", "property_accounts", "property_units", "transactions",
                       "account_reconciliations", "recurring_transactions", "import_rules", "mileage_logs",
                       "tenants", "property_documents", "users"):
        _reset_pg_sequence(table_name)
    db.session.commit()

    for k, v in (payload.get("settings") or {}).items():
        if k in DEFAULT_SETTINGS:
            _set_setting(k, v)


@app.route("/api/backup/profile/export", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def export_profile_backup():
    settings_map = _settings_map()
    for key in SENSITIVE_SETTING_KEYS:
        settings_map.pop(key, None)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("profile.json", json.dumps(settings_map, indent=2))
        for fname in LOGO_FILENAMES:
            path = os.path.join(DATA_DIR, fname)
            if os.path.exists(path):
                zf.write(path, fname)
    buf.seek(0)
    db.session.add(BackupLog(backup_type="profile", created_by=_current_username()))
    db.session.commit()
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"business_profile_{date.today().isoformat()}.zip")


@app.route("/api/backup/profile/import", methods=["POST"])
@require_role(ROLE_ADMIN)
def import_profile_backup():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        with zipfile.ZipFile(file) as zf:
            if "profile.json" not in zf.namelist():
                return jsonify({"error": "That doesn't look like a business profile export"}), 400
            settings_data = json.loads(zf.read("profile.json").decode("utf-8"))
            for key, value in settings_data.items():
                if key in DEFAULT_SETTINGS:
                    _set_setting(key, value)
            os.makedirs(DATA_DIR, exist_ok=True)
            for fname in LOGO_FILENAMES:
                if fname in zf.namelist():
                    with open(os.path.join(DATA_DIR, fname), "wb") as f:
                        f.write(zf.read(fname))
    except zipfile.BadZipFile:
        return jsonify({"error": "That file isn't a valid .zip export"}), 400
    return jsonify({"ok": True, "settings": _settings_map()})


def _build_full_backup_bytes():
    """Builds a Full Backup .zip (server-mode JSON export or standalone
    SQLite file, whichever this install is) and returns it as an in-memory
    BytesIO, positioned at the start and ready to read. Shared by the manual
    browser-download route below and the scheduled/on-demand auto-backup
    routine — same file format either way, just a different destination."""
    db.session.commit()
    buf = io.BytesIO()

    if is_server_mode(APP_CONFIG):
        payload = _export_server_backup_json()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("backup.json", json.dumps(payload, indent=2, default=str))
            for fname in LOGO_FILENAMES:
                path = os.path.join(DATA_DIR, fname)
                if os.path.exists(path):
                    zf.write(path, fname)
            _zip_write_receipts(zf)
            _zip_write_documents(zf)
    else:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(DB_PATH):
                zf.write(DB_PATH, "rental_manager.db")
            for fname in LOGO_FILENAMES:
                path = os.path.join(DATA_DIR, fname)
                if os.path.exists(path):
                    zf.write(path, fname)
            _zip_write_receipts(zf)
            _zip_write_documents(zf)

    buf.seek(0)
    return buf


@app.route("/api/backup/full/export", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def export_full_backup():
    buf = _build_full_backup_bytes()
    db.session.add(BackupLog(backup_type="full", created_by=_current_username()))
    db.session.commit()
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"rental_manager_full_backup_{date.today().isoformat()}.zip")


def _restore_full_backup_from_fileobj(file_obj):
    """Restores a Full Backup .zip from any file-like object (an uploaded
    Werkzeug FileStorage, or a plain open()'d file from the Backup folder —
    both support the .read()/seek() zipfile needs). Returns (ok, error,
    status_code); error/status_code are only meaningful when ok is False.
    Shared by the browser-upload route and the restore-from-folder route."""
    if is_server_mode(APP_CONFIG):
        try:
            with zipfile.ZipFile(file_obj) as zf:
                if "backup.json" not in zf.namelist():
                    return False, ("That looks like a standalone (SQLite) backup file, not a "
                                   "server-mode backup. The two formats aren't interchangeable."), 400
                payload = json.loads(zf.read("backup.json").decode("utf-8"))
                try:
                    _import_server_backup_json(payload)
                except ValueError as exc:
                    return False, str(exc), 400
                for fname in LOGO_FILENAMES:
                    dest = os.path.join(DATA_DIR, fname)
                    if fname in zf.namelist():
                        with open(dest, "wb") as f:
                            f.write(zf.read(fname))
                    elif os.path.exists(dest):
                        try:
                            os.remove(dest)
                        except OSError:
                            pass
                _zip_restore_receipts(zf)
                _zip_restore_documents(zf)
        except zipfile.BadZipFile:
            return False, "That file isn't a valid .zip export", 400
        return True, None, None

    # Standalone mode — original SQLite-file-swap approach.
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = os.path.join(DATA_DIR, "_restore_tmp.db")
    try:
        with zipfile.ZipFile(file_obj) as zf:
            if "rental_manager.db" not in zf.namelist():
                if "backup.json" in zf.namelist():
                    return False, ("That looks like a server-mode backup file, not a standalone "
                                   "(SQLite) backup. The two formats aren't interchangeable."), 400
                return False, "That doesn't look like a full backup file", 400

            with open(tmp_path, "wb") as f:
                f.write(zf.read("rental_manager.db"))

            # Sanity check before we touch the live database.
            test_conn = sqlite3.connect(tmp_path)
            try:
                tables = {r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                test_conn.close()
            required = {"properties", "transactions", "categories", "settings"}
            if not required.issubset(tables):
                return False, "That file doesn't look like a Rental Manager backup", 400

            db.session.remove()
            db.engine.dispose()
            os.replace(tmp_path, DB_PATH)
            for ext in ("-journal", "-wal", "-shm"):
                stale = DB_PATH + ext
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass

            for fname in LOGO_FILENAMES:
                dest = os.path.join(DATA_DIR, fname)
                if fname in zf.namelist():
                    with open(dest, "wb") as f:
                        f.write(zf.read(fname))
                elif os.path.exists(dest):
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
            _zip_restore_receipts(zf)
            _zip_restore_documents(zf)
    except zipfile.BadZipFile:
        return False, "That file isn't a valid .zip export", 400
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    with app.app_context():
        db.create_all()  # no-op if schema already matches; guards against restoring an older backup

    return True, None, None


@app.route("/api/backup/full/import", methods=["POST"])
@require_role(ROLE_ADMIN)
def import_full_backup():
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded"}), 400
    ok, error, status = _restore_full_backup_from_fileobj(file)
    if not ok:
        return jsonify({"error": error}), status
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Scheduled automatic backups — writes a Full Backup .zip straight into
# BACKUP_DIR on a Weekly or Every-30-Days cadence, on top of (never instead
# of) the manual Export/Import buttons above. Retention is capped at 30
# files, oldest deleted first — never unlimited. Off by default
# (backup_auto_frequency == "").
# ---------------------------------------------------------------------------

BACKUP_AUTO_FREQUENCIES = {"weekly": 7, "30days": 30}
BACKUP_AUTO_RETENTION_MAX = 30
BACKUP_AUTO_FILE_PREFIX = "rental_manager_auto_backup_"


def _clamp_backup_retention(raw):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(BACKUP_AUTO_RETENTION_MAX, n))


def _last_auto_backup_at():
    row = (
        BackupLog.query
        .filter_by(backup_type="auto")
        .order_by(BackupLog.created_at.desc())
        .first()
    )
    return row.created_at if row else None


def _auto_backup_files():
    """Existing auto-backup .zip files in BACKUP_DIR, oldest first."""
    try:
        names = [f for f in os.listdir(BACKUP_DIR)
                 if f.startswith(BACKUP_AUTO_FILE_PREFIX) and f.endswith(".zip")]
    except OSError:
        return []
    paths = [os.path.join(BACKUP_DIR, n) for n in names]
    paths.sort(key=lambda p: os.path.getmtime(p))
    return paths


def _create_auto_backup():
    """Writes a fresh auto-backup .zip into BACKUP_DIR, enforces retention
    (deletes the oldest file(s) once the configured cap is exceeded), and
    logs a BackupLog row. Called both by the scheduler when a backup is due
    and directly by the "Back Up Now" button (which always runs regardless
    of whether one is technically due yet)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    buf = _build_full_backup_bytes()
    stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"{BACKUP_AUTO_FILE_PREFIX}{stamp}.zip")
    with open(dest, "wb") as f:
        f.write(buf.read())

    retention = _clamp_backup_retention(_settings_map().get("backup_auto_retention"))
    existing = _auto_backup_files()
    while len(existing) > retention:
        oldest = existing.pop(0)
        try:
            os.remove(oldest)
        except OSError:
            pass

    db.session.add(BackupLog(backup_type="auto", created_by=None))
    db.session.commit()
    return dest


def _run_auto_backup_if_due():
    settings_map = _settings_map()
    frequency = settings_map.get("backup_auto_frequency") or ""
    interval_days = BACKUP_AUTO_FREQUENCIES.get(frequency)
    if not interval_days:
        return  # feature is off

    last = _last_auto_backup_at()
    if last is not None:
        days_since = (datetime.utcnow() - last).total_seconds() / 86400
        if days_since < interval_days:
            return  # not due yet

    _create_auto_backup()


_backup_scheduler_started = False


def _backup_scheduler_loop():
    # Give the app a little time to finish booting/migrating before the
    # first check.
    time.sleep(15)
    while True:
        try:
            with app.app_context():
                _run_auto_backup_if_due()
        except Exception:
            app.logger.exception("Scheduled auto-backup check failed")
        time.sleep(6 * 3600)  # re-check every 6 hours — cheap, and catches
                               # up quickly if the app wasn't running when a
                               # backup was originally due


def _start_backup_scheduler():
    global _backup_scheduler_started
    if _backup_scheduler_started:
        return
    # Avoid a duplicate thread from Flask's debug-mode reloader parent
    # process (irrelevant once packaged — the frozen exe never sets
    # app.debug — but keeps `python app.py` well-behaved too).
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    _backup_scheduler_started = True
    threading.Thread(target=_backup_scheduler_loop, daemon=True).start()


_start_backup_scheduler()


@app.route("/api/backup/auto/run-now", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def run_auto_backup_now():
    path = _create_auto_backup()
    return jsonify({"ok": True, "filename": os.path.basename(path)})


@app.route("/api/backup/auto/list", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def list_auto_backups():
    files = []
    for path in reversed(_auto_backup_files()):  # newest first
        try:
            stat = os.stat(path)
        except OSError:
            continue
        files.append({
            "filename": os.path.basename(path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        })
    return jsonify({"folder": BACKUP_DIR, "files": files})


@app.route("/api/backup/auto/open-folder", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_EDITOR)
def open_auto_backup_folder():
    """Opens the Backup folder in Explorer — only meaningful on the same
    Windows machine the app is running on (which is always true for this
    desktop-style local app), a no-op elsewhere."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if sys.platform == "win32":
        try:
            os.startfile(BACKUP_DIR)  # noqa: S606 — local desktop app, own data folder
        except OSError:
            pass
    return jsonify({"ok": True, "folder": BACKUP_DIR})


@app.route("/api/backup/auto/restore", methods=["POST"])
@require_role(ROLE_ADMIN)
def restore_auto_backup():
    data = request.get_json(force=True) or {}
    filename = data.get("filename") or ""
    # Reject anything that isn't a bare filename — no path traversal via
    # "../", no absolute paths. Restoring is only ever "pick one of the
    # files already listed from BACKUP_DIR", never an arbitrary path.
    if not filename or os.path.basename(filename) != filename:
        return jsonify({"error": "Invalid filename"}), 400
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"error": "That backup file no longer exists"}), 404

    with open(path, "rb") as f:
        ok, error, status = _restore_full_backup_from_fileobj(f)
    if not ok:
        return jsonify({"error": error}), status
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Factory reset — the "Danger Zone" master reset in Settings. Wipes every
# table back to empty, reseeds the same defaults a brand-new install gets,
# deletes uploaded files, and resets config.json (+ this process's in-memory
# copy of it) so the very next request is routed to the first-run setup
# wizard, exactly like a fresh install — no app restart required.
#
# Both reset options require a recent Full Backup export first — in BOTH
# Standalone and Network mode — since this is the one action in the app
# that can genuinely destroy everything with no in-app undo. Network mode
# additionally requires the Admin role via @require_role above; Standalone
# has no login at all, so the backup check is the only gate available there.
# ---------------------------------------------------------------------------

MIN_DAYS_BETWEEN_BACKUP_AND_RESET = 5


def _last_full_backup_at():
    # "auto" (scheduled) backups count exactly the same as a manual "full"
    # export for this purpose — both are a complete, restorable copy of
    # everything, just triggered differently.
    row = (
        BackupLog.query
        .filter(BackupLog.backup_type.in_(("full", "auto")))
        .order_by(BackupLog.created_at.desc())
        .first()
    )
    return row.created_at if row else None


def _backup_recency_status():
    last = _last_full_backup_at()
    days_since = (datetime.utcnow() - last).total_seconds() / 86400 if last else None
    ok = days_since is not None and days_since <= MIN_DAYS_BETWEEN_BACKUP_AND_RESET
    return {
        "last_full_backup_at": last.isoformat() if last else None,
        "days_since": round(days_since, 2) if days_since is not None else None,
        "required_days": MIN_DAYS_BETWEEN_BACKUP_AND_RESET,
        "ok": ok,
    }


@app.route("/api/backup/status", methods=["GET"])
def get_backup_status():
    return jsonify(_backup_recency_status())


@app.route("/api/system/factory-reset", methods=["POST"])
@require_role(ROLE_ADMIN)
def factory_reset():
    data = request.get_json(force=True) or {}
    if data.get("confirm") != "DELETE":
        return jsonify({"error": "Confirmation text must be exactly DELETE"}), 400

    scope = data.get("scope") or "full"
    if scope not in ("full", "data_only"):
        return jsonify({"error": "scope must be 'full' or 'data_only'"}), 400

    status = _backup_recency_status()
    if not status["ok"]:
        if status["last_full_backup_at"] is None:
            msg = ("A Full Backup export is required before you can reset — this install has never had " +
                   "one. Go to Backup & Restore above and click Export Full Backup first.")
        else:
            msg = (f"A Full Backup export is required within the last {MIN_DAYS_BETWEEN_BACKUP_AND_RESET} " +
                   f"days before you can reset — the most recent one was {status['days_since']:.1f} days " +
                   "ago. Export a fresh Full Backup above first.")
        return jsonify({"error": msg, "backup_status": status}), 400

    if scope == "data_only":
        _factory_reset_data_only()
    else:
        _factory_reset_full()

    return jsonify({"ok": True, "scope": scope})


def _factory_reset_full():
    """Wipes everything — including business profile/branding, colors, and
    (in Network mode) user accounts — and resets config.json so the very
    next request is routed back through the first-run setup wizard, exactly
    like a brand-new install. No app restart required."""
    was_server_mode = is_server_mode(APP_CONFIG)
    if was_server_mode and current_user.is_authenticated:
        logout_user()

    db.session.remove()
    db.drop_all()
    db.create_all()
    seed_categories()
    seed_settings()

    for fname in LOGO_FILENAMES:
        path = os.path.join(DATA_DIR, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    for sub in ("receipts", "documents"):
        sub_path = os.path.join(DATA_DIR, sub)
        if os.path.isdir(sub_path):
            shutil.rmtree(sub_path, ignore_errors=True)

    new_config = reset_config(DATA_DIR)
    APP_CONFIG.clear()
    APP_CONFIG.update(new_config)


def _factory_reset_data_only():
    """Wipes properties, transactions, and everything scoped to them
    (accounts, units, tenants, mileage, documents, recurring rules, import
    rules/sessions, reconciliations), and resets categories plus the two
    tax settings to their defaults — but deliberately leaves the business
    profile, branding/logo, colors, and (in Network mode) user logins
    untouched, and never touches config.json, so nobody is logged out or
    sent through the setup wizard. Deletes in child-before-parent order so
    it's safe under real foreign-key enforcement (Postgres), not just
    SQLite's more lenient default."""
    MileageLog.query.delete()
    AccountReconciliation.query.delete()
    Tenant.query.delete()
    PropertyDocument.query.delete()
    RecurringTransaction.query.delete()
    ImportRule.query.delete()
    Transaction.query.delete()
    EmailImportBatch.query.delete()
    EmailImportRule.query.delete()
    ImportSession.query.delete()
    PropertyUnit.query.delete()
    PropertyAccount.query.delete()
    Property.query.delete()
    Category.query.delete()
    db.session.commit()

    seed_categories()
    _set_setting("standard_mileage_rate", DEFAULT_SETTINGS["standard_mileage_rate"])
    _set_setting("form_1099_threshold", DEFAULT_SETTINGS["form_1099_threshold"])

    for sub in ("receipts", "documents"):
        sub_path = os.path.join(DATA_DIR, sub)
        if os.path.isdir(sub_path):
            shutil.rmtree(sub_path, ignore_errors=True)


if __name__ == "__main__":
    init_db()
    print("\nRental Property Manager running at http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
