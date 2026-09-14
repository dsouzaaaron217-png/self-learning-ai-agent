import json
import hashlib
import hmac
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from flask import Request

from app.config import OFFLINE_STRICT_MODE
from app.security import validate_ollama_url

# Field limits
MAX_TASK_TITLE_LEN = 200
MAX_TASK_DESC_LEN = 10000
MAX_TAGS_LEN = 500
MAX_NOTE_TITLE_LEN = 200
MAX_NOTE_CONTENT_LEN = 50000
MAX_MEMORY_CONTENT_LEN = 1000
MAX_CHAT_MESSAGE_LEN = 5000
MAX_CHAT_HISTORY_ITEMS = 50
MAX_FEEDBACK_TEXT_LEN = 1000
MAX_PAGINATION_LIMIT = 200

ALLOWED_PRIORITIES = {"low", "medium", "high", "urgent"}
ALLOWED_STATUSES = {"todo", "in_progress", "done"}
ALLOWED_MEMORY_CATEGORIES = {"preference", "habit", "correction", "fact"}
ALLOWED_FEEDBACK_ACTIONS = {"accepted", "corrected", "rejected"}
ALLOWED_SETTINGS_KEYS = {
    "engine",
    "ollama_url",
    "ollama_model",
    "dark_mode",
    "auto_suggest_tasks"
}

SUPPORTED_APPLICATION_NAME = "Cognito Offline Agent"
ALLOWED_MEMORY_STATUSES = {"active", "superseded", "deleted"}
ALLOWED_DECISION_ACTIONS = {"ADD", "UPDATE", "DELETE"}
ALLOWED_ENGINES = {"local", "ollama"}

