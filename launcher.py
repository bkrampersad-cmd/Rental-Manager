"""Desktop / server entry point.

Standalone mode: starts a local-only server and opens it in the browser —
this is the file PyInstaller freezes into RentalManager.exe, and the plain
Flask dev server is fine here since nothing is exposed beyond localhost.

Server mode: starts under Waitress (a production-grade pure-Python WSGI
server that needs no extra system dependencies on Windows) bound to all
network interfaces, and stays running unattended rather than opening a
browser — it's meant to be left running on a server machine, kept alive via
Windows Task Scheduler or NSSM (see the deployment guide).
"""
import os
import socket
import sys
import tempfile
import threading
import time
import webbrowser

import app as flask_app
from config import is_server_mode


def find_open_port(preferred=5000, tries=15):
    for i in range(tries):
        port = preferred + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


# Kept alive for the life of the process — closing this handle (e.g. by
# letting it get garbage collected) would release the lock early.
_single_instance_mutex = None
_SINGLE_INSTANCE_MUTEX_NAME = "Global\\RentalPropertyManager_SingleInstance"


def _is_first_instance():
    """True if this is the only copy of the app currently running, and
    holds a Windows named-mutex lock for the rest of this process's life.
    False means another copy is already running.

    Always True on non-Windows or if pywin32 isn't available — this is a
    Standalone-mode, packaged-.exe concern only: launching the app a second
    time while it's still running (easy to do by accident once its console
    window is hidden — see _run_hidden_with_tray) would otherwise start a
    second server thread, with its own scheduler threads, against the same
    SQLite database file at once, risking "database is locked" errors and
    duplicate scheduled work (auto-backups, email auto-import) rather than
    a clean failure."""
    global _single_instance_mutex
    if sys.platform != "win32":
        return True
    try:
        import win32api
        import win32event
        import winerror
    except ImportError:
        return True
    _single_instance_mutex = win32event.CreateMutex(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS


def _port_lock_path():
    return os.path.join(tempfile.gettempdir(), "rental_property_manager.port")


def _run_standalone():
    if not _is_first_instance():
        # Already running elsewhere — reopen the browser to that instance
        # instead of starting a second server against the same database.
        port = 5000
        try:
            with open(_port_lock_path()) as f:
                port = int(f.read().strip())
        except (OSError, ValueError):
            pass
        print("Rental Property Manager is already running — opening it in your browser.")
        webbrowser.open(f"http://127.0.0.1:{port}")
        return

    flask_app.init_db()
    port = find_open_port(5000)
    try:
        with open(_port_lock_path(), "w") as f:
            f.write(str(port))
    except OSError:
        pass  # non-fatal — worst case, a duplicate launch falls back to guessing port 5000

    server_thread = threading.Thread(
        target=lambda: flask_app.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    url = f"http://127.0.0.1:{port}"
    print(f"\nRental Property Manager is running at {url}")
    print("Opening in your browser... you can close this window to stop the app.\n")

    # Give the server a moment to bind before we launch the browser tab.
    time.sleep(1.2)
    webbrowser.open(url)

    if _run_hidden_with_tray(url):
        return  # tray icon took over; it only returns once "Quit" is chosen, which exits the process itself

    # Fallback — used for `python launcher.py`/run.bat (not frozen, so a
    # developer still wants to see console output) and, defensively, if the
    # tray icon couldn't be set up for any reason on the frozen .exe. Either
    # way this keeps the original, always-safe behavior: a visible console
    # window that closing (or Ctrl+C) stops the app.
    try:
        server_thread.join()
    except KeyboardInterrupt:
        sys.exit(0)


def _run_hidden_with_tray(url):
    """Only takes over for the frozen Windows .exe in Standalone mode: hides
    the console window and shows a small system tray icon (Open / Quit)
    instead, since a hidden console can no longer be closed to stop the
    app — the tray icon's Quit item replaces that.

    Returns False immediately (touching nothing — the console stays visible
    and untouched) if this isn't the frozen Windows build, or if pystray/the
    icon file aren't available for any reason, so a missing dependency can
    never leave the app hidden with no way to stop it. Only ever hides the
    console once the tray icon is confirmed up and running.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        import ctypes
        import pystray
        from PIL import Image
    except ImportError:
        return False

    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icon_path = os.path.join(base_dir, "assets", "app_icon.ico")
    try:
        image = Image.open(icon_path)
    except Exception:
        return False

    def on_open(icon_obj, item):
        webbrowser.open(url)

    def on_quit(icon_obj, item):
        icon_obj.stop()
        os._exit(0)  # hard exit — the Flask dev server thread has no graceful stop hook to call instead

    try:
        menu = pystray.Menu(
            pystray.MenuItem("Open Rental Property Manager", on_open, default=True),
            pystray.MenuItem("Quit", on_quit),
        )
        icon = pystray.Icon("RentalPropertyManager", image, "Rental Property Manager", menu)
    except Exception:
        return False

    # Only now — with the tray icon built and about to run — hide the
    # console, so there's always a way to interact with/stop the app.
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass

    icon.run()  # blocks on the main thread until Quit calls icon.stop()
    return True


def _run_server():
    from waitress import serve

    flask_app.init_db()
    port = int(os.environ.get("RENTAL_MANAGER_PORT", "5000"))

    print("\nRental Property Manager — Server Mode")
    print(f"Listening on http://0.0.0.0:{port} (reachable from other computers on your network)")
    print("Leave this running. To stop, close this window or press Ctrl+C.\n")

    try:
        serve(flask_app.app, host="0.0.0.0", port=port, threads=8)
    except KeyboardInterrupt:
        sys.exit(0)


def main():
    if is_server_mode(flask_app.APP_CONFIG):
        _run_server()
    else:
        _run_standalone()


if __name__ == "__main__":
    main()
