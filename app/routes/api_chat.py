from flask import Blueprint, request, jsonify
from app.vector_store import global_vector_store
from app.models import TaskModel, NoteModel, MemoryModel
from app.reasoning import get_reasoner

api_chat_bp = Blueprint("api_chat", __name__, url_prefix="/api/chat")

@api_chat_bp.route("", methods=["POST"])
def chat():
    data = request.get_json() or {}
    message = data.get("message", "").strip()
    history = data.get("history", [])

    if not message:
        return jsonify({"success": False, "error": "Message is required"}), 400

    # Retrieve relevant memories and notes
    retrieved_mems = global_vector_store.search(message, doc_type="memory", limit=4)
    active_tasks = TaskModel.list_all(status="todo") + TaskModel.list_all(status="in_progress")
    recent_notes = NoteModel.list_all()[:4]

    reasoner = get_reasoner()
    response = reasoner.generate_chat_response(
        message=message,
        chat_history=history,
        retrieved_memories=retrieved_mems,
        active_tasks=active_tasks,
        recent_notes=recent_notes
    )

    # Resolve full memory objects for referenced memory IDs
    cited_memories = []
    for mem_id in response.get("referenced_memories", []):
        m = MemoryModel.get(mem_id)
        if m:
            cited_memories.append(m)

    return jsonify({
        "success": True,
        "reply": response["reply"],
        "engine": response.get("engine", "local"),
        "cited_memories": cited_memories
    })
