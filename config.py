"""App-level configuration: standalone vs. server mode.

A small JSON file in the data directory records how this install is meant
to run. Standalone mode (the default, and everything the app has always
done) needs nothing here — it's SQLite, no login, just works. Server mode
points at a shared Postgres database and turns on login/roles.

This module deliberately has no Flask/SQLAlchemy imports so it can be
loaded very early (before the Flask app or the database engine exist) to
decide what connection string to use in the first place.
"""
import json
import os
import secrets

CONFIG_FILENAME = "config.json"

DEFAULT_CONFIG = {
    "mode": "standalone",  # or "server"
    "database_url": None,   # required when mode == "server", e.g. postgresql://user:pass@host:5432/dbname
    "secret_key": None,      # auto-generated on first run if missing
    # Gates the first-run setup wizard. False only on a genuinely fresh
    # install (no config.json yet) — see the grandfather logic below.
    "setup_complete": False,
    # Set by the setup wizard when server mode is chosen, and consumed once
    # (then cleared) the next time the app starts up already connected to
    # the new database. See app.py's _create_pending_admin_if_needed().
    "pending_admin_username": None,
    "pending_admin_email": None,
    "pending_admin_password_hash": None,
}


def _config_path(data_dir):
    return os.path.join(data_dir, CONFIG_FILENAME)


def load_config(data_dir):
    """Load config.json from data_dir, creating a default standalone config
    the first time this runs. Always returns a fully-populated dict (never
    partial), and persists a freshly generated secret_key if one is missing
    so it stays stable across restarts."""
    os.makedirs(data_dir, exist_ok=True)
    path = _config_path(data_dir)

    file_existed = os.path.exists(path)
    file_had_setup_key = False

    config = dict(DEFAULT_CONFIG)
    if file_existed:
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                config.update(loaded)
                file_had_setup_key = "setup_complete" in loaded
        except (json.JSONDecodeError, OSError):
            pass  # fall back to defaults rather than crash on a corrupt file

    changed = False
    if not config.get("secret_key"):
        config["secret_key"] = secrets.token_hex(32)
        changed = True
    if config.get("mode") not in ("standalone", "server"):
        config["mode"] = "standalone"
        changed = True

    # Grandfather in anyone who was already running this app before the
    # setup wizard existed (Phase 1 installs) — they already implicitly
    # chose standalone mode just by using it, so don't force them through a
    # wizard on their next launch.
    if file_existed and not file_had_setup_key:
        config["setup_complete"] = True
        changed = True

    if changed or not file_existed:
        save_config(data_dir, config)

    return config


def save_config(data_dir, config):
    os.makedirs(data_dir, exist_ok=True)
    path = _config_path(data_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def reset_config(data_dir):
    """Writes a brand-new default config to disk — standalone mode, a
    freshly generated secret key, and setup_complete cleared — and returns
    it. Used by the app's factory-reset feature so the very next request
    (no restart required) sees this install as if it were newly installed
    and gets routed back through the first-run setup wizard."""
    fresh = dict(DEFAULT_CONFIG)
    fresh["secret_key"] = secrets.token_hex(32)
    save_config(data_dir, fresh)
    return fresh


def get_database_url(config, sqlite_path):
    """Resolve the SQLAlchemy connection string for the current mode."""
    if config.get("mode") == "server" and config.get("database_url"):
        return config["database_url"]
    return f"sqlite:///{sqlite_path}"


def is_server_mode(config):
    return config.get("mode") == "server" and bool(config.get("database_url"))


def build_postgres_url(host, port, dbname, user, password):
    from urllib.parse import quote_plus
    user_enc = quote_plus(user or "")
    pass_enc = quote_plus(password or "")
    port = port or 5432
    return f"postgresql+psycopg2://{user_enc}:{pass_enc}@{host}:{port}/{dbname}"


def test_database_connection(database_url, timeout=5):
    """Pre-flight connectivity check used by the setup wizard, using a
    throwaway engine that's disposed immediately afterward. This lets us
    validate a Postgres connection *before* committing to it in config.json
    — the running process keeps using its current (SQLite) engine either
    way; switching engines requires a restart (see app.py)."""
    from sqlalchemy import create_engine, text
    engine = None
    try:
        engine = create_engine(database_url, connect_args={"connect_timeout": timeout})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - surface any failure reason to the wizard
        return False, str(exc)
    finally:
        if engine is not None:
            engine.dispose()
