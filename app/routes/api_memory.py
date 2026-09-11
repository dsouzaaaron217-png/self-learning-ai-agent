from flask import Blueprint, request, jsonify
from app.models import MemoryModel, MemoryDecisionLogModel
from app.memory.tracker import MemoryTracker
from app.vector_store import global_vector_store

api_memory_bp = Blueprint("api_memory", __name__, url_prefix="/api/memory")

@api_memory_bp.route("", methods=["GET"])
def list_active_memories():
    category = request.args.get("category")
    memories = MemoryModel.list_active(category=category)
    return jsonify({"success": True, "memories": memories})

@api_memory_bp.route("/all", methods=["GET"])
def list_all_memories():
    status = request.args.get("status")
    memories = MemoryModel.list_all(status=status)
    return jsonify({"success": True, "memories": memories})

@api_memory_bp.route("/<int:memory_id>", methods=["GET"])
def get_memory_with_history(memory_id):
    mem = MemoryModel.get(memory_id)
    if not mem:
        return jsonify({"success": False, "error": "Memory not found"}), 404
    
    # Retrieve audit history (provenance: why this memory exists in its current form)
    history = MemoryDecisionLogModel.get_for_memory(memory_id)
    return jsonify({
        "success": True,
        "memory": mem,
        "provenance_history": history
    })

@api_memory_bp.route("", methods=["POST"])
def manual_add_memory():
    """Manually add a learned habit or preference from the Transparency Dashboard."""
    data = request.get_json() or {}
    category = data.get("category", "preference")
    content = data.get("content", "").strip()
    confidence = float(data.get("confidence", 0.85))

    if not content:
        return jsonify({"success": False, "error": "Memory content is required"}), 400

    new_id = MemoryModel.create(
        category=category,
        content=content,
        confidence_weight=confidence,
        source_context="Manually defined in Transparency Dashboard"
    )

    global_vector_store.index_document(
        doc_id=new_id,
        doc_type="memory",
        text=content,
        metadata={"category": category, "confidence_weight": confidence}
    )

    MemoryDecisionLogModel.log(
        memory_id=new_id,
        action="ADD",
        reasoning="Manually created by user in Transparency Dashboard.",
        previous_content=None,
        new_content=content,
        triggered_by="manual_dashboard"
    )

    return jsonify({"success": True, "memory": MemoryModel.get(new_id)}), 201

@api_memory_bp.route("/<int:memory_id>", methods=["PUT"])
def manual_update_memory(memory_id):
    """Manually edit or fine-tune a learned memory."""
    mem = MemoryModel.get(memory_id)
    if not mem:
        return jsonify({"success": False, "error": "Memory not found"}), 404

    data = request.get_json() or {}
    content = data.get("content", mem["content"]).strip()
    category = data.get("category", mem["category"])
    confidence = float(data.get("confidence", mem["confidence_weight"]))

    old_content = mem["content"]
    MemoryModel.update(
        memory_id,
        content=content,
        category=category,
        confidence_weight=confidence,
        update_count=mem["update_count"] + 1
    )

    global_vector_store.index_document(
        doc_id=memory_id,
        doc_type="memory",
        text=content,
        metadata={"category": category, "confidence_weight": confidence}
    )

    MemoryDecisionLogModel.log(
        memory_id=memory_id,
        action="UPDATE",
        reasoning="User manually edited memory details via Transparency Dashboard.",
        previous_content=old_content,
        new_content=content,
        triggered_by="manual_edit"
    )

    return jsonify({"success": True, "memory": MemoryModel.get(memory_id)})

@api_memory_bp.route("/<int:memory_id>", methods=["DELETE"])
def manual_delete_memory(memory_id):
    """Deactivate or remove a memory."""
    mem = MemoryModel.get(memory_id)
    if not mem:
        return jsonify({"success": False, "error": "Memory not found"}), 404

    reason = request.args.get("reason", "Deleted by user via Transparency Dashboard")
    MemoryModel.update(memory_id, status="deleted")
    global_vector_store.remove_document(memory_id, doc_type="memory")

    MemoryDecisionLogModel.log(
        memory_id=memory_id,
        action="DELETE",
        reasoning=reason,
        previous_content=mem["content"],
        new_content=None,
        triggered_by="manual_delete"
    )

    return jsonify({"success": True, "message": "Memory deactivated and removed from active pool."})

@api_memory_bp.route("/decisions", methods=["GET"])
def get_decision_audit_logs():
    """Retrieve the recent ADD/UPDATE/DELETE decisions for the audit trail."""
    limit = int(request.args.get("limit", 40))
    logs = MemoryDecisionLogModel.get_recent(limit=limit)
    return jsonify({"success": True, "decisions": logs})

@api_memory_bp.route("/metrics", methods=["GET"])
def get_learning_metrics():
    """Get adaptation analytics, correction rate trends, and precision scores."""
    metrics = MemoryTracker.get_dashboard_metrics()
    return jsonify({"success": True, "metrics": metrics})
