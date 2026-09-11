import unittest
from app.reasoning.local_reasoner import LocalReasoner

class TestLocalReasoner(unittest.TestCase):
    def setUp(self):
        self.reasoner = LocalReasoner()

    def test_quick_capture_parsing(self):
        # Task with urgency and tags
        res1 = self.reasoner.parse_quick_capture("Fix critical authentication crash #security #bug")
        self.assertEqual(res1["type"], "task")
        self.assertEqual(res1["priority"], "urgent")
        self.assertIn("security", res1["tags"])
        self.assertIn("bug", res1["tags"])

        # Note detection
        res2 = self.reasoner.parse_quick_capture("Note: Local SQLite WAL mode provides high read concurrency")
        self.assertEqual(res2["type"], "note")

    def test_task_enhancements_with_memory(self):
        # Without memory -> heuristic
        res_heur = self.reasoner.suggest_task_enhancements("Prepare slide deck", "Quarterly updates", [])
        self.assertEqual(res_heur["suggested_priority"], "medium")
        self.assertTrue(len(res_heur["subtasks"]) > 0)

        # With learned memory for Acme client demo
        retrieved_mem = [{
            "id": 42,
            "text": "Acme client presentation tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.95}
        }]
        res_mem = self.reasoner.suggest_task_enhancements("Acme client presentation deck", "", retrieved_mem)
        self.assertEqual(res_mem["suggested_priority"], "urgent")
        self.assertEqual(res_mem["applied_memory_id"], 42)
        self.assertIn("Applied learned habit", res_mem["rationale"])

    def test_offline_chat(self):
        mems = [{
            "id": 1,
            "text": "User prefers deep work in the morning",
            "metadata": {"category": "habit", "confidence_weight": 0.85}
        }]
        tasks = [{"id": 1, "title": "Implement vector index", "priority": "high", "status": "todo"}]
        
        reply = self.reasoner.generate_chat_response(
            message="What are my priorities?",
            chat_history=[],
            retrieved_memories=mems,
            active_tasks=tasks,
            recent_notes=[]
        )
        self.assertIn("Implement vector index", reply["reply"])

if __name__ == "__main__":
    unittest.main()
