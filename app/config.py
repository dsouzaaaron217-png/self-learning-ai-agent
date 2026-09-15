import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Database and Storage paths
DB_PATH = DATA_DIR / "cognito_agent.db"
VECTOR_STORE_PATH = DATA_DIR / "vector_store.json"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

from app.security import validate_ollama_url

# Application settings
APP_NAME = "Cognito"
APP_VERSION = "1.2.0"
OFFLINE_STRICT_MODE = True  # Enforces 100% local operation

# Local LLM settings (Ollama / LocalAI / LM Studio)
_raw_ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
try:
    OLLAMA_BASE_URL = validate_ollama_url(_raw_ollama_base_url, strict_mode=OFFLINE_STRICT_MODE)
except ValueError as e:
    raise ValueError(f"Invalid OLLAMA_BASE_URL configuration: {e}") from e

DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
DEFAULT_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Active reasoning engine: "local" (built-in deterministic engine) or "ollama"
DEFAULT_REASONING_ENGINE = os.getenv("DEFAULT_ENGINE", "local")

# Memory pipeline thresholds
SIMILARITY_MATCH_THRESHOLD = 0.45  # Threshold for considering memories related
CONTRADICTION_THRESHOLD = 0.40    # Threshold for evaluating conflict / update
BASE_CONFIDENCE_WEIGHT = 0.70     # Default starting confidence
REINFORCEMENT_STEP = 0.15          # Weight boost upon user acceptance
PENALTY_STEP = 0.25                # Weight reduction upon user correction
MAX_CONFIDENCE_WEIGHT = 1.0
MIN_CONFIDENCE_WEIGHT = 0.1
SUGGESTION_CONFIDENCE_FLOOR = 0.40  # Minimum confidence required to drive automated suggestions
REJECTION_PENALTY_STEP = 0.10       # Mild weight reduction upon user suggestion dismissal/rejection
