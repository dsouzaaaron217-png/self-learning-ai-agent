import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
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

if __name__ == "__main__":
    unittest.main()
