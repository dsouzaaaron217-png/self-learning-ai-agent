from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class BaseReasoner(ABC):
    @abstractmethod
    def parse_quick_capture(self, text: str) -> Dict[str, Any]:
        """Classify input as task/note/reminder, extract tags, dates, priority."""
        pass

    @abstractmethod
    def suggest_task_enhancements(self, title: str, description: str,
                                   retrieved_memories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Suggest priority, subtasks, tags based on learned habits and text."""
        pass

    @abstractmethod
    def generate_chat_response(self, message: str,
                               chat_history: List[Dict[str, str]],
                               retrieved_memories: List[Dict[str, Any]],
                               active_tasks: List[Dict[str, Any]],
                               recent_notes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate response with memory grounding and action suggestions."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this reasoning engine is ready to use."""
        pass
