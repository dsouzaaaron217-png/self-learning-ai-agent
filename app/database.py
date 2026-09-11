import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from app import config

def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def get_db():
    """Context manager for SQLite database connection with row factory and WAL mode."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    """Initialize database tables and indexes."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Tasks Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                priority TEXT CHECK(priority IN ('low', 'medium', 'high', 'urgent')) DEFAULT 'medium',
                status TEXT CHECK(status IN ('todo', 'in_progress', 'done')) DEFAULT 'todo',
                tags TEXT DEFAULT '',
                due_date TEXT DEFAULT NULL,
                subtasks TEXT DEFAULT '[]',
                ai_suggestion_context TEXT DEFAULT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Notes Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Memories Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT CHECK(category IN ('preference', 'habit', 'correction', 'fact')) NOT NULL,
                content TEXT NOT NULL,
                confidence_weight REAL DEFAULT 0.70,
                status TEXT CHECK(status IN ('active', 'superseded', 'deleted')) DEFAULT 'active',
                source_context TEXT DEFAULT '',
                superseded_by INTEGER DEFAULT NULL REFERENCES memories(id),
                update_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        
        # Memory Decision Logs Table (Audit trail for Add, Update, Delete)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_decision_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                action TEXT CHECK(action IN ('ADD', 'UPDATE', 'DELETE')) NOT NULL,
                reasoning TEXT NOT NULL,
                previous_content TEXT DEFAULT NULL,
                new_content TEXT DEFAULT NULL,
                triggered_by TEXT DEFAULT 'system',
                timestamp TEXT NOT NULL
            )
        """)
        
        # Feedback Events Table (Tracking acceptances and corrections for adaptation metrics)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                suggestion_type TEXT NOT NULL,
                item_id INTEGER DEFAULT NULL,
                memory_id_applied INTEGER DEFAULT NULL,
                original_suggestion TEXT NOT NULL,
                user_action TEXT CHECK(user_action IN ('accepted', 'rejected', 'corrected')) NOT NULL,
                correction_detail TEXT DEFAULT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        
        # Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Indexes for fast retrieval
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_decision_logs_mem ON memory_decision_logs(memory_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_timestamp ON feedback_events(timestamp)")

        # Default settings if empty
        default_settings = [
            ("engine", "local"),
            ("ollama_url", "http://127.0.0.1:11434"),
            ("ollama_model", "llama3.2:3b"),
            ("dark_mode", "true"),
            ("auto_suggest_tasks", "true"),
        ]
        for key, val in default_settings:
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, val))
