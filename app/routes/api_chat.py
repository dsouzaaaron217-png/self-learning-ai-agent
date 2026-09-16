from flask import Blueprint, request, jsonify
from app.vector_store import global_vector_store
from app.models import TaskModel, NoteModel, MemoryModel, ChatMessageModel
from app.memory.pipeline import MemoryPipeline
from app.reasoning import get_reasoner
from app.validation import (
    parse_and_validate_json,
    validate_chat_input,
    validate_session_id,
    validate_pagination_limit,
    validate_task_input,
    validate_note_input,
)

api_chat_bp = Blueprint("api_chat", __name__, url_prefix="/api/chat")


@api_chat_bp.route("", methods=["POST"])
def chat():
    try:
        raw_data = parse_and_validate_json(request)
        message, history = validate_chat_input(raw_data)
        session_id = validate_session_id(raw_data.get("session_id"))
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    # 1. Persist user message in SQLite
    user_msg_id = ChatMessageModel.create(
        session_id=session_id,
        role="user",
        content=message
    )

    # 2. Memory Pipeline Integration: extract & reconcile facts from USER statement
    # Assistant messages must NOT be learned from, and failures must not prevent chat persistence.
    learned_memories = []
    try:
        extracted_facts = MemoryPipeline.extract_facts(message, context_type="chat_interaction")
        for fact in extracted_facts:
            decision = MemoryPipeline.reconcile_memory(
                category=fact["category"],
                content=fact["content"],
                confidence=fact["confidence"],
                source_context=f"Chat message #{user_msg_id} (session: {session_id})",
                triggered_by="chat_interaction"
            )
            learned_memories.append(decision)
    except Exception as e:
        print(f"[api_chat] Non-fatal memory learning error: {e}")

    # 3. Retrieve relevant memories and notes for grounding
    retrieved_mems = global_vector_store.search(message, doc_type="memory", limit=4)
    active_tasks = TaskModel.list_all(status="todo") + TaskModel.list_all(status="in_progress")
    recent_notes = NoteModel.list_all()[:4]

    # 4. Generate reasoning response
    reasoner = get_reasoner()
    response = reasoner.generate_chat_response(
        message=message,
        chat_history=history,
        retrieved_memories=retrieved_mems,
        active_tasks=active_tasks,
        recent_notes=recent_notes
    )

    # 5. Resolve full memory objects for referenced memory IDs
    cited_memories = []
    for mem_id in response.get("referenced_memories", []):
        m = MemoryModel.get(mem_id)
        if m:
            cited_memories.append(m)

    # 6. Persist assistant reply in SQLite
    assistant_msg_id = ChatMessageModel.create(
        session_id=session_id,
        role="assistant",
        content=response["reply"],
        cited_memories=cited_memories
    )

    return jsonify({
        "success": True,
        "reply": response["reply"],
        "engine": response.get("engine", "local"),
        "cited_memories": cited_memories,
        "user_message_id": user_msg_id,
        "assistant_message_id": assistant_msg_id,
        "learned_memories": learned_memories
    })


@api_chat_bp.route("/history", methods=["GET"])
def get_chat_history():
    try:
        session_id = validate_session_id(request.args.get("session_id"))
        limit = validate_pagination_limit(request.args.get("limit"), default=50)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    messages = ChatMessageModel.list_by_session(session_id=session_id, limit=limit)
    return jsonify({
        "success": True,
        "session_id": session_id,
        "count": len(messages),
        "messages": messages
    })


@api_chat_bp.route("/history", methods=["DELETE"])
def delete_chat_history():
    session_id = None
    if request.is_json and request.data:
        try:
            raw_data = parse_and_validate_json(request)
            if "session_id" in raw_data and raw_data["session_id"] is not None:
                session_id = validate_session_id(raw_data["session_id"])
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
    elif "session_id" in request.args:
        try:
            session_id = validate_session_id(request.args.get("session_id"))
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400

    cleared = ChatMessageModel.clear_history(session_id=session_id)
    return jsonify({
        "success": True,
        "cleared_count": cleared,
        "session_id": session_id
    })