def compute_backup_checksum(data_subtree: Dict[str, Any]) -> str:
    """
    Computes a deterministic canonical SHA-256 checksum over the 'data' subtree.
    Uses sorted keys, compact separators without whitespace, and utf-8 encoding.
    """
    canonical = json.dumps(
        data_subtree,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _validate_optional_timestamp(ts: Any, field_name: str):
    """Validates an optional ISO 8601 timestamp string if present."""
    if ts is None:
        return
    if not isinstance(ts, str) or not ts.strip():
        raise ValueError(f"{field_name} must be a valid ISO 8601 timestamp string.")
    clean_ts = ts.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(clean_ts)
    except Exception:
        raise ValueError(f"{field_name} contains an invalid timestamp format: '{ts}'.")

def validate_backup_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates a logical backup export payload prior to import:
    - Root dictionary and application header
    - Semantic version
    - Canonical SHA-256 checksum verification
    - Data subtree and entity lists/dicts
    - Record-level required fields, types, enum bounds, lengths, and positive unique IDs
    - Foreign-key integrity across memories, decision logs, and feedback events
    - Settings keys, engine, and Ollama URL validation

    Returns the validated payload dictionary.
    Raises ValueError with a specific, safe error message if validation fails.
    """
    if not isinstance(payload, dict):
        raise ValueError("Backup payload must be a key-value JSON object.")

    app_name = payload.get("application")
    if app_name != SUPPORTED_APPLICATION_NAME:
        raise ValueError(f"Invalid or unsupported application header '{app_name}'. Expected '{SUPPORTED_APPLICATION_NAME}'.")

    version = payload.get("version")
    if not isinstance(version, str) or not version.startswith("1."):
        raise ValueError(f"Unsupported backup version '{version}'. Expected version 1.x.")

    checksum = payload.get("checksum_sha256")
    if not checksum or not isinstance(checksum, str):
        raise ValueError("Backup payload missing required 'checksum_sha256' field.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Invalid backup file format. Expected a 'data' dictionary.")

    # Canonical checksum verification
    expected_checksum = compute_backup_checksum(data)
    if not hmac.compare_digest(checksum.strip().lower(), expected_checksum.lower()):
        raise ValueError("Backup checksum verification failed: payload has been modified or corrupted.")

    # Prevent accidental huge backup payloads
    max_items = 5000
    for entity in ("tasks", "notes", "memories", "decision_logs", "feedback_events"):
        if entity in data:
            if not isinstance(data[entity], list):
                raise ValueError(f"Entity '{entity}' in backup must be a list.")
            if len(data[entity]) > max_items:
                raise ValueError(f"Backup contains too many {entity} (max {max_items}).")

    # 1. Validate Tasks
    task_ids = set()
    for idx, t in enumerate(data.get("tasks", [])):
        if not isinstance(t, dict):
            raise ValueError(f"Task #{idx} must be an object.")
        t_id = t.get("id")
        if not isinstance(t_id, int) or t_id <= 0:
            raise ValueError(f"Task #{idx} must have a positive integer 'id'.")
        if t_id in task_ids:
            raise ValueError(f"Duplicate task id {t_id} detected in backup payload.")
        task_ids.add(t_id)

        title = t.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Task {t_id} missing required 'title' (non-empty string).")
        if len(title.strip()) > MAX_TASK_TITLE_LEN:
            raise ValueError(f"Task {t_id} title exceeds maximum length of {MAX_TASK_TITLE_LEN} characters.")

        desc = t.get("description")
        if desc is not None:
            if not isinstance(desc, str):
                raise ValueError(f"Task {t_id} 'description' must be a string.")
            if len(desc) > MAX_TASK_DESC_LEN:
                raise ValueError(f"Task {t_id} description exceeds maximum length of {MAX_TASK_DESC_LEN} characters.")

        prio = t.get("priority")
        if prio is not None and str(prio).strip().lower() not in ALLOWED_PRIORITIES:
            raise ValueError(f"Task {t_id} has invalid priority '{prio}'. Allowed: {', '.join(sorted(ALLOWED_PRIORITIES))}.")

        stat = t.get("status")
        if stat is not None and str(stat).strip().lower() not in ALLOWED_STATUSES:
            raise ValueError(f"Task {t_id} has invalid status '{stat}'. Allowed: {', '.join(sorted(ALLOWED_STATUSES))}.")

        tags = t.get("tags")
        if tags is not None:
            if not isinstance(tags, str):
                raise ValueError(f"Task {t_id} 'tags' must be a string.")
            if len(tags) > MAX_TAGS_LEN:
                raise ValueError(f"Task {t_id} tags exceed maximum length of {MAX_TAGS_LEN} characters.")

        subtasks = t.get("subtasks")
        if subtasks is not None and not isinstance(subtasks, (list, str)):
            raise ValueError(f"Task {t_id} 'subtasks' must be a list or JSON string.")

        ai_ctx = t.get("ai_suggestion_context")
        if ai_ctx is not None and not isinstance(ai_ctx, (dict, str)):
            raise ValueError(f"Task {t_id} 'ai_suggestion_context' must be an object or JSON string.")

        _validate_optional_timestamp(t.get("created_at"), f"Task {t_id} created_at")
        _validate_optional_timestamp(t.get("updated_at"), f"Task {t_id} updated_at")

    # 2. Validate Notes
    note_ids = set()
    for idx, n in enumerate(data.get("notes", [])):
        if not isinstance(n, dict):
            raise ValueError(f"Note #{idx} must be an object.")
        n_id = n.get("id")
        if not isinstance(n_id, int) or n_id <= 0:
            raise ValueError(f"Note #{idx} must have a positive integer 'id'.")
        if n_id in note_ids:
            raise ValueError(f"Duplicate note id {n_id} detected in backup payload.")
        note_ids.add(n_id)

        title = n.get("title", "")
        content = n.get("content", "")
        if not isinstance(title, str):
            raise ValueError(f"Note {n_id} 'title' must be a string.")
        if not isinstance(content, str):
            raise ValueError(f"Note {n_id} 'content' must be a string.")
        if len(title) > MAX_NOTE_TITLE_LEN:
            raise ValueError(f"Note {n_id} title exceeds maximum length of {MAX_NOTE_TITLE_LEN} characters.")
        if len(content) > MAX_NOTE_CONTENT_LEN:
            raise ValueError(f"Note {n_id} content exceeds maximum length of {MAX_NOTE_CONTENT_LEN} characters.")
        if not title.strip() and not content.strip():
            raise ValueError(f"Note {n_id} must have non-empty title or content.")

        tags = n.get("tags")
        if tags is not None:
            if not isinstance(tags, str):
                raise ValueError(f"Note {n_id} 'tags' must be a string.")
            if len(tags) > MAX_TAGS_LEN:
                raise ValueError(f"Note {n_id} tags exceed maximum length of {MAX_TAGS_LEN} characters.")

        pinned = n.get("pinned")
        if pinned is not None and not isinstance(pinned, (int, bool)):
            raise ValueError(f"Note {n_id} 'pinned' must be an integer or boolean.")

        _validate_optional_timestamp(n.get("created_at"), f"Note {n_id} created_at")
        _validate_optional_timestamp(n.get("updated_at"), f"Note {n_id} updated_at")

    # 3. Validate Memories
    memory_ids = set()
    for idx, m in enumerate(data.get("memories", [])):
        if not isinstance(m, dict):
            raise ValueError(f"Memory #{idx} must be an object.")
        m_id = m.get("id")
        if not isinstance(m_id, int) or m_id <= 0:
            raise ValueError(f"Memory #{idx} must have a positive integer 'id'.")
        if m_id in memory_ids:
            raise ValueError(f"Duplicate memory id {m_id} detected in backup payload.")
        memory_ids.add(m_id)

        cat = m.get("category")
        if not isinstance(cat, str) or str(cat).strip().lower() not in ALLOWED_MEMORY_CATEGORIES:
            raise ValueError(f"Memory {m_id} has invalid category '{cat}'. Allowed: {', '.join(sorted(ALLOWED_MEMORY_CATEGORIES))}.")

        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Memory {m_id} missing required 'content' (non-empty string).")
        if len(content.strip()) > MAX_MEMORY_CONTENT_LEN:
            raise ValueError(f"Memory {m_id} content exceeds maximum length of {MAX_MEMORY_CONTENT_LEN} characters.")

        conf = m.get("confidence_weight")
        if conf is not None:
            try:
                conf_f = float(conf)
                if not (0.0 <= conf_f <= 1.0):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(f"Memory {m_id} 'confidence_weight' must be a float between 0.0 and 1.0.")

        stat = m.get("status")
        if stat is not None and str(stat).strip().lower() not in ALLOWED_MEMORY_STATUSES:
            raise ValueError(f"Memory {m_id} has invalid status '{stat}'. Allowed: {', '.join(sorted(ALLOWED_MEMORY_STATUSES))}.")

        sup = m.get("superseded_by")
        if sup is not None and (not isinstance(sup, int) or sup <= 0):
            raise ValueError(f"Memory {m_id} 'superseded_by' must be a positive integer or null.")

        _validate_optional_timestamp(m.get("created_at"), f"Memory {m_id} created_at")
        _validate_optional_timestamp(m.get("updated_at"), f"Memory {m_id} updated_at")

    # 4. Validate Decision Logs
    log_ids = set()
    for idx, l in enumerate(data.get("decision_logs", [])):
        if not isinstance(l, dict):
            raise ValueError(f"Decision log #{idx} must be an object.")
        l_id = l.get("id")
        if not isinstance(l_id, int) or l_id <= 0:
            raise ValueError(f"Decision log #{idx} must have a positive integer 'id'.")
        if l_id in log_ids:
            raise ValueError(f"Duplicate decision log id {l_id} detected in backup payload.")
        log_ids.add(l_id)

        mem_id = l.get("memory_id")
        if not isinstance(mem_id, int) or mem_id <= 0:
            raise ValueError(f"Decision log {l_id} must have a positive integer 'memory_id'.")
        if memory_ids and mem_id not in memory_ids:
            raise ValueError(f"Decision log {l_id} references nonexistent memory_id {mem_id}.")

        act = l.get("action")
        if not isinstance(act, str) or str(act).strip().upper() not in ALLOWED_DECISION_ACTIONS:
            raise ValueError(f"Decision log {l_id} has invalid action '{act}'. Allowed: {', '.join(sorted(ALLOWED_DECISION_ACTIONS))}.")

        reasoning = l.get("reasoning")
        if not isinstance(reasoning, str):
            raise ValueError(f"Decision log {l_id} 'reasoning' must be a string.")

        ts = l.get("timestamp")
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError(f"Decision log {l_id} missing required 'timestamp'.")
        _validate_optional_timestamp(ts, f"Decision log {l_id} timestamp")

    # 5. Validate Feedback Events
    fb_ids = set()
    for idx, f in enumerate(data.get("feedback_events", [])):
        if not isinstance(f, dict):
            raise ValueError(f"Feedback event #{idx} must be an object.")
        f_id = f.get("id")
        if not isinstance(f_id, int) or f_id <= 0:
            raise ValueError(f"Feedback event #{idx} must have a positive integer 'id'.")
        if f_id in fb_ids:
            raise ValueError(f"Duplicate feedback event id {f_id} detected in backup payload.")
        fb_ids.add(f_id)

        st = f.get("suggestion_type")
        if not isinstance(st, str) or not st.strip():
            raise ValueError(f"Feedback event {f_id} missing required 'suggestion_type'.")

        orig = f.get("original_suggestion")
        if not isinstance(orig, str):
            raise ValueError(f"Feedback event {f_id} missing required 'original_suggestion'.")

        action = f.get("user_action")
        if not isinstance(action, str) or str(action).strip().lower() not in ALLOWED_FEEDBACK_ACTIONS:
            raise ValueError(f"Feedback event {f_id} has invalid user_action '{action}'. Allowed: {', '.join(sorted(ALLOWED_FEEDBACK_ACTIONS))}.")

        mem_applied = f.get("memory_id_applied")
        if mem_applied is not None:
            if not isinstance(mem_applied, int) or mem_applied <= 0:
                raise ValueError(f"Feedback event {f_id} 'memory_id_applied' must be a positive integer or null.")
            if memory_ids and mem_applied not in memory_ids:
                raise ValueError(f"Feedback event {f_id} references nonexistent memory_id_applied {mem_applied}.")

        ts = f.get("timestamp")
        if not isinstance(ts, str) or not ts.strip():
            raise ValueError(f"Feedback event {f_id} missing required 'timestamp'.")
        _validate_optional_timestamp(ts, f"Feedback event {f_id} timestamp")

    # 6. Validate Settings
    if "settings" in data:
        settings = data["settings"]
        if not isinstance(settings, dict):
            raise ValueError("Entity 'settings' in backup must be a key-value object.")
        for key, val in settings.items():
            if key not in ALLOWED_SETTINGS_KEYS:
                raise ValueError(f"Setting key '{key}' is not permitted. Allowed: {', '.join(sorted(ALLOWED_SETTINGS_KEYS))}.")
            if key == "engine":
                eng = str(val).strip().lower()
                if eng not in ALLOWED_ENGINES:
                    raise ValueError(f"Invalid engine value '{val}'. Must be 'local' or 'ollama'.")
            elif key == "ollama_url":
                validate_ollama_url(str(val).strip(), strict_mode=OFFLINE_STRICT_MODE)

    return payload

def parse_and_validate_json(request: Request) -> Dict[str, Any]:
    """
    Validates that the request has a JSON Content-Type and a valid JSON object (dict) body.
    Raises ValueError with a clear explanation if invalid.
    """
    if not request.is_json:
        raise ValueError("Request Content-Type must be application/json.")

    data = request.get_json(silent=True)
    if data is None:
        raise ValueError("Malformed or invalid JSON payload.")

    if not isinstance(data, dict):
        raise ValueError("JSON payload must be a key-value object.")

    return data

def validate_task_input(data: Dict[str, Any], is_update: bool = False) -> Dict[str, Any]:
    """Validates fields for task creation or update."""
    clean = {}

    if "title" in data:
        title = data["title"]
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Task title must be a non-empty string.")
        if len(title.strip()) > MAX_TASK_TITLE_LEN:
            raise ValueError(f"Task title exceeds maximum allowed length of {MAX_TASK_TITLE_LEN} characters.")
        clean["title"] = title.strip()
    elif not is_update:
        raise ValueError("Task title is required.")

    if "description" in data and data["description"] is not None:
        desc = data["description"]
        if not isinstance(desc, str):
            raise ValueError("Task description must be a string.")
        if len(desc) > MAX_TASK_DESC_LEN:
            raise ValueError(f"Task description exceeds maximum allowed length of {MAX_TASK_DESC_LEN} characters.")
        clean["description"] = desc.strip()

    if "priority" in data and data["priority"] is not None:
        prio = str(data["priority"]).strip().lower()
        if prio not in ALLOWED_PRIORITIES:
            raise ValueError(f"Invalid priority '{prio}'. Allowed values: {', '.join(sorted(ALLOWED_PRIORITIES))}.")
        clean["priority"] = prio

    if "status" in data and data["status"] is not None:
        status = str(data["status"]).strip().lower()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Allowed values: {', '.join(sorted(ALLOWED_STATUSES))}.")
        clean["status"] = status

    if "tags" in data and data["tags"] is not None:
        tags = str(data["tags"]).strip()
        if len(tags) > MAX_TAGS_LEN:
            raise ValueError(f"Tags field exceeds maximum allowed length of {MAX_TAGS_LEN} characters.")
        clean["tags"] = tags

    if "due_date" in data:
        clean["due_date"] = data["due_date"]

    if "subtasks" in data and data["subtasks"] is not None:
        if not isinstance(data["subtasks"], list):
            raise ValueError("Subtasks must be a list.")
        clean["subtasks"] = data["subtasks"]

    return clean

def validate_note_input(data: Dict[str, Any], is_update: bool = False) -> Dict[str, Any]:
    """Validates fields for note creation or update."""
    clean = {}

    title = data.get("title")
    content = data.get("content")

    if title is not None:
        if not isinstance(title, str):
            raise ValueError("Note title must be a string.")
        if len(title.strip()) > MAX_NOTE_TITLE_LEN:
            raise ValueError(f"Note title exceeds maximum allowed length of {MAX_NOTE_TITLE_LEN} characters.")
        clean["title"] = title.strip()

    if content is not None:
        if not isinstance(content, str):
            raise ValueError("Note content must be a string.")
        if len(content) > MAX_NOTE_CONTENT_LEN:
            raise ValueError(f"Note content exceeds maximum allowed length of {MAX_NOTE_CONTENT_LEN} characters.")
        clean["content"] = content

    if not is_update and not clean.get("title") and not clean.get("content"):
        raise ValueError("Note title or content is required.")

    if "tags" in data and data["tags"] is not None:
        tags = str(data["tags"]).strip()
        if len(tags) > MAX_TAGS_LEN:
            raise ValueError(f"Tags field exceeds maximum allowed length of {MAX_TAGS_LEN} characters.")
        clean["tags"] = tags

    if "pinned" in data:
        clean["pinned"] = 1 if data["pinned"] else 0

    return clean

def validate_memory_input(data: Dict[str, Any], is_update: bool = False) -> Dict[str, Any]:
    """Validates fields for memory creation or update."""
    clean = {}

    if "content" in data:
        content = data["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Memory content must be a non-empty string.")
        if len(content.strip()) > MAX_MEMORY_CONTENT_LEN:
            raise ValueError(f"Memory content exceeds maximum allowed length of {MAX_MEMORY_CONTENT_LEN} characters.")
        clean["content"] = content.strip()
    elif not is_update:
        raise ValueError("Memory content is required.")

    if "category" in data and data["category"] is not None:
        cat = str(data["category"]).strip().lower()
        if cat not in ALLOWED_MEMORY_CATEGORIES:
            raise ValueError(f"Invalid category '{cat}'. Allowed values: {', '.join(sorted(ALLOWED_MEMORY_CATEGORIES))}.")
        clean["category"] = cat

    if "confidence" in data and data["confidence"] is not None:
        try:
            conf = float(data["confidence"])
        except (ValueError, TypeError):
            raise ValueError("Confidence weight must be a number between 0.0 and 1.0.")
        if not (0.0 <= conf <= 1.0):
            raise ValueError("Confidence weight must be a number between 0.0 and 1.0.")
        clean["confidence"] = conf

    return clean

def validate_chat_input(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    """Validates user message and chat history for the copilot."""
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Chat message must be a non-empty string.")

    if len(message.strip()) > MAX_CHAT_MESSAGE_LEN:
        raise ValueError(f"Chat message exceeds maximum allowed length of {MAX_CHAT_MESSAGE_LEN} characters.")

    history = data.get("history", [])
    if not isinstance(history, list):
        raise ValueError("Chat history must be a list.")

    if len(history) > MAX_CHAT_HISTORY_ITEMS:
        raise ValueError(f"Chat history exceeds maximum of {MAX_CHAT_HISTORY_ITEMS} items.")

    for item in history:
        if not isinstance(item, dict) or "content" not in item:
            continue
        if len(str(item.get("content", ""))) > MAX_CHAT_MESSAGE_LEN:
            raise ValueError("A chat history entry exceeds the maximum length limit.")

    return message.strip(), history

def validate_feedback_input(data: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """Validates feedback action and correction detail."""
    action = data.get("action")
    if not isinstance(action, str) or action.lower() not in ALLOWED_FEEDBACK_ACTIONS:
        raise ValueError(f"Invalid action. Allowed: {', '.join(sorted(ALLOWED_FEEDBACK_ACTIONS))}.")

    correction = data.get("correction")
    if correction is not None:
        if not isinstance(correction, str):
            raise ValueError("Correction text must be a string.")
        if len(correction.strip()) > MAX_FEEDBACK_TEXT_LEN:
            raise ValueError(f"Correction text exceeds maximum allowed length of {MAX_FEEDBACK_TEXT_LEN} characters.")
        correction = correction.strip()

    return action.lower(), correction

def validate_pagination_limit(limit_val: Any, default: int = 50) -> int:
    """Validates limit query parameter."""
    if limit_val is None:
        return default
    try:
        val = int(limit_val)
        if val <= 0:
            raise ValueError("Limit must be a positive integer.")
        return min(val, MAX_PAGINATION_LIMIT)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid pagination limit. Must be an integer between 1 and {MAX_PAGINATION_LIMIT}.")
