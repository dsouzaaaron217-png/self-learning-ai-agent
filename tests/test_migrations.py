"""
Unit tests for Cognito Phase 5A & 5B:
Database Schema Versioning and Migration Foundation.

Covers:
1. Fresh database initialization and table creation (migrations 1 & 2).
2. Existing populated database bootstrap without data loss.
3. schema_migrations table structure and metadata.
4. get_current_migration_version() reporting.
5. Ascending order execution of migrations.
6. Skipping already-applied migrations.
7. Atomic rollback on failed migration without partial schema contamination.
8. Preservation of existing tables, rows, columns, and relations.
9. PRAGMA foreign_keys = ON and WAL journal mode configuration.
10. Idempotent re-initialization.
11. Persistence across connection closure and restart.
12. Database snapshot and restore compatibility with migration metadata.
13. Migration 002 chat_messages schema, columns, and indexes.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import database
from app.database import (
    init_db,
    apply_migrations,
    get_applied_migrations,
    get_current_migration_version,
    ensure_migration_table,
    SCHEMA_MIGRATIONS_TABLE,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
)
from app.database_snapshot import create_snapshot, validate_snapshot, restore_snapshot


class TestDatabaseMigrations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_mig.db"
        self.backup_dir = Path(self.temp_dir.name) / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.vec_path = Path(self.temp_dir.name) / "test_vec.json"

        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_backup = patch("app.config.BACKUP_DIR", self.backup_dir)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.vec_path)
        self.p_db.start()
        self.p_backup.start()
        self.p_vec.start()

    def tearDown(self):
        self.p_db.stop()
        self.p_backup.stop()
        self.p_vec.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_fresh_db_initialization(self):
        """Fresh database init runs migrations in registry and creates all tables."""
        applied = init_db(self.db_path)
        self.assertEqual(applied, [1, 2])

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        ver = get_current_migration_version(conn=conn)
        conn.close()

        expected_tables = {
            SCHEMA_MIGRATIONS_TABLE,
            "tasks",
            "notes",
            "memories",
            "memory_decision_logs",
            "feedback_events",
            "settings",
            "chat_messages",
        }
        self.assertTrue(expected_tables.issubset(tables))
        self.assertEqual(ver, 2)

    def test_02_existing_database_bootstrap_preserves_data(self):
        """Pre-existing populated database without schema_migrations bootstraps safely with zero data loss."""
        # Create an un-migrated database with legacy tables and populated rows
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE tasks (
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
        cur.execute("""
            CREATE TABLE notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE memories (
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
        cur.execute("""
            CREATE TABLE memory_decision_logs (
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
        cur.execute("""
            CREATE TABLE feedback_events (
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
        cur.execute("""
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Insert pre-existing user data
        cur.execute(
            "INSERT INTO tasks (title, description, priority, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("Existing Task", "Important task from Phase 4", "high", "in_progress", "2026-09-01T10:00:00Z", "2026-09-01T10:00:00Z")
        )
        cur.execute(
            "INSERT INTO notes (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("Existing Note", "Valuable user thoughts", "2026-09-01T10:05:00Z", "2026-09-01T10:05:00Z")
        )
        cur.execute(
            "INSERT INTO memories (category, content, confidence_weight, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "Pre-existing user preference", 0.85, "active", "2026-09-01T10:10:00Z", "2026-09-01T10:10:00Z")
        )
        cur.execute(
            "INSERT INTO memory_decision_logs (memory_id, action, reasoning, timestamp) VALUES (?, ?, ?, ?)",
            (1, "ADD", "Bootstrap test decision", "2026-09-01T10:10:00Z")
        )
        cur.execute(
            "INSERT INTO feedback_events (suggestion_type, original_suggestion, user_action, timestamp) VALUES (?, ?, ?, ?)",
            ("task_suggestion", "Old suggestion", "accepted", "2026-09-01T10:15:00Z")
        )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("custom_setting", "custom_val")
        )
        conn.commit()
        conn.close()

        # Run init_db on this populated database
        applied = init_db(self.db_path)
        self.assertEqual(applied, [1, 2])

        # Verify all data remains intact
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        task = cur.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Existing Task")
        self.assertEqual(task["priority"], "high")

        note = cur.execute("SELECT * FROM notes WHERE id = 1").fetchone()
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Existing Note")

        mem = cur.execute("SELECT * FROM memories WHERE id = 1").fetchone()
        self.assertIsNotNone(mem)
        self.assertEqual(mem["content"], "Pre-existing user preference")
        self.assertEqual(mem["confidence_weight"], 0.85)

        log = cur.execute("SELECT * FROM memory_decision_logs WHERE id = 1").fetchone()
        self.assertIsNotNone(log)
        self.assertEqual(log["action"], "ADD")

        fb = cur.execute("SELECT * FROM feedback_events WHERE id = 1").fetchone()
        self.assertIsNotNone(fb)
        self.assertEqual(fb["user_action"], "accepted")

        setting = cur.execute("SELECT value FROM settings WHERE key = 'custom_setting'").fetchone()
        self.assertIsNotNone(setting)
        self.assertEqual(setting["value"], "custom_val")

        # Verify schema_migrations contains versions 1 and 2
        mig = cur.execute(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version ASC").fetchall()
        self.assertEqual(len(mig), 2)
        self.assertEqual([r["version"] for r in mig], [1, 2])

        conn.close()

    def test_03_schema_migrations_table_structure(self):
        """schema_migrations has version INTEGER PRIMARY KEY and applied_at TEXT NOT NULL."""
        conn = sqlite3.connect(str(self.db_path))
        ensure_migration_table(conn)

        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({SCHEMA_MIGRATIONS_TABLE})")
        cols = {row[1]: {"type": row[2].upper(), "notnull": row[3], "pk": row[5]} for row in cur.fetchall()}
        conn.close()

        self.assertIn("version", cols)
        self.assertEqual(cols["version"]["type"], "INTEGER")
        self.assertEqual(cols["version"]["pk"], 1)

        self.assertIn("applied_at", cols)
        self.assertEqual(cols["applied_at"]["type"], "TEXT")
        self.assertEqual(cols["applied_at"]["notnull"], 1)

    def test_04_get_current_migration_version(self):
        """get_current_migration_version returns 0 for empty and correct version after migration."""
        conn = sqlite3.connect(str(self.db_path))
        self.assertEqual(get_current_migration_version(conn), 0)

        init_db(self.db_path)
        self.assertEqual(get_current_migration_version(conn), 2)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 2)
        conn.close()

    def test_05_ascending_order_execution(self):
        """apply_migrations executes migrations in ascending version order regardless of list order."""
        execution_order = []

        def mig_1(c):
            execution_order.append(1)

        def mig_2(c):
            execution_order.append(2)

        def mig_3(c):
            execution_order.append(3)

        custom_registry = [
            (3, "three", mig_3),
            (1, "one", mig_1),
            (2, "two", mig_2),
        ]

        conn = sqlite3.connect(str(self.db_path))
        applied = apply_migrations(conn=conn, migrations=custom_registry)
        conn.close()

        self.assertEqual(applied, [1, 2, 3])
        self.assertEqual(execution_order, [1, 2, 3])

    def test_06_already_applied_migrations_skipped(self):
        """Already applied migrations are skipped on subsequent runs."""
        call_count = [0]

        def test_mig(c):
            call_count[0] += 1

        registry = [(1, "first", test_mig)]
        conn = sqlite3.connect(str(self.db_path))

        applied_first = apply_migrations(conn=conn, migrations=registry)
        self.assertEqual(applied_first, [1])
        self.assertEqual(call_count[0], 1)

        # Run again
        applied_second = apply_migrations(conn=conn, migrations=registry)
        self.assertEqual(applied_second, [])
        self.assertEqual(call_count[0], 1)  # not called again
        conn.close()

    def test_07_failed_migration_rolls_back_atomically(self):
        """Failed migration rolls back changes and does not record version in schema_migrations."""
        def good_mig(c):
            c.execute("CREATE TABLE good_table (id INTEGER PRIMARY KEY)")

        def failing_mig(c):
            c.execute("CREATE TABLE bad_table (id INTEGER PRIMARY KEY)")
            raise RuntimeError("Deliberate migration failure")

        registry = [
            (1, "good", good_mig),
            (2, "failing", failing_mig),
        ]

        conn = sqlite3.connect(str(self.db_path))
        # First migration should succeed
        applied = apply_migrations(conn=conn, migrations=[registry[0]])
        self.assertEqual(applied, [1])
        self.assertEqual(get_current_migration_version(conn), 1)

        # Attempting second migration must raise and rollback bad_table
        with self.assertRaises(RuntimeError):
            apply_migrations(conn=conn, migrations=registry)

        # Confirm bad_table was NOT created and version remains 1
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bad_table'")
        self.assertIsNone(cur.fetchone())

        cur.execute(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE}")
        versions = [r[0] for r in cur.fetchall()]
        self.assertEqual(versions, [1])
        conn.close()

    def test_08_existing_columns_and_tables_remain_intact(self):
        """Idempotent baseline migration preserves existing tables and schema structures."""
        init_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(tasks)")
        task_cols = [r[1] for r in cur.fetchall()]
        self.assertIn("ai_suggestion_context", task_cols)
        self.assertIn("subtasks", task_cols)

        cur.execute("PRAGMA table_info(memories)")
        mem_cols = [r[1] for r in cur.fetchall()]
        self.assertIn("confidence_weight", mem_cols)
        self.assertIn("update_count", mem_cols)
        self.assertIn("superseded_by", mem_cols)
        conn.close()

    def test_09_pragma_foreign_keys_and_wal_active(self):
        """Connection opened through database helpers has foreign_keys = ON and journal_mode = WAL."""
        init_db(self.db_path)

        from app.database import get_db
        with get_db() as conn:
            cur = conn.cursor()

            cur.execute("PRAGMA foreign_keys")
            fk_val = cur.fetchone()[0]
            self.assertEqual(fk_val, 1)

            cur.execute("PRAGMA journal_mode")
            jm_val = cur.fetchone()[0]
            self.assertEqual(jm_val.lower(), "wal")

    def test_10_idempotent_reinitialization(self):
        """Calling init_db multiple times is strictly safe and idempotent."""
        first = init_db(self.db_path)
        self.assertEqual(first, [1, 2])

        second = init_db(self.db_path)
        self.assertEqual(second, [])

        third = init_db(self.db_path)
        self.assertEqual(third, [])

    def test_11_migration_table_persists_across_restart(self):
        """Migrations table and version record persist across closing connection and reopening."""
        init_db(self.db_path)

        # Fresh connection to path
        fresh_conn = sqlite3.connect(str(self.db_path))
        self.assertEqual(get_applied_migrations(fresh_conn), [1, 2])
        self.assertEqual(get_current_migration_version(fresh_conn), 2)
        fresh_conn.close()

    def test_12_snapshot_and_backup_compatibility(self):
        """Snapshots taken of migrated databases include schema_migrations and validate cleanly."""
        init_db(self.db_path)

        snap_path = create_snapshot()
        self.assertTrue(snap_path.exists())

        # Validate snapshot
        valid, reason = validate_snapshot(snap_path)
        self.assertTrue(valid, f"Snapshot failed validation: {reason}")

        # Check snapshot contents
        snap_conn = sqlite3.connect(str(snap_path))
        cur = snap_conn.cursor()
        cur.execute(f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version ASC")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r[0] for r in rows], [1, 2])
        snap_conn.close()

        # Test restoring snapshot
        new_active = Path(self.temp_dir.name) / "restored.db"
        with patch("app.config.DB_PATH", new_active):
            result = restore_snapshot(snap_path)
            self.assertTrue(result.sqlite_restored)

            restored_conn = sqlite3.connect(str(new_active))
            ver = get_current_migration_version(restored_conn)
            restored_conn.close()
            self.assertEqual(ver, 2)

    def test_13_migration_002_chat_messages_schema_and_indexes(self):
        """Migration 002 creates chat_messages table with expected columns and indexes."""
        init_db(self.db_path)

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        # Verify table info
        cur.execute("PRAGMA table_info(chat_messages)")
        cols = {row[1]: {"type": row[2].upper(), "notnull": row[3], "pk": row[5]} for row in cur.fetchall()}

        self.assertIn("id", cols)
        self.assertEqual(cols["id"]["pk"], 1)

        self.assertIn("session_id", cols)
        self.assertEqual(cols["session_id"]["type"], "TEXT")
        self.assertEqual(cols["session_id"]["notnull"], 1)

        self.assertIn("role", cols)
        self.assertEqual(cols["role"]["type"], "TEXT")
        self.assertEqual(cols["role"]["notnull"], 1)

        self.assertIn("content", cols)
        self.assertEqual(cols["content"]["type"], "TEXT")
        self.assertEqual(cols["content"]["notnull"], 1)

        self.assertIn("cited_memories", cols)
        self.assertEqual(cols["cited_memories"]["type"], "TEXT")

        self.assertIn("created_at", cols)
        self.assertEqual(cols["created_at"]["type"], "TEXT")
        self.assertEqual(cols["created_at"]["notnull"], 1)

        # Verify indexes
        cur.execute("PRAGMA index_list(chat_messages)")
        indexes = {row[1] for row in cur.fetchall()}
        self.assertIn("idx_chat_messages_session_id", indexes)
        self.assertIn("idx_chat_messages_created_at", indexes)
        self.assertIn("idx_chat_messages_session_created", indexes)

        conn.close()


if __name__ == "__main__":
    unittest.main()
