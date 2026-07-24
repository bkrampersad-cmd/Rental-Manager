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


def _run_standalone():
    flask_app.init_db()
    port = find_open_port(5000)

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

    try:
        server_thread.join()
    except KeyboardInterrupt:
        sys.exit(0)


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
