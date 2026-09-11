import re
from typing import List, Dict, Any, Optional, Tuple
from app.models import MemoryModel, MemoryDecisionLogModel, FeedbackModel
from app.vector_store import global_vector_store
from app.config import (
    SIMILARITY_MATCH_THRESHOLD,
    CONTRADICTION_THRESHOLD,
    BASE_CONFIDENCE_WEIGHT,
    REINFORCEMENT_STEP,
    PENALTY_STEP,
    MAX_CONFIDENCE_WEIGHT,
    MIN_CONFIDENCE_WEIGHT
)

class MemoryPipeline:
    """
    Two-Phase Memory Writer:
    Phase 1: Interaction Summarization and Fact Extraction
    Phase 2: Reconciliation (ADD, UPDATE, DELETE) with full provenance
    """

    @classmethod
    def extract_facts(cls, text: str, context_type: str = "general") -> List[Dict[str, Any]]:
        """
        Phase 1: Extract discrete atomic statements/rules from interactions.
        Identifies preferences, habits, corrections, and facts.
        """
        facts = []
        raw = text.strip()
        lower = raw.lower()

        # Pattern 1: Explicit preference or habit expressions
        pref_patterns = [
            (r'(?:i prefer|i like to|always|i usually|my habit is to)\s+([^.?!;]+)', 'preference'),
            (r'(?:never|don\'t|do not ever)\s+([^.?!;]+)', 'preference'),
            (r'(?:every morning|every evening|daily|on weekdays)\s+([^.?!;]+)', 'habit'),
            (r'(?:priority for|priority of)\s+([a-zA-Z0-9\s]+)\s+(?:is|should be)\s+(urgent|high|medium|low)', 'preference'),
            (r'([a-zA-Z0-9\s]+)\s+(?:tasks?|meetings?|items?)\s+(?:are|should be)\s+(urgent|high|medium|low)\s+priority', 'preference')
        ]

        for pat, cat in pref_patterns:
            matches = re.findall(pat, lower)
            for m in matches:
                if isinstance(m, tuple):
                    statement = f"{m[0].strip().title()} tasks are marked {m[1].strip()} priority."
                else:
                    statement = f"User prefers to {m.strip()}."
                facts.append({
                    "category": cat,
                    "content": statement,
                    "confidence": BASE_CONFIDENCE_WEIGHT,
                    "raw_extracted": str(m)
                })

        # Pattern 2: Explicit correction expressions
        if any(w in lower for w in ["not what i meant", "wrong priority", "actually", "change it to", "instead", "should be"]):
            facts.append({
                "category": "correction",
                "content": f"User corrected: '{raw}'",
                "confidence": 0.85,
                "raw_extracted": raw
            })

        # Pattern 3: Habit clues in routine tasks
        if any(w in lower for w in ["routine", "gym", "workout", "standup", "weekly review", "meditation"]):
            facts.append({
                "category": "habit",
                "content": f"User maintains a regular routine: '{raw}'.",
                "confidence": 0.75,
                "raw_extracted": raw
            })

        return facts

    @classmethod
    def reconcile_memory(cls, category: str, content: str, confidence: float,
                         source_context: str, triggered_by: str = "interaction") -> Dict[str, Any]:
        """
        Phase 2: Compare candidate fact against existing memories and decide:
        - ADD: Novel fact
        - UPDATE: Refines or contradicts an existing memory
        - DELETE: Invalidates an existing memory
        """
        # Search existing active memories for semantic similarity
        existing_memories = MemoryModel.list_active()
        
        # If text indicates complete cancellation/removal of a previous rule
        is_deletion = any(neg in content.lower() for neg in [
            "don't do this anymore", "delete this rule", "stop suggesting", "no longer valid", "never suggest"
        ])

        top_match: Optional[Dict[str, Any]] = None
        highest_sim = 0.0

        for mem in existing_memories:
            # Query similarity via vector store
            vec_search = global_vector_store.search(content, doc_type="memory", limit=3)
            for hit in vec_search:
                if hit["id"] == mem["id"] and hit["similarity"] > highest_sim:
                    highest_sim = hit["similarity"]
                    top_match = mem

        # Decision 1: DELETE
        if is_deletion and top_match and highest_sim >= CONTRADICTION_THRESHOLD:
            MemoryModel.update(top_match["id"], status="deleted")
            global_vector_store.remove_document(top_match["id"], doc_type="memory")
            
            MemoryDecisionLogModel.log(
                memory_id=top_match["id"],
                action="DELETE",
                reasoning=f"User feedback indicated this preference is no longer valid. Match similarity: {highest_sim:.2f}",
                previous_content=top_match["content"],
                new_content=None,
                triggered_by=triggered_by
            )
            return {
                "action": "DELETE",
                "memory_id": top_match["id"],
                "reasoning": "Memory invalidated and deactivated upon user command."
            }

        # Decision 2: UPDATE (Refinement or contradiction of existing memory)
        if top_match and highest_sim >= CONTRADICTION_THRESHOLD:
            old_content = top_match["content"]
            old_id = top_match["id"]
            new_confidence = min(MAX_CONFIDENCE_WEIGHT, top_match["confidence_weight"] + 0.1)
            new_update_count = top_match["update_count"] + 1

            # Update the existing memory record
            MemoryModel.update(
                old_id,
                content=content,
                confidence_weight=new_confidence,
                update_count=new_update_count,
                source_context=f"{source_context} (Updated from: '{old_content[:40]}...')"
            )

            # Re-index in vector store
            global_vector_store.index_document(
                doc_id=old_id,
                doc_type="memory",
                text=content,
                metadata={"category": category, "confidence_weight": new_confidence}
            )

            reasoning = f"Updated existing memory #{old_id} based on new feedback/correction (similarity: {highest_sim:.2f})."
            MemoryDecisionLogModel.log(
                memory_id=old_id,
                action="UPDATE",
                reasoning=reasoning,
                previous_content=old_content,
                new_content=content,
                triggered_by=triggered_by
            )

            return {
                "action": "UPDATE",
                "memory_id": old_id,
                "previous_content": old_content,
                "new_content": content,
                "reasoning": reasoning
            }

        # Decision 3: ADD (Genuinely new memory)
        new_id = MemoryModel.create(
            category=category,
            content=content,
            confidence_weight=confidence,
            source_context=source_context
        )

        # Index in vector store
        global_vector_store.index_document(
            doc_id=new_id,
            doc_type="memory",
            text=content,
            metadata={"category": category, "confidence_weight": confidence}
        )

        reasoning = f"Novel preference or habit identified. Initial confidence: {confidence:.2f}."
        MemoryDecisionLogModel.log(
            memory_id=new_id,
            action="ADD",
            reasoning=reasoning,
            previous_content=None,
            new_content=content,
            triggered_by=triggered_by
        )

        return {
            "action": "ADD",
            "memory_id": new_id,
            "new_content": content,
            "reasoning": reasoning
        }

    @classmethod
    def process_correction(cls, item_id: Optional[int], original_suggestion: str,
                           user_correction: str, memory_id_applied: Optional[int] = None) -> Dict[str, Any]:
        """
        Processes an explicit user correction on an agent recommendation.
        Weights the correction directly into memory updates or new memory creation.
        """
        # Record feedback event
        FeedbackModel.record(
            suggestion_type="task_suggestion",
            item_id=item_id,
            memory_id_applied=memory_id_applied,
            original_suggestion=original_suggestion,
            user_action="corrected",
            correction_detail=user_correction
        )

        # If a memory was applied, decrease its weight or supersede it
        if memory_id_applied:
            applied_mem = MemoryModel.get(memory_id_applied)
            if applied_mem:
                new_weight = max(MIN_CONFIDENCE_WEIGHT, applied_mem["confidence_weight"] - PENALTY_STEP)
                MemoryModel.update(memory_id_applied, confidence_weight=new_weight)

        # Formulate clean corrected rule
        clean_rule = user_correction.strip()
        if not clean_rule.lower().startswith("user"):
            clean_rule = f"User prefers: {clean_rule}"

        # Reconcile corrected memory
        decision = cls.reconcile_memory(
            category="correction",
            content=clean_rule,
            confidence=0.90,
            source_context=f"Correction on suggestion: '{original_suggestion[:45]}...'",
            triggered_by="feedback_correction"
        )
        return decision

    @classmethod
    def process_acceptance(cls, item_id: Optional[int], original_suggestion: str,
                           memory_id_applied: Optional[int] = None):
        """
        Reinforces a suggestion accepted by the user.
        """
        FeedbackModel.record(
            suggestion_type="task_suggestion",
            item_id=item_id,
            memory_id_applied=memory_id_applied,
            original_suggestion=original_suggestion,
            user_action="accepted"
        )

        if memory_id_applied:
            applied_mem = MemoryModel.get(memory_id_applied)
            if applied_mem:
                new_weight = min(MAX_CONFIDENCE_WEIGHT, applied_mem["confidence_weight"] + REINFORCEMENT_STEP)
                MemoryModel.update(memory_id_applied, confidence_weight=new_weight)
                
                # Log reinforcement
                MemoryDecisionLogModel.log(
                    memory_id=memory_id_applied,
                    action="UPDATE",
                    reasoning=f"Reinforced confidence (+{REINFORCEMENT_STEP:.2f}) after user accepted suggestion.",
                    previous_content=applied_mem["content"],
                    new_content=applied_mem["content"],
                    triggered_by="feedback_acceptance"
                )

    @classmethod
    def initialize_default_vector_index(cls):
        """Re-index all active memories and notes into vector store on startup."""
        active_mems = MemoryModel.list_active()
        for m in active_mems:
            global_vector_store.index_document(
                doc_id=m["id"],
                doc_type="memory",
                text=m["content"],
                metadata={"category": m["category"], "confidence_weight": m["confidence_weight"]},
                auto_rebuild=False
            )
        global_vector_store.rebuild_idf()
        global_vector_store.save()
