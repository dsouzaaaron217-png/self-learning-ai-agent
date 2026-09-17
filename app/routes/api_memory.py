from flask import Blueprint, request, jsonify
from app.models import MemoryModel, MemoryDecisionLogModel, FeedbackModel
from app.memory.tracker import MemoryTracker
from app.vector_store import global_vector_store
from app.validation import (
    parse_and_validate_json,
    validate_memory_input,
    validate_pagination_limit
)

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
    try:
        raw_data = parse_and_validate_json(request)
        clean = validate_memory_input(raw_data, is_update=False)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    category = clean.get("category", "preference")
    content = clean["content"]
    confidence = clean.get("confidence", 0.85)

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

    try:
        raw_data = parse_and_validate_json(request)
        clean = validate_memory_input(raw_data, is_update=True)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    content = clean.get("content", mem["content"])
    category = clean.get("category", mem["category"])
    confidence = clean.get("confidence", mem["confidence_weight"])

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
    try:
        limit = validate_pagination_limit(request.args.get("limit"), default=40)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    logs = MemoryDecisionLogModel.get_recent(limit=limit)
    return jsonify({"success": True, "decisions": logs})

@api_memory_bp.route("/metrics", methods=["GET"])
def get_learning_metrics():
    """Get adaptation analytics, correction rate trends, and precision scores."""
    metrics = MemoryTracker.get_dashboard_metrics()
    return jsonify({"success": True, "metrics": metrics})

@api_memory_bp.route("/superseded", methods=["GET"])
def list_superseded_memories():
    """Retrieve all superseded memories with relationship to their replacements."""
    memories = MemoryModel.list_superseded()
    return jsonify({"success": True, "memories": memories})

@api_memory_bp.route("/search", methods=["GET"])
def search_memories():
    """Search memories across all or active statuses."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"success": False, "error": "Search query 'q' is required."}), 400

    include_superseded = request.args.get("include_superseded", "").lower() in ("true", "1", "yes")
    status = None if include_superseded else "active"
    results = MemoryModel.search_all_statuses(q=q, status=status)
    return jsonify({
        "success": True,
        "query": q,
        "include_superseded": include_superseded,
        "memories": results
    })

@api_memory_bp.route("/rejection-rate", methods=["GET"])
def get_rejection_rate():
    """Get dedicated transparent rejection-rate metric."""
    metrics = FeedbackModel.get_metrics()
    return jsonify({
        "success": True,
        "rejection_rate_percent": metrics.get("rejection_rate_percent", 0.0),
        "rejected_count": metrics.get("rejected_count", 0),
        "total_events": metrics.get("total_events", 0)
    })
