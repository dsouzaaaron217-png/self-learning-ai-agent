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

    def test_confidence_floor_boundary(self):
        # 0.39 should not drive suggestion
        mem_39 = [{
            "id": 1,
            "text": "Security audit tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.39}
        }]
        res_39 = self.reasoner.suggest_task_enhancements("Security audit review", "", mem_39)
        self.assertIsNone(res_39["applied_memory_id"])

        # 0.40 should drive suggestion
        mem_40 = [{
            "id": 2,
            "text": "Security audit tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.40}
        }]
        res_40 = self.reasoner.suggest_task_enhancements("Security audit review", "", mem_40)
        self.assertEqual(res_40["applied_memory_id"], 2)
        self.assertEqual(res_40["suggested_priority"], "urgent")

    def test_generic_stop_words_no_false_positive(self):
        mem = [{
            "id": 5,
            "text": "User prefers client presentation tasks are marked high priority.",
            "metadata": {"confidence_weight": 0.90}
        }]
        # Generic word "tasks" alone must not match
        res_generic = self.reasoner.suggest_task_enhancements("General documentation tasks", "Routine chores", mem)
        self.assertIsNone(res_generic["applied_memory_id"])
        self.assertEqual(res_generic["suggested_priority"], "medium")

if __name__ == "__main__":
    unittest.main()
