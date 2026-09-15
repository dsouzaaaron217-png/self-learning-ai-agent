import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Set
from app.config import SUGGESTION_CONFIDENCE_FLOOR
from app.reasoning.base import BaseReasoner

GENERIC_WORKFLOW_STOP_WORDS = {
    "task", "tasks", "item", "items", "workflow", "work", "routine",
    "marked", "priority", "user", "prefers", "always", "usually",
    "with", "make", "will", "from", "that", "this",
    "urgent", "critical", "high", "low", "medium", "should", "must",
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "by", "is", "are", "be"
}

def extract_meaningful_tokens(text: str) -> Set[str]:
    """
    Normalizes text into a set of distinct meaningful topic tokens.
    Strips punctuation, lowercases, and filters out generic stop words.
    """
    if not text or not isinstance(text, str):
        return set()
    tokens = set(re.findall(r'\b[a-z0-9_]+\b', text.lower()))
    return {t for t in tokens if len(t) > 2 and t not in GENERIC_WORKFLOW_STOP_WORDS}

class LocalReasoner(BaseReasoner):
    """
    100% offline, zero-dependency semantic and heuristic reasoning engine.
    Applies learned memories, rules, and NLP patterns on-device.
    """

    def is_available(self) -> bool:
        return True

    def parse_quick_capture(self, text: str) -> Dict[str, Any]:
        raw = text.strip()
        tags = re.findall(r'#(\w+)', raw)
        clean_text = re.sub(r'#\w+', '', raw).strip()

        # Date / deadline detection
        due_date = None
        lower = clean_text.lower()
        now = datetime.now()
        
        if "today" in lower:
            due_date = now.strftime("%Y-%m-%d")
        elif "tomorrow" in lower:
            due_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "next week" in lower:
            due_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        elif "friday" in lower:
            days_ahead = (4 - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            due_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
        elif "monday" in lower:
            days_ahead = (0 - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            due_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        # Type classification
        is_explicit_note = lower.startswith("note:") or lower.startswith("idea:") or "\n" in raw or len(raw) > 160
        is_task_verb = any(lower.startswith(v) for v in [
            "todo", "task", "remind", "call", "email", "fix", "buy", "review",
            "write", "build", "create", "finish", "prepare", "send", "schedule", "deploy"
        ])
        
        item_type = "note" if is_explicit_note and not is_task_verb else "task"

        # Priority detection from raw text
        priority = "medium"
        if any(w in lower for w in ["urgent", "asap", "critical", "immediately", "blocker", "!high"]):
            priority = "urgent" if "urgent" in lower or "critical" in lower else "high"
        elif any(w in lower for w in ["someday", "low priority", "when free", "!low"]):
            priority = "low"

        # Clean title
        title = re.sub(r'^(note:|idea:|task:|todo:|remind me to)\s*', '', clean_text, flags=re.IGNORECASE).strip()
        if not title:
            title = raw[:50]

        return {
            "type": item_type,
            "title": title,
            "tags": ", ".join(tags),
            "priority": priority,
            "due_date": due_date,
            "raw_text": raw
        }

    def suggest_task_enhancements(self, title: str, description: str,
                                   retrieved_memories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Enhance task with suggested priority, subtasks, and tags,
        heavily weighting learned habits and memories.
        """
        combined_text = f"{title} {description}".lower()
        suggested_priority = "medium"
        applied_memory = None
        rationale = "Evaluated task context and standard workflow patterns."

        # 1. Check if any retrieved memory directly informs priority or workflow
        task_topic_tokens = extract_meaningful_tokens(combined_text)
        candidates = []

        for mem in retrieved_memories:
            confidence = mem.get("metadata", {}).get("confidence_weight")
            if confidence is None:
                confidence = 0.70
            try:
                confidence = float(confidence)
            except (ValueError, TypeError):
                confidence = 0.70

            # A. Confidence Floor: ignore memories below floor (0.40)
            if confidence < SUGGESTION_CONFIDENCE_FLOOR:
                continue

            mem_text = mem.get("text", "").lower()

            # Determine target priority rule
            target_priority = None
            if "urgent" in mem_text or "critical" in mem_text:
                target_priority = "urgent"
            elif "high priority" in mem_text or "high" in mem_text:
                target_priority = "high"
            elif "low priority" in mem_text or "low" in mem_text:
                target_priority = "low"
            elif "medium priority" in mem_text or "medium" in mem_text:
                target_priority = "medium"

            if not target_priority:
                continue

            # B. Meaningful Topic Overlap (require at least one non-generic topic token)
            mem_topic_tokens = extract_meaningful_tokens(mem.get("text", ""))
            overlap = mem_topic_tokens & task_topic_tokens
            if not overlap:
                continue

            # Secondary recency factor
            recency_val = 0.0
            raw_ts = mem.get("timestamp") or mem.get("metadata", {}).get("timestamp")
            if raw_ts:
                try:
                    recency_val = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")).timestamp()
                except Exception:
                    recency_val = 0.0

            mem_id = mem.get("id", 0)
            try:
                mem_id_int = int(mem_id)
            except (ValueError, TypeError):
                mem_id_int = 0

            candidates.append({
                "mem": mem,
                "target_priority": target_priority,
                "confidence": confidence,
                "overlap_count": len(overlap),
                "recency": recency_val,
                "id": mem_id_int
            })

        # C. Candidate Arbitration:
        # Prefer stronger confidence first, then topic overlap count, then recency, with deterministic tie-breaker
        if candidates:
            candidates.sort(
                key=lambda c: (
                    round(c["confidence"], 4),
                    c["overlap_count"],
                    c["recency"],
                    c["id"]
                ),
                reverse=True
            )
            best = candidates[0]
            suggested_priority = best["target_priority"]
            applied_memory = best["mem"]
            conf_pct = int(round(best["confidence"] * 100))
            category_label = applied_memory.get("metadata", {}).get("category", "habit").title()
            rationale = f"Applied learned {category_label.lower()} (Confidence: {conf_pct}%): '{applied_memory.get('text')}'"

        # Fallback to heuristics if no memory match
        if not applied_memory:
            if any(w in combined_text for w in ["prod", "production", "crash", "bug", "security", "tax", "deadline", "urgent"]):
                suggested_priority = "urgent" if "urgent" in combined_text or "crash" in combined_text else "high"
                rationale = "Urgency keywords detected in task description."
            elif any(w in combined_text for w in ["read", "browse", "maybe", "someday", "explore", "idea"]):
                suggested_priority = "low"
                rationale = "Exploratory / non-time-sensitive task."

        # Generate intelligent subtasks
        subtasks = []
        if any(w in combined_text for w in ["client", "meeting", "presentation", "demo", "call", "sync", "interview"]):
            subtasks = [
                {"id": 1, "title": "Prepare agenda, slides & key talking points", "completed": False},
                {"id": 2, "title": "Conduct session and record action items", "completed": False},
                {"id": 3, "title": "Send follow-up notes & deliverables to attendees", "completed": False}
            ]
        elif any(w in combined_text for w in ["code", "build", "implement", "feature", "bug", "fix", "api", "database", "refactor"]):
            subtasks = [
                {"id": 1, "title": "Draft technical requirements & test cases", "completed": False},
                {"id": 2, "title": "Implement core logic & handle edge cases", "completed": False},
                {"id": 3, "title": "Run automated tests & verify offline functionality", "completed": False},
                {"id": 4, "title": "Review code diff & document changes", "completed": False}
            ]
        elif any(w in combined_text for w in ["write", "article", "draft", "post", "documentation", "doc", "proposal"]):
            subtasks = [
                {"id": 1, "title": "Research key points & create structured outline", "completed": False},
                {"id": 2, "title": "Draft complete first version", "completed": False},
                {"id": 3, "title": "Proofread, refine clarity, and publish", "completed": False}
            ]
        elif any(w in combined_text for w in ["plan", "organize", "quarterly", "strategy", "roadmap"]):
            subtasks = [
                {"id": 1, "title": "Audit past outcomes & gather initial data", "completed": False},
                {"id": 2, "title": "Define top 3 measurable milestones", "completed": False},
                {"id": 3, "title": "Assign responsibilities & schedule checkpoints", "completed": False}
            ]
        else:
            subtasks = [
                {"id": 1, "title": f"Review prerequisites for '{title[:35]}'", "completed": False},
                {"id": 2, "title": "Execute core action items", "completed": False},
                {"id": 3, "title": "Verify completion & archive notes", "completed": False}
            ]

        # Suggested tags
        suggested_tags = []
        tag_map = {
            "work": ["meeting", "client", "report", "presentation", "sprint", "roadmap"],
            "dev": ["code", "bug", "api", "feature", "build", "refactor", "deploy", "database"],
            "personal": ["buy", "groceries", "gym", "workout", "doctor", "health", "car"],
            "learning": ["read", "study", "research", "book", "course", "tutorial"]
        }
        for tag, keywords in tag_map.items():
            if any(k in combined_text for k in keywords):
                suggested_tags.append(tag)

        return {
            "suggested_priority": suggested_priority,
            "subtasks": subtasks,
            "suggested_tags": suggested_tags,
            "rationale": rationale,
            "applied_memory_id": applied_memory["id"] if applied_memory else None,
            "applied_memory_text": applied_memory["text"] if applied_memory else None
        }

    def generate_chat_response(self, message: str,
                               chat_history: List[Dict[str, str]],
                               retrieved_memories: List[Dict[str, Any]],
                               active_tasks: List[Dict[str, Any]],
                               recent_notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Synthesizes a helpful, memory-grounded response offline.
        """
        msg_lower = message.lower()
        referenced_memories = []

        # 1. Query about learned habits / memory
        if any(q in msg_lower for q in ["what have you learned", "learned about me", "my habits", "my preferences", "show memories"]):
            if not retrieved_memories and not any(True for _ in [1]):
                text = "I haven't recorded enough habits yet. As you capture notes, create tasks, and correct my suggestions, I'll adapt to your workflow!"
            else:
                mem_list = "\n".join([f"- **{m.get('metadata', {}).get('category', 'Habit').title()}**: {m.get('text')} *(Confidence: {int(m.get('metadata', {}).get('confidence_weight', 0.7)*100)}%)*" for m in retrieved_memories[:5]])
                text = f"Here is what I've learned about your habits and preferences:\n\n{mem_list}\n\nYou can review, fine-tune, or delete any of these in the **Transparency Dashboard**."
                referenced_memories = [m.get("id") for m in retrieved_memories[:5]]

        # 2. Query about tasks or what to do next
        elif any(q in msg_lower for q in ["priority", "priorities", "what should i do", "what's next", "next task", "tasks today"]):
            urgent_tasks = [t for t in active_tasks if t.get("priority") in ("urgent", "high") and t.get("status") != "done"]
            todo_tasks = [t for t in active_tasks if t.get("status") != "done"]
            
            if not todo_tasks:
                text = "🎉 You have no pending tasks! You can use the **Quick Capture** bar (or press `Ctrl+K`) to capture a new goal or note."
            elif urgent_tasks:
                items = "\n".join([f"- **[{t.get('priority').upper()}]** {t.get('title')}" for t in urgent_tasks[:4]])
                text = f"Based on your high-priority queue, here are your top tasks:\n\n{items}\n\nWould you like me to generate subtasks for any of these?"
            else:
                items = "\n".join([f"- {t.get('title')} *({t.get('priority')} priority)*" for t in todo_tasks[:4]])
                text = f"You have {len(todo_tasks)} active task(s). Here are the next ones in queue:\n\n{items}"

        # 3. Query about notes
        elif any(q in msg_lower for q in ["notes", "find note", "search notes", "summarize notes"]):
            if not recent_notes:
                text = "No notes saved yet. Capture your first thought or meeting notes from the **Notes** tab!"
            else:
                items = "\n".join([f"- **{n.get('title')}**: {n.get('content')[:90]}..." for n in recent_notes[:3]])
                text = f"Here are your most recent notes:\n\n{items}\n\nLet me know if you want to expand any of these into concrete tasks."

        # 4. General / Help query
        else:
            grounding = ""
            if retrieved_memories:
                top_m = retrieved_memories[0]
                grounding = f"\n\n*(Grounded on your saved preference: \"{top_m.get('text')}\")*"
                referenced_memories.append(top_m.get("id"))

            text = (
                f"I'm your **Offline Productivity Agent**. Everything runs 100% locally on your machine with zero cloud calls.\n\n"
                f"- **Quick Capture**: Press `Ctrl+K` or type above to log tasks, thoughts, and deadlines.\n"
                f"- **Adaptive Learning**: Whenever you accept or correct my recommendations, I adapt my memory weights.\n"
                f"- **Full Transparency**: Visit the **Memories** tab to inspect *why* I learned each rule."
                f"{grounding}"
            )

        return {
            "reply": text,
            "referenced_memories": referenced_memories,
            "engine": "local"
        }
