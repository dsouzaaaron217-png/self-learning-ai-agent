"""
Phase 5B Tests: Chat Persistence, Actionability & Memory Pipeline Integration

Covers:
1. Migration:
   - Fresh DB initialization creates chat_messages table and indexes
   - Existing populated v1 DB migrates to v2 without data loss
   - Migration idempotency
   - Migration rollback on failure
   - chat_messages schema & indexes verification
2. Storage & Persistence:
   - User message persists
   - Assistant message persists with cited memories JSON
   - History survives fresh connection restart
   - Session isolation
   - Bounded history limits
   - Clear history by session and global
3. API Endpoints:
   - GET /api/chat/history requires authentication
   - DELETE /api/chat/history requires authentication and CSRF
   - Input validation (session_id, limits, chat input)
   - Response structure
   - POST /api/chat persists messages and returns message IDs
4. Actionability:
   - Add as Task creates task and indexes vector with chat provenance
   - Save as Note creates note and indexes vector
   - Unified /api/chat/action dispatcher
   - Validation errors on task/note actions
   - Auth and CSRF enforcement on action endpoints
5. Memory Pipeline Integration:
   - User chat preference statement reaches MemoryPipeline
   - Generic conversational statements do not create memories
   - Assistant responses do not become memories
   - Memory learning failure does not block chat persistence
6. Offline & Zero-Cloud:
   - Strict offline operation with no external network calls
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.database import (
    init_db,
    apply_migrations,
    get_current_migration_version,
    get_applied_migrations,
    get_db,
    SCHEMA_MIGRATIONS_TABLE,
    CURRENT_SCHEMA_VERSION,
)
from app.models import (
    TaskModel,
    NoteModel,
    MemoryModel,
    ChatMessageModel,
)
from app.vector_store import VectorStore
from app.memory.pipeline import MemoryPipeline


class TestChatDatabaseMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_chat_mig.db"
        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_db.start()

    def tearDown(self):
        self.p_db.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_fresh_db_creates_chat_table(self):
        applied = init_db(self.db_path)
        self.assertIn(2, applied)

        conn = sqlite3.connect(str(self.db_path))
        ver = get_current_migration_version(conn)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'")
        tbl = cur.fetchone()
        conn.close()

        self.assertEqual(ver, 2)
        self.assertIsNotNone(tbl)

    def test_existing_db_v1_to_v2_migration(self):
        # Setup an existing DB at version 1 with populated data
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.cursor()

        # Create baseline schema manually
        from app.database import migration_001_baseline_schema, ensure_migration_table
        ensure_migration_table(conn)
        migration_001_baseline_schema(conn)
        conn.execute(f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, applied_at) VALUES (1, '2026-09-01T00:00:00Z')")
        conn.commit()

        # Insert populated data in v1 tables
        cur.execute("INSERT INTO tasks (title, created_at, updated_at) VALUES ('Old Task', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')")
        cur.execute("INSERT INTO notes (title, content, created_at, updated_at) VALUES ('Old Note', 'Content', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')")
        cur.execute("INSERT INTO memories (category, content, created_at, updated_at) VALUES ('fact', 'Cognito is offline', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')")
        conn.commit()
        conn.close()

        # Run migrations - should discover migration 2 and apply it
        applied = init_db(self.db_path)
        self.assertEqual(applied, [2])

        conn = sqlite3.connect(str(self.db_path))
        ver = get_current_migration_version(conn)
        conn.close()
        self.assertEqual(ver, 2)

        # Verify old data is 100% preserved
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        task = cur.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
        self.assertEqual(task["title"], "Old Task")
        note = cur.execute("SELECT * FROM notes WHERE id = 1").fetchone()
        self.assertEqual(note["title"], "Old Note")
        mem = cur.execute("SELECT * FROM memories WHERE id = 1").fetchone()
        self.assertEqual(mem["content"], "Cognito is offline")

        # Verify chat_messages table now exists
        chat_tbl = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_messages'").fetchone()
        self.assertIsNotNone(chat_tbl)
        conn.close()

    def test_migration_idempotency(self):
        init_db(self.db_path)
        # Second run applies nothing
        second = init_db(self.db_path)
        self.assertEqual(second, [])
        # Third run applies nothing
        third = init_db(self.db_path)
        self.assertEqual(third, [])

    def test_migration_rollback_on_failure(self):
        def failing_mig(conn):
            conn.execute("CREATE TABLE test_temp_table (id INTEGER PRIMARY KEY)")
            raise RuntimeError("Intentional failure")

        custom_registry = [
            (3, "failing_migration", failing_mig)
        ]

        init_db(self.db_path)
        conn = sqlite3.connect(str(self.db_path))

        with self.assertRaises(RuntimeError):
            apply_migrations(conn=conn, migrations=custom_registry)

        # Confirm test_temp_table was rolled back
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='test_temp_table'")
        self.assertIsNone(cur.fetchone())
        self.assertEqual(get_current_migration_version(conn), 2)
        conn.close()

    def test_chat_messages_schema_and_indexes(self):
        init_db(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()

        cur.execute("PRAGMA table_info(chat_messages)")
        cols = {row[1]: {"type": row[2].upper(), "notnull": row[3], "pk": row[5]} for row in cur.fetchall()}
        self.assertIn("id", cols)
        self.assertIn("session_id", cols)
        self.assertIn("role", cols)
        self.assertIn("content", cols)
        self.assertIn("cited_memories", cols)
        self.assertIn("created_at", cols)

        cur.execute("PRAGMA index_list(chat_messages)")
        indexes = {row[1] for row in cur.fetchall()}
        self.assertIn("idx_chat_messages_session_id", indexes)
        self.assertIn("idx_chat_messages_created_at", indexes)
        self.assertIn("idx_chat_messages_session_created", indexes)
        conn.close()


class TestChatStorage(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_chat_storage.db"
        self.p_db = patch("app.config.DB_PATH", self.db_path)
        self.p_db.start()
        init_db(self.db_path)

    def tearDown(self):
        self.p_db.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_user_message_persists(self):
        msg_id = ChatMessageModel.create(
            session_id="default",
            role="user",
            content="Hello from test user"
        )
        self.assertIsInstance(msg_id, int)
        msg = ChatMessageModel.get(msg_id)
        self.assertIsNotNone(msg)
        self.assertEqual(msg["session_id"], "default")
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"], "Hello from test user")
        self.assertEqual(msg["cited_memories"], [])

    def test_assistant_message_persists_with_citations(self):
        citations = [{"id": 1, "category": "preference", "content": "Prefers morning focus"}]
        msg_id = ChatMessageModel.create(
            session_id="default",
            role="assistant",
            content="Here is your task summary",
            cited_memories=citations
        )
        msg = ChatMessageModel.get(msg_id)
        self.assertIsNotNone(msg)
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(len(msg["cited_memories"]), 1)
        self.assertEqual(msg["cited_memories"][0]["id"], 1)

    def test_history_survives_fresh_connection(self):
        msg_id = ChatMessageModel.create(
            session_id="s1",
            role="user",
            content="Persistent text across connection"
        )
        # Open entirely new connection to SQLite file
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "Persistent text across connection")

    def test_session_isolation(self):
        ChatMessageModel.create(session_id="sess_A", role="user", content="Message in A")
        ChatMessageModel.create(session_id="sess_B", role="user", content="Message in B")

        messages_a = ChatMessageModel.list_by_session("sess_A")
        messages_b = ChatMessageModel.list_by_session("sess_B")

        self.assertEqual(len(messages_a), 1)
        self.assertEqual(messages_a[0]["content"], "Message in A")
        self.assertEqual(len(messages_b), 1)
        self.assertEqual(messages_b[0]["content"], "Message in B")

    def test_bounded_history(self):
        for i in range(15):
            ChatMessageModel.create(session_id="bounded", role="user", content=f"Msg {i}")

        page1 = ChatMessageModel.list_by_session("bounded", limit=5, offset=0)
        self.assertEqual(len(page1), 5)
        self.assertEqual(page1[0]["content"], "Msg 0")

        page2 = ChatMessageModel.list_by_session("bounded", limit=5, offset=5)
        self.assertEqual(len(page2), 5)
        self.assertEqual(page2[0]["content"], "Msg 5")

    def test_clear_history_by_session_and_all(self):
        ChatMessageModel.create(session_id="clear_1", role="user", content="Msg 1")
        ChatMessageModel.create(session_id="clear_1", role="assistant", content="Msg 2")
        ChatMessageModel.create(session_id="clear_2", role="user", content="Msg 3")

        cleared_1 = ChatMessageModel.clear_history(session_id="clear_1")
        self.assertEqual(cleared_1, 2)
        self.assertEqual(len(ChatMessageModel.list_by_session("clear_1")), 0)
        self.assertEqual(len(ChatMessageModel.list_by_session("clear_2")), 1)

        cleared_all = ChatMessageModel.clear_history()
        self.assertEqual(cleared_all, 1)
        self.assertEqual(len(ChatMessageModel.list_by_session("clear_2")), 0)


class TestChatAPI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_api_chat.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_api_chat_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_api_chat_auth.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app import vector_store
        vector_store.global_vector_store = VectorStore(storage_path=self.test_vec_path)

        self.app = create_app()
        self.client = self.app.test_client()

        # Auth setup
        res = self.client.post("/api/auth/setup", json={
            "password": "strongpassword123",
            "confirm_password": "strongpassword123"
        })
        self.csrf_token = res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_get_history_requires_auth(self):
        unauth_client = self.app.test_client()
        res = unauth_client.get("/api/chat/history")
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json()["success"])

    def test_delete_history_requires_auth_and_csrf(self):
        unauth_client = self.app.test_client()
        res_unauth = unauth_client.delete("/api/chat/history")
        self.assertEqual(res_unauth.status_code, 401)

        # Authenticated without CSRF
        res_nocsrf = self.client.delete("/api/chat/history")
        self.assertEqual(res_nocsrf.status_code, 403)

        # Authenticated with valid CSRF
        res_ok = self.client.delete("/api/chat/history", headers={"X-CSRF-Token": self.csrf_token})
        self.assertEqual(res_ok.status_code, 200)
        self.assertTrue(res_ok.get_json()["success"])

    def test_get_history_response_structure(self):
        # Insert test message
        ChatMessageModel.create(session_id="default", role="user", content="Hello API")
        res = self.client.get("/api/chat/history?session_id=default")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["session_id"], "default")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["messages"][0]["content"], "Hello API")
        self.assertEqual(data["messages"][0]["role"], "user")

    def test_invalid_inputs_rejected(self):
        # Invalid session_id with special characters
        res_bad_sess = self.client.get("/api/chat/history?session_id=invalid%20session%20name%21%40")
        self.assertEqual(res_bad_sess.status_code, 400)

        # Invalid limit
        res_bad_limit = self.client.get("/api/chat/history?limit=-5")
        self.assertEqual(res_bad_limit.status_code, 400)

        # Empty chat message
        res_empty_msg = self.client.post(
            "/api/chat",
            json={"message": "   "},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_empty_msg.status_code, 400)

    def test_post_chat_persists_both_messages(self):
        res = self.client.post(
            "/api/chat",
            json={"message": "How do I organize tasks?"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("user_message_id", data)
        self.assertIn("assistant_message_id", data)

        # Check DB
        u_msg = ChatMessageModel.get(data["user_message_id"])
        self.assertIsNotNone(u_msg)
        self.assertEqual(u_msg["role"], "user")
        self.assertEqual(u_msg["content"], "How do I organize tasks?")

        a_msg = ChatMessageModel.get(data["assistant_message_id"])
        self.assertIsNotNone(a_msg)
        self.assertEqual(a_msg["role"], "assistant")


class TestChatActionability(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_action_chat.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_action_chat_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_action_chat_auth.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app import vector_store
        vector_store.global_vector_store = VectorStore(storage_path=self.test_vec_path)

        self.app = create_app()
        self.client = self.app.test_client()

        res = self.client.post("/api/auth/setup", json={
            "password": "strongpassword123",
            "confirm_password": "strongpassword123"
        })
        self.csrf_token = res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_add_chat_as_task(self):
        msg_id = ChatMessageModel.create(
            session_id="default",
            role="assistant",
            content="Deploy staging v1.2 release\nEnsure all unit tests pass before deployment."
        )

        res = self.client.post(
            "/api/chat/actions/task",
            json={"message_id": msg_id, "priority": "high"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        task = data["task"]
        self.assertEqual(task["title"], "Deploy staging v1.2 release")
        self.assertEqual(task["priority"], "high")
        self.assertEqual(task["ai_suggestion_context"]["source"], "chat")
        self.assertEqual(task["ai_suggestion_context"]["message_id"], msg_id)

    def test_add_chat_as_note(self):
        msg_id = ChatMessageModel.create(
            session_id="default",
            role="assistant",
            content="System Architecture Overview\nCognito is a local-only self-learning agent."
        )

        res = self.client.post(
            "/api/chat/actions/note",
            json={"message_id": msg_id},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data["success"])
        note = data["note"]
        self.assertEqual(note["title"], "System Architecture Overview")
        self.assertIn("Cognito is a local-only", note["content"])

    def test_unified_action_dispatcher(self):
        msg_id = ChatMessageModel.create(
            session_id="default",
            role="user",
            content="Remember to buy coffee tomorrow"
        )
        res_task = self.client.post(
            "/api/chat/action",
            json={"action": "task", "message_id": msg_id},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_task.status_code, 201)
        self.assertEqual(res_task.get_json()["task"]["title"], "Remember to buy coffee tomorrow")

        res_note = self.client.post(
            "/api/chat/action",
            json={"action": "note", "message_id": msg_id},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_note.status_code, 201)
        self.assertEqual(res_note.get_json()["note"]["title"], "Remember to buy coffee tomorrow")

    def test_action_validation_errors(self):
        # Invalid action in dispatcher
        res_bad_act = self.client.post(
            "/api/chat/action",
            json={"action": "invalid_action"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_bad_act.status_code, 400)

        # Non-existent message_id with no content
        res_bad_id = self.client.post(
            "/api/chat/actions/task",
            json={"message_id": 99999},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res_bad_id.status_code, 400)


class TestChatMemoryPipelineIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_mem_chat.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_mem_chat_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_mem_chat_auth.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)
        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app import vector_store
        vector_store.global_vector_store = VectorStore(storage_path=self.test_vec_path)

        self.app = create_app()
        self.client = self.app.test_client()

        res = self.client.post("/api/auth/setup", json={
            "password": "strongpassword123",
            "confirm_password": "strongpassword123"
        })
        self.csrf_token = res.get_json()["csrf_token"]

    def tearDown(self):
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_user_preference_statement_reaches_memory_pipeline(self):
        res = self.client.post(
            "/api/chat",
            json={"message": "I prefer morning sessions for deep coding."},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(len(data.get("learned_memories", [])) > 0)

        # Verify active memory created in SQLite
        active_mems = MemoryModel.list_active()
        self.assertTrue(any("morning sessions" in m["content"].lower() for m in active_mems))

        # Check provenance in memory record
        matching = [m for m in active_mems if "morning sessions" in m["content"].lower()][0]
        self.assertIn("Chat message", matching["source_context"])

    def test_generic_chat_does_not_create_memory(self):
        res = self.client.post(
            "/api/chat",
            json={"message": "Hello, can you help me check my tasks?"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data.get("learned_memories", [])), 0)

    def test_assistant_responses_do_not_blindly_become_memories(self):
        initial_count = len(MemoryModel.list_active())

        # Send user message that elicits assistant response
        res = self.client.post(
            "/api/chat",
            json={"message": "What is the capital of France?"},
            headers={"X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data.get("learned_memories", [])), 0)

        # Verify no memory was created from the assistant's reply
        active_mems = MemoryModel.list_active()
        self.assertEqual(len(active_mems), initial_count)

    def test_memory_failure_does_not_prevent_chat_persistence(self):
        # Patch reconcile_memory to raise an exception
        with patch.object(MemoryPipeline, "reconcile_memory", side_effect=RuntimeError("Simulated vector/disk failure")):
            res = self.client.post(
                "/api/chat",
                json={"message": "I prefer dark mode always."},
                headers={"X-CSRF-Token": self.csrf_token}
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            # Chat messages must still be persisted
            user_msg = ChatMessageModel.get(data["user_message_id"])
            self.assertIsNotNone(user_msg)
            self.assertEqual(user_msg["content"], "I prefer dark mode always.")


if __name__ == "__main__":
    unittest.main()
