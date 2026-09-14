import json
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, Response
from app.database import get_db, utc_now_iso
from app.models import TaskModel, NoteModel, MemoryModel, MemoryDecisionLogModel, FeedbackModel, SettingsModel
from app import vector_store
from app.reasoning.ollama_adapter import OllamaAdapter
from app.config import OFFLINE_STRICT_MODE
from app.security import validate_ollama_url

api_backup_bp = Blueprint("api_backup", __name__, url_prefix="/api")

@api_backup_bp.route("/backup/export", methods=["GET"])
def export_backup():
    """Generates a comprehensive JSON backup of all user data and memory history."""
    with get_db() as conn:
        cur = conn.cursor()
        
        cur.execute("SELECT * FROM tasks")
        tasks = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM notes")
        notes = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM memories")
        memories = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM memory_decision_logs")
        decision_logs = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM feedback_events")
        feedback_events = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT * FROM settings")
        settings = {r[0]: r[1] for r in cur.fetchall()}

    backup_payload = {
        "version": "1.2.0",
        "exported_at": utc_now_iso(),
        "application": "Cognito Offline Agent",
        "data": {
            "tasks": tasks,
            "notes": notes,
            "memories": memories,
            "decision_logs": decision_logs,
            "feedback_events": feedback_events,
            "settings": settings
        }
    }

    from app.validation import compute_backup_checksum, validate_backup_payload
    checksum = compute_backup_checksum(backup_payload["data"])
    backup_payload["checksum_sha256"] = checksum

    filename = f"cognito_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json.dumps(backup_payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

from app.validation import parse_and_validate_json, ALLOWED_SETTINGS_KEYS, validate_backup_payload