@api_chat_bp.route("/actions/task", methods=["POST"])
def add_chat_as_task():
    try:
        raw_data = parse_and_validate_json(request)
        message_id = raw_data.get("message_id")

        title = raw_data.get("title")
        description = raw_data.get("description", "")

        if not title and message_id is not None:
            if not isinstance(message_id, int):
                raise ValueError("message_id must be an integer.")
            msg = ChatMessageModel.get(message_id)
            if not msg:
                raise ValueError(f"Chat message #{message_id} not found.")
            content = msg["content"]
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if lines:
                first_line = lines[0].lstrip("#*- ").strip()
                title = first_line[:80]
                description = description or content
            else:
                title = "Action from Chat"
                description = description or content
        elif not title and raw_data.get("content"):
            content = str(raw_data.get("content")).strip()
            lines = [l.strip() for l in content.splitlines() if l.strip()]
            if lines:
                first_line = lines[0].lstrip("#*- ").strip()
                title = first_line[:80]
                description = description or content
            else:
                title = "Action from Chat"
                description = description or content

        task_payload = {
            "title": title,
            "description": description,
            "priority": raw_data.get("priority", "medium"),
            "due_date": raw_data.get("due_date"),
            "tags": raw_data.get("tags", "chat"),
            "subtasks": raw_data.get("subtasks")
        }
        validated = validate_task_input(task_payload)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    ai_ctx = {
        "source": "chat",
        "message_id": message_id
    }
    task_id = TaskModel.create(
        title=validated["title"],
        description=validated.get("description", ""),
        priority=validated.get("priority", "medium"),
        status=validated.get("status", "todo"),
        tags=validated.get("tags", "chat"),
        due_date=validated.get("due_date"),
        subtasks=validated.get("subtasks"),
        ai_suggestion_context=ai_ctx
    )

    try:
        global_vector_store.index_document(
            doc_id=task_id,
            doc_type="task",
            text=validated["title"],
            metadata={"priority": validated.get("priority", "medium")}
        )
    except Exception as e:
        print(f"[api_chat] Vector index failed for task #{task_id}: {e}")

    return jsonify({
        "success": True,
        "task": TaskModel.get(task_id)
    }), 201


@api_chat_bp.route("/actions/note", methods=["POST"])
def add_chat_as_note():
    try:
        raw_data = parse_and_validate_json(request)
        message_id = raw_data.get("message_id")

        content = raw_data.get("content")
        title = raw_data.get("title")

        if not content and message_id is not None:
            if not isinstance(message_id, int):
                raise ValueError("message_id must be an integer.")
            msg = ChatMessageModel.get(message_id)
            if not msg:
                raise ValueError(f"Chat message #{message_id} not found.")
            content = msg["content"]

        if not content:
            raise ValueError("Note content is required.")

        if not title:
            lines = [l.strip() for l in str(content).splitlines() if l.strip()]
            if lines:
                title = lines[0].lstrip("#*- ").strip()[:60]
            else:
                title = "Chat Note"

        note_payload = {
            "title": title,
            "content": content,
            "tags": raw_data.get("tags", "chat"),
            "pinned": raw_data.get("pinned", 0)
        }
        validated = validate_note_input(note_payload)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    note_id = NoteModel.create(
        title=validated.get("title", "Chat Note"),
        content=validated.get("content", ""),
        tags=validated.get("tags", "chat"),
        pinned=validated.get("pinned", 0)
    )

    try:
        global_vector_store.index_document(
            doc_id=note_id,
            doc_type="note",
            text=f"{validated.get('title', '')}\n{validated.get('content', '')}",
            metadata={"pinned": validated.get("pinned", 0)}
        )
    except Exception as e:
        print(f"[api_chat] Vector index failed for note #{note_id}: {e}")

    return jsonify({
        "success": True,
        "note": NoteModel.get(note_id)
    }), 201


@api_chat_bp.route("/action", methods=["POST"])
def chat_action_dispatcher():
    """Unified action endpoint supporting action='task' or action='note'."""
    try:
        raw_data = parse_and_validate_json(request)
        action = raw_data.get("action")
        if not action or action not in ("task", "note"):
            raise ValueError("Action must be either 'task' or 'note'.")
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    if action == "task":
        return add_chat_as_task()
    else:
        return add_chat_as_note()
