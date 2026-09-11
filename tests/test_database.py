import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_cognito.db"
        
        # Patch DB_PATH in app.config and app.database
        self.patcher = patch("app.config.DB_PATH", self.test_db_path)
        self.patcher.start()
        
        from app.database import init_db
        init_db()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_task_crud(self):
        from app.models import TaskModel
        task_id = TaskModel.create(
            title="Deploy release v1.2",
            description="Run offline verification tests",
            priority="high",
            tags="dev, release",
            subtasks=[{"id": 1, "title": "Run tests", "completed": False}]
        )
        self.assertIsInstance(task_id, int)

        task = TaskModel.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Deploy release v1.2")
        self.assertEqual(task["priority"], "high")
        self.assertEqual(len(task["subtasks"]), 1)

        # Update
        TaskModel.update(task_id, status="in_progress")
        updated = TaskModel.get(task_id)
        self.assertEqual(updated["status"], "in_progress")

        # Delete
        self.assertTrue(TaskModel.delete(task_id))
        self.assertIsNone(TaskModel.get(task_id))

    def test_memory_and_decision_logs(self):
        from app.models import MemoryModel, MemoryDecisionLogModel
        mem_id = MemoryModel.create(
            category="preference",
            content="User prefers morning sessions for coding.",
            confidence_weight=0.8
        )
        self.assertIsInstance(mem_id, int)

        # Log decision
        log_id = MemoryDecisionLogModel.log(
            memory_id=mem_id,
            action="ADD",
            reasoning="Discovered habit from task scheduling history",
            triggered_by="task_capture"
        )
        self.assertIsInstance(log_id, int)

        logs = MemoryDecisionLogModel.get_for_memory(mem_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "ADD")

    def test_feedback_metrics(self):
        from app.models import FeedbackModel
        FeedbackModel.record("task_suggestion", 1, None, "Priority: High", "accepted")
        FeedbackModel.record("task_suggestion", 2, None, "Priority: High", "corrected", "Client tasks are medium priority")
        
        metrics = FeedbackModel.get_metrics()
        self.assertEqual(metrics["total_events"], 2)
        self.assertEqual(metrics["accepted_count"], 1)
        self.assertEqual(metrics["corrected_count"], 1)
        self.assertEqual(metrics["correction_rate_percent"], 50.0)

if __name__ == "__main__":
    unittest.main()
