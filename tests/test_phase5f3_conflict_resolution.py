import os
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.vector_store import VectorStore
from app.config import BASE_CONFIDENCE_WEIGHT
import app.routes.api_memory as api_memory_module
import app.routes.api_notes as api_notes_module
import app.routes.api_tasks as api_tasks_module
import app.routes.api_chat as api_chat_module


class TestPhase5F3ConflictResolution(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        self.test_db_path = self.test_dir / "test_5f3.db"
        self.test_vec_path = self.test_dir / "test_vec_5f3.json"
        self.test_auth_path = self.test_dir / "test_auth_5f3.json"

        self.p_db = patch("app.config.DB_PATH", self.test_db_path)
        self.p_vec = patch("app.config.VECTOR_STORE_PATH", self.test_vec_path)
        self.p_auth = patch("app.auth.AUTH_FILE_PATH", self.test_auth_path)

        self.p_db.start()
        self.p_vec.start()
        self.p_auth.start()

        from app.database import init_db
        init_db()

        from app import vector_store
        self.vs = VectorStore(storage_path=self.test_vec_path)
        vector_store.global_vector_store = self.vs

        # Patch route-level vector stores
        self.p_route_mem = patch.object(api_memory_module, "global_vector_store", self.vs)
        self.p_route_notes = patch.object(api_notes_module, "global_vector_store", self.vs)
        self.p_route_tasks = patch.object(api_tasks_module, "global_vector_store", self.vs)
        self.p_route_chat = patch.object(api_chat_module, "global_vector_store", self.vs)

        self.p_route_mem.start()
        self.p_route_notes.start()
        self.p_route_tasks.start()
        self.p_route_chat.start()

        from app import create_app
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Perform auth setup to create authenticated session
        setup_res = self.client.post('/api/auth/setup', json={
            'password': 'TestPassword123!',
            'confirm_password': 'TestPassword123!'
        })
        self.assertIn(setup_res.status_code, (200, 201))
        self.csrf_token = setup_res.get_json().get("csrf_token")
        self.headers = {
            "Content-Type": "application/json",
            "X-CSRF-Token": self.csrf_token
        }

    def tearDown(self):
        self.p_route_chat.stop()
        self.p_route_tasks.stop()
        self.p_route_notes.stop()
        self.p_route_mem.stop()

        self.p_auth.stop()
        self.p_vec.stop()
        self.p_db.stop()

        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _create_conflict(self, old_content="User works until 18:00", new_content="User works until 20:00", sim=0.48):
        """Helper to create a realistic candidate and conflicting memory pair."""
        from app.database import get_db, utc_now_iso
        now = utc_now_iso()
        with get_db() as conn:
            cur = conn.cursor()
            # 1. Existing memory
            cur.execute("""
                INSERT INTO memories (category, content, confidence_weight, status, source_context, created_at, updated_at)
                VALUES ('habit', ?, ?, 'active', 'User direct input', ?, ?)
            """, (old_content, BASE_CONFIDENCE_WEIGHT, now, now))
            old_id = cur.lastrowid

            # 2. Flagged candidate memory (penalized confidence 0.35)
            cur.execute("""
                INSERT INTO memories (category, content, confidence_weight, status, source_context, created_at, updated_at)
                VALUES ('habit', ?, 0.35, 'active', 'Copilot extraction', ?, ?)
            """, (new_content, now, now))
            new_id = cur.lastrowid

            # 3. Decision log for FLAG_FOR_REVIEW
            reasoning = f"Flagged for review due to conflict with active memory #{old_id}. Match similarity: {sim:.2f}"
            cur.execute("""
                INSERT INTO memory_decision_logs (memory_id, action, reasoning, previous_content, new_content, triggered_by, timestamp)
                VALUES (?, 'FLAG_FOR_REVIEW', ?, ?, ?, 'system', ?)
            """, (new_id, reasoning, old_content, new_content, now))

        # Index both in vector store
        self.vs.index_document(
            doc_id=old_id,
            doc_type="memory",
            text=old_content,
            metadata={"category": "habit", "confidence_weight": BASE_CONFIDENCE_WEIGHT}
        )
        self.vs.index_document(
            doc_id=new_id,
            doc_type="memory",
            text=new_content,
            metadata={"category": "habit", "confidence_weight": 0.35, "flagged": True}
        )

        return old_id, new_id

    # =========================================================================
    # 1. CONFLICT LISTING TESTS
    # =========================================================================

    def test_list_conflicts_empty(self):
        """GET /api/memory/conflicts returns empty list when no conflicts exist."""
        res = self.client.get("/api/memory/conflicts")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["conflicts"], [])

    def test_list_conflicts_one_conflict(self):
        """GET /api/memory/conflicts returns candidate, conflicting, and provenance details."""
        old_id, new_id = self._create_conflict()
        res = self.client.get("/api/memory/conflicts")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["conflicts"]), 1)

        c = data["conflicts"][0]
        self.assertEqual(c["id"], new_id)
        self.assertEqual(c["candidate_memory"]["id"], new_id)
        self.assertEqual(c["candidate_memory"]["content"], "User works until 20:00")
        self.assertAlmostEqual(c["candidate_memory"]["confidence_weight"], 0.35)

        self.assertEqual(c["conflicting_memory"]["id"], old_id)
        self.assertEqual(c["conflicting_memory"]["content"], "User works until 18:00")

        self.assertEqual(c["conflict_provenance"]["conflicting_memory_id"], old_id)
        self.assertAlmostEqual(c["conflict_provenance"]["similarity_score"], 0.48)
        self.assertIn(f"active memory #{old_id}", c["conflict_provenance"]["reasoning"])

    def test_list_conflicts_multiple_conflicts(self):
        """GET /api/memory/conflicts returns all active unresolved conflicts."""
        self._create_conflict("Rule A1", "Rule A2")
        self._create_conflict("Rule B1", "Rule B2")
        res = self.client.get("/api/memory/conflicts")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["conflicts"]), 2)

    def test_list_conflicts_resolved_conflicts_excluded(self):
        """Once resolved, conflicts no longer appear in GET /api/memory/conflicts."""
        old_id, new_id = self._create_conflict()

        # Resolve via keep_new
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 200)

        # Listing should now be empty
        res = self.client.get("/api/memory/conflicts")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["conflicts"]), 0)

    def test_list_conflicts_requires_authentication(self):
        """GET /api/memory/conflicts requires an authenticated session."""
        anon = self.app.test_client()
        res = anon.get("/api/memory/conflicts")
        self.assertEqual(res.status_code, 401)

    # =========================================================================
    # 2. CONFLICT DETAIL TESTS
    # =========================================================================

    def test_get_conflict_detail_valid(self):
        """GET /api/memory/conflicts/<id> returns 200 with full conflict information."""
        old_id, new_id = self._create_conflict()
        res = self.client.get(f"/api/memory/conflicts/{new_id}")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["conflict"]["id"], new_id)
        self.assertEqual(data["conflict"]["candidate_memory"]["id"], new_id)
        self.assertEqual(data["conflict"]["conflicting_memory"]["id"], old_id)

    def test_get_conflict_detail_nonexistent(self):
        """GET /api/memory/conflicts/99999 returns 404."""
        res = self.client.get("/api/memory/conflicts/99999")
        self.assertEqual(res.status_code, 404)
        data = res.get_json()
        self.assertFalse(data["success"])

    def test_get_conflict_detail_already_resolved(self):
        """GET /api/memory/conflicts/<id> returns 404 if the conflict was already resolved."""
        old_id, new_id = self._create_conflict()
        self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_both"},
            headers=self.headers
        )
        res = self.client.get(f"/api/memory/conflicts/{new_id}")
        self.assertEqual(res.status_code, 404)

    def test_get_conflict_detail_unflagged_memory(self):
        """GET /api/memory/conflicts/<id> returns 404 for an ordinary, unflagged memory."""
        from app.models import MemoryModel
        m_id = MemoryModel.create(category="preference", content="Unflagged rule")
        res = self.client.get(f"/api/memory/conflicts/{m_id}")
        self.assertEqual(res.status_code, 404)

    def test_get_conflict_detail_requires_authentication(self):
        """GET /api/memory/conflicts/<id> requires authentication."""
        old_id, new_id = self._create_conflict()
        anon = self.app.test_client()
        res = anon.get(f"/api/memory/conflicts/{new_id}")
        self.assertEqual(res.status_code, 401)

    # =========================================================================
    # 3. RESOLUTION ACTION TESTS
    # =========================================================================

    def test_resolve_keep_new(self):
        """
        keep_new resolution:
        - Candidate memory remains active
        - Candidate confidence is restored to BASE_CONFIDENCE_WEIGHT
        - Conflicting memory status becomes 'superseded' with superseded_by = candidate_id
        - Candidate is no longer marked is_flagged
        - Vector store: old memory removed, candidate re-indexed with flagged: False
        - Audit log: action = 'SUPERSEDE', triggered_by = 'conflict_resolution'
        """
        from app.models import MemoryModel, MemoryDecisionLogModel
        old_id, new_id = self._create_conflict("Morning routine at 7am", "Morning routine at 6am")

        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action"], "keep_new")
        self.assertTrue(data["vector_synced"])

        # Check SQLite candidate memory
        candidate = MemoryModel.get(new_id)
        self.assertEqual(candidate["status"], "active")
        self.assertGreaterEqual(candidate["confidence_weight"], BASE_CONFIDENCE_WEIGHT)

        # Check SQLite conflicting memory
        conflicting = MemoryModel.get(old_id)
        self.assertEqual(conflicting["status"], "superseded")
        self.assertEqual(conflicting["superseded_by"], new_id)

        # Check dynamic is_flagged calculation
        active_memories = MemoryModel.list_active()
        cand_active = next((m for m in active_memories if m["id"] == new_id), None)
        self.assertIsNotNone(cand_active)
        self.assertEqual(cand_active["is_flagged"], 0)

        # Check decision log
        logs = MemoryDecisionLogModel.get_for_memory(new_id)
        resolution_log = next((l for l in logs if l["triggered_by"] == "conflict_resolution"), None)
        self.assertIsNotNone(resolution_log)
        self.assertEqual(resolution_log["action"], "SUPERSEDE")
        self.assertEqual(resolution_log["new_content"], "Morning routine at 6am")
        self.assertEqual(resolution_log["previous_content"], "Morning routine at 7am")

        # Check vector store
        old_doc = self.vs.documents.get(f"memory_{old_id}")
        self.assertIsNone(old_doc)
        new_doc = self.vs.documents.get(f"memory_{new_id}")
        self.assertIsNotNone(new_doc)
        self.assertFalse(new_doc["metadata"].get("flagged", False))
        self.assertAlmostEqual(new_doc["metadata"]["confidence_weight"], candidate["confidence_weight"])

    def test_resolve_keep_old(self):
        """
        keep_old resolution:
        - Conflicting memory remains active and unchanged
        - Candidate memory becomes deleted
        - Vector store: candidate removed, old remains
        - Audit log: action = 'DELETE', triggered_by = 'conflict_resolution'
        """
        from app.models import MemoryModel, MemoryDecisionLogModel
        old_id, new_id = self._create_conflict("Work in dark room", "Work in bright room")

        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_old"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action"], "keep_old")

        # Check SQLite candidate memory
        candidate = MemoryModel.get(new_id)
        self.assertEqual(candidate["status"], "deleted")

        # Check SQLite conflicting memory
        conflicting = MemoryModel.get(old_id)
        self.assertEqual(conflicting["status"], "active")
        self.assertIsNone(conflicting["superseded_by"])

        # Check decision log
        logs = MemoryDecisionLogModel.get_for_memory(new_id)
        resolution_log = next((l for l in logs if l["triggered_by"] == "conflict_resolution"), None)
        self.assertIsNotNone(resolution_log)
        self.assertEqual(resolution_log["action"], "DELETE")
        self.assertEqual(resolution_log["previous_content"], "Work in bright room")
        self.assertIsNone(resolution_log["new_content"])

        # Check vector store
        cand_doc = self.vs.documents.get(f"memory_{new_id}")
        self.assertIsNone(cand_doc)
        old_doc = self.vs.documents.get(f"memory_{old_id}")
        self.assertIsNotNone(old_doc)

    def test_resolve_keep_both(self):
        """
        keep_both resolution:
        - Both candidate and old memory remain active
        - Candidate confidence is restored to BASE_CONFIDENCE_WEIGHT
        - Candidate is no longer marked is_flagged
        - Neither memory is superseded
        - Vector store: candidate re-indexed with flagged: False; old remains indexed
        - Audit log: action = 'UPDATE', triggered_by = 'conflict_resolution'
        """
        from app.models import MemoryModel, MemoryDecisionLogModel
        old_id, new_id = self._create_conflict("Prefers tea in afternoon", "Prefers coffee in morning")

        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_both"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action"], "keep_both")

        # Check SQLite
        candidate = MemoryModel.get(new_id)
        self.assertEqual(candidate["status"], "active")
        self.assertGreaterEqual(candidate["confidence_weight"], BASE_CONFIDENCE_WEIGHT)

        conflicting = MemoryModel.get(old_id)
        self.assertEqual(conflicting["status"], "active")
        self.assertIsNone(conflicting["superseded_by"])

        # Check dynamic is_flagged
        active_memories = MemoryModel.list_active()
        cand_active = next((m for m in active_memories if m["id"] == new_id), None)
        self.assertIsNotNone(cand_active)
        self.assertEqual(cand_active["is_flagged"], 0)

        # Check decision log
        logs = MemoryDecisionLogModel.get_for_memory(new_id)
        resolution_log = next((l for l in logs if l["triggered_by"] == "conflict_resolution"), None)
        self.assertIsNotNone(resolution_log)
        self.assertEqual(resolution_log["action"], "UPDATE")

        # Check vector store
        cand_doc = self.vs.documents.get(f"memory_{new_id}")
        self.assertIsNotNone(cand_doc)
        self.assertFalse(cand_doc["metadata"].get("flagged", False))
        old_doc = self.vs.documents.get(f"memory_{old_id}")
        self.assertIsNotNone(old_doc)

    # =========================================================================
    # 4. VALIDATION TESTS
    # =========================================================================

    def test_resolve_missing_action(self):
        """Resolving without 'action' field returns 400 Bad Request."""
        old_id, new_id = self._create_conflict()
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("action is required", res.get_json()["error"])

    def test_resolve_invalid_action(self):
        """Resolving with unsupported action returns 400 Bad Request."""
        old_id, new_id = self._create_conflict()
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "discard_all"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Invalid resolution action", res.get_json()["error"])

    def test_resolve_malformed_json(self):
        """Sending non-JSON or malformed payload returns 400."""
        old_id, new_id = self._create_conflict()
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            data="not a json",
            headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf_token}
        )
        self.assertEqual(res.status_code, 400)

    def test_resolve_wrong_data_types(self):
        """Action as non-string (int/bool/list) returns 400."""
        old_id, new_id = self._create_conflict()
        for bad_action in [123, True, ["keep_new"], None]:
            res = self.client.post(
                f"/api/memory/conflicts/{new_id}/resolve",
                json={"action": bad_action},
                headers=self.headers
            )
            self.assertEqual(res.status_code, 400)

    def test_resolve_nonexistent_id(self):
        """Resolving nonexistent memory ID returns 404."""
        res = self.client.post(
            "/api/memory/conflicts/99999/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 404)
        self.assertIn("Memory not found", res.get_json()["error"])

    def test_resolve_already_resolved_conflict(self):
        """Attempting to resolve a conflict twice returns 400."""
        old_id, new_id = self._create_conflict()
        # First resolution succeeds
        res1 = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res1.status_code, 200)

        # Second resolution is rejected
        res2 = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res2.status_code, 400)
        self.assertIn("not in an unresolved conflict state", res2.get_json()["error"])

    def test_resolve_unflagged_memory(self):
        """Attempting to resolve an unflagged memory returns 400."""
        from app.models import MemoryModel
        m_id = MemoryModel.create(category="preference", content="Standard active rule")
        res = self.client.post(
            f"/api/memory/conflicts/{m_id}/resolve",
            json={"action": "keep_new"},
            headers=self.headers
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("not in an unresolved conflict state", res.get_json()["error"])

    # =========================================================================
    # 5. SECURITY TESTS
    # =========================================================================

    def test_resolve_unauthenticated_request(self):
        """POST /api/memory/conflicts/<id>/resolve requires authentication."""
        old_id, new_id = self._create_conflict()
        anon = self.app.test_client()
        res = anon.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"}
        )
        self.assertEqual(res.status_code, 401)

    def test_resolve_missing_csrf(self):
        """POST /api/memory/conflicts/<id>/resolve without CSRF header returns 403."""
        old_id, new_id = self._create_conflict()
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"}
            # Notice: no X-CSRF-Token header
        )
        self.assertEqual(res.status_code, 403)

    def test_resolve_invalid_csrf(self):
        """POST /api/memory/conflicts/<id>/resolve with invalid CSRF token returns 403."""
        old_id, new_id = self._create_conflict()
        res = self.client.post(
            f"/api/memory/conflicts/{new_id}/resolve",
            json={"action": "keep_new"},
            headers={"Content-Type": "application/json", "X-CSRF-Token": "invalid_token_xyz"}
        )
        self.assertEqual(res.status_code, 403)

    # =========================================================================
    # 6. TRANSACTIONAL INTEGRITY & VECTOR STORE FAULT TOLERANCE
    # =========================================================================

    def test_vector_sync_failure_does_not_corrupt_sqlite(self):
        """
        When vector store raises an exception during resolution:
        - SQLite transaction remains committed and authoritative
        - API returns 200 with vector_synced: False
        """
        from app.models import MemoryModel
        old_id, new_id = self._create_conflict("Read emails at 9am", "Read emails at 11am")

        # Mock vector store to throw an error during index_document
        with patch.object(self.vs, "index_document", side_effect=RuntimeError("Simulated disk full")):
            res = self.client.post(
                f"/api/memory/conflicts/{new_id}/resolve",
                json={"action": "keep_new"},
                headers=self.headers
            )
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])
            self.assertFalse(data["vector_synced"])

        # Confirm SQLite state was updated correctly
        cand = MemoryModel.get(new_id)
        self.assertEqual(cand["status"], "active")
        self.assertGreaterEqual(cand["confidence_weight"], BASE_CONFIDENCE_WEIGHT)
        conf = MemoryModel.get(old_id)
        self.assertEqual(conf["status"], "superseded")

    def test_sqlite_rollback_on_database_error(self):
        """
        If a database error occurs during the SQLite resolution transaction:
        - The transaction rolls back cleanly
        - No partial changes or orphaned decision logs remain
        """
        from app.models import MemoryModel
        old_id, new_id = self._create_conflict("Walk at 6pm", "Walk at 7pm")

        # Simulate DB failure during execute by patching cursor
        from app.database import get_db
        with patch("app.models.utc_now_iso", side_effect=Exception("Database lock error")):
            with self.assertRaises(Exception):
                MemoryModel.resolve_conflict(new_id, "keep_new", vector_store=self.vs)

        # Candidate should remain active with original penalized confidence
        cand = MemoryModel.get(new_id)
        self.assertEqual(cand["confidence_weight"], 0.35)
        self.assertEqual(cand["status"], "active")

        # Old memory should remain active
        old = MemoryModel.get(old_id)
        self.assertEqual(old["status"], "active")
        self.assertIsNone(old["superseded_by"])


if __name__ == "__main__":
    unittest.main()
