import json
import hashlib
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, Response
from app.database import get_db, utc_now_iso
from app.models import TaskModel, NoteModel, MemoryModel, MemoryDecisionLogModel, FeedbackModel, SettingsModel
from app.vector_store import global_vector_store
from app.reasoning.ollama_adapter import OllamaAdapter

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

    serialized = json.dumps(backup_payload, ensure_ascii=False, indent=2)
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    backup_payload["checksum_sha256"] = checksum

    filename = f"cognito_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json.dumps(backup_payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )

@api_backup_bp.route("/backup/import", methods=["POST"])
def import_backup():
    """Restores data from a JSON backup file and rebuilds vector store."""
    data = request.get_json()
    if not data or "data" not in data:
        return jsonify({"success": False, "error": "Invalid backup file format"}), 400

    payload_data = data["data"]
    restored_counts = {}

    with get_db() as conn:
        cur = conn.cursor()

        # Restore tasks
        if "tasks" in payload_data:
            cur.execute("DELETE FROM tasks")
            for t in payload_data["tasks"]:
                cur.execute("""
                    INSERT INTO tasks (id, title, description, priority, status, tags, due_date, subtasks, ai_suggestion_context, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (t["id"], t["title"], t.get("description", ""), t.get("priority", "medium"),
                      t.get("status", "todo"), t.get("tags", ""), t.get("due_date"),
                      t.get("subtasks", "[]") if isinstance(t.get("subtasks"), str) else json.dumps(t.get("subtasks", [])),
                      t.get("ai_suggestion_context") if isinstance(t.get("ai_suggestion_context"), str) else json.dumps(t.get("ai_suggestion_context")),
                      t.get("created_at", utc_now_iso()), t.get("updated_at", utc_now_iso())))
            restored_counts["tasks"] = len(payload_data["tasks"])

        # Restore notes
        if "notes" in payload_data:
            cur.execute("DELETE FROM notes")
            for n in payload_data["notes"]:
                cur.execute("""
                    INSERT INTO notes (id, title, content, tags, pinned, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (n["id"], n["title"], n["content"], n.get("tags", ""), n.get("pinned", 0),
                      n.get("created_at", utc_now_iso()), n.get("updated_at", utc_now_iso())))
            restored_counts["notes"] = len(payload_data["notes"])

        # Restore memories
        if "memories" in payload_data:
            cur.execute("DELETE FROM memories")
            for m in payload_data["memories"]:
                cur.execute("""
                    INSERT INTO memories (id, category, content, confidence_weight, status, source_context, superseded_by, update_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (m["id"], m["category"], m["content"], m.get("confidence_weight", 0.7),
                      m.get("status", "active"), m.get("source_context", ""), m.get("superseded_by"),
                      m.get("update_count", 0), m.get("created_at", utc_now_iso()), m.get("updated_at", utc_now_iso())))
            restored_counts["memories"] = len(payload_data["memories"])

        # Restore decision logs
        if "decision_logs" in payload_data:
            cur.execute("DELETE FROM memory_decision_logs")
            for l in payload_data["decision_logs"]:
                cur.execute("""
                    INSERT INTO memory_decision_logs (id, memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (l["id"], l["memory_id"], l["action"], l["reasoning"], l.get("previous_content"),
                      l.get("new_content"), l.get("triggered_by", "system"), l["timestamp"]))
            restored_counts["decision_logs"] = len(payload_data["decision_logs"])

        # Restore feedback events
        if "feedback_events" in payload_data:
            cur.execute("DELETE FROM feedback_events")
            for f in payload_data["feedback_events"]:
                cur.execute("""
                    INSERT INTO feedback_events (id, suggestion_type, item_id, memory_id_applied, original_suggestion, user_action, correction_detail, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (f["id"], f["suggestion_type"], f.get("item_id"), f.get("memory_id_applied"),
                      f["original_suggestion"], f["user_action"], f.get("correction_detail"), f["timestamp"]))
            restored_counts["feedback_events"] = len(payload_data["feedback_events"])

    # Rebuild vector store from restored items
    global_vector_store.documents = {}
    for m in MemoryModel.list_active():
        global_vector_store.index_document(m["id"], "memory", m["content"], {"confidence_weight": m["confidence_weight"]}, auto_rebuild=False)
    for n in NoteModel.list_all():
        global_vector_store.index_document(n["id"], "note", f"{n['title']}\n{n['content']}", auto_rebuild=False)
    for t in TaskModel.list_all():
        global_vector_store.index_document(t["id"], "task", f"{t['title']} {t.get('description', '')}", auto_rebuild=False)
    global_vector_store.rebuild_idf()
    global_vector_store.save()

    return jsonify({"success": True, "restored": restored_counts})

@api_backup_bp.route("/settings", methods=["GET"])
def get_settings():
    settings = SettingsModel.get_all()
    ollama_url = settings.get("ollama_url", "http://127.0.0.1:11434")
    ollama_available = OllamaAdapter(base_url=ollama_url).is_available()
    
    return jsonify({
        "success": True,
        "settings": settings,
        "ollama_status": {
            "configured_url": ollama_url,
            "is_reachable": ollama_available
        },
        "privacy": {
            "offline_enforced": True,
            "cloud_calls": 0,
            "telemetry": "Disabled"
        }
    })

@api_backup_bp.route("/settings", methods=["POST"])
def update_settings():
    data = request.get_json() or {}
    for key, val in data.items():
        SettingsModel.set(key, str(val))
    return jsonify({"success": True, "settings": SettingsModel.get_all()})
