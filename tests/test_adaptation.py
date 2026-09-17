import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from app.vector_store import VectorStore

class TestAdaptationSuite(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_adapt.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_adapt_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_adapt_auth.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app import vector_store
        self.vs = VectorStore(storage_path=self.test_vec_path)
        vector_store.global_vector_store = self.vs

        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

        # Set up test authentication and grab CSRF token
        setup_res = self.client.post("/api/auth/setup", json={
            "password": "testpassword123",
            "confirm_password": "testpassword123"
        })
        self.csrf_token = setup_res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------
    # 1. QUICK CAPTURE LEARNING TESTS
    # -------------------------------------------------------------
    def test_quick_capture_task_learns_memory(self):
        from app.models import MemoryModel, MemoryDecisionLogModel

        capture_text = "Acme demo deck. I prefer client presentation tasks are marked urgent priority."
        res = self.client.post(
            "/api/tasks/quick-capture",
            json={"text": capture_text},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["type"], "task")
        self.assertIn("learned_memories", data)
        self.assertTrue(len(data["learned_memories"]) >= 1)
        self.assertEqual(data["learned_memories"][0]["action"], "ADD")

        # Verify persisted in SQLite
        active_mems = MemoryModel.list_active()
        self.assertTrue(any("client presentation" in m["content"].lower() and "urgent" in m["content"].lower() for m in active_mems))

        # Verify decision log recorded with task_capture trigger and bounded source
        logs = MemoryDecisionLogModel.get_recent(limit=10)
        task_logs = [l for l in logs if l["triggered_by"] == "task_capture"]
        self.assertTrue(len(task_logs) >= 1)
        self.assertIn(f"Quick capture task #{data['item']['id']}", MemoryModel.get(task_logs[0]["memory_id"])["source_context"])

        # Verify document indexed in vector store
        doc_key = f"memory_{data['learned_memories'][0]['memory_id']}"
        self.assertIn(doc_key, self.vs.documents)

    def test_quick_capture_note_learns_memory(self):
        from app.models import MemoryModel, MemoryDecisionLogModel

        capture_text = "Note: Every morning team standup meeting at 9am."
        res = self.client.post(
            "/api/tasks/quick-capture",
            json={"text": capture_text},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["type"], "note")
        self.assertIn("learned_memories", data)
        self.assertTrue(len(data["learned_memories"]) >= 1)
        self.assertEqual(data["learned_memories"][0]["action"], "ADD")

        # Verify persisted in SQLite
        note_id = data["item"]["id"]
        logs = MemoryDecisionLogModel.get_recent(limit=10)
        note_logs = [l for l in logs if l["triggered_by"] == "note_capture"]
        self.assertTrue(len(note_logs) >= 1)
        mem = MemoryModel.get(note_logs[0]["memory_id"])
        self.assertIn(f"Quick capture note #{note_id}", mem["source_context"])

        # Verify vector store index
        doc_key = f"memory_{mem['id']}"
        self.assertIn(doc_key, self.vs.documents)

    def test_quick_capture_idempotency_prevents_duplicate_active_memory(self):
        from app.models import MemoryModel

        text = "Security review. I prefer pull request tasks are marked high priority."
        # First capture
        res1 = self.client.post(
            "/api/tasks/quick-capture",
            json={"text": text},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        data1 = res1.get_json()
        self.assertEqual(data1["learned_memories"][0]["action"], "ADD")
        mem_id = data1["learned_memories"][0]["memory_id"]

        # Second equivalent capture
        res2 = self.client.post(
            "/api/tasks/quick-capture",
            json={"text": text},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        data2 = res2.get_json()
        # Second run should confirm with NOOP, not create duplicate
        self.assertEqual(data2["learned_memories"][0]["action"], "NOOP")
        self.assertEqual(data2["learned_memories"][0]["memory_id"], mem_id)

        # Confirm count in SQLite is exactly 1 active memory
        active_mems = MemoryModel.list_active()
        matching = [m for m in active_mems if "pull request" in m["content"].lower()]
        self.assertEqual(len(matching), 1)

    # -------------------------------------------------------------
    # 2. COMPLETE FEEDBACK STATE MACHINE TESTS
    # -------------------------------------------------------------
    def test_feedback_accepted_workflow(self):
        from app.models import MemoryModel, TaskModel

        # Create explicit memory with confidence 0.70
        mem_id = MemoryModel.create(
            category="preference",
            content="Deploy tasks are marked urgent priority.",
            confidence_weight=0.70,
            source_context="test"
        )
        self.vs.index_document(doc_id=mem_id, doc_type="memory", text="Deploy tasks are marked urgent priority.", metadata={"confidence_weight": 0.70})

        # Create task with this memory applied
        task_id = TaskModel.create(
            title="Deploy release to staging",
            priority="urgent",
            ai_suggestion_context={"suggested_priority": "urgent", "applied_memory_id": mem_id}
        )

        # Submit accepted feedback
        res_fb = self.client.post(
            f"/api/tasks/{task_id}/feedback",
            json={"action": "accepted"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_fb.status_code, 200)
        self.assertTrue(res_fb.get_json()["success"])

        # Check confidence reinforced from 0.70 to 0.85
        mem_after = MemoryModel.get(mem_id)
        self.assertAlmostEqual(mem_after["confidence_weight"], 0.85, places=2)

        # Check vector store confidence synchronized
        doc = self.vs.documents[f"memory_{mem_id}"]
        self.assertAlmostEqual(doc["metadata"]["confidence_weight"], 0.85, places=2)

    def test_feedback_corrected_workflow(self):
        from app.models import MemoryModel, TaskModel

        mem_id = MemoryModel.create(
            category="preference",
            content="Database migration tasks are marked urgent priority.",
            confidence_weight=0.70,
            source_context="test"
        )
        self.vs.index_document(doc_id=mem_id, doc_type="memory", text="Database migration tasks are marked urgent priority.", metadata={"confidence_weight": 0.70})

        task_id = TaskModel.create(
            title="Database migration schema",
            priority="urgent",
            ai_suggestion_context={"suggested_priority": "urgent", "applied_memory_id": mem_id}
        )

        # Submit corrected feedback
        res_fb = self.client.post(
            f"/api/tasks/{task_id}/feedback",
            json={
                "action": "corrected",
                "correction": "Database migration tasks are medium priority"
            },
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_fb.status_code, 200)
        fb_data = res_fb.get_json()
        self.assertTrue(fb_data["success"])
        self.assertEqual(fb_data["decision"]["action"], "SUPERSEDE")

        # Phase 5E: Old memory is preserved with status='superseded' and points to replacement
        mem_old = MemoryModel.get(mem_id)
        self.assertEqual(mem_old["status"], "superseded")
        self.assertIsNotNone(mem_old["superseded_by"])
        self.assertIn("urgent priority", mem_old["content"])  # Original content preserved intact

        # Phase 5E: Replacement memory is active with the corrected rule and confidence 0.90
        rep_id = mem_old["superseded_by"]
        replacement = MemoryModel.get(rep_id)
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement["status"], "active")
        self.assertIn("medium priority", replacement["content"])
        self.assertAlmostEqual(replacement["confidence_weight"], 0.90, places=2)

        # Verify vector store synchronization: old memory unindexed, replacement indexed
        self.assertNotIn(f"memory_{mem_id}", self.vs.documents)
        self.assertIn(f"memory_{rep_id}", self.vs.documents)
        self.assertIn("medium priority", self.vs.documents[f"memory_{rep_id}"]["text"])

    def test_feedback_rejected_workflow(self):
        from app.models import MemoryModel, TaskModel, FeedbackModel

        mem_id = MemoryModel.create(
            category="preference",
            content="Client presentation tasks are marked urgent priority.",
            confidence_weight=0.70,
            source_context="initial"
        )
        self.vs.index_document(doc_id=mem_id, doc_type="memory", text="Client presentation tasks are marked urgent priority.", metadata={"confidence_weight": 0.70})

        task_id = TaskModel.create(
            title="Client presentation for stakeholders",
            priority="urgent",
            ai_suggestion_context={"suggested_priority": "urgent", "applied_memory_id": mem_id}
        )

        # Submit rejected feedback
        res_rej = self.client.post(
            f"/api/tasks/{task_id}/feedback",
            json={"action": "rejected"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_rej.status_code, 200)
        self.assertTrue(res_rej.get_json()["success"])
        self.assertIn("dismissed", res_rej.get_json()["message"].lower())

        # 1. Verify event in feedback_events
        metrics = FeedbackModel.get_metrics()
        self.assertEqual(metrics["rejected_count"], 1)

        # 2. Verify applied memory penalized mildly by 0.10 (0.70 -> 0.60)
        mem = MemoryModel.get(mem_id)
        self.assertAlmostEqual(mem["confidence_weight"], 0.60, places=2)

        # 3. Verify vector store synchronized
        doc = self.vs.documents[f"memory_{mem_id}"]
        self.assertAlmostEqual(doc["metadata"]["confidence_weight"], 0.60, places=2)

        # 4. Verify task context updated
        t = TaskModel.get(task_id)
        self.assertFalse(t["ai_suggestion_context"]["user_accepted"])

        # 5. Verify NO correction memory created
        active = MemoryModel.list_active(category="correction")
        self.assertEqual(len(active), 0)

    def test_repeated_rejection_respects_confidence_floor(self):
        from app.models import MemoryModel, TaskModel
        from app.memory.pipeline import MemoryPipeline
        from app.config import MIN_CONFIDENCE_WEIGHT

        dec = MemoryPipeline.reconcile_memory(
            category="preference",
            content="Documentation review tasks are marked low priority.",
            confidence=0.25,
            source_context="initial"
        )
        mem_id = dec["memory_id"]

        # Simulate task with this memory applied
        task_id = TaskModel.create(
            title="Documentation review",
            ai_suggestion_context={"suggested_priority": "low", "applied_memory_id": mem_id}
        )

        # Rejection 1: 0.25 -> 0.15
        self.client.post(f"/api/tasks/{task_id}/feedback", json={"action": "rejected"}, headers={"X-CSRF-Token": self.csrf_token})
        self.assertAlmostEqual(MemoryModel.get(mem_id)["confidence_weight"], 0.15, places=2)

        # Rejection 2: 0.15 -> 0.10 (MIN_CONFIDENCE_WEIGHT)
        self.client.post(f"/api/tasks/{task_id}/feedback", json={"action": "rejected"}, headers={"X-CSRF-Token": self.csrf_token})
        self.assertAlmostEqual(MemoryModel.get(mem_id)["confidence_weight"], MIN_CONFIDENCE_WEIGHT, places=2)

        # Rejection 3: Remains bounded at 0.10
        self.client.post(f"/api/tasks/{task_id}/feedback", json={"action": "rejected"}, headers={"X-CSRF-Token": self.csrf_token})
        self.assertAlmostEqual(MemoryModel.get(mem_id)["confidence_weight"], MIN_CONFIDENCE_WEIGHT, places=2)

    # -------------------------------------------------------------
    # 3. LOCAL REASONER HARDENING TESTS
    # -------------------------------------------------------------
    def test_local_reasoner_confidence_floor(self):
        from app.reasoning.local_reasoner import LocalReasoner

        reasoner = LocalReasoner()

        # Memory with confidence 0.39 (below floor 0.40)
        low_conf_mem = [{
            "id": 10,
            "text": "Client presentation tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.39, "category": "habit"}
        }]
        res_low = reasoner.suggest_task_enhancements("Client presentation deck", "", low_conf_mem)
        self.assertIsNone(res_low["applied_memory_id"])
        self.assertNotEqual(res_low["suggested_priority"], "urgent")

        # Memory with confidence 0.40 (at floor)
        valid_conf_mem = [{
            "id": 11,
            "text": "Client presentation tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.40, "category": "habit"}
        }]
        res_valid = reasoner.suggest_task_enhancements("Client presentation deck", "", valid_conf_mem)
        self.assertEqual(res_valid["applied_memory_id"], 11)
        self.assertEqual(res_valid["suggested_priority"], "urgent")

    def test_local_reasoner_keyword_overmatching_prevention(self):
        from app.reasoning.local_reasoner import LocalReasoner

        reasoner = LocalReasoner()

        mem = [{
            "id": 20,
            "text": "User prefers client presentation tasks are marked high priority.",
            "metadata": {"confidence_weight": 0.85, "category": "preference"}
        }]

        # Task sharing ONLY generic word "tasks" -> Must NOT trigger high priority
        res_generic = reasoner.suggest_task_enhancements("General documentation tasks", "Routine chores", mem)
        self.assertIsNone(res_generic["applied_memory_id"])
        self.assertEqual(res_generic["suggested_priority"], "medium")

        # Task sharing meaningful topic tokens ("client", "presentation") -> Must match
        res_topic = reasoner.suggest_task_enhancements("Prepare client presentation for Acme", "", mem)
        self.assertEqual(res_topic["applied_memory_id"], 20)
        self.assertEqual(res_topic["suggested_priority"], "high")

    def test_local_reasoner_arbitration_prefers_higher_confidence(self):
        from app.reasoning.local_reasoner import LocalReasoner

        reasoner = LocalReasoner()

        # Memory A: High priority, conf 0.60
        mem_a = {
            "id": 1,
            "text": "Marketing campaign tasks are marked high priority.",
            "metadata": {"confidence_weight": 0.60, "category": "habit"}
        }
        # Memory B: Urgent priority, conf 0.90
        mem_b = {
            "id": 2,
            "text": "Marketing campaign tasks are marked urgent priority.",
            "metadata": {"confidence_weight": 0.90, "category": "habit"}
        }

        # Order 1: [A, B]
        res1 = reasoner.suggest_task_enhancements("Marketing campaign launch", "", [mem_a, mem_b])
        self.assertEqual(res1["applied_memory_id"], 2)
        self.assertEqual(res1["suggested_priority"], "urgent")

        # Order 2: [B, A]
        res2 = reasoner.suggest_task_enhancements("Marketing campaign launch", "", [mem_b, mem_a])
        self.assertEqual(res2["applied_memory_id"], 2)
        self.assertEqual(res2["suggested_priority"], "urgent")

    # -------------------------------------------------------------
    # 4. OLLAMA ATTRIBUTION CONSERVATISM TESTS
    # -------------------------------------------------------------
    def test_ollama_attribution_accepts_valid_id(self):
        from app.reasoning.ollama_adapter import OllamaAdapter

        adapter = OllamaAdapter()
        retrieved = [
            {"id": 101, "text": "Acme tasks are marked urgent priority.", "metadata": {"confidence_weight": 0.85}},
            {"id": 102, "text": "Beta tasks are marked low priority.", "metadata": {"confidence_weight": 0.80}}
        ]

        mock_reply = json.dumps({
            "priority": "urgent",
            "subtasks": ["Step 1", "Step 2"],
            "tags": ["acme"],
            "rationale": "Followed Acme preference habit.",
            "applied_memory_id": 101
        })

        with patch.object(adapter, "is_available", return_value=True), \
             patch.object(adapter, "_query_ollama", return_value=mock_reply):
            res = adapter.suggest_task_enhancements("Prepare Acme demo", "", retrieved)
            self.assertEqual(res["applied_memory_id"], 101)
            self.assertEqual(res["applied_memory_text"], "Acme tasks are marked urgent priority.")
            self.assertEqual(res["suggested_priority"], "urgent")

    def test_ollama_attribution_rejects_invalid_or_missing_id(self):
        from app.reasoning.ollama_adapter import OllamaAdapter

        adapter = OllamaAdapter()
        retrieved = [
            {"id": 101, "text": "Acme tasks are marked urgent priority.", "metadata": {"confidence_weight": 0.85}}
        ]

        # Case 1: Model returned an ID not in candidate list (e.g. 999)
        mock_reply_invalid = json.dumps({
            "priority": "high",
            "subtasks": ["Step 1"],
            "tags": ["test"],
            "rationale": "General reasoning.",
            "applied_memory_id": 999
        })
        with patch.object(adapter, "is_available", return_value=True), \
             patch.object(adapter, "_query_ollama", return_value=mock_reply_invalid):
            res1 = adapter.suggest_task_enhancements("Task title", "", retrieved)
            self.assertIsNone(res1["applied_memory_id"])
            self.assertIsNone(res1["applied_memory_text"])

        # Case 2: Model returned null applied_memory_id
        mock_reply_null = json.dumps({
            "priority": "medium",
            "subtasks": ["Step 1"],
            "tags": ["test"],
            "rationale": "General reasoning.",
            "applied_memory_id": None
        })
        with patch.object(adapter, "is_available", return_value=True), \
             patch.object(adapter, "_query_ollama", return_value=mock_reply_null):
            res2 = adapter.suggest_task_enhancements("Task title", "", retrieved)
            self.assertIsNone(res2["applied_memory_id"])
            self.assertIsNone(res2["applied_memory_text"])

    # -------------------------------------------------------------
    # 5. ADAPTATION METRICS AGGREGATION TESTS
    # -------------------------------------------------------------
    def test_metrics_aggregation_counts(self):
        from app.models import FeedbackModel
        from app.memory.tracker import MemoryTracker

        # Record mixed feedback events
        FeedbackModel.record("task_suggestion", 1, None, "P: high", "accepted")
        FeedbackModel.record("task_suggestion", 2, None, "P: high", "accepted")
        FeedbackModel.record("task_suggestion", 3, None, "P: urgent", "accepted")
        FeedbackModel.record("task_suggestion", 4, None, "P: low", "corrected", "Should be high")
        FeedbackModel.record("task_suggestion", 5, None, "P: urgent", "rejected")

        metrics = FeedbackModel.get_metrics()
        self.assertEqual(metrics["total_events"], 5)
        self.assertEqual(metrics["accepted_count"], 3)
        self.assertEqual(metrics["corrected_count"], 1)
        self.assertEqual(metrics["rejected_count"], 1)
        self.assertAlmostEqual(metrics["acceptance_rate_percent"], 60.0, places=1)
        self.assertAlmostEqual(metrics["correction_rate_percent"], 20.0, places=1)

        dashboard = MemoryTracker.get_dashboard_metrics()
        self.assertEqual(dashboard["feedback"]["total_events"], 5)
        self.assertIn("trend_data", dashboard)


if __name__ == "__main__":
    unittest.main()
