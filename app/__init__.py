from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, session
from app.config import OFFLINE_STRICT_MODE
from app.database import init_db
from app.models import MemoryModel, MemoryDecisionLogModel
from app.memory.pipeline import MemoryPipeline
from app.routes import register_blueprints
from app.auth import (
    get_or_create_secret_key,
    validate_csrf_token,
    get_auth_version,
    generate_csrf_token
)

def create_app() -> Flask:
    """Application factory for Cognito Offline Agent."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static"
    )

    # Session and Request Limits
    app.secret_key = get_or_create_secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False  # Localhost HTTP
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB payload limit

    # Initialize SQLite database and tables
    init_db()

    # Seed baseline starter memories if database is fresh
    _seed_initial_memories()

    # Initialize Vector index
    MemoryPipeline.initialize_default_vector_index()

    # Register API blueprints
    register_blueprints(app)

    # Security Headers
    @app.after_request
    def apply_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
        return response

    # API Authentication & CSRF Protection Hook
    @app.before_request
    def protect_api_routes():
        if request.path.startswith("/api/"):
            public_auth_paths = {"/api/auth/status", "/api/auth/setup", "/api/auth/login"}
            if request.path not in public_auth_paths:
                # 1. Authentication Check
                if not session.get("authenticated"):
                    return jsonify({"success": False, "error": "Authentication required"}), 401

                # 2. Inactivity Check (2 hours = 7200 seconds)
                now_ts = datetime.now(timezone.utc).timestamp()
                last_activity = session.get("last_activity")
                if last_activity is not None and (now_ts - last_activity > 7200):
                    session.clear()
                    session["csrf_token"] = generate_csrf_token()
                    return jsonify({"success": False, "error": "Session expired due to inactivity."}), 401

                # 3. Auth Version Check (Invalidate sessions on password rotation)
                current_version = get_auth_version()
                session_version = session.get("auth_version", 1)
                if session_version != current_version:
                    session.clear()
                    session["csrf_token"] = generate_csrf_token()
                    return jsonify({"success": False, "error": "Session invalidated due to password change. Please log in again."}), 401

                # Update activity timestamp for active authenticated session
                session["last_activity"] = now_ts

                # 4. CSRF Protection for state-changing methods
                if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                    submitted_token = request.headers.get("X-CSRF-Token")
                    session_token = session.get("csrf_token")
                    if not validate_csrf_token(session_token, submitted_token):
                        return jsonify({"success": False, "error": "Invalid or missing CSRF token"}), 403

    # Error Handlers
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"success": False, "error": getattr(e, "description", "Bad Request")}), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify({"success": False, "error": getattr(e, "description", "Authentication required")}), 401

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({"success": False, "error": getattr(e, "description", "Forbidden")}), 403

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": "Resource not found"}), 404
        return "Not Found", 404

    @app.errorhandler(413)
    def payload_too_large(e):
        return jsonify({"success": False, "error": "Request payload exceeds maximum allowed size (10 MB)"}), 413

    @app.errorhandler(500)
    def internal_error(e):
        return jsonify({"success": False, "error": "Internal server error"}), 500

    @app.route("/")
    def index():
        return render_template("index.html")

    return app

def _seed_initial_memories():
    """Seeds baseline productivity habits and decision logs if brand new."""
    existing = MemoryModel.list_all()
    if not existing:
        m1_id = MemoryModel.create(
            category="habit",
            content="User prefers deep work and development tasks before 12:00 PM.",
            confidence_weight=0.85,
            source_context="Initial productivity template",
            status="active"
        )
        MemoryDecisionLogModel.log(
            memory_id=m1_id,
            action="ADD",
            reasoning="Initialized default deep work scheduling preference.",
            triggered_by="system_init"
        )

        m2_id = MemoryModel.create(
            category="preference",
            content="Production bugs and system outages are marked Urgent priority.",
            confidence_weight=0.95,
            source_context="Initial productivity template",
            status="active"
        )
        MemoryDecisionLogModel.log(
            memory_id=m2_id,
            action="ADD",
            reasoning="Standard priority rule for production issues.",
            triggered_by="system_init"
        )
