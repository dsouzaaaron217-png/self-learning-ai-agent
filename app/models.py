import json
import re
from typing import List, Dict, Any, Optional
from app.config import BASE_CONFIDENCE_WEIGHT
from app.database import get_db, utc_now_iso

class TaskModel:
    @staticmethod
    def create(title: str, description: str = '', priority: str = 'medium',
               status: str = 'todo', tags: str = '', due_date: Optional[str] = None,
               subtasks: Optional[List[Dict[str, Any]]] = None,
               ai_suggestion_context: Optional[Dict[str, Any]] = None) -> int:
        now = utc_now_iso()
        subtasks_json = json.dumps(subtasks or [])
        ai_ctx_json = json.dumps(ai_suggestion_context) if ai_suggestion_context else None
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO tasks (title, description, priority, status, tags, due_date, subtasks, ai_suggestion_context, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (title.strip(), description.strip(), priority, status, tags.strip(), due_date, subtasks_json, ai_ctx_json, now, now))
            return cur.lastrowid

    @staticmethod
    def get(task_id: int) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            res['subtasks'] = json.loads(res['subtasks'] or '[]')
            if res.get('ai_suggestion_context'):
                res['ai_suggestion_context'] = json.loads(res['ai_suggestion_context'])
            return res

    @staticmethod
    def list_all(status: Optional[str] = None, priority: Optional[str] = None,
                 q: Optional[str] = None, tag: Optional[str] = None,
                 due: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params = []
        conditions = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if priority:
            conditions.append("priority = ?")
            params.append(priority)
        if q:
            conditions.append("(title LIKE ? OR description LIKE ?)")
            like_q = f"%{q}%"
            params.extend([like_q, like_q])
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if due == "overdue":
            conditions.append("due_date < date('now') AND due_date IS NOT NULL AND due_date != '' AND status != 'done'")
        elif due == "today":
            conditions.append("due_date = date('now') AND due_date IS NOT NULL AND due_date != ''")
        elif due == "upcoming":
            conditions.append("due_date > date('now') AND due_date IS NOT NULL AND due_date != ''")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY CASE priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END, id DESC"
        
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            tasks = []
            for row in rows:
                t = dict(row)
                t['subtasks'] = json.loads(t['subtasks'] or '[]')
                if t.get('ai_suggestion_context'):
                    t['ai_suggestion_context'] = json.loads(t['ai_suggestion_context'])
                tasks.append(t)
            return tasks

    @staticmethod
    def update(task_id: int, **kwargs) -> bool:
        allowed = {'title', 'description', 'priority', 'status', 'tags', 'due_date', 'subtasks', 'ai_suggestion_context'}
        fields = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                if k in ('subtasks', 'ai_suggestion_context') and isinstance(v, (dict, list)):
                    values.append(json.dumps(v))
                else:
                    values.append(v)
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(task_id)

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", tuple(values))
            return cur.rowcount > 0

    @staticmethod
    def delete(task_id: int) -> bool:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            return cur.rowcount > 0


class NoteModel:
    @staticmethod
    def create(title: str, content: str, tags: str = '', pinned: int = 0) -> int:
        now = utc_now_iso()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO notes (title, content, tags, pinned, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title.strip(), content.strip(), tags.strip(), pinned, now, now))
            return cur.lastrowid

    @staticmethod
    def get(note_id: int) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_all() -> List[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM notes ORDER BY pinned DESC, updated_at DESC")
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def update(note_id: int, **kwargs) -> bool:
        allowed = {'title', 'content', 'tags', 'pinned'}
        fields = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                values.append(v)
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(note_id)

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE notes SET {', '.join(fields)} WHERE id = ?", tuple(values))
            return cur.rowcount > 0

    @staticmethod
    def delete(note_id: int) -> bool:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            return cur.rowcount > 0


class MemoryModel:
    @staticmethod
    def create(category: str, content: str, confidence_weight: float = 0.70,
               source_context: str = '', status: str = 'active') -> int:
        now = utc_now_iso()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO memories (category, content, confidence_weight, status, source_context, update_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """, (category, content.strip(), confidence_weight, status, source_context.strip(), now, now))
            return cur.lastrowid

    @staticmethod
    def get(memory_id: int) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM memories WHERE id = ?", (memory_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    @staticmethod
    def list_active(category: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT m.*,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM memory_decision_logs l
                       WHERE l.memory_id = m.id AND l.action = 'FLAG_FOR_REVIEW'
                       AND NOT EXISTS (
                           SELECT 1 FROM memory_decision_logs r
                           WHERE r.memory_id = m.id AND r.triggered_by = 'conflict_resolution'
                           AND r.id > l.id
                       )
                   ) THEN 1 ELSE 0 END as is_flagged
            FROM memories m
            WHERE m.status = 'active'
        """
        params = []
        if category:
            query += " AND m.category = ?"
            params.append(category)
        query += " ORDER BY m.confidence_weight DESC, m.updated_at DESC"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def list_all(status: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT m.*,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM memory_decision_logs l
                       WHERE l.memory_id = m.id AND l.action = 'FLAG_FOR_REVIEW'
                       AND NOT EXISTS (
                           SELECT 1 FROM memory_decision_logs r
                           WHERE r.memory_id = m.id AND r.triggered_by = 'conflict_resolution'
                           AND r.id > l.id
                       )
                   ) THEN 1 ELSE 0 END as is_flagged
            FROM memories m
        """
        params = []
        if status:
            query += " WHERE m.status = ?"
            params.append(status)
        query += " ORDER BY m.updated_at DESC"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def list_superseded() -> List[Dict[str, Any]]:
        """Returns superseded memories with relationship to their replacements."""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT m.*, r.content as replacement_content
                FROM memories m
                LEFT JOIN memories r ON m.superseded_by = r.id
                WHERE m.status = 'superseded'
                ORDER BY m.updated_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def list_flagged_conflicts() -> List[Dict[str, Any]]:
        """Returns active memories involved in unresolved FLAG_FOR_REVIEW decisions."""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT m.*
                FROM memories m
                INNER JOIN memory_decision_logs l ON m.id = l.memory_id
                WHERE m.status = 'active' AND l.action = 'FLAG_FOR_REVIEW'
                AND NOT EXISTS (
                    SELECT 1 FROM memory_decision_logs r
                    WHERE r.memory_id = m.id AND r.triggered_by = 'conflict_resolution'
                    AND r.id > l.id
                )
                ORDER BY m.updated_at DESC
            """)
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def search_all_statuses(q: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search memories by content text, optionally filtered by status."""
        query = """
            SELECT m.*,
                   CASE WHEN EXISTS (
                       SELECT 1 FROM memory_decision_logs l
                       WHERE l.memory_id = m.id AND l.action = 'FLAG_FOR_REVIEW'
                       AND NOT EXISTS (
                           SELECT 1 FROM memory_decision_logs r
                           WHERE r.memory_id = m.id AND r.triggered_by = 'conflict_resolution'
                           AND r.id > l.id
                       )
                   ) THEN 1 ELSE 0 END as is_flagged
            FROM memories m
            WHERE m.content LIKE ?
        """
        params = [f"%{q}%"]
        if status:
            query += " AND m.status = ?"
            params.append(status)
        query += " ORDER BY m.updated_at DESC"
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(query, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get_unresolved_conflict(memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Returns full conflict information for an active candidate memory if it is
        currently in an unresolved FLAG_FOR_REVIEW conflict state.
        Returns None if memory does not exist, is not active, or is already resolved.
        """
        candidate = MemoryModel.get(memory_id)
        if not candidate or candidate["status"] != "active":
            return None

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM memory_decision_logs
                WHERE memory_id = ? AND action = 'FLAG_FOR_REVIEW'
                ORDER BY id DESC LIMIT 1
            """, (memory_id,))
            flag_log = cur.fetchone()
            if not flag_log:
                return None

            flag_dict = dict(flag_log)

            cur.execute("""
                SELECT 1 FROM memory_decision_logs
                WHERE memory_id = ? AND triggered_by = 'conflict_resolution' AND id > ?
                LIMIT 1
            """, (memory_id, flag_dict["id"]))
            if cur.fetchone():
                return None

        conflicting_id = None
        match = re.search(r'active memory #(\d+)', flag_dict.get("reasoning", ""))
        if match:
            try:
                conflicting_id = int(match.group(1))
            except ValueError:
                conflicting_id = None

        sim_score = None
        sim_match = re.search(r'similarity:\s*([\d\.]+)', flag_dict.get("reasoning", ""), re.IGNORECASE)
        if sim_match:
            try:
                sim_score = float(sim_match.group(1))
            except ValueError:
                sim_score = None

        conflicting_mem = None
        if conflicting_id:
            conflicting_mem = MemoryModel.get(conflicting_id)

        if not conflicting_mem and flag_dict.get("previous_content"):
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM memories WHERE content = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                    (flag_dict["previous_content"],)
                )
                row = cur.fetchone()
                if row:
                    conflicting_mem = dict(row)
                    conflicting_id = conflicting_mem["id"]

        if conflicting_mem:
            conflicting_data = {
                "id": conflicting_mem["id"],
                "category": conflicting_mem["category"],
                "content": conflicting_mem["content"],
                "confidence": conflicting_mem.get("confidence_weight", BASE_CONFIDENCE_WEIGHT),
                "confidence_weight": conflicting_mem.get("confidence_weight", BASE_CONFIDENCE_WEIGHT),
                "status": conflicting_mem["status"],
                "source_context": conflicting_mem.get("source_context", ""),
                "created_at": conflicting_mem.get("created_at"),
                "updated_at": conflicting_mem.get("updated_at")
            }
        elif flag_dict.get("previous_content"):
            conflicting_data = {
                "id": conflicting_id,
                "category": candidate["category"],
                "content": flag_dict["previous_content"],
                "confidence": None,
                "confidence_weight": None,
                "status": "active" if conflicting_id else "unknown",
                "source_context": "",
                "created_at": None,
                "updated_at": None
            }
        else:
            conflicting_data = None

        candidate_data = {
            "id": candidate["id"],
            "category": candidate["category"],
            "content": candidate["content"],
            "confidence": candidate.get("confidence_weight", BASE_CONFIDENCE_WEIGHT),
            "confidence_weight": candidate.get("confidence_weight", BASE_CONFIDENCE_WEIGHT),
            "status": candidate["status"],
            "source_context": candidate.get("source_context", ""),
            "created_at": candidate.get("created_at"),
            "updated_at": candidate.get("updated_at")
        }

        return {
            "id": candidate["id"],
            "candidate_memory": candidate_data,
            "conflicting_memory": conflicting_data,
            "similarity": sim_score,
            "conflict_provenance": {
                "decision_log_id": flag_dict["id"],
                "reasoning": flag_dict.get("reasoning", ""),
                "similarity_score": sim_score,
                "timestamp": flag_dict.get("timestamp"),
                "trigger": flag_dict.get("triggered_by", "system"),
                "triggered_by": flag_dict.get("triggered_by", "system"),
                "conflicting_memory_id": conflicting_id
            }
        }

    @staticmethod
    def list_unresolved_conflicts() -> List[Dict[str, Any]]:
        """Returns all unresolved active memory conflicts."""
        flagged_memories = MemoryModel.list_flagged_conflicts()
        conflicts = []
        for mem in flagged_memories:
            c = MemoryModel.get_unresolved_conflict(mem["id"])
            if c:
                conflicts.append(c)
        return conflicts

    @staticmethod
    def resolve_conflict(memory_id: int, action: str, vector_store: Optional[Any] = None) -> Dict[str, Any]:
        """
        Atomically resolves a memory conflict in SQLite and synchronizes the vector store.
        Supported actions: 'keep_new', 'keep_old', 'keep_both'.
        """
        if action not in ('keep_new', 'keep_old', 'keep_both'):
            raise ValueError(f"Invalid resolution action: {action}")

        conflict = MemoryModel.get_unresolved_conflict(memory_id)
        if not conflict:
            raise ValueError(f"Memory #{memory_id} is not in an unresolved conflict state.")

        candidate = conflict["candidate_memory"]
        conflicting = conflict["conflicting_memory"]
        conflicting_id = conflicting["id"] if conflicting else None
        now = utc_now_iso()
        new_confidence = max(candidate.get("confidence_weight", BASE_CONFIDENCE_WEIGHT), BASE_CONFIDENCE_WEIGHT)

        with get_db() as conn:
            cur = conn.cursor()

            if action == "keep_new":
                # 1. Candidate remains active; restore confidence; clear flag state
                cur.execute("""
                    UPDATE memories
                    SET confidence_weight = ?, updated_at = ?
                    WHERE id = ?
                """, (new_confidence, now, memory_id))

                # 2. Conflicting memory becomes superseded by candidate
                if conflicting_id:
                    cur.execute("""
                        UPDATE memories
                        SET status = 'superseded', superseded_by = ?, updated_at = ?
                        WHERE id = ?
                    """, (memory_id, now, conflicting_id))

                # 3. Decision log on candidate memory
                reasoning = (
                    f"User resolved conflict: kept new memory #{memory_id}, superseded memory #{conflicting_id}."
                    if conflicting_id else
                    f"User resolved conflict: kept new memory #{memory_id}."
                )
                cur.execute("""
                    INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, 'SUPERSEDE', ?, ?, ?, 'conflict_resolution', ?)
                """, (
                    memory_id,
                    reasoning,
                    conflicting["content"] if conflicting else None,
                    candidate["content"],
                    now
                ))

            elif action == "keep_old":
                # 1. Candidate memory is deactivated (status = 'deleted')
                cur.execute("""
                    UPDATE memories
                    SET status = 'deleted', updated_at = ?
                    WHERE id = ?
                """, (now, memory_id))

                # 2. Conflicting memory remains active and unchanged

                # 3. Decision log on candidate memory
                reasoning = (
                    f"User resolved conflict: rejected new candidate memory #{memory_id} in favor of existing memory #{conflicting_id}."
                    if conflicting_id else
                    f"User resolved conflict: rejected new candidate memory #{memory_id}."
                )
                cur.execute("""
                    INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, 'DELETE', ?, ?, NULL, 'conflict_resolution', ?)
                """, (
                    memory_id,
                    reasoning,
                    candidate["content"],
                    now
                ))

            elif action == "keep_both":
                # 1. Candidate remains active; restore confidence; clear flag state
                cur.execute("""
                    UPDATE memories
                    SET confidence_weight = ?, updated_at = ?
                    WHERE id = ?
                """, (new_confidence, now, memory_id))

                # 2. Conflicting memory remains active and unchanged

                # 3. Decision log on candidate memory
                reasoning = (
                    f"User resolved conflict: kept both memories (#{memory_id} and #{conflicting_id}) as valid preferences."
                    if conflicting_id else
                    f"User resolved conflict: kept candidate memory #{memory_id} as valid preference."
                )
                cur.execute("""
                    INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                    VALUES (?, 'UPDATE', ?, ?, ?, 'conflict_resolution', ?)
                """, (
                    memory_id,
                    reasoning,
                    candidate["content"],
                    candidate["content"],
                    now
                ))

        # Vector store synchronization
        if vector_store is None:
            try:
                from app.vector_store import global_vector_store
                vs = global_vector_store
            except Exception:
                vs = None
        else:
            vs = vector_store

        vector_synced = True
        if vs is not None:
            try:
                if action == "keep_new":
                    if conflicting_id:
                        vs.remove_document(conflicting_id, doc_type="memory")
                    vs.index_document(
                        doc_id=memory_id,
                        doc_type="memory",
                        text=candidate["content"],
                        metadata={"category": candidate["category"], "confidence_weight": new_confidence, "flagged": False}
                    )
                elif action == "keep_old":
                    vs.remove_document(memory_id, doc_type="memory")
                elif action == "keep_both":
                    vs.index_document(
                        doc_id=memory_id,
                        doc_type="memory",
                        text=candidate["content"],
                        metadata={"category": candidate["category"], "confidence_weight": new_confidence, "flagged": False}
                    )
            except Exception as e:
                vector_synced = False
                print(f"[MemoryConflict] Warning: vector store synchronization failed during conflict resolution for memory #{memory_id}: {e}. SQLite remains authoritative.")

        return {
            "success": True,
            "action": action,
            "memory_id": memory_id,
            "conflicting_memory_id": conflicting_id,
            "vector_synced": vector_synced,
            "memory": MemoryModel.get(memory_id)
        }

    @staticmethod
    def update(memory_id: int, **kwargs) -> bool:
        allowed = {'category', 'content', 'confidence_weight', 'status', 'source_context', 'superseded_by', 'update_count'}
        fields = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                fields.append(f"{k} = ?")
                values.append(v)
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(memory_id)

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", tuple(values))
            return cur.rowcount > 0

    @staticmethod
    def delete(memory_id: int) -> bool:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0


class MemoryDecisionLogModel:
    @staticmethod
    def log(memory_id: int, action: str, reasoning: str,
            previous_content: Optional[str] = None,
            new_content: Optional[str] = None,
            triggered_by: str = 'system') -> int:
        now = utc_now_iso()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (memory_id, action, reasoning, previous_content, new_content, triggered_by, now))
            return cur.lastrowid

    @staticmethod
    def get_for_memory(memory_id: int) -> List[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM memory_decision_logs 
                WHERE memory_id = ? 
                ORDER BY timestamp DESC
            """, (memory_id,))
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def get_recent(limit: int = 50) -> List[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT l.*, m.category as memory_category, m.content as current_memory_content
                FROM memory_decision_logs l
                LEFT JOIN memories m ON l.memory_id = m.id
                ORDER BY l.timestamp DESC
                LIMIT ?
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]


