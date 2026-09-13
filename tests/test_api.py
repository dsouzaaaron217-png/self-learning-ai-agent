import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.vector_store import VectorStore

class TestAPIEndpoints(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_api.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_api_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_auth.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app import vector_store
        vector_store.global_vector_store = VectorStore(storage_path=self.test_vec_path)

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

    def test_quick_capture_api(self):
        res = self.client.post(
            "/api/tasks/quick-capture",
            json={"text": "Review pull request #code #dev"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["type"], "task")
        self.assertIn("Review pull request", data["item"]["title"])

    def test_feedback_loop_api(self):
        # 1. Create a task
        res_t = self.client.post(
            "/api/tasks",
            json={"title": "Client meeting with Acme"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_t.status_code, 201)
        task_id = res_t.get_json()["task"]["id"]

        # 2. Correct the priority
        res_fb = self.client.post(
            f"/api/tasks/{task_id}/feedback",
            json={
                "action": "corrected",
                "correction": "Acme client meetings must be high priority"
            },
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_fb.status_code, 200)
        fb_data = res_fb.get_json()
        self.assertTrue(fb_data["success"])
        self.assertEqual(fb_data["decision"]["action"], "ADD")

        # 3. Check Transparency Dashboard API
        res_mem = self.client.get("/api/memory")
        self.assertEqual(res_mem.status_code, 200)
        mems = res_mem.get_json()["memories"]
        self.assertTrue(any("Acme client meetings" in m["content"] for m in mems))

        # 4. Check decision audit log
        res_dec = self.client.get("/api/memory/decisions")
        self.assertEqual(res_dec.status_code, 200)
        decs = res_dec.get_json()["decisions"]
        self.assertTrue(len(decs) > 0)

    def test_backup_export_and_import(self):
        # Create a note
        self.client.post(
            "/api/notes",
            json={"title": "Test Note", "content": "Important secret content"},
            headers={"X-CSRF-Token": self.csrf_token}
        )

        # Export
        res_exp = self.client.get("/api/backup/export")
        self.assertEqual(res_exp.status_code, 200)
        backup_data = json.loads(res_exp.data.decode("utf-8"))
        self.assertIn("tasks", backup_data["data"])
        self.assertIn("notes", backup_data["data"])
        self.assertEqual(len(backup_data["data"]["notes"]), 1)

        # Import into fresh state
        res_imp = self.client.post(
            "/api/backup/import",
            json=backup_data,
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_imp.status_code, 200)
        self.assertTrue(res_imp.get_json()["success"])

    def test_chat_api(self):
        res_chat = self.client.post(
            "/api/chat",
            json={"message": "What can you do?"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_chat.status_code, 200)
        data = res_chat.get_json()
        self.assertTrue(data["success"])
        self.assertIn("Offline Productivity Agent", data["reply"])

if __name__ == "__main__":
    unittest.main()
