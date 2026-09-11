from typing import Optional
from app.reasoning.base import BaseReasoner
from app.reasoning.local_reasoner import LocalReasoner
from app.reasoning.ollama_adapter import OllamaAdapter
from app.models import SettingsModel

_local_reasoner_instance = LocalReasoner()
_ollama_adapter_instance = OllamaAdapter()

def get_reasoner(engine_type: Optional[str] = None) -> BaseReasoner:
    """
    Return the active reasoning engine.
    If engine_type is not provided, queries current user setting.
    """
    if not engine_type:
        engine_type = SettingsModel.get("engine", "local")

    if engine_type == "ollama":
        url = SettingsModel.get("ollama_url", "http://127.0.0.1:11434")
        model = SettingsModel.get("ollama_model", "llama3.2:3b")
        adapter = OllamaAdapter(base_url=url, model=model)
        if adapter.is_available():
            return adapter
        # Seamless fallback if Ollama is not active
        return _local_reasoner_instance

    return _local_reasoner_instance
