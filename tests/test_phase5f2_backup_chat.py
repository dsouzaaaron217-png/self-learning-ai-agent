import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.vector_store import VectorStore
from app.models import TaskModel, NoteModel, MemoryModel, ChatMessageModel
from app.validation import compute_backup_checksum, validate_backup_payload
from app.backup_crypto import encrypt_backup, decrypt_backup

class TestPhase5F2BackupChatIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test_backup_chat.db"
        self.vec_path = self.temp_path / "test_backup_chat_vec.json"
        self.auth_path = self.temp_path / "test_backup_chat_auth.json"
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

        # Initial password setup and grab CSRF token
        setup_res = self.client.post("/api/auth/setup", json={
            "password": "masterpassword123",
            "confirm_password": "masterpassword123"
        })
        self.assertEqual(setup_res.status_code, 201)
        self.csrf_token = setup_res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_backup.stop()
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def _auth_headers(self, csrf=None):
        return {"X-CSRF-Token": csrf or self.csrf_token}

    def _create_sample_chat_messages(self):
        """Creates sample chat messages in different sessions with citations."""
        msg1_id = ChatMessageModel.create(
            session_id="session_work",
            role="user",
            content="Plan deep work session before noon.",
            created_at="2026-09-18T10:00:00+00:00"
        )
        msg2_id = ChatMessageModel.create(
            session_id="session_work",
            role="assistant",
            content="Scheduled deep work session based on your habit.",
            cited_memories=[{"id": 1, "content": "User prefers deep work before noon."}],
            created_at="2026-09-18T10:00:05+00:00"
        )
        msg3_id = ChatMessageModel.create(
            session_id="session_personal",
            role="user",
            content="Buy groceries after work.",
            created_at="2026-09-18T11:00:00+00:00"
        )
        msg4_id = ChatMessageModel.create(
            session_id="default",
            role="system",
            content="System session initialized.",
            created_at="2026-09-18T09:00:00+00:00"
        )
        return [msg1_id, msg2_id, msg3_id, msg4_id]

    # =========================================================================
    # 1. Backup Export Tests
    # =========================================================================

    def test_export_includes_chat_messages_and_bumped_version(self):
        """Backup export contains chat_messages in data and bumps version to 1.3.0."""
        msg_ids = self._create_sample_chat_messages()

        res = self.client.get("/api/backup/export")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()

        self.assertEqual(payload["version"], "1.3.0")
        self.assertEqual(payload["application"], "Cognito Offline Agent")
        self.assertIn("chat_messages", payload["data"])

        exported_msgs = payload["data"]["chat_messages"]
        self.assertEqual(len(exported_msgs), len(msg_ids))

        # Check that IDs and sessions match
        exported_ids = [m["id"] for m in exported_msgs]
        self.assertEqual(exported_ids, msg_ids)

        sessions = {m["session_id"] for m in exported_msgs}
        self.assertIn("session_work", sessions)
        self.assertIn("session_personal", sessions)
        self.assertIn("default", sessions)

    def test_chat_messages_contain_all_required_fields(self):
        """Exported chat messages contain id, session_id, role, content, cited_memories, created_at."""
        self._create_sample_chat_messages()

        res = self.client.get("/api/backup/export")
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        messages = payload["data"]["chat_messages"]

        required_keys = {"id", "session_id", "role", "content", "cited_memories", "created_at"}
        for msg in messages:
            self.assertTrue(required_keys.issubset(msg.keys()))
            self.assertIsInstance(msg["id"], int)
            self.assertIsInstance(msg["session_id"], str)
            self.assertIn(msg["role"], {"user", "assistant", "system"})
            self.assertIsInstance(msg["content"], str)
            self.assertIsInstance(msg["created_at"], str)
            self.assertTrue(msg["cited_memories"] is None or isinstance(msg["cited_memories"], (list, str)))

        # Find assistant message and verify citation preservation
        assistant_msg = next(m for m in messages if m["role"] == "assistant")
        self.assertIsNotNone(assistant_msg["cited_memories"])
        if isinstance(assistant_msg["cited_memories"], list):
            self.assertEqual(assistant_msg["cited_memories"][0]["id"], 1)

    # =========================================================================
    # 2. Backup Restore Tests
    # =========================================================================

    def test_import_restores_chat_history(self):
        """Importing a backup restores chat messages faithfully into SQLite."""
        self._create_sample_chat_messages()

        # Export current state
        res_export = self.client.get("/api/backup/export")
        backup_payload = res_export.get_json()

        # Wipe out chat history
        ChatMessageModel.clear_history()
        self.assertEqual(len(ChatMessageModel.list_by_session("session_work")), 0)
        self.assertEqual(len(ChatMessageModel.list_by_session("session_personal")), 0)

        # Import the backup
        res_import = self.client.post(
            "/api/backup/import",
            json=backup_payload,
            headers=self._auth_headers()
        )
        self.assertEqual(res_import.status_code, 200)
        data = res_import.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["sqlite_restored"])
        self.assertEqual(data["restored"]["chat_messages"], 4)

        # Verify chat messages are restored in session_work
        work_msgs = ChatMessageModel.list_by_session("session_work")
        self.assertEqual(len(work_msgs), 2)
        self.assertEqual(work_msgs[0]["role"], "user")
        self.assertEqual(work_msgs[0]["content"], "Plan deep work session before noon.")
        self.assertEqual(work_msgs[1]["role"], "assistant")
        self.assertEqual(work_msgs[1]["cited_memories"][0]["id"], 1)

        # Verify auto-increment continues cleanly after max restored ID
        new_id = ChatMessageModel.create(session_id="session_work", role="user", content="New follow-up.")
        self.assertGreater(new_id, max(m["id"] for m in work_msgs))

    def test_multiple_sessions_restore_correctly_without_mixing(self):
        """Multiple distinct chat sessions restore with session boundaries intact."""
        self._create_sample_chat_messages()

        # Pre-seed existing unrelated session in database before import
        ChatMessageModel.create(session_id="unrelated_session", role="user", content="Pre-existing data")

        res_export = self.client.get("/api/backup/export")
        backup_payload = res_export.get_json()

        # Add a local session that shouldn't survive the restore
        ChatMessageModel.create(session_id="temporary_session", role="user", content="Temp message")
        self.assertEqual(len(ChatMessageModel.list_by_session("temporary_session")), 1)

        # Import backup
        res_import = self.client.post(
            "/api/backup/import",
            json=backup_payload,
            headers=self._auth_headers()
        )
        self.assertEqual(res_import.status_code, 200)

        # Check history endpoint per session
        res_work = self.client.get("/api/chat/history?session_id=session_work")
        self.assertEqual(res_work.status_code, 200)
        self.assertEqual(res_work.get_json()["count"], 2)

        res_personal = self.client.get("/api/chat/history?session_id=session_personal")
        self.assertEqual(res_personal.status_code, 200)
        self.assertEqual(res_personal.get_json()["count"], 1)

        # The temporary session from prior to restore should be gone (cleared on import)
        res_temp = self.client.get("/api/chat/history?session_id=temporary_session")
        self.assertEqual(res_temp.get_json()["count"], 0)

    def test_old_backups_without_chat_messages_still_restore(self):
        """Backward compatibility: Backups from version 1.1.0/1.2.0 without chat_messages restore smoothly."""
        # Create a pre-existing message that should NOT be wiped if chat_messages is absent from payload
        ChatMessageModel.create(session_id="existing_session", role="user", content="Should persist")

        legacy_data = {
            "tasks": [
                {
                    "id": 1,
                    "title": "Legacy Task",
                    "description": "Created in v1.2.0",
                    "priority": "medium",
                    "status": "todo",
                    "tags": "legacy",
                    "due_date": "2026-10-01",
                    "subtasks": "[]",
                    "created_at": "2026-09-01T10:00:00+00:00",
                    "updated_at": "2026-09-01T10:00:00+00:00"
                }
            ],
            "notes": [
                {
                    "id": 1,
                    "title": "Legacy Note",
                    "content": "Notes from v1.2.0",
                    "tags": "legacy",
                    "pinned": 0,
                    "created_at": "2026-09-01T10:00:00+00:00",
                    "updated_at": "2026-09-01T10:00:00+00:00"
                }
            ],
            "memories": [
                {
                    "id": 1,
                    "category": "preference",
                    "content": "User prefers coffee.",
                    "confidence_weight": 0.8,
                    "status": "active",
                    "source_context": "legacy",
                    "superseded_by": None,
                    "update_count": 0,
                    "created_at": "2026-09-01T10:00:00+00:00",
                    "updated_at": "2026-09-01T10:00:00+00:00"
                }
            ],
            "decision_logs": [],
            "feedback_events": [],
            "settings": {"engine": "local"}
        }

        checksum = compute_backup_checksum(legacy_data)
        legacy_payload = {
            "version": "1.2.0",
            "exported_at": "2026-09-01T10:00:00+00:00",
            "application": "Cognito Offline Agent",
            "data": legacy_data,
            "checksum_sha256": checksum
        }

        res = self.client.post(
            "/api/backup/import",
            json=legacy_payload,
            headers=self._auth_headers()
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["sqlite_restored"])
        self.assertNotIn("chat_messages", data["restored"])

        # Legacy task, note, and memory restored
        self.assertIsNotNone(TaskModel.get(1))
        self.assertIsNotNone(NoteModel.get(1))
        self.assertIsNotNone(MemoryModel.get(1))

        # Existing chat message is preserved when payload omits chat_messages
        msgs = ChatMessageModel.list_by_session("existing_session")
        self.assertEqual(len(msgs), 1)

    # =========================================================================
    # 3. Validation & Malformed Chat Messages Tests
    # =========================================================================

    def test_malformed_chat_messages_rejected(self):
        """Strict validation rejects malformed chat message entries with descriptive errors."""
        base_data = {
            "tasks": [],
            "notes": [],
            "memories": [],
            "decision_logs": [],
            "feedback_events": [],
            "settings": {"engine": "local"}
        }

        bad_cases = [
            ("Non-list chat_messages", {"chat_messages": "not-a-list"}, "must be a list"),
            ("Non-dict chat message", {"chat_messages": ["invalid"]}, "must be an object"),
            ("Invalid id type", {"chat_messages": [{"id": "bad", "session_id": "s1", "role": "user", "content": "c"}]}, "positive integer 'id'"),
            ("Negative id", {"chat_messages": [{"id": -1, "session_id": "s1", "role": "user", "content": "c"}]}, "positive integer 'id'"),
            ("Duplicate id", {"chat_messages": [
                {"id": 1, "session_id": "s1", "role": "user", "content": "c1"},
                {"id": 1, "session_id": "s1", "role": "user", "content": "c2"}
            ]}, "Duplicate chat message id"),
            ("Missing session_id", {"chat_messages": [{"id": 1, "session_id": "", "role": "user", "content": "c"}]}, "missing required 'session_id'"),
            ("Oversized session_id", {"chat_messages": [{"id": 1, "session_id": "s" * 65, "role": "user", "content": "c"}]}, "session_id exceeds maximum length"),
            ("Invalid chars in session_id", {"chat_messages": [{"id": 1, "session_id": "bad@session!", "role": "user", "content": "c"}]}, "session_id contains invalid characters"),
            ("Invalid role", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "root", "content": "c"}]}, "invalid role"),
            ("Missing content", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "user", "content": ""}]}, "missing required 'content'"),
            ("Oversized content", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "user", "content": "x" * 5001}]}, "content exceeds maximum length"),
            ("Invalid cited_memories string", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "user", "content": "c", "cited_memories": "{bad:json}"}]}, "not a valid JSON list"),
            ("Non-list cited_memories", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "user", "content": "c", "cited_memories": 12345}]}, "must be a list, valid JSON string, or null"),
            ("Invalid created_at", {"chat_messages": [{"id": 1, "session_id": "s1", "role": "user", "content": "c", "created_at": "not-a-timestamp"}]}, "invalid timestamp format")
        ]

        for desc, patch_data, expected_err in bad_cases:
            with self.subTest(desc=desc):
                test_data = dict(base_data)
                test_data.update(patch_data)
                payload = {
                    "version": "1.3.0",
                    "exported_at": "2026-09-18T10:00:00+00:00",
                    "application": "Cognito Offline Agent",
                    "data": test_data,
                    "checksum_sha256": compute_backup_checksum(test_data)
                }
                res = self.client.post("/api/backup/import", json=payload, headers=self._auth_headers())
                self.assertEqual(res.status_code, 400, f"Case '{desc}' should return 400")
                self.assertIn(expected_err.lower(), res.get_json()["error"].lower())

    # =========================================================================
    # 4. Checksum Integrity & Tampering Tests
    # =========================================================================

    def test_checksum_integrity_with_chat_messages(self):
        """Tampering with any chat message field triggers checksum mismatch and rejection."""
        self._create_sample_chat_messages()
        res_export = self.client.get("/api/backup/export")
        valid_payload = res_export.get_json()

        # 1. Valid payload imports cleanly
        res_ok = self.client.post("/api/backup/import", json=valid_payload, headers=self._auth_headers())
        self.assertEqual(res_ok.status_code, 200)

        # 2. Tamper with message content
        tampered_content = json.loads(json.dumps(valid_payload))
        tampered_content["data"]["chat_messages"][0]["content"] += " [tampered]"
        res_tampered1 = self.client.post("/api/backup/import", json=tampered_content, headers=self._auth_headers())
        self.assertEqual(res_tampered1.status_code, 400)
        self.assertIn("checksum verification failed", res_tampered1.get_json()["error"].lower())

        # 3. Tamper with session_id
        tampered_session = json.loads(json.dumps(valid_payload))
        tampered_session["data"]["chat_messages"][0]["session_id"] = "different_session"
        res_tampered2 = self.client.post("/api/backup/import", json=tampered_session, headers=self._auth_headers())
        self.assertEqual(res_tampered2.status_code, 400)
        self.assertIn("checksum verification failed", res_tampered2.get_json()["error"].lower())

    # =========================================================================
    # 5. Security: Authentication & CSRF
    # =========================================================================

    def test_auth_and_csrf_enforced_on_backup_endpoints(self):
        """Backup export and import strictly require authentication and valid CSRF."""
        unauth_client = self.app.test_client()

        # Unauthenticated export
        res_unauth_export = unauth_client.get("/api/backup/export")
        self.assertEqual(res_unauth_export.status_code, 401)

        # Unauthenticated import
        res_unauth_import = unauth_client.post("/api/backup/import", json={})
        self.assertEqual(res_unauth_import.status_code, 401)

        # Authenticated without CSRF
        res_no_csrf = self.client.post("/api/backup/import", json={})
        self.assertEqual(res_no_csrf.status_code, 403)
        self.assertIn("CSRF", res_no_csrf.get_json()["error"])

        # Authenticated with invalid CSRF
        res_bad_csrf = self.client.post(
            "/api/backup/import",
            json={},
            headers={"X-CSRF-Token": "bad-csrf-token"}
        )
        self.assertEqual(res_bad_csrf.status_code, 403)

    # =========================================================================
    # 6. Atomic Restore Failure & Encrypted Envelope Tests
    # =========================================================================

    def test_restore_failure_remains_atomic(self):
        """Failed import (e.g. malformed or tampered) leaves existing chat messages intact."""
        self._create_sample_chat_messages()
        initial_work_msgs = ChatMessageModel.list_by_session("session_work")
        self.assertEqual(len(initial_work_msgs), 2)

        # Attempt import of tampered payload
        bad_payload = {
            "version": "1.3.0",
            "application": "Cognito Offline Agent",
            "data": {"chat_messages": [{"id": 999, "session_id": "fail", "role": "user", "content": "bad"}]},
            "checksum_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
        }
        res = self.client.post("/api/backup/import", json=bad_payload, headers=self._auth_headers())
        self.assertEqual(res.status_code, 400)

        # Verify existing data was untouched
        after_work_msgs = ChatMessageModel.list_by_session("session_work")
        self.assertEqual(len(after_work_msgs), 2)
        self.assertEqual(after_work_msgs[0]["content"], initial_work_msgs[0]["content"])

    def test_encrypted_backup_with_chat_messages_roundtrip(self):
        """Encrypted export (AES-256-GCM) with chat messages decrypts and restores faithfully."""
        self._create_sample_chat_messages()

        # Export encrypted backup
        passphrase = "super-secret-backup-pass"
        res_export = self.client.post(
            "/api/backup/export",
            json={"passphrase": passphrase},
            headers=self._auth_headers()
        )
        self.assertEqual(res_export.status_code, 200)
        envelope = res_export.get_json()
        self.assertEqual(envelope["format"], "cognito_backup_encrypted")

        # Clear database
        ChatMessageModel.clear_history()
        self.assertEqual(len(ChatMessageModel.list_by_session("session_work")), 0)

        # Import encrypted backup
        res_import = self.client.post(
            "/api/backup/import",
            json={"backup": envelope, "passphrase": passphrase},
            headers=self._auth_headers()
        )
        self.assertEqual(res_import.status_code, 200)
        data = res_import.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["is_encrypted"])
        self.assertEqual(data["restored"]["chat_messages"], 4)

        # Verify restored session content
        msgs = ChatMessageModel.list_by_session("session_work")
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["content"], "Plan deep work session before noon.")

if __name__ == "__main__":
    unittest.main()
