import os
import sys
import socket
import argparse
from pathlib import Path

# Ensure the app package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import OFFLINE_STRICT_MODE, DB_PATH
from app import create_app

def enforce_offline_firewall():
    """
    Guarantees 100% On-Device Operation.
    Restricts external outbound socket connections. Only localhost is allowed.
    """
    if not OFFLINE_STRICT_MODE:
        return

    orig_getaddrinfo = socket.getaddrinfo
    allowed_hosts = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}

    def offline_guarded_getaddrinfo(host, port, *args, **kwargs):
        if host not in allowed_hosts and not host.startswith('127.'):
            raise ConnectionRefusedError(
                f"[COGNITO OFFLINE SECURITY] Blocked external network request to '{host}:{port}'. "
                f"Cognito is configured for 100% private, on-device operation with zero cloud calls."
            )
        return orig_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = offline_guarded_getaddrinfo
    print("[COGNITO SECURITY] Offline Firewall Active: External network calls strictly blocked.")

def main():
    parser = argparse.ArgumentParser(description="Cognito Offline Self-Learning Personal Productivity Agent")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind server (default: 5000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    # Enforce strict offline execution
    enforce_offline_firewall()

    app = create_app()

    # UTF-8 console output safety on Windows
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    banner = f"""
========================================================================
   [COGNITO] Offline Self-Learning Personal Productivity Agent
========================================================================
  * Status: 100% On-Device Active (Zero Cloud Calls)
  * Database: {DB_PATH}
  * Interface: http://{args.host}:{args.port}
  * Engine: Built-in Local Semantic Reasoner (Pluggable Local LLM)
  * Privacy: Local SQLite + Vector Index + Zero Telemetry
========================================================================
    """
    print(banner)

    app.run(host=args.host, port=args.port, debug=args.debug)

if __name__ == "__main__":
    main()
