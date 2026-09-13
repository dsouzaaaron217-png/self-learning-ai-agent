import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.vector_store import VectorStore

class TestVectorStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "test_vector.json"
        self.store = VectorStore(storage_path=self.store_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_indexing_and_search(self):
        self.store.index_document(1, "memory", "User prefers morning sessions for deep coding work", {"confidence_weight": 0.85})
        self.store.index_document(2, "memory", "Gym workout routine scheduled every weekday at 6pm", {"confidence_weight": 0.80})
        self.store.index_document(3, "note", "Architecture notes: Cognito uses SQLite and pure-Python vector search")

        # Query relevant to coding
        results = self.store.search("morning coding tasks", limit=2)
        self.assertTrue(len(results) > 0)
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[0]["doc_type"], "memory")
        self.assertTrue(results[0]["similarity"] > 0.0)

        # Query relevant to workout
        gym_results = self.store.search("exercise and gym evening", limit=2)
        self.assertTrue(len(gym_results) > 0)
        self.assertEqual(gym_results[0]["id"], 2)

    def test_persistence_and_reload(self):
        self.store.index_document(10, "memory", "Client demos should always be medium priority")
        self.store.save()

        # Reload into fresh store instance
        new_store = VectorStore(storage_path=self.store_path)
        hits = new_store.search("client demo priority", limit=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], 10)


