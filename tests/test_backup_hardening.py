"""
Phase 3E Tests: Logical Backup, Export, and Import Hardening

Covers:
1. test_export_backup_canonical_checksum
2. test_import_valid_backup_with_checksum
3. test_import_rejected_missing_checksum
4. test_import_rejected_invalid_checksum
5. test_import_rejected_tampered_payload
6. test_import_rejected_unsupported_version
7. test_import_rejected_wrong_application
8. test_import_rejected_missing_required_fields
9. test_import_rejected_invalid_field_types
10. test_import_rejected_invalid_enum_values
11. test_import_rejected_duplicate_ids
12. test_import_rejected_invalid_foreign_keys
13. test_import_restores_settings
14. test_pre_restore_safety_snapshot_created
15. test_existing_data_preserved_on_failed_import
16. test_vector_store_properly_rebuilt_post_import
17. test_vector_sync_failure_preserves_sqlite_import
18. test_oversized_backup_rejected
19. test_malformed_backup_rejected
20. test_checksum_verification_happens_before_database_mutation
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.validation import compute_backup_checksum
from app.vector_store import VectorStore
from app.models import TaskModel, NoteModel, MemoryModel, SettingsModel


class TestBackupHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_backup.db"
        self.vec_path = self.temp_path / "test_backup_vec.json"
        self.auth_path = self.temp_path / "test_auth.json"
        self.backup_dir = self.temp_path / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.auth_path)
        self.p_backup = patch("app.config.BACKUP_DIR", self.backup_dir)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()
        self.p_backup.start()

        from app import vector_store
        self.vs = VectorStore(storage_path=self.vec_path)
        vector_store.global_vector_store = self.vs

        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

        # Set up test authentication and grab CSRF token
        setup_res = self.client.post("/api/auth/setup", json={
            "password": "testpassword123",
            "confirm_password": "testpassword123"
        })
        self.assertIn(setup_res.status_code, (200, 201))
        self.csrf_token = setup_res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_backup.stop()
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def _auth_headers(self):
        return {"X-CSRF-Token": self.csrf_token}

    def _create_sample_payload(self):
        """Creates a valid, complete backup payload with matching canonical checksum."""
        data = {
            "tasks": [
                {
                    "id": 1,
                    "title": "Complete Phase 3E",
                    "description": "Hardening backup import and export",
                    "priority": "high",
                    "status": "todo",
                    "tags": "security,backup",
                    "due_date": "2026-09-30",
                    "subtasks": [{"id": 1, "title": "Implement checksum", "completed": True}],
                    "created_at": "2026-09-14T10:00:00+00:00",
                    "updated_at": "2026-09-14T10:00:00+00:00"
                }
            ],
            "notes": [
                {
                    "id": 1,
                    "title": "Architecture Note",
                    "content": "Offline SQLite is authoritative.",
                    "tags": "core,architecture",
                    "pinned": 1,
                    "created_at": "2026-09-14T10:00:00+00:00",
                    "updated_at": "2026-09-14T10:00:00+00:00"
                }
            ],
            "memories": [
                {
                    "id": 1,
                    "category": "preference",
                    "content": "User prefers concise reports.",
                    "confidence_weight": 0.85,
                    "status": "active",
                    "source_context": "Initial setup",
                    "superseded_by": None,
                    "update_count": 0,
                    "created_at": "2026-09-14T10:00:00+00:00",
                    "updated_at": "2026-09-14T10:00:00+00:00"
                }
            ],
            "decision_logs": [
                {
                    "id": 1,
                    "memory_id": 1,
                    "action": "ADD",
                    "reasoning": "Learned from user prompt",
                    "previous_content": None,
                    "new_content": "User prefers concise reports.",
                    "triggered_by": "system",
                    "timestamp": "2026-09-14T10:00:00+00:00"
                }
            ],
            "feedback_events": [
                {
                    "id": 1,
                    "suggestion_type": "task_suggestion",
                    "item_id": 1,
                    "memory_id_applied": 1,
                    "original_suggestion": "Suggested high priority",
                    "user_action": "accepted",
                    "correction_detail": None,
                    "timestamp": "2026-09-14T10:00:00+00:00"
                }
            ],
            "settings": {
                "dark_mode": "true",
                "engine": "local"
            }
        }
        checksum = compute_backup_checksum(data)
        return {
            "version": "1.2.0",
            "application": "Cognito Offline Agent",
            "exported_at": "2026-09-14T10:00:00+00:00",
            "checksum_sha256": checksum,
            "data": data
        }

    # -------------------------------------------------------------------------
    # 1. Export canonical checksum
    # -------------------------------------------------------------------------
    def test_export_backup_canonical_checksum(self):
        """Export generates valid canonical SHA-256 over 'data' subtree."""
        TaskModel.create(title="Export Task", priority="medium")
        res = self.client.get("/api/backup/export")
        self.assertEqual(res.status_code, 200)

        payload = json.loads(res.data.decode("utf-8"))
        self.assertIn("checksum_sha256", payload)
        self.assertIn("data", payload)

        # Verify recomputed canonical checksum matches
        expected_checksum = compute_backup_checksum(payload["data"])
        self.assertEqual(payload["checksum_sha256"], expected_checksum)

        # Formatting differences to the serialized representation do not change the canonical checksum
        minified_str = json.dumps(payload["data"], separators=(",", ":"))
        pretty_str = json.dumps(payload["data"], indent=4)
        parsed_minified = json.loads(minified_str)
        parsed_pretty = json.loads(pretty_str)
        self.assertEqual(compute_backup_checksum(parsed_minified), payload["checksum_sha256"])
        self.assertEqual(compute_backup_checksum(parsed_pretty), payload["checksum_sha256"])

    # -------------------------------------------------------------------------
    # 2. Valid import with checksum
    # -------------------------------------------------------------------------
    def test_import_valid_backup_with_checksum(self):
        """Valid backup with matching checksum restores all entities and settings."""
        payload = self._create_sample_payload()
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(body["sqlite_restored"])
        self.assertTrue(body["vector_synced"])
        self.assertEqual(body["restored"]["tasks"], 1)
        self.assertEqual(body["restored"]["notes"], 1)
        self.assertEqual(body["restored"]["memories"], 1)
        self.assertEqual(body["restored"]["decision_logs"], 1)
        self.assertEqual(body["restored"]["feedback_events"], 1)
        self.assertEqual(body["restored"]["settings"], 2)

        # Verify task exists in SQLite
        task = TaskModel.get(1)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Complete Phase 3E")

        # Verify note exists in SQLite
        note = NoteModel.get(1)
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Architecture Note")

    # -------------------------------------------------------------------------
    # 3. Missing checksum rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_missing_checksum(self):
        """Import without 'checksum_sha256' is rejected with HTTP 400."""
        payload = self._create_sample_payload()
        del payload["checksum_sha256"]
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("checksum_sha256", res.get_json()["error"])

    # -------------------------------------------------------------------------
    # 4. Invalid checksum rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_invalid_checksum(self):
        """Import with mismatched checksum is rejected with HTTP 400."""
        payload = self._create_sample_payload()
        payload["checksum_sha256"] = "0" * 64
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("checksum", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 5. Tampered payload rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_tampered_payload(self):
        """Altering a single byte of content without recomputing checksum causes rejection."""
        payload = self._create_sample_payload()
        # Tamper content
        payload["data"]["notes"][0]["content"] = "Tampered content by attacker"
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("checksum", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 6. Unsupported version rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_unsupported_version(self):
        """Incompatible major version is rejected."""
        payload = self._create_sample_payload()
        payload["version"] = "2.0.0"
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("version", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 7. Wrong application rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_wrong_application(self):
        """Foreign application backup is rejected."""
        payload = self._create_sample_payload()
        payload["application"] = "Foreign App"
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("application", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 8. Missing required fields rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_missing_required_fields(self):
        """Task missing 'title' is rejected before DB modification."""
        payload = self._create_sample_payload()
        del payload["data"]["tasks"][0]["title"]
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("title", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 9. Invalid field types rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_invalid_field_types(self):
        """Task with non-integer id is rejected."""
        payload = self._create_sample_payload()
        payload["data"]["tasks"][0]["id"] = "not_an_int"
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("integer", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 10. Invalid enum values rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_invalid_enum_values(self):
        """Task with illegal priority value is rejected."""
        payload = self._create_sample_payload()
        payload["data"]["tasks"][0]["priority"] = "ultra_urgent"
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("priority", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 11. Duplicate IDs rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_duplicate_ids(self):
        """Payload containing duplicate IDs within an entity list is rejected."""
        payload = self._create_sample_payload()
        duplicate_task = dict(payload["data"]["tasks"][0])
        duplicate_task["title"] = "Second task with same id"
        payload["data"]["tasks"].append(duplicate_task)
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("duplicate", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 12. Invalid foreign keys rejected
    # -------------------------------------------------------------------------
    def test_import_rejected_invalid_foreign_keys(self):
        """Decision log referencing a memory ID not present in the backup is rejected."""
        payload = self._create_sample_payload()
        payload["data"]["decision_logs"][0]["memory_id"] = 9999
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("nonexistent memory_id", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 13. Settings restoration
    # -------------------------------------------------------------------------
    def test_import_restores_settings(self):
        """Settings from backup payload are restored and strictly validated."""
        payload = self._create_sample_payload()
        payload["data"]["settings"] = {
            "dark_mode": "false",
            "engine": "local",
            "ollama_model": "mistral:7b"
        }
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 200)

        self.assertEqual(SettingsModel.get("dark_mode"), "false")
        self.assertEqual(SettingsModel.get("ollama_model"), "mistral:7b")

    # -------------------------------------------------------------------------
    # 14. Pre-restore safety snapshot created
    # -------------------------------------------------------------------------
    def test_pre_restore_safety_snapshot_created(self):
        """Importing a backup creates an emergency safety snapshot before mutating SQLite."""
        NoteModel.create(title="Active Note Pre-Import", content="Must be preserved in snapshot")
        payload = self._create_sample_payload()

        # Count snapshots before import
        snapshots_before = list(self.backup_dir.glob("cognito_snapshot_*.db"))

        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 200)

        snapshots_after = list(self.backup_dir.glob("cognito_snapshot_*.db"))
        self.assertGreater(len(snapshots_after), len(snapshots_before))

        # Validate the created snapshot contains the pre-import note
        latest_snapshot = max(snapshots_after, key=lambda p: p.stat().st_mtime)
        conn = sqlite3.connect(f"file:{latest_snapshot.as_posix()}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT content FROM notes WHERE title = 'Active Note Pre-Import'")
        row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Must be preserved in snapshot")

    # -------------------------------------------------------------------------
    # 15. Existing data preserved on failed import
    # -------------------------------------------------------------------------
    def test_existing_data_preserved_on_failed_import(self):
        """If an import is rejected at validation or fails, active DB is left untouched."""
        t_id = TaskModel.create(title="Preserved Task", priority="urgent")
        n_id = NoteModel.create(title="Preserved Note", content="Original content")

        # Attempt to import an invalid payload
        payload = self._create_sample_payload()
        payload["checksum_sha256"] = "corrupted_checksum"
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)

        # Existing database records remain completely intact
        self.assertIsNotNone(TaskModel.get(t_id))
        self.assertIsNotNone(NoteModel.get(n_id))

    # -------------------------------------------------------------------------
    # 16. Vector store properly rebuilt post-import
    # -------------------------------------------------------------------------
    def test_vector_store_properly_rebuilt_post_import(self):
        """Vector store is rebuilt from authoritative SQLite using Phase 3B/3C mechanisms."""
        payload = self._create_sample_payload()
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 200)

        from app import vector_store
        vs = vector_store.global_vector_store
        self.assertIn("task_1", vs.documents)
        self.assertIn("note_1", vs.documents)
        self.assertIn("memory_1", vs.documents)

        # Verify Phase 3C content sync passes completely
        self.assertTrue(vs._check_db_content_sync())

    # -------------------------------------------------------------------------
    # 17. Vector sync failure preserves SQLite import
    # -------------------------------------------------------------------------
    def test_vector_sync_failure_preserves_sqlite_import(self):
        """If vector store fails to save post-import, SQLite remains restored and failure is surfaced."""
        payload = self._create_sample_payload()
        from app import vector_store

        with patch.object(vector_store.global_vector_store, "rebuild_from_db", side_effect=IOError("Vector disk write error")):
            res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
            self.assertEqual(res.status_code, 200)
            body = res.get_json()
            self.assertFalse(body["success"])
            self.assertTrue(body["sqlite_restored"])
            self.assertFalse(body["vector_synced"])
            self.assertIn("vector store synchronization failed", body["error"])

        # SQLite database holds the restored records intact
        task = TaskModel.get(1)
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Complete Phase 3E")

    # -------------------------------------------------------------------------
    # 18. Oversized backup rejected
    # -------------------------------------------------------------------------
    def test_oversized_backup_rejected(self):
        """Payload exceeding 5,000 items is rejected cleanly with HTTP 400."""
        payload = self._create_sample_payload()
        payload["data"]["tasks"] = [{"id": i, "title": f"Task {i}"} for i in range(1, 5002)]
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])
        res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)
        self.assertIn("too many tasks", res.get_json()["error"].lower())

    # -------------------------------------------------------------------------
    # 19. Malformed backup rejected
    # -------------------------------------------------------------------------
    def test_malformed_backup_rejected(self):
        """Non-dictionary or malformed JSON payloads return HTTP 400."""
        res1 = self.client.post("/api/backup/import", json=["not", "a", "dict"], headers=self._auth_headers())
        self.assertEqual(res1.status_code, 400)

        res2 = self.client.post(
            "/api/backup/import",
            data="invalid json syntax {",
            content_type="application/json",
            headers=self._auth_headers()
        )
        self.assertEqual(res2.status_code, 400)

    # -------------------------------------------------------------------------
    # 20. Checksum verification happens before database mutation
    # -------------------------------------------------------------------------
    def test_checksum_verification_happens_before_database_mutation(self):
        """Corrupt checksum aborts before database_snapshot.create_snapshot or get_db are called."""
        payload = self._create_sample_payload()
        payload["checksum_sha256"] = "invalid_corrupted_checksum"

        with patch("app.database_snapshot.create_snapshot") as mock_snap:
            with patch("app.database.get_db") as mock_db:
                res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
                self.assertEqual(res.status_code, 400)
                mock_snap.assert_not_called()
                mock_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
