import os
import json
import hmac
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from werkzeug.security import generate_password_hash, check_password_hash
from app.config import DATA_DIR

AUTH_FILE_PATH = DATA_DIR / "auth.json"
MIN_PASSWORD_LENGTH = 8

def _get_auth_data() -> Optional[Dict[str, Any]]:
    """Reads auth.json if it exists."""
    if not AUTH_FILE_PATH.exists():
        return None
    try:
        with open(AUTH_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_auth_data(data: Dict[str, Any]) -> None:
    """Saves auth.json with restrictive permissions."""
    AUTH_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = AUTH_FILE_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    # Restrict permissions on POSIX systems where supported
    if os.name == "posix":
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass

    temp_path.replace(AUTH_FILE_PATH)

def is_auth_configured() -> bool:
    """Returns True if a master password has already been configured."""
    data = _get_auth_data()
    return bool(data and data.get("password_hash"))

def get_or_create_secret_key() -> str:
    """
    Returns the Flask session secret key.
    Loads from auth.json, or creates a strong random 256-bit token.
    Can be overridden via COGNITO_SECRET_KEY environment variable.
    """
    env_secret = os.getenv("COGNITO_SECRET_KEY")
    if env_secret:
        return env_secret

    data = _get_auth_data() or {}
    secret_key = data.get("secret_key")
    if not secret_key:
        secret_key = secrets.token_hex(32)
        data["secret_key"] = secret_key
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _save_auth_data(data)

    return secret_key

def setup_password(password: str) -> None:
    """
    Sets the initial master password.
    Rejects passwords shorter than MIN_PASSWORD_LENGTH.
    Stores only a strong scrypt/werkzeug password hash.
    """
    if is_auth_configured():
        raise ValueError("Password is already configured.")

    if not isinstance(password, str) or len(password.strip()) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    data = _get_auth_data() or {}
    if "secret_key" not in data:
        data["secret_key"] = secrets.token_hex(32)

    data["password_hash"] = generate_password_hash(password.strip())
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    _save_auth_data(data)

def verify_password(password: str) -> bool:
    """
    Verifies a supplied password against the stored password hash.
    Uses constant-time comparison in check_password_hash.
    """
    if not isinstance(password, str) or not password:
        return False

    data = _get_auth_data()
    if not data or not data.get("password_hash"):
        return False

    return check_password_hash(data["password_hash"], password)

def generate_csrf_token() -> str:
    """Generates a cryptographically secure random token for CSRF protection."""
    return secrets.token_hex(32)

def validate_csrf_token(session_token: Optional[str], submitted_token: Optional[str]) -> bool:
    """
    Validates submitted CSRF token against session token using constant-time comparison.
    """
    if not session_token or not submitted_token:
        return False
    if not isinstance(session_token, str) or not isinstance(submitted_token, str):
        return False
    return hmac.compare_digest(session_token, submitted_token)
