from flask import Blueprint, request, jsonify
from app.models import NoteModel
from app.vector_store import global_vector_store
from app.memory.pipeline import MemoryPipeline
from app.validation import parse_and_validate_json, validate_note_input
from app.markdown_utils import render_safe_markdown

api_notes_bp = Blueprint("api_notes", __name__, url_prefix="/api/notes")

def _enrich_note(note: dict) -> dict:
    """Enriches note dictionary with dynamically rendered safe HTML."""
    if not note:
        return note
    res = dict(note)
    res["rendered_html"] = render_safe_markdown(res.get("content", ""))
    return res

@api_notes_bp.route("", methods=["GET"])
def list_notes():
    notes = [_enrich_note(n) for n in NoteModel.list_all()]
    return jsonify({"success": True, "notes": notes})

@api_notes_bp.route("", methods=["POST"])
def create_note():
    try:
        raw_data = parse_and_validate_json(request)
        clean = validate_note_input(raw_data, is_update=False)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    title = clean.get("title")
    content = clean.get("content", "")

    if not title:
        title = content[:40] + ("..." if len(content) > 40 else "")

    tags = clean.get("tags", "")
    pinned = clean.get("pinned", 0)

    note_id = NoteModel.create(title=title, content=content, tags=tags, pinned=pinned)

    # Index in vector store
    global_vector_store.index_document(
        doc_id=note_id,
        doc_type="note",
        text=f"{title}\n{content}\n{tags}",
        metadata={"tags": tags, "pinned": pinned}
    )

    # Phase 1 & 2: Extract any embedded habits or preferences from raw note content
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
        "note": _enrich_note(NoteModel.get(note_id)),
        "learned_memories": memory_decisions
    }), 201

@api_notes_bp.route("/<int:note_id>", methods=["GET"])
def get_note(note_id):
    note = NoteModel.get(note_id)
    if not note:
        return jsonify({"success": False, "error": "Note not found"}), 404
    
    # Retrieve related memories for this note
    related_mems = global_vector_store.search(f"{note['title']} {note['content']}", doc_type="memory", limit=3)
    return jsonify({"success": True, "note": _enrich_note(note), "related_memories": related_mems})

@api_notes_bp.route("/<int:note_id>", methods=["PUT"])
def update_note(note_id):
    existing = NoteModel.get(note_id)
    if not existing:
        return jsonify({"success": False, "error": "Note not found"}), 404

    try:
        raw_data = parse_and_validate_json(request)
        clean = validate_note_input(raw_data, is_update=True)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    success = NoteModel.update(note_id, **clean)
    if not success:
        return jsonify({"success": False, "error": "No valid fields to update"}), 400

    updated = NoteModel.get(note_id)
    global_vector_store.index_document(
        doc_id=note_id,
        doc_type="note",
        text=f"{updated['title']}\n{updated['content']}\n{updated.get('tags', '')}",
        metadata={"tags": updated.get("tags"), "pinned": updated.get("pinned")}
    )

    # Route through MemoryPipeline if content or title was changed (using raw user-authored text)
    memory_decisions = []
    if "title" in clean or "content" in clean:
        raw_text = f"{updated['title']}\n{updated['content']}"
        extracted_facts = MemoryPipeline.extract_facts(raw_text, context_type="note_update")
        for fact in extracted_facts:
            decision = MemoryPipeline.reconcile_memory(
                category=fact["category"],
                content=fact["content"],
                confidence=fact["confidence"],
                source_context=f"Updated note #{note_id} ('{updated['title']}')",
                triggered_by="note_update"
            )
            memory_decisions.append(decision)

    return jsonify({
        "success": True,
        "note": _enrich_note(updated),
        "learned_memories": memory_decisions
    })

@api_notes_bp.route("/<int:note_id>/pin", methods=["POST"])
def toggle_pin_note(note_id):
    note = NoteModel.get(note_id)
    if not note:
        return jsonify({"success": False, "error": "Note not found"}), 404

    target_pin = None
    if request.is_json and request.get_data():
        try:
            raw_data = parse_and_validate_json(request)
            if "pinned" in raw_data:
                p = raw_data["pinned"]
                if isinstance(p, bool):
                    target_pin = 1 if p else 0
                elif isinstance(p, int) and p in (0, 1) and not isinstance(p, bool):
                    target_pin = p
                else:
                    return jsonify({"success": False, "error": "Pinned must be a boolean."}), 400
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400

    new_pinned = target_pin if target_pin is not None else (0 if note.get("pinned", 0) else 1)
    NoteModel.update(note_id, pinned=new_pinned)
    updated = NoteModel.get(note_id)
    global_vector_store.index_document(
        doc_id=note_id,
        doc_type="note",
        text=f"{updated['title']}\n{updated['content']}\n{updated.get('tags', '')}",
        metadata={"tags": updated.get("tags"), "pinned": updated.get("pinned")}
    )
    return jsonify({"success": True, "note": _enrich_note(updated)})

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