class FeedbackModel:
    @staticmethod
    def record(suggestion_type: str, item_id: Optional[int], memory_id_applied: Optional[int],
               original_suggestion: str, user_action: str, correction_detail: Optional[str] = None) -> int:
        now = utc_now_iso()
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO feedback_events (suggestion_type, item_id, memory_id_applied, original_suggestion, user_action, correction_detail, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (suggestion_type, item_id, memory_id_applied, original_suggestion, user_action, correction_detail, now))
            return cur.lastrowid

    @staticmethod
    def get_metrics() -> Dict[str, Any]:
        """Calculates adaptation metrics and correction rate over time."""
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM feedback_events")
            total = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM feedback_events WHERE user_action = 'accepted'")
            accepted = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM feedback_events WHERE user_action = 'corrected'")
            corrected = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM feedback_events WHERE user_action = 'rejected'")
            rejected = cur.fetchone()[0]

            # Recent events (last 10)
            cur.execute("SELECT * FROM feedback_events ORDER BY timestamp DESC LIMIT 10")
            recent = [dict(r) for r in cur.fetchall()]

            correction_rate = (corrected / total * 100.0) if total > 0 else 0.0
            acceptance_rate = (accepted / total * 100.0) if total > 0 else 0.0
            rejection_rate = (rejected / total * 100.0) if total > 0 else 0.0

            return {
                "total_events": total,
                "accepted_count": accepted,
                "corrected_count": corrected,
                "rejected_count": rejected,
                "correction_rate_percent": round(correction_rate, 1),
                "acceptance_rate_percent": round(acceptance_rate, 1),
                "rejection_rate_percent": round(rejection_rate, 1),
                "recent_feedback": recent
            }


