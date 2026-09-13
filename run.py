import os
import sys
import socket
import argparse
from pathlib import Path

# Ensure the app package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import OFFLINE_STRICT_MODE, DB_PATH
from app.security import validate_bind_host
from app import create_app

import ipaddress

def is_loopback_host(host: str) -> bool:
    """
    Validates whether a target hostname or IP address is strictly a local loopback destination.
    Uses ipaddress module rather than prefix heuristics to prevent prefix bypasses.
    """
    if not host or not isinstance(host, str):
        return False
    norm = host.strip("[]").lower()
    if norm in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(norm)
        return ip.is_loopback
    except ValueError:
        return False

def enforce_offline_firewall():
    """
    Defense-in-depth guard restricting outbound socket lookups to localhost.
    Note: Application-level monkey-patching, not an OS-level firewall.
    """
    if not OFFLINE_STRICT_MODE:
        return

    orig_getaddrinfo = socket.getaddrinfo

    def offline_guarded_getaddrinfo(host, port, *args, **kwargs):
        if not is_loopback_host(host):
            raise ConnectionRefusedError(
                f"[COGNITO OFFLINE SECURITY] Blocked external network request to '{host}:{port}'. "
                f"Cognito is configured for 100% private, on-device operation with zero cloud calls."
            )
        return orig_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = offline_guarded_getaddrinfo
    print("[COGNITO SECURITY] Offline Network Guard (defense-in-depth): Outbound socket lookups restricted to localhost.")

def main():
    parser = argparse.ArgumentParser(description="Cognito Offline Self-Learning Personal Productivity Agent")
    parser.add_argument("--port", type=int, default=5000, help="Port to bind server (default: 5000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    # Validate bind host in strict offline mode
    try:
        validated_host = validate_bind_host(args.host, strict_mode=OFFLINE_STRICT_MODE)
    except ValueError as e:
        print(f"\n[COGNITO SECURITY ERROR] {e}\n", file=sys.stderr)
        print(
            "[COGNITO SECURITY] In strict offline mode (OFFLINE_STRICT_MODE=True), "
            "binding to 0.0.0.0, LAN, or public interfaces is strictly prohibited to prevent external network exposure. "
            "The server must bind exclusively to loopback (127.0.0.1, localhost, or ::1).\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Enforce strict offline execution (defense-in-depth)
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
