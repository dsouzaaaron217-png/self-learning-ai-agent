import os
import json
import hmac
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
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

def get_auth_version() -> int:
    """Returns the current password/auth version integer from auth.json (default 1)."""
    data = _get_auth_data()
    if not data:
        return 1
    return int(data.get("password_version", 1))

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
    data["password_version"] = 1
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_auth_data(data)

def change_password(current_password: str, new_password: str) -> int:
    """
    Changes the master password.
    - Constant-time verification of current_password.
    - Enforces existing password validation policy.
    - Increments password_version to invalidate other active sessions.
    - Updates updated_at.
    - Returns the new password_version.
    """
    if not is_auth_configured():
        raise ValueError("Application password is not configured.")

    if not isinstance(current_password, str) or not verify_password(current_password):
        raise ValueError("Current password is incorrect.")

    if not isinstance(new_password, str) or len(new_password.strip()) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    data = _get_auth_data() or {}
    new_version = int(data.get("password_version", 1)) + 1
    data["password_hash"] = generate_password_hash(new_password.strip())
    data["password_version"] = new_version
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_auth_data(data)
    return new_version

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

class LoginRateLimiter:
    """
    In-memory rate limiter for login attempts.
    Guarantees zero persistence of sensitive data and zero external dependencies.
    Applies temporary lockout after repeated failed attempts.
    """
    MAX_ATTEMPTS = 5         # Maximum failed attempts before lockout
    LOCKOUT_DURATION = 60    # Lockout duration in seconds
    WINDOW_SECONDS = 300     # 5-minute rolling window for counting failures

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, lockout_duration: int = LOCKOUT_DURATION, window_seconds: int = WINDOW_SECONDS):
        self.max_attempts = max_attempts
        self.lockout_duration = lockout_duration
        self.window_seconds = window_seconds
        self._failures: Dict[str, List[float]] = {}
        self._lockouts: Dict[str, float] = {}

    def get_lockout_remaining(self, identifier: str, now: Optional[float] = None) -> int:
        """Returns remaining lockout seconds, or 0 if not locked out."""
        now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()
        lockout_until = self._lockouts.get(identifier, 0.0)
        if lockout_until > now_ts:
            return max(1, int(lockout_until - now_ts + 0.999))
        if identifier in self._lockouts:
            del self._lockouts[identifier]
        return 0

    def record_failure(self, identifier: str, now: Optional[float] = None) -> Tuple[bool, int]:
        """
        Records a failed attempt for the identifier.
        Returns (is_locked_out, remaining_seconds_or_failure_count).
        """
        now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()

        # Clean old failures outside window
        window_start = now_ts - self.window_seconds
        attempts = [t for t in self._failures.get(identifier, []) if t > window_start]
        attempts.append(now_ts)
        self._failures[identifier] = attempts

        if len(attempts) >= self.max_attempts:
            self._lockouts[identifier] = now_ts + self.lockout_duration
            return True, self.lockout_duration

        return False, len(attempts)

    def record_success(self, identifier: str) -> None:
        """Clears all failure and lockout records for the identifier upon successful login."""
        self._failures.pop(identifier, None)
        self._lockouts.pop(identifier, None)

    def reset(self) -> None:
        """Resets all tracking state (used in testing and maintenance)."""
        self._failures.clear()
        self._lockouts.clear()

login_rate_limiter = LoginRateLimiter()
