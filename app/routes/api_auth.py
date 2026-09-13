from flask import Blueprint, request, jsonify, session
from app.auth import (
    is_auth_configured,
    setup_password,
    verify_password,
    generate_csrf_token,
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
    session["csrf_token"] = generate_csrf_token()

    return jsonify({
        "success": True,
        "message": "Master password created successfully.",
        "csrf_token": session["csrf_token"]
    }), 201

@api_auth_bp.route("/login", methods=["POST"])
def login():
    """Authenticate with local master password and establish session."""
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
        return jsonify({"success": False, "error": "Invalid password."}), 401

    session["authenticated"] = True
    session["csrf_token"] = generate_csrf_token()

    return jsonify({
        "success": True,
        "message": "Authenticated successfully.",
        "csrf_token": session["csrf_token"]
    })

@api_auth_bp.route("/logout", methods=["POST"])
def logout():
    """Invalidates the authenticated session."""
    session.clear()
    session["csrf_token"] = generate_csrf_token()
    return jsonify({"success": True, "message": "Logged out successfully."})
