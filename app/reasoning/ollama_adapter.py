import json
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional
from app.config import OLLAMA_BASE_URL, DEFAULT_OLLAMA_MODEL, OFFLINE_STRICT_MODE, SUGGESTION_CONFIDENCE_FLOOR
from app.security import validate_ollama_url
from app.reasoning.base import BaseReasoner
from app.reasoning.local_reasoner import LocalReasoner

class OllamaAdapter(BaseReasoner):
    """
    Adapter for communicating with locally hosted LLMs via Ollama API (http://127.0.0.1:11434).
    Guarantees 100% on-device operation; strictly contacts localhost only.
    Falls back gracefully to LocalReasoner if local LLM server is unreachable.
    """

    def __init__(self, base_url: str = OLLAMA_BASE_URL, model: str = DEFAULT_OLLAMA_MODEL):
        self.base_url = validate_ollama_url(base_url, strict_mode=OFFLINE_STRICT_MODE).rstrip("/")
        self.model = model
        self.fallback = LocalReasoner()
        # Explicit empty ProxyHandler prevents inheriting environment proxies (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def is_available(self) -> bool:
        """Check if local Ollama daemon is running on localhost."""
        try:
            validated_url = validate_ollama_url(self.base_url, strict_mode=OFFLINE_STRICT_MODE).rstrip("/")
            req = urllib.request.Request(f"{validated_url}/api/tags", headers={"User-Agent": "Cognito-Local"})
            with self._opener.open(req, timeout=1.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _query_ollama(self, prompt: str, system: Optional[str] = None) -> Optional[str]:
        """Send generation request to local Ollama instance."""
        try:
            validated_url = validate_ollama_url(self.base_url, strict_mode=OFFLINE_STRICT_MODE).rstrip("/")
        except ValueError:
            return None

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False
        }
        if system:
            payload["system"] = system

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{validated_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener.open(req, timeout=12.0) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                return res_json.get("response", "")
        except Exception:
            return None

    def parse_quick_capture(self, text: str) -> Dict[str, Any]:
        # Fast local heuristic parser is virtually instantaneous for capture
        return self.fallback.parse_quick_capture(text)

    def suggest_task_enhancements(self, title: str, description: str,
                                   retrieved_memories: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.is_available():
            return self.fallback.suggest_task_enhancements(title, description, retrieved_memories)

        # Filter candidate memories meeting confidence floor and map by ID
        candidate_map = {}
        for m in (retrieved_memories or [])[:4]:
            conf = m.get("metadata", {}).get("confidence_weight")
            if conf is None:
                conf = 0.70
            try:
                if float(conf) >= SUGGESTION_CONFIDENCE_FLOOR and "id" in m:
                    candidate_map[int(m["id"])] = m
            except (ValueError, TypeError):
                continue

        if candidate_map:
            mem_context = "\n".join([f"- [ID: {m_id}] {m.get('text')}" for m_id, m in candidate_map.items()])
            attribution_instruction = "4. If a specific known habit above directly determined your priority recommendation, set 'applied_memory_id' to its integer ID. Otherwise set 'applied_memory_id' to null."
        else:
            mem_context = "None"
            attribution_instruction = "4. Set 'applied_memory_id' to null."

        prompt = (
            f"User Task: {title}\n"
            f"Description: {description}\n"
            f"Known User Habits & Preferences:\n{mem_context}\n\n"
            f"Based on the above, suggest:\n"
            f"1. Priority: [low | medium | high | urgent]\n"
            f"2. Three concrete subtasks\n"
            f"3. Tags\n"
            f"{attribution_instruction}\n"
            f"Return clean JSON format: {{\"priority\": \"...\", \"subtasks\": [\"...\"], \"tags\": [\"...\"], \"rationale\": \"...\", \"applied_memory_id\": null}}"
        )
        
        raw_res = self._query_ollama(prompt, system="You are an offline personal task assistant. Return only valid JSON.")
        if raw_res:
            try:
                # Extract JSON block
                clean_json = raw_res.strip()
                if "```json" in clean_json:
                    clean_json = clean_json.split("```json")[1].split("```")[0].strip()
                elif "```" in clean_json:
                    clean_json = clean_json.split("```")[1].split("```")[0].strip()
                parsed = json.loads(clean_json)

                # Validate applied_memory_id conservatively
                raw_id = parsed.get("applied_memory_id")
                applied_mem_id = None
                applied_mem_text = None
                if raw_id is not None:
                    try:
                        clean_id = int(raw_id)
                        if clean_id in candidate_map:
                            applied_mem_id = clean_id
                            applied_mem_text = candidate_map[clean_id].get("text")
                    except (ValueError, TypeError):
                        applied_mem_id = None
                        applied_mem_text = None

                subtasks = [
                    {"id": idx + 1, "title": s, "completed": False}
                    for idx, s in enumerate(parsed.get("subtasks", []))
                ]
                return {
                    "suggested_priority": parsed.get("priority", "medium").lower(),
                    "subtasks": subtasks if subtasks else self.fallback.suggest_task_enhancements(title, description, retrieved_memories)["subtasks"],
                    "suggested_tags": parsed.get("tags", []),
                    "rationale": parsed.get("rationale", "Generated by local Ollama model."),
                    "applied_memory_id": applied_mem_id,
                    "applied_memory_text": applied_mem_text
                }
            except Exception:
                pass

        return self.fallback.suggest_task_enhancements(title, description, retrieved_memories)

    def generate_chat_response(self, message: str,
                               chat_history: List[Dict[str, str]],
                               retrieved_memories: List[Dict[str, Any]],
                               active_tasks: List[Dict[str, Any]],
                               recent_notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not self.is_available():
            return self.fallback.generate_chat_response(message, chat_history, retrieved_memories, active_tasks, recent_notes)

        # Format prompt with memory & context grounding
        mem_str = "\n".join([f"- {m.get('text')}" for m in retrieved_memories[:5]]) or "No specific habits found."
        task_str = "\n".join([f"- [{t.get('priority')}] {t.get('title')}" for t in active_tasks[:5]]) or "No open tasks."
        note_str = "\n".join([f"- {n.get('title')}: {n.get('content')[:80]}" for n in recent_notes[:3]]) or "No notes."

        system_prompt = (
            "You are Cognito, an offline personal productivity agent running locally on the user's computer. "
            "Help the user plan their day, organize tasks, and answer questions. "
            "Always respect the user's learned habits and personal preferences."
        )

        user_prompt = (
            f"User's Learned Habits & Preferences:\n{mem_str}\n\n"
            f"Current Active Tasks:\n{task_str}\n\n"
            f"Recent Notes:\n{note_str}\n\n"
            f"User message: {message}\n"
            f"Reply helpfully and concisely:"
        )

        res = self._query_ollama(user_prompt, system=system_prompt)
        if res:
            return {
                "reply": res.strip(),
                "referenced_memories": [m.get("id") for m in retrieved_memories[:3]],
                "engine": f"ollama ({self.model})"
            }

        return self.fallback.generate_chat_response(message, chat_history, retrieved_memories, active_tasks, recent_notes)
