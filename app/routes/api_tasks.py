from flask import Blueprint, request, jsonify
from app.models import TaskModel
from app.vector_store import global_vector_store
from app.reasoning import get_reasoner
from app.memory.pipeline import MemoryPipeline

api_tasks_bp = Blueprint("api_tasks", __name__, url_prefix="/api/tasks")

@api_tasks_bp.route("", methods=["GET"])
def list_tasks():
    status = request.args.get("status")
    priority = request.args.get("priority")
    tasks = TaskModel.list_all(status=status, priority=priority)
    return jsonify({"success": True, "tasks": tasks})

@api_tasks_bp.route("", methods=["POST"])
def create_task():
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"success": False, "error": "Task title is required"}), 400

    description = data.get("description", "").strip()
    user_priority = data.get("priority")
    due_date = data.get("due_date")
    tags = data.get("tags", "")

    # Retrieve relevant memories for task enhancement
    retrieved_mems = global_vector_store.search(f"{title} {description}", doc_type="memory", limit=4)
    reasoner = get_reasoner()
    enhancement = reasoner.suggest_task_enhancements(title, description, retrieved_mems)

    # Use user priority if explicitly provided, else suggested priority
    final_priority = user_priority if user_priority in ('low', 'medium', 'high', 'urgent') else enhancement["suggested_priority"]
    subtasks = data.get("subtasks") or enhancement["subtasks"]

    # AI context to track recommendation origin for feedback
    ai_context = {
        "suggested_priority": enhancement["suggested_priority"],
        "applied_memory_id": enhancement.get("applied_memory_id"),
        "applied_memory_text": enhancement.get("applied_memory_text"),
        "rationale": enhancement["rationale"],
        "user_accepted": None
    }

    task_id = TaskModel.create(
        title=title,
        description=description,
        priority=final_priority,
        tags=tags or ", ".join(enhancement.get("suggested_tags", [])),
        due_date=due_date,
        subtasks=subtasks,
        ai_suggestion_context=ai_context
    )

    # Index task in vector store
    global_vector_store.index_document(
        doc_id=task_id,
        doc_type="task",
        text=f"{title} {description} {tags}",
        metadata={"priority": final_priority}
    )

    created_task = TaskModel.get(task_id)
    return jsonify({
        "success": True,
        "task": created_task,
        "ai_suggestion": enhancement
    }), 201

@api_tasks_bp.route("/quick-capture", methods=["POST"])
def quick_capture():
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"success": False, "error": "Capture text is required"}), 400

    reasoner = get_reasoner()
    parsed = reasoner.parse_quick_capture(text)

    # If classified as task
    if parsed["type"] == "task":
        retrieved_mems = global_vector_store.search(parsed["title"], doc_type="memory", limit=3)
        enhancement = reasoner.suggest_task_enhancements(parsed["title"], "", retrieved_mems)
        
        priority = parsed["priority"] if parsed["priority"] != "medium" else enhancement["suggested_priority"]
        tags = parsed["tags"] or ", ".join(enhancement.get("suggested_tags", []))

        ai_ctx = {
            "suggested_priority": enhancement["suggested_priority"],
            "applied_memory_id": enhancement.get("applied_memory_id"),
            "applied_memory_text": enhancement.get("applied_memory_text"),
            "rationale": enhancement["rationale"]
        }

        task_id = TaskModel.create(
            title=parsed["title"],
            description="",
            priority=priority,
            tags=tags,
            due_date=parsed.get("due_date"),
            subtasks=enhancement.get("subtasks", []),
            ai_suggestion_context=ai_ctx
        )

        global_vector_store.index_document(
            doc_id=task_id,
            doc_type="task",
            text=parsed["title"],
            metadata={"priority": priority}
        )

        # Extract any latent habit/facts from the capture string
        MemoryPipeline.extract_facts(text, context_type="task_capture")

        return jsonify({
            "success": True,
            "type": "task",
            "item": TaskModel.get(task_id),
            "ai_suggestion": enhancement
        })
    else:
        # Save as note
        from app.models import NoteModel
        note_id = NoteModel.create(
            title=parsed["title"][:50],
            content=text,
            tags=parsed["tags"]
        )
        global_vector_store.index_document(
            doc_id=note_id,
            doc_type="note",
            text=f"{parsed['title']} {text}"
        )
        return jsonify({
            "success": True,
            "type": "note",
            "item": NoteModel.get(note_id)
        })

@api_tasks_bp.route("/<int:task_id>", methods=["GET"])
def get_task(task_id):
    task = TaskModel.get(task_id)
    if not task:
        return jsonify({"success": False, "error": "Task not found"}), 404
    return jsonify({"success": True, "task": task})

@api_tasks_bp.route("/<int:task_id>", methods=["PUT"])
def update_task(task_id):
    data = request.get_json() or {}
    success = TaskModel.update(task_id, **data)
    if not success:
        return jsonify({"success": False, "error": "Task not found or no valid fields to update"}), 400

    updated = TaskModel.get(task_id)
    # Update vector index
    global_vector_store.index_document(
        doc_id=task_id,
        doc_type="task",
        text=f"{updated['title']} {updated.get('description', '')} {updated.get('tags', '')}",
        metadata={"priority": updated.get("priority")}
    )
    return jsonify({"success": True, "task": updated})

@api_tasks_bp.route("/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    success = TaskModel.delete(task_id)
    if not success:
        return jsonify({"success": False, "error": "Task not found"}), 404
    global_vector_store.remove_document(task_id, doc_type="task")
    return jsonify({"success": True, "message": "Task deleted"})

@api_tasks_bp.route("/<int:task_id>/feedback", methods=["POST"])
def submit_feedback(task_id):
    """
    Submits user feedback on an AI suggestion for this task.
    Actions: 'accepted' or 'corrected'.
    """
    data = request.get_json() or {}
    action = data.get("action")  # 'accepted' or 'corrected'
    correction_text = data.get("correction", "")
    
    task = TaskModel.get(task_id)
    if not task:
        return jsonify({"success": False, "error": "Task not found"}), 404

    ai_ctx = task.get("ai_suggestion_context") or {}
    applied_mem_id = ai_ctx.get("applied_memory_id")
    orig_suggestion = f"Priority: {ai_ctx.get('suggested_priority', task.get('priority'))}"

    if action == "accepted":
        MemoryPipeline.process_acceptance(
            item_id=task_id,
            original_suggestion=orig_suggestion,
            memory_id_applied=applied_mem_id
        )
        ai_ctx["user_accepted"] = True
        TaskModel.update(task_id, ai_suggestion_context=ai_ctx)
        return jsonify({"success": True, "message": "Suggestion accepted and preference reinforced."})

    elif action == "corrected":
        if not correction_text:
            return jsonify({"success": False, "error": "Correction details required"}), 400

        decision = MemoryPipeline.process_correction(
            item_id=task_id,
            original_suggestion=orig_suggestion,
            user_correction=correction_text,
            memory_id_applied=applied_mem_id
        )
        ai_ctx["user_accepted"] = False
        ai_ctx["user_correction"] = correction_text
        ai_ctx["memory_decision"] = decision
        TaskModel.update(task_id, ai_suggestion_context=ai_ctx)
        return jsonify({
            "success": True,
            "message": "Correction recorded and memory updated.",
            "decision": decision
        })

    return jsonify({"success": False, "error": "Invalid feedback action"}), 400
