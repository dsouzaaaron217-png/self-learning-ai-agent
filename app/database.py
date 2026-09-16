import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Tuple, Callable, Union, Set
from app import config

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
CURRENT_SCHEMA_VERSION = 1

def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def get_db(db_path: Optional[Union[Path, str]] = None):
    """Context manager for SQLite database connection with row factory, foreign keys, and WAL mode."""
    target_path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(target_path, timeout=15.0)
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

def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensures that the schema_migrations table exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)

def get_applied_migrations(conn: sqlite3.Connection) -> List[int]:
    """Returns a list of applied migration versions in ascending order."""
    ensure_migration_table(conn)
    cur = conn.cursor()
    cur.execute(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version ASC")
    return [row[0] for row in cur.fetchall()]

def get_current_migration_version(conn: sqlite3.Connection) -> int:
    """Returns the highest applied migration version number, or 0 if none applied."""
    applied = get_applied_migrations(conn)
    return max(applied) if applied else 0

def migration_001_baseline_schema(conn: sqlite3.Connection) -> None:
    """
    Migration 001: Baseline schema for Cognito (Phases 1-4).
    Safely creates all baseline tables, indexes, and initial settings.
    Idempotent and safe for both fresh databases and existing populated databases.
    """
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

MigrationRegistryEntry = Tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: List[MigrationRegistryEntry] = [
    (1, "baseline_schema", migration_001_baseline_schema),
]

def apply_single_migration(
    conn: sqlite3.Connection,
    version: int,
    name: str,
    migration_func: Callable[[sqlite3.Connection], None]
) -> None:
    """
    Executes a single migration within an atomic, isolated transaction.
    If the migration succeeds, records the version in schema_migrations and commits.
    If the migration fails, rolls back all changes and leaves schema_migrations unchanged.
    """
    old_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        migration_func(conn)
        conn.execute(
            f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, applied_at) VALUES (?, ?)",
            (version, utc_now_iso())
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.isolation_level = old_isolation

def apply_migrations(
    conn: Optional[sqlite3.Connection] = None,
    db_path: Optional[Union[Path, str]] = None,
    migrations: Optional[List[MigrationRegistryEntry]] = None
) -> List[int]:
    """
    Discovers and applies any pending migrations in strict ascending version order.
    Returns a list of newly applied migration versions.
    """
    registry = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m[0])

    if conn is not None:
        return _apply_migrations_to_connection(conn, registry)

    target_path = Path(db_path or config.DB_PATH)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    db_conn = sqlite3.connect(str(target_path), timeout=15.0)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("PRAGMA foreign_keys = ON")
    db_conn.execute("PRAGMA journal_mode = WAL")
    try:
        return _apply_migrations_to_connection(db_conn, registry)
    finally:
        db_conn.close()

def _apply_migrations_to_connection(
    conn: sqlite3.Connection,
    registry: List[MigrationRegistryEntry]
) -> List[int]:
    ensure_migration_table(conn)
    applied_versions = set(get_applied_migrations(conn))
    newly_applied = []

    for version, name, func in registry:
        if version in applied_versions:
            continue
        apply_single_migration(conn, version, name, func)
        applied_versions.add(version)
        newly_applied.append(version)

    return newly_applied

def init_db(db_path: Optional[Union[Path, str]] = None) -> List[int]:
    """Initialize database tables and run any pending migrations."""
    return apply_migrations(db_path=db_path)
