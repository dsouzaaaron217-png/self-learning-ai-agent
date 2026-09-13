from typing import Dict, Any, Optional, List, Tuple
from flask import Request

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
