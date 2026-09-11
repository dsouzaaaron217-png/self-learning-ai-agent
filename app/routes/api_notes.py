from flask import Blueprint, request, jsonify
from app.models import NoteModel
from app.vector_store import global_vector_store
from app.memory.pipeline import MemoryPipeline

api_notes_bp = Blueprint("api_notes", __name__, url_prefix="/api/notes")

@api_notes_bp.route("", methods=["GET"])
def list_notes():
    notes = NoteModel.list_all()
    return jsonify({"success": True, "notes": notes})

@api_notes_bp.route("", methods=["POST"])
def create_note():
    data = request.get_json() or {}
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()
    if not title and not content:
        return jsonify({"success": False, "error": "Note title or content is required"}), 400

    if not title:
        title = content[:40] + ("..." if len(content) > 40 else "")

    tags = data.get("tags", "")
    pinned = int(data.get("pinned", 0))

    note_id = NoteModel.create(title=title, content=content, tags=tags, pinned=pinned)

    # Index in vector store
    global_vector_store.index_document(
        doc_id=note_id,
        doc_type="note",
        text=f"{title}\n{content}\n{tags}",
        metadata={"tags": tags, "pinned": pinned}
    )

    # Phase 1 & 2: Extract any embedded habits or preferences from note content
    extracted_facts = MemoryPipeline.extract_facts(f"{title}\n{content}", context_type="note_capture")
    memory_decisions = []
    for fact in extracted_facts:
        decision = MemoryPipeline.reconcile_memory(
            category=fact["category"],
            content=fact["content"],
            confidence=fact["confidence"],
            source_context=f"Discovered in note #{note_id} ('{title}')",
            triggered_by="note_capture"
        )
        memory_decisions.append(decision)

    return jsonify({
        "success": True,
        "note": NoteModel.get(note_id),
        "learned_memories": memory_decisions
    }), 201

@api_notes_bp.route("/<int:note_id>", methods=["GET"])
def get_note(note_id):
    note = NoteModel.get(note_id)
    if not note:
        return jsonify({"success": False, "error": "Note not found"}), 404
    
    # Retrieve related memories for this note
    related_mems = global_vector_store.search(f"{note['title']} {note['content']}", doc_type="memory", limit=3)
    return jsonify({"success": True, "note": note, "related_memories": related_mems})

@api_notes_bp.route("/<int:note_id>", methods=["PUT"])
def update_note(note_id):
    data = request.get_json() or {}
    success = NoteModel.update(note_id, **data)
    if not success:
        return jsonify({"success": False, "error": "Note not found or invalid fields"}), 400

    updated = NoteModel.get(note_id)
    global_vector_store.index_document(
        doc_id=note_id,
        doc_type="note",
        text=f"{updated['title']}\n{updated['content']}\n{updated.get('tags', '')}",
        metadata={"tags": updated.get("tags"), "pinned": updated.get("pinned")}
    )
    return jsonify({"success": True, "note": updated})

@api_notes_bp.route("/<int:note_id>", methods=["DELETE"])
def delete_note(note_id):
    success = NoteModel.delete(note_id)
    if not success:
        return jsonify({"success": False, "error": "Note not found"}), 404
    global_vector_store.remove_document(note_id, doc_type="note")
    return jsonify({"success": True, "message": "Note deleted"})

@api_notes_bp.route("/search", methods=["GET"])
def search_notes():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"success": True, "results": []})
    
    results = global_vector_store.search(query, doc_type="note", limit=10)
    return jsonify({"success": True, "query": query, "results": results})