@api_backup_bp.route("/backup/import", methods=["POST"])
def import_backup():
    """Restores data from a JSON backup file and rebuilds vector store."""
    try:
        data = parse_and_validate_json(request)
        validated_payload = validate_backup_payload(data)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    payload_data = validated_payload["data"]

    # Pre-restore safety snapshot of active SQLite database
    from app.database_snapshot import create_snapshot
    try:
        create_snapshot()
    except Exception as snap_err:
        return jsonify({
            "success": False,
            "error": f"Failed to create pre-restore safety snapshot: {snap_err}"
        }), 500

    restored_counts = {}

    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA defer_foreign_keys = ON")

            # Reverse dependency order deletion
            if "feedback_events" in payload_data:
                cur.execute("DELETE FROM feedback_events")
            if "decision_logs" in payload_data:
                cur.execute("DELETE FROM memory_decision_logs")
            if "memories" in payload_data:
                cur.execute("DELETE FROM memories")
            if "notes" in payload_data:
                cur.execute("DELETE FROM notes")
            if "tasks" in payload_data:
                cur.execute("DELETE FROM tasks")

            # Dependency order insertion
            if "tasks" in payload_data:
                for t in payload_data["tasks"]:
                    cur.execute("""
                        INSERT INTO tasks (id, title, description, priority, status, tags, due_date, subtasks, ai_suggestion_context, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (t["id"], t["title"], t.get("description", ""), t.get("priority", "medium"),
                          t.get("status", "todo"), t.get("tags", ""), t.get("due_date"),
                          t.get("subtasks", "[]") if isinstance(t.get("subtasks"), str) else json.dumps(t.get("subtasks", [])),
                          t.get("ai_suggestion_context") if isinstance(t.get("ai_suggestion_context"), str) else (json.dumps(t.get("ai_suggestion_context")) if t.get("ai_suggestion_context") is not None else None),
                          t.get("created_at", utc_now_iso()), t.get("updated_at", utc_now_iso())))
                restored_counts["tasks"] = len(payload_data["tasks"])

            if "notes" in payload_data:
                for n in payload_data["notes"]:
                    cur.execute("""
                        INSERT INTO notes (id, title, content, tags, pinned, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (n["id"], n["title"], n["content"], n.get("tags", ""), int(n.get("pinned", 0)),
                          n.get("created_at", utc_now_iso()), n.get("updated_at", utc_now_iso())))
                restored_counts["notes"] = len(payload_data["notes"])

            if "memories" in payload_data:
                for m in payload_data["memories"]:
                    cur.execute("""
                        INSERT INTO memories (id, category, content, confidence_weight, status, source_context, superseded_by, update_count, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (m["id"], m["category"], m["content"], float(m.get("confidence_weight", 0.7)),
                          m.get("status", "active"), m.get("source_context", ""), m.get("superseded_by"),
                          int(m.get("update_count", 0)), m.get("created_at", utc_now_iso()), m.get("updated_at", utc_now_iso())))
                restored_counts["memories"] = len(payload_data["memories"])

            if "decision_logs" in payload_data:
                for l in payload_data["decision_logs"]:
                    cur.execute("""
                        INSERT INTO memory_decision_logs (id, memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (l["id"], l["memory_id"], l["action"], l["reasoning"], l.get("previous_content"),
                          l.get("new_content"), l.get("triggered_by", "system"), l["timestamp"]))
                restored_counts["decision_logs"] = len(payload_data["decision_logs"])

            if "feedback_events" in payload_data:
                for f in payload_data["feedback_events"]:
                    cur.execute("""
                        INSERT INTO feedback_events (id, suggestion_type, item_id, memory_id_applied, original_suggestion, user_action, correction_detail, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (f["id"], f["suggestion_type"], f.get("item_id"), f.get("memory_id_applied"),
                          f["original_suggestion"], f["user_action"], f.get("correction_detail"), f["timestamp"]))
                restored_counts["feedback_events"] = len(payload_data["feedback_events"])

            if "settings" in payload_data:
                for key, val in payload_data["settings"].items():
                    cur.execute("""
                        INSERT INTO settings (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """, (key, str(val)))
                restored_counts["settings"] = len(payload_data["settings"])

    except Exception as db_err:
        return jsonify({
            "success": False,
            "sqlite_restored": False,
            "error": f"Database import transaction failed: {db_err}"
        }), 500

    # Vector store reconciliation via Phase 3B/3C mechanisms
    vector_synced = True
    try:
        vector_store.global_vector_store.rebuild_from_db()
        vector_store.global_vector_store.ensure_valid_index()
    except Exception as vs_err:
        vector_synced = False
        print(f"[BackupImport] Warning: vector store synchronization failed post-import: {vs_err}. SQLite remains authoritative.")

    return jsonify({
        "success": vector_synced,
        "sqlite_restored": True,
        "vector_synced": vector_synced,
        "restored": restored_counts,
        "error": None if vector_synced else "SQLite imported successfully, but vector store synchronization failed."
    }), 200

@api_backup_bp.route("/settings", methods=["GET"])
def get_settings():
    settings = SettingsModel.get_all()
    ollama_url = settings.get("ollama_url", "http://127.0.0.1:11434")
    try:
        validated_url = validate_ollama_url(ollama_url, strict_mode=OFFLINE_STRICT_MODE)
        ollama_available = OllamaAdapter(base_url=validated_url).is_available()
    except ValueError:
        ollama_available = False

    return jsonify({
        "success": True,
        "settings": settings,
        "ollama_status": {
            "configured_url": ollama_url,
            "is_reachable": ollama_available
        },
        "privacy": {
            "offline_enforced": OFFLINE_STRICT_MODE,
            "cloud_calls": 0,
            "telemetry": "Disabled"
        }
    })

@api_backup_bp.route("/settings", methods=["POST"])
def update_settings():
    try:
        data = parse_and_validate_json(request)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # Strict check: only permitted settings keys
    for key in data.keys():
        if key not in ALLOWED_SETTINGS_KEYS:
            return jsonify({
                "success": False,
                "error": f"Setting key '{key}' is not permitted. Allowed: {', '.join(sorted(ALLOWED_SETTINGS_KEYS))}."
            }), 400

    if "engine" in data:
        engine = str(data["engine"]).strip().lower()
        if engine not in {"local", "ollama"}:
            return jsonify({"success": False, "error": "Invalid engine value. Must be 'local' or 'ollama'."}), 400
        data["engine"] = engine

    # Validate Ollama URL using the centralized security validator
    if "ollama_url" in data:
        raw_url = str(data["ollama_url"]).strip()
        try:
            validated_url = validate_ollama_url(raw_url, strict_mode=OFFLINE_STRICT_MODE)
            data["ollama_url"] = validated_url
        except ValueError as e:
            return jsonify({
                "success": False,
                "error": f"Invalid Ollama URL: {e}"
            }), 400

    for key, val in data.items():
        SettingsModel.set(key, str(val))

    return jsonify({"success": True, "settings": SettingsModel.get_all()})
