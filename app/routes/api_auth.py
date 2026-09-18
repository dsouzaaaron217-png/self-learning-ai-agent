from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session
from app.auth import (
    is_auth_configured,
    setup_password,
    change_password,
    verify_password,
    generate_csrf_token,
    get_auth_version,
    login_rate_limiter,
    MIN_PASSWORD_LENGTH
)
from app.validation import parse_and_validate_json

api_auth_bp = Blueprint("api_auth", __name__, url_prefix="/api/auth")

@api_auth_bp.route("/status", methods=["GET"])
def auth_status():
    """Returns whether password is set and whether the current session is authenticated."""
    if "csrf_token" not in session:
        session["csrf_token"] = generate_csrf_token()

    is_configured = is_auth_configured()
    is_authenticated = bool(session.get("authenticated"))

    if is_authenticated:
        now_ts = datetime.now(timezone.utc).timestamp()
        last_activity = session.get("last_activity")
        if last_activity is not None and (now_ts - last_activity > 7200):
            session.clear()
            session["csrf_token"] = generate_csrf_token()
            is_authenticated = False
        elif session.get("auth_version", 1) != get_auth_version():
            session.clear()
            session["csrf_token"] = generate_csrf_token()
            is_authenticated = False

    return jsonify({
        "success": True,
        "initialized": is_configured,
        "authenticated": is_authenticated,
        "csrf_token": session.get("csrf_token")
    })

@api_auth_bp.route("/setup", methods=["POST"])
def setup():
    """Initial first-run setup to create the local master password."""
    if is_auth_configured():
        return jsonify({"success": False, "error": "Cognito master password has already been configured."}), 400

    try:
        data = parse_and_validate_json(request)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    password = data.get("password")
    confirm = data.get("confirm_password")

    if not isinstance(password, str) or not password:
        return jsonify({"success": False, "error": "Password is required."}), 400

    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify({
            "success": False,
            "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        }), 400

    if password != confirm:
        return jsonify({"success": False, "error": "Passwords do not match."}), 400

    try:
        setup_password(password)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Automatically log the user in upon successful setup
    session["authenticated"] = True
    session["auth_version"] = get_auth_version()
    session["csrf_token"] = generate_csrf_token()
    session["last_activity"] = datetime.now(timezone.utc).timestamp()
    session.permanent = True

    return jsonify({
        "success": True,
        "message": "Master password created successfully.",
        "csrf_token": session["csrf_token"]
    }), 201

@api_auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate with local master password and establish session."""
    client_id = request.remote_addr or "127.0.0.1"
    remaining = login_rate_limiter.get_lockout_remaining(client_id)
    if remaining > 0:
        resp = jsonify({
            "success": False,
            "error": f"Too many failed login attempts. Please try again in {remaining} seconds."
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(remaining)
        return resp

    if not is_auth_configured():
        return jsonify({
            "success": False,
            "error": "Application is not initialized. Please complete initial password setup."
        }), 400

    try:
        data = parse_and_validate_json(request)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    password = data.get("password")
    if not isinstance(password, str) or not password:
        return jsonify({"success": False, "error": "Password is required."}), 400

    if not verify_password(password):
        locked_out, count_or_secs = login_rate_limiter.record_failure(client_id)
        if locked_out:
            resp = jsonify({
                "success": False,
                "error": f"Too many failed login attempts. Account temporarily locked for {count_or_secs} seconds."
            })
            resp.status_code = 429
            resp.headers["Retry-After"] = str(count_or_secs)
            return resp
        return jsonify({"success": False, "error": "Invalid password."}), 401

    login_rate_limiter.record_success(client_id)
    session["authenticated"] = True
    session["auth_version"] = get_auth_version()
    session["csrf_token"] = generate_csrf_token()
    session["last_activity"] = datetime.now(timezone.utc).timestamp()
    session.permanent = True

    return jsonify({
        "success": True,
        "message": "Authenticated successfully.",
        "csrf_token": session["csrf_token"]
    })

@api_auth_bp.route("/change-password", methods=["POST"])
def change_password_route():
    """Rotate master password, invalidating other sessions."""
    try:
        data = parse_and_validate_json(request)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    current_password = data.get("current_password")
    new_password = data.get("new_password")
    confirm_password = data.get("confirm_password")

    if not isinstance(current_password, str) or not current_password:
        return jsonify({"success": False, "error": "Current password is required."}), 400

    if not isinstance(new_password, str) or not new_password:
        return jsonify({"success": False, "error": "New password is required."}), 400

    if len(new_password) < MIN_PASSWORD_LENGTH:
        return jsonify({
            "success": False,
            "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        }), 400

    if new_password != confirm_password:
        return jsonify({"success": False, "error": "Passwords do not match."}), 400

    if not verify_password(current_password):
        return jsonify({"success": False, "error": "Current password is incorrect."}), 401

    try:
        new_version = change_password(current_password, new_password)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    session["auth_version"] = new_version
    session["csrf_token"] = generate_csrf_token()
    session["last_activity"] = datetime.now(timezone.utc).timestamp()

    return jsonify({
        "success": True,
        "message": "Password changed successfully.",
        "csrf_token": session["csrf_token"]
    })

@api_auth_bp.route("/logout", methods=["POST"])
def logout():
    """Invalidates the authenticated session."""
    session.clear()
    session["csrf_token"] = generate_csrf_token()
    return jsonify({"success": True, "message": "Logged out successfully."})