class SettingsModel:
    @staticmethod
    def get(key: str, default: Optional[str] = None) -> Optional[str]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else default

    @staticmethod
    def set(key: str, value: str):
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))

    @staticmethod
    def get_all() -> Dict[str, str]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM settings")
            return {row[0]: row[1] for row in cur.fetchall()}


class ChatMessageModel:
    @staticmethod
    def create(
        session_id: str,
        role: str,
        content: str,
        cited_memories: Optional[Any] = None,
        created_at: Optional[str] = None
    ) -> int:
        now = created_at or utc_now_iso()
        citations_json = None
        if cited_memories is not None:
            if isinstance(cited_memories, str):
                citations_json = cited_memories
            else:
                citations_json = json.dumps(cited_memories)

        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO chat_messages (session_id, role, content, cited_memories, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id.strip(), role.strip().lower(), content.strip(), citations_json, now))
            return cur.lastrowid

    @staticmethod
    def get(message_id: int) -> Optional[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get('cited_memories'):
                try:
                    res['cited_memories'] = json.loads(res['cited_memories'])
                except Exception:
                    pass
            else:
                res['cited_memories'] = []
            return res

    @staticmethod
    def list_by_session(
        session_id: str = "default",
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM chat_messages
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ? OFFSET ?
            """, (session_id.strip(), limit, offset))
            rows = cur.fetchall()
            messages = []
            for row in rows:
                m = dict(row)
                if m.get('cited_memories'):
                    try:
                        m['cited_memories'] = json.loads(m['cited_memories'])
                    except Exception:
                        pass
                else:
                    m['cited_memories'] = []
                messages.append(m)
            return messages

    @staticmethod
    def clear_history(session_id: Optional[str] = None) -> int:
        with get_db() as conn:
            cur = conn.cursor()
            if session_id:
                cur.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id.strip(),))
            else:
                cur.execute("DELETE FROM chat_messages")
            return cur.rowcount

    @staticmethod
    def delete(message_id: int) -> bool:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
            return cur.rowcount > 0