class TestVectorStoreResilience(unittest.TestCase):
    """
    Phase 3B tests:
    - Atomic file replacement and cleanup on write failure
    - Corruption and missing file detection
    - Deterministic rebuild from authoritative SQLite (memories, tasks, notes)
    - Prevention of duplicates and exclusion of inactive/deleted records
    - Startup path recovery without unnecessary rebuilds for valid stores
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_resilience.db"
        self.vec_path = self.temp_path / "test_resilience_vector.json"

        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.vec_path)
        self.p_db.start()
        self.p_vec.start()

        from app.database import init_db
        init_db()

        self.store = VectorStore(storage_path=self.vec_path)

    def tearDown(self):
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def test_atomic_save_success(self):
        self.store.index_document(1, "memory", "Morning deep work session")
        self.store.save()

        self.assertTrue(self.vec_path.exists())
        with open(self.vec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total_docs"], 1)
        self.assertIn("memory_1", data["documents"])

        # Ensure no temporary files remain
        tmp_files = list(self.temp_path.glob("*.tmp*"))
        self.assertEqual(tmp_files, [])

    def test_original_file_remains_intact_if_write_fails(self):
        self.store.index_document(1, "memory", "Initial authoritative document")
        self.store.save()

        orig_content = self.vec_path.read_text(encoding="utf-8")

        # Stage a new document in memory
        self.store.index_document(2, "memory", "Unsaved document that fails write", auto_rebuild=False)

        # Simulate write failure during serialization
        with patch("json.dump", side_effect=OSError("Simulated disk error")):
            with self.assertRaises(Exception):
                self.store.save()

        # Original file must remain completely intact and identical
        self.assertEqual(self.vec_path.read_text(encoding="utf-8"), orig_content)

        # Ensure no leftover temporary files
        tmp_files = list(self.temp_path.glob("*.tmp*"))
        self.assertEqual(tmp_files, [])

    def test_missing_vector_store_triggers_recovery(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        MemoryModel.create(category="habit", content="Regular standup at 10am")
        TaskModel.create(title="Prepare sprint demo", priority="high")
        NoteModel.create(title="Sprint notes", content="Sprint goals and tickets")

        if self.vec_path.exists():
            self.vec_path.unlink()

        self.assertFalse(self.vec_path.exists())

        recovered = self.store.ensure_valid_index()
        self.assertTrue(recovered)
        self.assertTrue(self.vec_path.exists())
        self.assertEqual(len(self.store.documents), 3)
        self.assertIn("memory_1", self.store.documents)
        self.assertIn("task_1", self.store.documents)
        self.assertIn("note_1", self.store.documents)

    def test_malformed_vector_store_triggers_recovery(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        MemoryModel.create(category="habit", content="Regular standup at 10am")
        TaskModel.create(title="Prepare sprint demo", priority="high")
        NoteModel.create(title="Sprint notes", content="Sprint goals and tickets")

        # Corrupt file content
        self.vec_path.write_text('{"total_docs": 3, "documents": {"corrupt": "NOT_A_DICT"}}', encoding="utf-8")

        recovered = self.store.ensure_valid_index()
        self.assertTrue(recovered)
        self.assertEqual(len(self.store.documents), 3)
        self.assertIn("memory_1", self.store.documents)
        self.assertIn("task_1", self.store.documents)
        self.assertIn("note_1", self.store.documents)

        # Verify file is now valid JSON
        with open(self.vec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total_docs"], 3)

    def test_rebuild_restores_active_memories(self):
        from app.models import MemoryModel

        m1 = MemoryModel.create(category="habit", content="Coding before 12pm", confidence_weight=0.90)
        m2 = MemoryModel.create(category="preference", content="Dark mode preferred", confidence_weight=0.80)

        count = self.store.rebuild_from_db()
        self.assertEqual(count, 2)
        self.assertEqual(self.store.documents[f"memory_{m1}"]["doc_type"], "memory")
        self.assertEqual(self.store.documents[f"memory_{m1}"]["text"], "Coding before 12pm")
        self.assertEqual(self.store.documents[f"memory_{m1}"]["metadata"]["confidence_weight"], 0.90)
        self.assertEqual(self.store.documents[f"memory_{m2}"]["text"], "Dark mode preferred")

    def test_rebuild_restores_tasks(self):
        from app.models import TaskModel

        t1 = TaskModel.create(
            title="Deploy security update",
            description="Apply offline network guard",
            priority="urgent",
            tags="security,release"
        )

        count = self.store.rebuild_from_db()
        self.assertEqual(count, 1)
        doc = self.store.documents[f"task_{t1}"]
        self.assertEqual(doc["doc_type"], "task")
        self.assertIn("Deploy security update", doc["text"])
        self.assertIn("Apply offline network guard", doc["text"])
        self.assertIn("security,release", doc["text"])
        self.assertEqual(doc["metadata"]["priority"], "urgent")

    def test_rebuild_restores_notes(self):
        from app.models import NoteModel

        n1 = NoteModel.create(
            title="Cognito Architecture",
            content="Pure-Python on-device intelligence",
            tags="arch,local",
            pinned=1
        )

        count = self.store.rebuild_from_db()
        self.assertEqual(count, 1)
        doc = self.store.documents[f"note_{n1}"]
        self.assertEqual(doc["doc_type"], "note")
        self.assertIn("Cognito Architecture", doc["text"])
        self.assertIn("Pure-Python on-device intelligence", doc["text"])
        self.assertEqual(doc["metadata"]["pinned"], 1)

    def test_inactive_and_deleted_records_not_indexed(self):
        from app.models import MemoryModel

        m_active = MemoryModel.create(category="habit", content="Active habit", status="active")
        m_deleted = MemoryModel.create(category="preference", content="Deleted preference", status="deleted")
        m_superseded = MemoryModel.create(category="preference", content="Superseded fact", status="superseded")

        count = self.store.rebuild_from_db()
        self.assertEqual(count, 1)
        self.assertIn(f"memory_{m_active}", self.store.documents)
        self.assertNotIn(f"memory_{m_deleted}", self.store.documents)
        self.assertNotIn(f"memory_{m_superseded}", self.store.documents)

    def test_rebuild_does_not_create_duplicate_vector_documents(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        MemoryModel.create(category="habit", content="Habit rule")
        TaskModel.create(title="Task item")
        NoteModel.create(title="Note item", content="Note content")

        count1 = self.store.rebuild_from_db()
        count2 = self.store.rebuild_from_db()

        self.assertEqual(count1, 3)
        self.assertEqual(count2, 3)
        self.assertEqual(len(self.store.documents), 3)
        self.assertEqual(len(set(self.store.documents.keys())), 3)

    def test_valid_synchronized_store_does_not_rebuild(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        m = MemoryModel.create(category="preference", content="Synchronized preference")
        t = TaskModel.create(title="Synchronized task")
        n = NoteModel.create(title="Synchronized note", content="Synchronized content")
        self.store.rebuild_from_db()

        # Load into a new store pointing to the same file
        new_store = VectorStore(storage_path=self.vec_path)
        with patch.object(new_store, "rebuild_from_db") as mock_rebuild:
            rebuilt = new_store.ensure_valid_index()
            self.assertFalse(rebuilt)
            mock_rebuild.assert_not_called()
            self.assertIn(f"memory_{m}", new_store.documents)
            self.assertIn(f"task_{t}", new_store.documents)
            self.assertIn(f"note_{n}", new_store.documents)

    def test_valid_store_missing_authoritative_record_triggers_rebuild(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        m = MemoryModel.create(category="preference", content="Active memory")
        t = TaskModel.create(title="Active task")
        n = NoteModel.create(title="Active note", content="Active note content")

        # Save store with ONLY memory_1, missing task_1 and note_1
        self.store.documents = {}
        self.store.index_document(m, "memory", "Active memory")
        self.store.save()

        new_store = VectorStore(storage_path=self.vec_path)
        self.assertEqual(len(new_store.documents), 1)

        # ensure_valid_index must detect missing authoritative records and trigger rebuild
        rebuilt = new_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertEqual(len(new_store.documents), 3)
        self.assertIn(f"memory_{m}", new_store.documents)
        self.assertIn(f"task_{t}", new_store.documents)
        self.assertIn(f"note_{n}", new_store.documents)

    def test_valid_store_containing_extra_stale_document_triggers_rebuild(self):
        from app.models import MemoryModel

        m = MemoryModel.create(category="habit", content="Authoritative habit")

        # Save store with authoritative memory_1 AND stale task_999 (which does not exist in SQLite)
        self.store.documents = {}
        self.store.index_document(m, "memory", "Authoritative habit", auto_rebuild=False)
        self.store.index_document(999, "task", "Stale deleted task", auto_rebuild=False)
        self.store.rebuild_idf()
        self.store.save()

        new_store = VectorStore(storage_path=self.vec_path)
        self.assertEqual(len(new_store.documents), 2)
        self.assertIn("task_999", new_store.documents)

        # ensure_valid_index must detect stale/extra record and rebuild from authoritative SQLite
        rebuilt = new_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertEqual(len(new_store.documents), 1)
        self.assertIn(f"memory_{m}", new_store.documents)
        self.assertNotIn("task_999", new_store.documents)

    def test_empty_db_and_empty_store_is_valid(self):
        # When DB has zero records, an empty store is valid and does not trigger rebuild
        empty_payload = {"total_docs": 0, "idf": {}, "documents": {}}
        self.vec_path.write_text(json.dumps(empty_payload), encoding="utf-8")

        new_store = VectorStore(storage_path=self.vec_path)
        with patch.object(new_store, "rebuild_from_db") as mock_rebuild:
            rebuilt = new_store.ensure_valid_index()
            self.assertFalse(rebuilt)
            mock_rebuild.assert_not_called()
            self.assertEqual(len(new_store.documents), 0)

    def test_empty_store_with_active_db_triggers_rebuild(self):
        from app.models import MemoryModel

        MemoryModel.create(category="preference", content="Active preference in DB")
        empty_payload = {"total_docs": 0, "idf": {}, "documents": {}}
        self.vec_path.write_text(json.dumps(empty_payload), encoding="utf-8")

        new_store = VectorStore(storage_path=self.vec_path)
        rebuilt = new_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertEqual(len(new_store.documents), 1)

    def test_sqlite_records_not_modified_during_vector_recovery(self):
        from app.models import MemoryModel, TaskModel, NoteModel

        m_id = MemoryModel.create(category="habit", content="Protected habit")
        t_id = TaskModel.create(title="Protected task")
        n_id = NoteModel.create(title="Protected note", content="Protected content")

        # Corrupt the vector store
        self.vec_path.write_text("CORRUPT_BYTES", encoding="utf-8")

        self.store.ensure_valid_index()

        # Verify SQLite data is completely intact
        m = MemoryModel.get(m_id)
        t = TaskModel.get(t_id)
        n = NoteModel.get(n_id)

        self.assertIsNotNone(m)
        self.assertEqual(m["content"], "Protected habit")
        self.assertIsNotNone(t)
        self.assertEqual(t["title"], "Protected task")
        self.assertIsNotNone(n)
        self.assertEqual(n["content"], "Protected content")

    def test_startup_recovery_integration(self):
        from app.models import MemoryModel
        from app import vector_store

        MemoryModel.create(category="habit", content="Startup recovery test")

        auth_path = self.temp_path / "test_auth.json"
        with patch("app.auth.AUTH_FILE_PATH", auth_path):
            if self.vec_path.exists():
                self.vec_path.unlink()

            vector_store.global_vector_store = VectorStore(storage_path=self.vec_path)

            from app import create_app
            app = create_app()

            self.assertTrue(self.vec_path.exists())
            self.assertTrue(len(vector_store.global_vector_store.documents) >= 1)


if __name__ == "__main__":
    unittest.main()
