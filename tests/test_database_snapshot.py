"""
Phase 3D Tests: SQLite Snapshot & Recovery Hardening

Covers:
- Fresh/empty database snapshot creation and validation
- Populated database snapshot verifying all entity types and relationships
- Uncheckpointed WAL writes included in snapshots without manual checkpointing
- Comprehensive snapshot validation (quick_check, integrity_check, schema checks)
- Rejection of nonexistent, empty, truncated, corrupted, and schema-deficient files
- Failure safety and atomic temporary file cleanup
- Reliable point-in-time database restoration via sqlite3.backup()
- Prevention of active DB modification when restoring invalid snapshots
- Automatic pre-restore validated safety snapshot creation
- Deterministic vector store reconciliation post-restore
- Non-destructive vector sync failure handling (SQLite preserved)
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.database_snapshot import (
    create_snapshot,
    validate_snapshot,
    restore_snapshot,
    SnapshotError,
    SnapshotValidationError,
    RestoreResult,
    REQUIRED_TABLES,
)
from app.vector_store import VectorStore


class TestDatabaseSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_active.db"
        self.backup_dir = self.temp_path / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.vec_path = self.temp_path / "test_vec.json"

        # Patch paths to point to temporary test directory
        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_backup = patch("app.config.BACKUP_DIR", self.backup_dir)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.vec_path)
        self.p_db.start()
        self.p_backup.start()
        self.p_vec.start()

        # Initialize SQLite schema
        from app.database import init_db
        init_db()

        # Initialize global vector store
        from app import vector_store
        self.vs = VectorStore(storage_path=self.vec_path)
        vector_store.global_vector_store = self.vs

    def tearDown(self):
        self.p_db.stop()
        self.p_backup.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    # ----------------------------------------------------------------------
    # 1. Fresh / Empty Database Snapshot
    # ----------------------------------------------------------------------
    def test_snapshot_empty_database(self):
        """Snapshot of a freshly initialized database succeeds, exists, and passes integrity checks."""
        snapshot_file = create_snapshot()
        self.assertTrue(snapshot_file.is_file())
        self.assertTrue(snapshot_file.stat().st_size > 0)

        # Validate through public validator
        is_valid, reason = validate_snapshot(snapshot_file)
        self.assertTrue(is_valid, f"Validation failed: {reason}")

        # Verify it can be opened independently and has all tables
        conn = sqlite3.connect(f"file:{snapshot_file.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        conn.close()

        self.assertTrue(REQUIRED_TABLES.issubset(tables))

    # ----------------------------------------------------------------------
    # 2. Populated Database Snapshot
    # ----------------------------------------------------------------------
    def test_snapshot_populated_database(self):
        """Snapshot preserves all entity rows, columns, IDs, and settings."""
        from app.models import (
            TaskModel, NoteModel, MemoryModel,
            MemoryDecisionLogModel, FeedbackModel, SettingsModel
        )

        t_id = TaskModel.create(
            title="Important Task",
            description="Phase 3D Verification",
            priority="urgent",
            subtasks=[{"id": 1, "title": "Check backup", "completed": True}]
        )
        n_id = NoteModel.create(title="Architecture Note", content="Snapshot via backup API")
        m_id = MemoryModel.create(category="preference", content="Always test snapshots", confidence_weight=0.9)
        l_id = MemoryDecisionLogModel.log(memory_id=m_id, action="ADD", reasoning="Initial preference")
        FeedbackModel.record("task_suggestion", item_id=t_id, memory_id_applied=m_id,
                              original_suggestion="Suggested urgent", user_action="accepted")
        SettingsModel.set("custom_key", "custom_val")

        # Create snapshot
        snapshot_file = create_snapshot()
        self.assertTrue(snapshot_file.is_file())

        is_valid, reason = validate_snapshot(snapshot_file)
        self.assertTrue(is_valid, reason)

        # Inspect snapshot content directly
        conn = sqlite3.connect(f"file:{snapshot_file.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()

        cur.execute("SELECT title, priority, subtasks FROM tasks WHERE id = ?", (t_id,))
        task_row = cur.fetchone()
        self.assertIsNotNone(task_row)
        self.assertEqual(task_row[0], "Important Task")
        self.assertEqual(task_row[1], "urgent")
        self.assertIn("Check backup", task_row[2])

        cur.execute("SELECT title, content FROM notes WHERE id = ?", (n_id,))
        note_row = cur.fetchone()
        self.assertEqual(note_row[0], "Architecture Note")

        cur.execute("SELECT category, content, confidence_weight FROM memories WHERE id = ?", (m_id,))
        mem_row = cur.fetchone()
        self.assertEqual(mem_row[0], "preference")
        self.assertEqual(mem_row[1], "Always test snapshots")
        self.assertAlmostEqual(mem_row[2], 0.9)

        cur.execute("SELECT action, reasoning FROM memory_decision_logs WHERE id = ?", (l_id,))
        log_row = cur.fetchone()
        self.assertEqual(log_row[0], "ADD")

        cur.execute("SELECT value FROM settings WHERE key = 'custom_key'")
        setting_row = cur.fetchone()
        self.assertEqual(setting_row[0], "custom_val")

        conn.close()

    # ----------------------------------------------------------------------
    # 3. WAL Mode / Uncheckpointed Writes
    # ----------------------------------------------------------------------
    def test_snapshot_uncheckpointed_wal_writes(self):
        """
        Snapshot captures uncheckpointed transactions in WAL mode without
        requiring manual filesystem copying of -wal or manual checkpoint.
        """
        from app.database import get_db

        # Verify WAL mode is active
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            self.assertEqual(mode.lower(), "wal")

            # Insert records within WAL connection
            cur.execute("INSERT INTO notes (title, content, tags, pinned, created_at, updated_at) "
                        "VALUES ('WAL Note', 'Direct WAL content', '', 0, '2026-09-14T00:00:00', '2026-09-14T00:00:00')")
            cur.execute("INSERT INTO tasks (title, description, priority, status, tags, created_at, updated_at) "
                        "VALUES ('WAL Task', 'Direct WAL task', 'medium', 'todo', '', '2026-09-14T00:00:00', '2026-09-14T00:00:00')")

        # Take snapshot
        snapshot_file = create_snapshot()
        self.assertTrue(snapshot_file.is_file())

        # Open snapshot independently
        conn = sqlite3.connect(f"file:{snapshot_file.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT content FROM notes WHERE title = 'WAL Note'")
        row_note = cur.fetchone()
        self.assertIsNotNone(row_note)
        self.assertEqual(row_note[0], "Direct WAL content")

        cur.execute("SELECT description FROM tasks WHERE title = 'WAL Task'")
        row_task = cur.fetchone()
        self.assertIsNotNone(row_task)
        self.assertEqual(row_task[0], "Direct WAL task")
        conn.close()

    # ----------------------------------------------------------------------
    # 4. Snapshot Validation
    # ----------------------------------------------------------------------
    def test_validate_snapshot_scenarios(self):
        """Validates behavior of validate_snapshot across various invalid file conditions."""
        # A. Nonexistent file
        nonexistent = self.temp_path / "does_not_exist.db"
        is_val, reason = validate_snapshot(nonexistent)
        self.assertFalse(is_val)
        self.assertIn("does not exist", reason.lower())

        # B. 0-byte file
        empty_file = self.temp_path / "empty.db"
        empty_file.write_bytes(b"")
        is_val, reason = validate_snapshot(empty_file)
        self.assertFalse(is_val)
        self.assertIn("empty", reason.lower())

        # C. Corrupted / garbage bytes
        corrupt_file = self.temp_path / "corrupt.db"
        corrupt_file.write_bytes(b"SQLite format 3\x00\xff\xfe\x00random_garbage_corrupted_payload")
        is_val, reason = validate_snapshot(corrupt_file)
        self.assertFalse(is_val)

        # D. Valid SQLite database but missing required Cognito tables
        incomplete_db = self.temp_path / "incomplete.db"
        conn = sqlite3.connect(str(incomplete_db))
        conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY, info TEXT)")
        conn.close()
        is_val, reason = validate_snapshot(incomplete_db)
        self.assertFalse(is_val)
        self.assertIn("missing required tables", reason.lower())

        # E. Valid snapshot
        valid_snap = create_snapshot()
        is_val, reason = validate_snapshot(valid_snap)
        self.assertTrue(is_val)
        self.assertIn("valid", reason.lower())

    # ----------------------------------------------------------------------
    # 5. Snapshot Failure Safety & Cleanup
    # ----------------------------------------------------------------------
    def test_snapshot_failure_cleanup_and_active_db_safety(self):
        """On snapshot failure, temporary files are removed and active DB is untouched."""
        from app.models import NoteModel
        n_id = NoteModel.create(title="Unmodified Note", content="Original content")

        # 1. Simulate validation failure on generated temporary snapshot
        with patch("app.database_snapshot.validate_snapshot", return_value=(False, "Simulated corruption")):
            with self.assertRaises(SnapshotError):
                create_snapshot()

        # Check no temporary .tmp.db files remain in backup dir
        tmp_files = list(self.backup_dir.glob("*.tmp.db"))
        self.assertEqual(len(tmp_files), 0, f"Found lingering temporary files: {tmp_files}")

        # 2. Simulate disk error when creating destination temporary database
        orig_connect = sqlite3.connect

        def fail_on_temp(*args, **kwargs):
            if any(str(a).endswith(".tmp.db") for a in args):
                # Write dummy bytes to simulate a partially created temp file
                Path(args[0]).write_bytes(b"dummy partial bytes")
                raise sqlite3.OperationalError("Simulated disk failure")
            return orig_connect(*args, **kwargs)

        with patch("sqlite3.connect", side_effect=fail_on_temp):
            with self.assertRaises(SnapshotError):
                create_snapshot()

        # Verify temp file was cleaned up
        tmp_files = list(self.backup_dir.glob("*.tmp.db"))
        self.assertEqual(len(tmp_files), 0, f"Lingering temp files after connect failure: {tmp_files}")

        # Verify active database was untouched
        note = NoteModel.get(n_id)
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Unmodified Note")

    # ----------------------------------------------------------------------
    # 6. Safe Restore: State Reversion
    # ----------------------------------------------------------------------
    def test_restore_valid_snapshot(self):
        """Restoring from a snapshot successfully resets active DB to snapshot state."""
        from app.models import TaskModel, NoteModel

        # State 1: 1 task, 1 note
        t1_id = TaskModel.create(title="State 1 Task")
        n1_id = NoteModel.create(title="State 1 Note", content="Content 1")
        snapshot_a = create_snapshot()

        # State 2: Modify active DB
        TaskModel.delete(t1_id)
        t2_id = TaskModel.create(title="State 2 Task")
        NoteModel.update(n1_id, title="State 2 Note Modified")

        # Verify State 2 is active
        self.assertIsNone(TaskModel.get(t1_id))
        self.assertIsNotNone(TaskModel.get(t2_id))
        self.assertEqual(NoteModel.get(n1_id)["title"], "State 2 Note Modified")

        # Restore Snapshot A
        res = restore_snapshot(snapshot_a)
        self.assertTrue(res.success)
        self.assertTrue(res.sqlite_restored)
        self.assertTrue(res.vector_synced)
        self.assertIsNone(res.error)

        # Active DB must now strictly reflect State 1
        self.assertIsNotNone(TaskModel.get(t1_id))
        self.assertIsNone(TaskModel.get(t2_id))
        self.assertEqual(NoteModel.get(n1_id)["title"], "State 1 Note")

    # ----------------------------------------------------------------------
    # 7. Invalid Restore Rejection
    # ----------------------------------------------------------------------
    def test_restore_invalid_snapshot_rejected(self):
        """Attempting to restore a corrupted snapshot is rejected and leaves active DB intact."""
        from app.models import NoteModel
        n_id = NoteModel.create(title="Preserved Active Note", content="Cannot be corrupted")

        corrupt_snap = self.temp_path / "corrupt_snap.db"
        corrupt_snap.write_bytes(b"CORRUPT_BYTES_DATA")

        res = restore_snapshot(corrupt_snap)
        self.assertFalse(res.success)
        self.assertFalse(res.sqlite_restored)
        self.assertIn("pre-validation", res.error.lower())

        # Active DB remains intact
        note = NoteModel.get(n_id)
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Preserved Active Note")

    # ----------------------------------------------------------------------
    # 8. Pre-Restore Safety Snapshot Creation
    # ----------------------------------------------------------------------
    def test_pre_restore_safety_snapshot_created(self):
        """Restoring creates a valid emergency safety snapshot of current active DB."""
        from app.models import NoteModel

        # State 1
        NoteModel.create(title="Initial Note", content="State 1")
        snap1 = create_snapshot()

        # State 2
        NoteModel.create(title="State 2 Note", content="State 2 content")

        # Restore snap1 -> triggers safety snapshot of State 2
        res = restore_snapshot(snap1)
        self.assertTrue(res.success)
        self.assertIsNotNone(res.safety_snapshot_path)
        self.assertTrue(res.safety_snapshot_path.is_file())

        # Verify safety snapshot is valid and contains State 2
        is_valid, reason = validate_snapshot(res.safety_snapshot_path)
        self.assertTrue(is_valid, reason)

        conn = sqlite3.connect(f"file:{res.safety_snapshot_path.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notes WHERE title = 'State 2 Note'")
        count = cur.fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    # ----------------------------------------------------------------------
    # 9. Vector Store Recovery After Restore
    # ----------------------------------------------------------------------
    def test_vector_store_reconciled_after_restore(self):
        """Restoring SQLite automatically invokes ensure_valid_index() to synchronize vector store."""
        from app.models import MemoryModel
        from app import vector_store

        # Snapshot 1 contains Memory 1
        m1_id = MemoryModel.create(category="preference", content="Rule 1 from Snapshot", confidence_weight=0.8)
        vector_store.global_vector_store.rebuild_from_db()
        self.assertIn(f"memory_{m1_id}", vector_store.global_vector_store.documents)
        snap1 = create_snapshot()

        # State 2: Replace Memory 1 with Memory 2
        MemoryModel.update(m1_id, status="deleted")
        m2_id = MemoryModel.create(category="preference", content="Rule 2 after Snapshot", confidence_weight=0.9)
        vector_store.global_vector_store.rebuild_from_db()
        self.assertNotIn(f"memory_{m1_id}", vector_store.global_vector_store.documents)
        self.assertIn(f"memory_{m2_id}", vector_store.global_vector_store.documents)

        # Restore Snapshot 1
        res = restore_snapshot(snap1)
        self.assertTrue(res.success)
        self.assertTrue(res.vector_synced)

        # Vector store must now reflect Snapshot 1 state
        self.assertIn(f"memory_{m1_id}", vector_store.global_vector_store.documents)
        self.assertNotIn(f"memory_{m2_id}", vector_store.global_vector_store.documents)

    # ----------------------------------------------------------------------
    # 10. Vector Sync Failure Does Not Roll Back SQLite
    # ----------------------------------------------------------------------
    def test_vector_sync_failure_does_not_corrupt_sqlite(self):
        """If vector sync fails post-restore, SQLite remains restored and failure is surfaced."""
        from app.models import NoteModel
        from app import vector_store

        NoteModel.create(title="Target Restored Note", content="Should persist even if vector fails")
        snap = create_snapshot()

        # Wipe active note
        from app.database import get_db
        with get_db() as conn:
            conn.execute("DELETE FROM notes")

        # Simulate vector store failure during ensure_valid_index
        with patch.object(vector_store.global_vector_store, "ensure_valid_index", side_effect=IOError("Vector disk error")):
            res = restore_snapshot(snap)

            # SQLite restored successfully, but overall restore is False because vector sync failed
            self.assertFalse(res.success)
            self.assertTrue(res.sqlite_restored)
            self.assertFalse(res.vector_synced)
            self.assertIn("vector store synchronization failed", res.error)

        # SQLite database has been restored and is completely intact
        restored_note = NoteModel.get(1)
        self.assertIsNotNone(restored_note)
        self.assertEqual(restored_note["title"], "Target Restored Note")


    # ----------------------------------------------------------------------
    # 11. Concurrent Live Operations During Snapshot
    # ----------------------------------------------------------------------
    def test_concurrent_read_write_during_snapshot(self):
        """Active read and write operations can continue while a snapshot is being taken."""
        import threading
        from app.database import get_db

        write_count = 10
        errors = []

        def background_writer():
            try:
                for i in range(write_count):
                    with get_db() as conn:
                        conn.execute(
                            "INSERT INTO notes (title, content, tags, pinned, created_at, updated_at) "
                            "VALUES (?, ?, '', 0, '2026-09-14T00:00:00', '2026-09-14T00:00:00')",
                            (f"Concurrent Note {i}", f"Content {i}")
                        )
            except Exception as e:
                errors.append(e)

        thread = threading.Thread(target=background_writer)
        thread.start()

        # Take snapshot concurrently
        snapshot_file = create_snapshot()
        thread.join()

        self.assertEqual(len(errors), 0, f"Concurrent writer experienced errors: {errors}")
        is_valid, reason = validate_snapshot(snapshot_file)
        self.assertTrue(is_valid, reason)


if __name__ == "__main__":
    unittest.main()
