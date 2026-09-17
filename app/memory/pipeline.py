import re
import string
from typing import List, Dict, Any, Optional, Tuple
from app.models import MemoryModel, MemoryDecisionLogModel, FeedbackModel
from app.database import get_db, utc_now_iso
from app import vector_store
from app.config import (
    SIMILARITY_MATCH_THRESHOLD,
    CONTRADICTION_THRESHOLD,
    BASE_CONFIDENCE_WEIGHT,
    REINFORCEMENT_STEP,
    PENALTY_STEP,
    MAX_CONFIDENCE_WEIGHT,
    MIN_CONFIDENCE_WEIGHT,
    REJECTION_PENALTY_STEP
)

def normalize_statement(text: str) -> str:
    """
    Safely normalizes a statement for identical content matching:
    lowercased, stripped, trailing punctuation removed, whitespace collapsed.
    """
    if not text or not isinstance(text, str):
        return ""
    cleaned = text.strip().lower()
    cleaned = re.sub(r'[.?!;]+$', '', cleaned).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned

class MemoryPipeline:
    """
    Two-Phase Memory Writer:
    Phase 1: Interaction Summarization and Fact Extraction
    Phase 2: Reconciliation (ADD, UPDATE, DELETE, NOOP) with full provenance
    """

    @classmethod
    def _sync_vector_confidence(cls, memory_id: int, new_weight: float):
        """
        Synchronizes updated confidence weight into the vector store document metadata.
        Safely handles missing vector documents or persistence issues without corrupting SQLite.
        """
        try:
            doc_key = f"memory_{memory_id}"
            vs = vector_store.global_vector_store
            if doc_key in vs.documents:
                vs.documents[doc_key]["metadata"]["confidence_weight"] = round(new_weight, 4)
                vs.save()
        except Exception as e:
            print(f"[MemoryPipeline] Vector synchronization failed for confidence on memory #{memory_id}: {e}. SQLite remains authoritative; will reconcile on next validation.")

    @classmethod
    def extract_facts(cls, text: str, context_type: str = "general", source_role: str = "user") -> List[Dict[str, Any]]:
        """
        Phase 1: Extract discrete atomic statements/rules from interactions.
        Identifies preferences, habits, corrections, and facts.
        """
        # Safety: Only extract facts from user-authored content
        if source_role not in ("user",):
            return []

        facts = []
        raw = text.strip()
        lower = raw.lower()

        # Prompt-injection safety: reject content with system prompt manipulation
        INJECTION_PHRASES = [
            "ignore previous", "ignore all previous", "you are now",
            "system:", "[inst]", "[/inst]", "<<sys>>", "<</sys>>",
            "disregard above", "new instructions:", "override:",
            "forget everything", "ignore the above"
        ]
        if any(phrase in lower for phrase in INJECTION_PHRASES):
            return []

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

        # Sanitize extracted facts
        for fact in facts:
            # Strip control characters
            sanitized = ''.join(c for c in fact['content'] if c in string.printable)
            # Enforce max length
            if len(sanitized) > 1000:
                sanitized = sanitized[:1000]
            fact['content'] = sanitized

        return facts

    @classmethod
    def reconcile_memory(cls, category: str, content: str, confidence: float,
                          source_context: str, triggered_by: str = "interaction") -> Dict[str, Any]:
        """
        Phase 2: Compare candidate fact against existing memories and decide:
        - NOOP: Identical statement already active (idempotent confirmation)
        - UPDATE: Refines or contradicts an existing memory
        - DELETE: Invalidates an existing memory
        - ADD: Novel fact
        """
        existing_memories = MemoryModel.list_active()
        vs = vector_store.global_vector_store

        # 1. Idempotency check: Exact normalized match against existing active memories
        norm_candidate = normalize_statement(content)
        for mem in existing_memories:
            if normalize_statement(mem["content"]) == norm_candidate:
                return {
                    "action": "NOOP",
                    "memory_id": mem["id"],
                    "content": mem["content"],
                    "vector_synced": True,
                    "reasoning": f"Identical active memory #{mem['id']} already exists; confirmed without duplicate creation."
                }

        # If text indicates complete cancellation/removal of a previous rule
        is_deletion = any(neg in content.lower() for neg in [
            "don't do this anymore", "delete this rule", "stop suggesting", "no longer valid", "never suggest"
        ])

        # 2. Pure semantic matching: Compute cosine similarity against all active memories once
        top_match: Optional[Dict[str, Any]] = None
        highest_sim = 0.0

        query_tokens = vector_store.tokenize(content)
        query_tf = vs._compute_tf(query_tokens)
        query_vec = vs._vectorize_tf(query_tf)

        # For cancellation and correction queries, also vectorize the target rule content without meta-phrases
        is_correction = category == 'correction' or 'correction' in triggered_by
        clean_query_vec = None
        if is_deletion or is_correction:
            clean_content = re.sub(
                r'(?i)\b(don\'t do this anymore|delete this rule|stop suggesting|no longer valid|never suggest|user corrected|actually|change it to|instead|should be)[:\s\'\"]*',
                '',
                content
            ).strip()
            if clean_content:
                clean_tokens = vector_store.tokenize(clean_content)
                clean_tf = vs._compute_tf(clean_tokens)
                clean_query_vec = vs._vectorize_tf(clean_tf)

        if (query_vec or clean_query_vec) and existing_memories:
            for mem in existing_memories:
                doc_key = f"memory_{mem['id']}"
                if doc_key in vs.documents and vs.documents[doc_key].get("vector"):
                    doc_vec = vs.documents[doc_key]["vector"]
                else:
                    mem_tokens = vector_store.tokenize(mem["content"])
                    mem_tf = vs._compute_tf(mem_tokens)
                    doc_vec = vs._vectorize_tf(mem_tf)

                sim = vs.cosine_similarity(query_vec, doc_vec) if query_vec else 0.0
                if clean_query_vec:
                    clean_sim = vs.cosine_similarity(clean_query_vec, doc_vec)
                    if clean_sim > sim:
                        sim = clean_sim

                if sim > highest_sim:
                    highest_sim = sim
                    top_match = mem

        # Decision 1: DELETE (Cancellation intent with similarity >= CONTRADICTION_THRESHOLD)
        if is_deletion and top_match and highest_sim >= CONTRADICTION_THRESHOLD:
            MemoryModel.update(top_match["id"], status="deleted")
            vector_synced = True
            try:
                vs.remove_document(top_match["id"], doc_type="memory")
            except Exception as e:
                vector_synced = False
                print(f"[MemoryPipeline] Vector synchronization failed for deleted memory #{top_match['id']}: {e}. SQLite remains authoritative; will reconcile on next validation.")

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
                "vector_synced": vector_synced,
                "reasoning": "Memory invalidated and deactivated upon user command."
            }

        is_correction = category == 'correction' or 'correction' in triggered_by

        # Decision 1.5: SUPERSEDE (Correction intent with similarity >= SIMILARITY_MATCH_THRESHOLD)
        if not is_deletion and is_correction and top_match and highest_sim >= SIMILARITY_MATCH_THRESHOLD:
            old_id = top_match["id"]
            now = utc_now_iso()
            reasoning = f"Superseded memory #{old_id} with new correction. Match similarity: {highest_sim:.2f}"

            # Atomic SQLite operation: create replacement, mark old superseded, record log
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO memories (category, content, confidence_weight, status, source_context, update_count, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, 0, ?, ?)
                """, (category, content.strip(), confidence, source_context.strip(), now, now))
                new_id = cur.lastrowid

                cur.execute("""
                    UPDATE memories
                    SET status = 'superseded', superseded_by = ?, updated_at = ?
                    WHERE id = ?
                """, (new_id, now, old_id))

                cur.execute("""
                    INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, 'SUPERSEDE', ?, ?, ?, ?, ?)
                """, (new_id, reasoning, top_match["content"], content, triggered_by, now))

            vector_synced = True
            try:
                vs.remove_document(old_id, doc_type="memory")
                vs.index_document(
                    doc_id=new_id,
                    doc_type="memory",
                    text=content,
                    metadata={"category": category, "confidence_weight": confidence}
                )
            except Exception as e:
                vector_synced = False
                print(f"[MemoryPipeline] Vector synchronization failed for superseded memory #{old_id}->#{new_id}: {e}")

            return {
                "action": "SUPERSEDE",
                "memory_id": new_id,
                "superseded_memory_id": old_id,
                "vector_synced": vector_synced,
                "reasoning": reasoning
            }

        # Decision 1.8: FLAG_FOR_REVIEW (Ambiguous conflict: CONTRADICTION_THRESHOLD <= similarity < SIMILARITY_MATCH_THRESHOLD)
        if not is_deletion and top_match and CONTRADICTION_THRESHOLD <= highest_sim < SIMILARITY_MATCH_THRESHOLD:
            old_id = top_match["id"]
            now = utc_now_iso()
            flag_confidence = min(confidence, 0.35)
            reasoning = f"Flagged for review due to conflict with active memory #{old_id}. Match similarity: {highest_sim:.2f}"

            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO memories (category, content, confidence_weight, status, source_context, update_count, created_at, updated_at)
                    VALUES (?, ?, ?, 'active', ?, 0, ?, ?)
                """, (category, content.strip(), flag_confidence, source_context.strip(), now, now))
                new_id = cur.lastrowid

                cur.execute("""
                    INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, 'FLAG_FOR_REVIEW', ?, ?, ?, ?, ?)
                """, (new_id, reasoning, top_match["content"], content, triggered_by, now))

            vector_synced = True
            try:
                vs.index_document(
                    doc_id=new_id,
                    doc_type="memory",
                    text=content,
                    metadata={"category": category, "confidence_weight": flag_confidence, "flagged": True}
                )
            except Exception as e:
                vector_synced = False
                print(f"[MemoryPipeline] Vector synchronization failed for flagged memory #{new_id}: {e}")

            return {
                "action": "FLAG_FOR_REVIEW",
                "memory_id": new_id,
                "conflicting_memory_id": old_id,
                "vector_synced": vector_synced,
                "reasoning": reasoning
            }

        # Decision 2: UPDATE (General high-similarity refinement: similarity >= SIMILARITY_MATCH_THRESHOLD and not correction)
        if not is_deletion and not is_correction and top_match and highest_sim >= SIMILARITY_MATCH_THRESHOLD:
            old_content = top_match["content"]
            old_id = top_match["id"]
            new_confidence = min(MAX_CONFIDENCE_WEIGHT, top_match["confidence_weight"] + 0.1)
            new_update_count = top_match["update_count"] + 1

            # Bounded provenance context: strip prior nested update suffixes to avoid recursive concatenation
            clean_source = re.sub(r'\s*\(Updated from:[\s\S]*?\)', '', source_context).strip()
            snippet = old_content[:40] + ("..." if len(old_content) > 40 else "")
            bounded_context = f"{clean_source} (Updated from: '{snippet}')" if clean_source else f"Updated from: '{snippet}'"

            # Update SQLite
            MemoryModel.update(
                old_id,
                content=content,
                confidence_weight=new_confidence,
                update_count=new_update_count,
                source_context=bounded_context
            )

            # Re-index in vector store
            vector_synced = True
            try:
                vs.index_document(
                    doc_id=old_id,
                    doc_type="memory",
                    text=content,
                    metadata={"category": category, "confidence_weight": new_confidence}
                )
            except Exception as e:
                vector_synced = False
                print(f"[MemoryPipeline] Vector synchronization failed for updated memory #{old_id}: {e}. SQLite remains authoritative; will reconcile on next validation.")

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
                "vector_synced": vector_synced,
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
        vector_synced = True
        try:
            vs.index_document(
                doc_id=new_id,
                doc_type="memory",
                text=content,
                metadata={"category": category, "confidence_weight": confidence}
            )
        except Exception as e:
            vector_synced = False
            print(f"[MemoryPipeline] Vector synchronization failed for new memory #{new_id}: {e}. SQLite remains authoritative; will reconcile on next validation.")

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
            "vector_synced": vector_synced,
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
                cls._sync_vector_confidence(memory_id_applied, new_weight)

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
                cls._sync_vector_confidence(memory_id_applied, new_weight)
                
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
    def process_rejection(cls, item_id: Optional[int], original_suggestion: str,
                          memory_id_applied: Optional[int] = None):
        """
        Records user rejection/dismissal of an AI suggestion.
        Applies a mild confidence penalty (-0.10) to the applied memory if present,
        never dropping below MIN_CONFIDENCE_WEIGHT.
        Does NOT create a correction rule.
        """
        FeedbackModel.record(
            suggestion_type="task_suggestion",
            item_id=item_id,
            memory_id_applied=memory_id_applied,
            original_suggestion=original_suggestion,
            user_action="rejected"
        )

        if memory_id_applied:
            applied_mem = MemoryModel.get(memory_id_applied)
            if applied_mem:
                new_weight = max(MIN_CONFIDENCE_WEIGHT, round(applied_mem["confidence_weight"] - REJECTION_PENALTY_STEP, 4))
                MemoryModel.update(memory_id_applied, confidence_weight=new_weight)
                cls._sync_vector_confidence(memory_id_applied, new_weight)

                # Log mild penalty
                MemoryDecisionLogModel.log(
                    memory_id=memory_id_applied,
                    action="UPDATE",
                    reasoning=f"Reduced confidence (-{REJECTION_PENALTY_STEP:.2f}) after user dismissed/rejected suggestion.",
                    previous_content=applied_mem["content"],
                    new_content=applied_mem["content"],
                    triggered_by="feedback_rejection"
                )

    @classmethod
    def initialize_default_vector_index(cls):
        """Ensure vector store is populated and valid on startup, recovering from SQLite if needed."""
        return vector_store.global_vector_store.ensure_valid_index()
