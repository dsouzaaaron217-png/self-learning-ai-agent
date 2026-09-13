import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from app.vector_store import VectorStore

class TestMemoryPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_pipe.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_vec.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_db.start()
        self.p_vec.start()

        from app.database import init_db
        init_db()

        # Wire up a fresh global vector store
        from app import vector_store
        vector_store.global_vector_store = VectorStore(storage_path=self.test_vec_path)

    def tearDown(self):
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def test_fact_extraction(self):
        from app.memory.pipeline import MemoryPipeline
        text = "I prefer client tasks are marked high priority. Also gym workout routine every weekday at 6pm."
        facts = MemoryPipeline.extract_facts(text)
        self.assertTrue(len(facts) >= 1)
        categories = [f["category"] for f in facts]
        self.assertTrue("preference" in categories or "habit" in categories)

    def test_two_phase_add_and_update(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel, MemoryDecisionLogModel

        # Step 1: Initial fact -> ADD
        dec1 = MemoryPipeline.reconcile_memory(
            category="preference",
            content="Client presentation tasks are marked high priority.",
            confidence=0.75,
            source_context="Initial note #1"
        )
        self.assertEqual(dec1["action"], "ADD")
        mem_id = dec1["memory_id"]
        
        # Verify log recorded
        logs = MemoryDecisionLogModel.get_for_memory(mem_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "ADD")

        # Step 2: User correction on similar topic -> UPDATE with provenance
        dec2 = MemoryPipeline.reconcile_memory(
            category="preference",
            content="Client presentation tasks are marked medium priority instead.",
            confidence=0.90,
            source_context="User correction"
        )
        self.assertEqual(dec2["action"], "UPDATE")
        self.assertEqual(dec2["memory_id"], mem_id)

        # Check updated memory state
        updated_mem = MemoryModel.get(mem_id)
        self.assertIn("medium priority", updated_mem["content"])
        self.assertEqual(updated_mem["update_count"], 1)

        # Check that audit log has both ADD and UPDATE
        logs_after = MemoryDecisionLogModel.get_for_memory(mem_id)
        self.assertEqual(len(logs_after), 2)
        actions = [l["action"] for l in logs_after]
        self.assertIn("ADD", actions)
        self.assertIn("UPDATE", actions)

    def test_reconcile_delete(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel

        # Create memory
        dec_add = MemoryPipeline.reconcile_memory(
            category="preference",
            content="Always schedule meetings on Friday afternoons.",
            confidence=0.8,
            source_context="Note"
        )
        mem_id = dec_add["memory_id"]

        # Delete rule
        dec_del = MemoryPipeline.reconcile_memory(
            category="preference",
            content="Don't do this anymore: stop suggesting meetings on Friday afternoons.",
            confidence=0.95,
            source_context="User feedback"
        )
        self.assertEqual(dec_del["action"], "DELETE")
        self.assertEqual(dec_del["memory_id"], mem_id)

        # Verify marked as deleted
        mem = MemoryModel.get(mem_id)
        self.assertEqual(mem["status"], "deleted")


class TestMemoryConsistencyAndReconciliation(unittest.TestCase):
    """
    Phase 3C tests:
    - Normalization and identical statement idempotency (NOOP)
    - Prevention of duplicate active memories and source_context pollution
    - Elimination of repeated vector searches inside reconcile_memory
    - Pure semantic cosine similarity without recency/confidence distortion
    - Distinction between NOOP and semantic UPDATE
    - Synchronization of vector store metadata on feedback acceptance and correction
    - Graceful handling of missing vector doc during feedback sync
    - Synchronization of vector store on ADD, UPDATE, and DELETE
    - Fault tolerance: vector store failure does not corrupt or abort SQLite operations
    - Exclusion of deleted/inactive memories from vector index
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_pipe_3c.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_vec_3c.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_db.start()
        self.p_vec.start()

        from app.database import init_db
        init_db()

        from app import vector_store
        self.vs = VectorStore(storage_path=self.test_vec_path)
        vector_store.global_vector_store = self.vs

    def tearDown(self):
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def test_normalize_statement(self):
        from app.memory.pipeline import normalize_statement
        self.assertEqual(normalize_statement("  Always check emails!  "), "always check emails")
        self.assertEqual(normalize_statement("Meeting at 9am???"), "meeting at 9am")
        self.assertEqual(normalize_statement("Multiple    spaces   between   words."), "multiple spaces between words")
        self.assertEqual(normalize_statement(None), "")
        self.assertEqual(normalize_statement(""), "")

    def test_feedback_acceptance_syncs_vector_metadata(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app.config import REINFORCEMENT_STEP
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("preference", "Morning standup at 9am", 0.70, "user note")
        mem_id = dec["memory_id"]
        doc_key = f"memory_{mem_id}"
        self.assertIn(doc_key, vector_store.global_vector_store.documents)
        self.assertEqual(vector_store.global_vector_store.documents[doc_key]["metadata"]["confidence_weight"], 0.70)

        MemoryPipeline.process_acceptance(item_id=1, original_suggestion="Suggest standup", memory_id_applied=mem_id)

        mem = MemoryModel.get(mem_id)
        expected_weight = round(0.70 + REINFORCEMENT_STEP, 4)
        self.assertAlmostEqual(mem["confidence_weight"], expected_weight)
        self.assertAlmostEqual(vector_store.global_vector_store.documents[doc_key]["metadata"]["confidence_weight"], expected_weight)

    def test_feedback_correction_syncs_vector_metadata(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app.config import PENALTY_STEP
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("preference", "Morning standup at 9am", 0.70, "user note")
        mem_id = dec["memory_id"]
        doc_key = f"memory_{mem_id}"

        MemoryPipeline.process_correction(item_id=1, original_suggestion="Suggest standup", user_correction="Standup at 10am", memory_id_applied=mem_id)

        mem = MemoryModel.get(mem_id)
        expected_weight = round(0.70 - PENALTY_STEP, 4)
        self.assertAlmostEqual(mem["confidence_weight"], expected_weight)
        self.assertAlmostEqual(vector_store.global_vector_store.documents[doc_key]["metadata"]["confidence_weight"], expected_weight)

    def test_feedback_sync_handles_missing_vector_doc_gracefully(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        mem_id = MemoryModel.create("preference", "Directly inserted memory", 0.60, "direct")
        doc_key = f"memory_{mem_id}"
        if doc_key in vector_store.global_vector_store.documents:
            del vector_store.global_vector_store.documents[doc_key]

        MemoryPipeline.process_acceptance(item_id=1, original_suggestion="test", memory_id_applied=mem_id)
        mem = MemoryModel.get(mem_id)
        self.assertTrue(mem["confidence_weight"] > 0.60)

        MemoryPipeline.process_correction(item_id=1, original_suggestion="test", user_correction="correction", memory_id_applied=mem_id)
        mem2 = MemoryModel.get(mem_id)
        self.assertIsNotNone(mem2)

    def test_identical_statement_is_idempotent_noop(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel, MemoryDecisionLogModel

        dec1 = MemoryPipeline.reconcile_memory("preference", "Prefer Python for scripting.", 0.80, "note 1")
        self.assertEqual(dec1["action"], "ADD")
        mem_id = dec1["memory_id"]

        dec2 = MemoryPipeline.reconcile_memory("preference", "  prefer python for scripting!  ", 0.80, "note 2")
        self.assertEqual(dec2["action"], "NOOP")
        self.assertEqual(dec2["memory_id"], mem_id)

        active = MemoryModel.list_active()
        self.assertEqual(len(active), 1)

        logs = MemoryDecisionLogModel.get_for_memory(mem_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "ADD")

    def test_repeated_identical_statements_do_not_create_duplicate_active_memories(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        for i in range(5):
            res = MemoryPipeline.reconcile_memory("preference", "Daily backup runs at midnight.", 0.75, f"run {i}")
            if i == 0:
                self.assertEqual(res["action"], "ADD")
            else:
                self.assertEqual(res["action"], "NOOP")

        active = MemoryModel.list_active()
        self.assertEqual(len(active), 1)

        mem_docs = [k for k in vector_store.global_vector_store.documents.keys() if k.startswith("memory_")]
        self.assertEqual(len(mem_docs), 1)

    def test_repeated_identical_statements_do_not_pollute_source_context(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel

        dec = MemoryPipeline.reconcile_memory("preference", "Backup database at midnight.", 0.75, "Initial note")
        mem_id = dec["memory_id"]

        for i in range(3):
            MemoryPipeline.reconcile_memory("preference", "Backup database at midnight.", 0.75, f"Pollution attempt {i}")

        mem = MemoryModel.get(mem_id)
        self.assertEqual(mem["source_context"], "Initial note")

        # Sequential refinements
        MemoryPipeline.reconcile_memory("preference", "Backup database and logs at midnight.", 0.80, "Refinement 1")
        mem_up1 = MemoryModel.get(mem_id)
        self.assertIn("Refinement 1", mem_up1["source_context"])
        self.assertEqual(mem_up1["source_context"].count("Updated from:"), 1)

        MemoryPipeline.reconcile_memory("preference", "Backup database and compress logs at midnight.", 0.85, "Refinement 2")
        mem_up2 = MemoryModel.get(mem_id)
        self.assertIn("Refinement 2", mem_up2["source_context"])
        self.assertEqual(mem_up2["source_context"].count("Updated from:"), 1)

    def test_reconciliation_does_not_perform_vector_searches_in_loop(self):
        from app.memory.pipeline import MemoryPipeline
        from app import vector_store

        MemoryPipeline.reconcile_memory("preference", "Task review in morning", 0.70, "note 1")
        MemoryPipeline.reconcile_memory("preference", "Deploy on Thursdays", 0.70, "note 2")

        with patch.object(vector_store.global_vector_store, "search", wraps=vector_store.global_vector_store.search) as mock_search:
            MemoryPipeline.reconcile_memory("preference", "Task review in late morning", 0.75, "note 3")
            mock_search.assert_not_called()

    def test_pure_semantic_matching_not_biased_by_recency_or_confidence(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        # Memory 1: Older, low confidence (0.20), high semantic similarity
        m1_id = MemoryModel.create(
            category="preference",
            content="Draft product specifications in markdown format",
            confidence_weight=0.20,
            source_context="old note"
        )
        vector_store.global_vector_store.index_document(
            m1_id, "memory", "Draft product specifications in markdown format",
            metadata={"category": "preference", "confidence_weight": 0.20}
        )
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        vector_store.global_vector_store.documents[f"memory_{m1_id}"]["updated_at"] = old_time

        # Memory 2: Recent, high confidence (1.00), lower semantic similarity
        m2_id = MemoryModel.create(
            category="preference",
            content="Organize roadmap slides in google drive",
            confidence_weight=1.00,
            source_context="new note"
        )
        vector_store.global_vector_store.index_document(
            m2_id, "memory", "Organize roadmap slides in google drive",
            metadata={"category": "preference", "confidence_weight": 1.00}
        )

        candidate = "Draft software specifications in markdown format"
        dec = MemoryPipeline.reconcile_memory("preference", candidate, 0.85, "update note")

        self.assertEqual(dec["action"], "UPDATE")
        self.assertEqual(dec["memory_id"], m1_id)
        self.assertNotEqual(dec["memory_id"], m2_id)

    def test_related_refinement_produces_update_not_noop(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel

        dec1 = MemoryPipeline.reconcile_memory("preference", "Client presentation tasks are marked high priority.", 0.75, "note 1")
        self.assertEqual(dec1["action"], "ADD")
        m_id = dec1["memory_id"]

        dec2 = MemoryPipeline.reconcile_memory("preference", "Client presentation tasks are marked urgent priority.", 0.85, "correction")
        self.assertEqual(dec2["action"], "UPDATE")
        self.assertEqual(dec2["memory_id"], m_id)

        mem = MemoryModel.get(m_id)
        self.assertIn("urgent priority", mem["content"])
        self.assertEqual(mem["update_count"], 1)

    def test_update_keeps_vector_and_sqlite_synchronized(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("preference", "Review pull requests before noon.", 0.70, "note 1")
        m_id = dec["memory_id"]

        MemoryPipeline.reconcile_memory("preference", "Review pull requests before 11am.", 0.80, "note 2")

        mem = MemoryModel.get(m_id)
        self.assertEqual(mem["content"], "Review pull requests before 11am.")

        doc = vector_store.global_vector_store.documents[f"memory_{m_id}"]
        self.assertEqual(doc["text"], "Review pull requests before 11am.")
        self.assertEqual(doc["metadata"]["confidence_weight"], mem["confidence_weight"])

    def test_delete_removes_vector_document(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("preference", "Always book flights on Tuesdays.", 0.80, "note")
        m_id = dec["memory_id"]
        self.assertIn(f"memory_{m_id}", vector_store.global_vector_store.documents)

        dec_del = MemoryPipeline.reconcile_memory(
            "preference",
            "Don't do this anymore: stop suggesting flights on Tuesdays.",
            0.90,
            "user command"
        )
        self.assertEqual(dec_del["action"], "DELETE")
        self.assertEqual(dec_del["memory_id"], m_id)

        mem = MemoryModel.get(m_id)
        self.assertEqual(mem["status"], "deleted")
        self.assertNotIn(f"memory_{m_id}", vector_store.global_vector_store.documents)

    def test_add_creates_vector_document(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("habit", "Water the office plants on Monday mornings.", 0.75, "note")
        self.assertEqual(dec["action"], "ADD")
        m_id = dec["memory_id"]

        mem = MemoryModel.get(m_id)
        self.assertEqual(mem["status"], "active")

        self.assertIn(f"memory_{m_id}", vector_store.global_vector_store.documents)
        doc = vector_store.global_vector_store.documents[f"memory_{m_id}"]
        self.assertEqual(doc["doc_type"], "memory")
        self.assertEqual(doc["text"], "Water the office plants on Monday mornings.")

    def test_vector_store_failure_does_not_corrupt_sqlite(self):
        from app.memory.pipeline import MemoryPipeline
        from app.models import MemoryModel
        from app import vector_store

        # ADD failure
        with patch.object(vector_store.global_vector_store, "index_document", side_effect=IOError("Disk full")):
            dec = MemoryPipeline.reconcile_memory("preference", "Unique resilient preference test.", 0.75, "note")
            self.assertEqual(dec["action"], "ADD")
            self.assertFalse(dec["vector_synced"])
            m_id = dec["memory_id"]
            mem = MemoryModel.get(m_id)
            self.assertIsNotNone(mem)
            self.assertEqual(mem["content"], "Unique resilient preference test.")

        # UPDATE failure
        with patch.object(vector_store.global_vector_store, "index_document", side_effect=IOError("Disk full")):
            dec_up = MemoryPipeline.reconcile_memory("preference", "Unique resilient preference updated.", 0.85, "note 2")
            self.assertEqual(dec_up["action"], "UPDATE")
            self.assertFalse(dec_up["vector_synced"])
            mem_up = MemoryModel.get(m_id)
            self.assertEqual(mem_up["content"], "Unique resilient preference updated.")

        # DELETE failure
        with patch.object(vector_store.global_vector_store, "remove_document", side_effect=IOError("Disk full")):
            dec_del = MemoryPipeline.reconcile_memory("preference", "Don't do this anymore: stop suggesting unique resilient preference.", 0.95, "note 3")
            self.assertEqual(dec_del["action"], "DELETE")
            self.assertFalse(dec_del["vector_synced"])
            mem_del = MemoryModel.get(m_id)
            self.assertEqual(mem_del["status"], "deleted")

    def test_stale_deleted_memory_vector_documents_not_retained(self):
        from app.memory.pipeline import MemoryPipeline
        from app import vector_store

        dec = MemoryPipeline.reconcile_memory("preference", "Temporary rule about team meetings.", 0.80, "note")
        m_id = dec["memory_id"]
        self.assertIn(f"memory_{m_id}", vector_store.global_vector_store.documents)

        # Invalidate
        MemoryPipeline.reconcile_memory("preference", "Don't do this anymore: stop suggesting team meetings rule.", 0.90, "user")
        self.assertNotIn(f"memory_{m_id}", vector_store.global_vector_store.documents)

        # Trigger index validation / rebuild
        vector_store.global_vector_store.ensure_valid_index()
        self.assertNotIn(f"memory_{m_id}", vector_store.global_vector_store.documents)

    def test_failed_add_recovered_subsequently_from_sqlite(self):
        from app.memory.pipeline import MemoryPipeline
        from app import vector_store

        # Simulate vector store failure during ADD
        with patch.object(vector_store.global_vector_store, "save", side_effect=IOError("Disk write failed")):
            dec = MemoryPipeline.reconcile_memory("preference", "Unique recovery ADD test.", 0.75, "note 1")
            self.assertEqual(dec["action"], "ADD")
            self.assertFalse(dec["vector_synced"])
            m_id = dec["memory_id"]

        doc_key = f"memory_{m_id}"
        if doc_key in vector_store.global_vector_store.documents:
            del vector_store.global_vector_store.documents[doc_key]

        # Trigger recovery via existing validation path
        rebuilt = vector_store.global_vector_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertIn(doc_key, vector_store.global_vector_store.documents)
        doc = vector_store.global_vector_store.documents[doc_key]
        self.assertEqual(doc["text"], "Unique recovery ADD test.")
        self.assertEqual(doc["metadata"]["confidence_weight"], 0.75)

    def test_failed_update_recovered_subsequently_from_sqlite(self):
        from app.memory.pipeline import MemoryPipeline
        from app import vector_store

        # Initial memory created and synchronized
        dec = MemoryPipeline.reconcile_memory("preference", "Review pull requests before noon.", 0.70, "initial")
        self.assertEqual(dec["action"], "ADD")
        self.assertTrue(dec["vector_synced"])
        m_id = dec["memory_id"]
        doc_key = f"memory_{m_id}"
        self.assertEqual(vector_store.global_vector_store.documents[doc_key]["text"], "Review pull requests before noon.")

        # Simulate vector store failure during UPDATE
        with patch.object(vector_store.global_vector_store, "save", side_effect=IOError("Disk write failed")):
            dec_up = MemoryPipeline.reconcile_memory("preference", "Review pull requests before 11am.", 0.85, "refinement")
            self.assertEqual(dec_up["action"], "UPDATE")
            self.assertFalse(dec_up["vector_synced"])

        # Stale content remains in vector store
        vector_store.global_vector_store.documents[doc_key]["text"] = "Review pull requests before noon."

        # Trigger recovery via existing validation path
        rebuilt = vector_store.global_vector_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertEqual(vector_store.global_vector_store.documents[doc_key]["text"], "Review pull requests before 11am.")

    def test_failed_delete_recovered_subsequently_from_sqlite(self):
        from app.memory.pipeline import MemoryPipeline
        from app import vector_store

        # Initial memory created and synchronized
        dec = MemoryPipeline.reconcile_memory("preference", "Always book morning flights on Tuesdays.", 0.80, "initial")
        self.assertEqual(dec["action"], "ADD")
        self.assertTrue(dec["vector_synced"])
        m_id = dec["memory_id"]
        doc_key = f"memory_{m_id}"
        self.assertIn(doc_key, vector_store.global_vector_store.documents)

        # Simulate vector store failure during DELETE
        with patch.object(vector_store.global_vector_store, "remove_document", side_effect=IOError("Disk write failed")):
            dec_del = MemoryPipeline.reconcile_memory(
                "preference",
                "Don't do this anymore: stop suggesting morning flights on Tuesdays.",
                0.90,
                "user"
            )
            self.assertEqual(dec_del["action"], "DELETE")
            self.assertFalse(dec_del["vector_synced"])

        # Stale document remains in vector store
        self.assertIn(doc_key, vector_store.global_vector_store.documents)

        # Trigger recovery via existing validation path
        rebuilt = vector_store.global_vector_store.ensure_valid_index()
        self.assertTrue(rebuilt)
        self.assertNotIn(doc_key, vector_store.global_vector_store.documents)


if __name__ == "__main__":
    unittest.main()
