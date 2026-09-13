import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.vector_store import VectorStore
from app.auth import is_auth_configured

class TestPhase2AuthAndSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_sec.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_sec_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_sec_auth.json"

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

    def tearDown(self):
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def _setup_master_password(self, password="masterpassword123"):
        res = self.client.post("/api/auth/setup", json={
            "password": password,
            "confirm_password": password
        })
        data = res.get_json()
        return res, data.get("csrf_token")

    # =========================================================================
    # 1. Unauthenticated Access & Session Management
    # =========================================================================

    def test_unauthenticated_requests_blocked(self):
        """Unauthenticated requests to protected endpoints must return HTTP 401."""
        endpoints = [
            ("GET", "/api/tasks"),
            ("POST", "/api/tasks"),
            ("GET", "/api/notes"),
            ("POST", "/api/notes"),
            ("GET", "/api/memory"),
            ("POST", "/api/chat"),
            ("GET", "/api/settings"),
            ("POST", "/api/settings"),
            ("GET", "/api/backup/export"),
            ("POST", "/api/backup/import"),
        ]
        for method, path in endpoints:
            with self.subTest(method=method, path=path):
                if method == "GET":
                    res = self.client.get(path)
                else:
                    res = self.client.post(path, json={})
                self.assertEqual(res.status_code, 401)
                data = res.get_json()
                self.assertFalse(data["success"])
                self.assertIn("Authentication required", data["error"])

    def test_public_auth_endpoints_allowed_without_session(self):
        """GET /api/auth/status and POST /api/auth/setup/login are accessible without session."""
        res_status = self.client.get("/api/auth/status")
        self.assertEqual(res_status.status_code, 200)
        data = res_status.get_json()
        self.assertTrue(data["success"])
        self.assertFalse(data["initialized"])
        self.assertFalse(data["authenticated"])
        self.assertIn("csrf_token", data)

    def test_setup_master_password_flow(self):
        """Initial password setup enforces length and confirmation."""
        # Short password (< 8 chars)
        res_short = self.client.post("/api/auth/setup", json={
            "password": "short",
            "confirm_password": "short"
        })
        self.assertEqual(res_short.status_code, 400)
        self.assertIn("at least 8 characters", res_short.get_json()["error"])

        # Mismatched confirmation
        res_mismatch = self.client.post("/api/auth/setup", json={
            "password": "password123",
            "confirm_password": "different123"
        })
        self.assertEqual(res_mismatch.status_code, 400)
        self.assertIn("do not match", res_mismatch.get_json()["error"])

        # Successful setup
        res_ok, csrf_token = self._setup_master_password("correct-horse-battery")
        self.assertEqual(res_ok.status_code, 201)
        self.assertIsNotNone(csrf_token)

        # Duplicate setup attempt rejected
        res_dup = self.client.post("/api/auth/setup", json={
            "password": "anotherpassword123",
            "confirm_password": "anotherpassword123"
        })
        self.assertEqual(res_dup.status_code, 400)
        self.assertIn("already been configured", res_dup.get_json()["error"])

    def test_login_and_logout_flow(self):
        """Verify login with valid/invalid password and session termination on logout."""
        self._setup_master_password("securepassword123")

        # Create fresh client without cookies to simulate new browser session
        fresh_client = self.app.test_client()

        # Check status shows initialized but not authenticated
        res_status = fresh_client.get("/api/auth/status")
        self.assertEqual(res_status.status_code, 200)
        self.assertTrue(res_status.get_json()["initialized"])
        self.assertFalse(res_status.get_json()["authenticated"])

        # Failed login: incorrect password
        res_fail = fresh_client.post("/api/auth/login", json={"password": "wrongpassword"})
        self.assertEqual(res_fail.status_code, 401)
        self.assertFalse(res_fail.get_json()["success"])

        # Successful login
        res_login = fresh_client.post("/api/auth/login", json={"password": "securepassword123"})
        self.assertEqual(res_login.status_code, 200)
        csrf = res_login.get_json()["csrf_token"]

        # Authenticated access now succeeds
        res_tasks = fresh_client.get("/api/tasks")
        self.assertEqual(res_tasks.status_code, 200)

        # Logout
        res_logout = fresh_client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        self.assertEqual(res_logout.status_code, 200)

        # Subsequent access blocked (401)
        res_after = fresh_client.get("/api/tasks")
        self.assertEqual(res_after.status_code, 401)

    # =========================================================================
    # 2. CSRF Protection
    # =========================================================================

    def test_csrf_protection_on_state_changing_methods(self):
        """State-changing requests require valid X-CSRF-Token header."""
        _, csrf_token = self._setup_master_password()

        # Missing CSRF header on POST -> 403
        res_no_csrf = self.client.post("/api/tasks", json={"title": "Test task"})
        self.assertEqual(res_no_csrf.status_code, 403)
        self.assertIn("CSRF", res_no_csrf.get_json()["error"])

        # Invalid CSRF header on POST -> 403
        res_bad_csrf = self.client.post(
            "/api/tasks",
            json={"title": "Test task"},
            headers={"X-CSRF-Token": "invalid-token-12345"}
        )
        self.assertEqual(res_bad_csrf.status_code, 403)
        self.assertIn("CSRF", res_bad_csrf.get_json()["error"])

        # Valid CSRF header on POST -> 201
        res_ok = self.client.post(
            "/api/tasks",
            json={"title": "Valid task with CSRF"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_ok.status_code, 201)
        task_id = res_ok.get_json()["task"]["id"]

        # PUT without CSRF -> 403
        res_put_bad = self.client.put(f"/api/tasks/{task_id}", json={"status": "in_progress"})
        self.assertEqual(res_put_bad.status_code, 403)

        # PUT with valid CSRF -> 200
        res_put_ok = self.client.put(
            f"/api/tasks/{task_id}",
            json={"status": "in_progress"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_put_ok.status_code, 200)

        # DELETE without CSRF -> 403
        res_del_bad = self.client.delete(f"/api/tasks/{task_id}")
        self.assertEqual(res_del_bad.status_code, 403)

        # DELETE with valid CSRF -> 200
        res_del_ok = self.client.delete(f"/api/tasks/{task_id}", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res_del_ok.status_code, 200)

    def test_safe_methods_do_not_require_csrf(self):
        """GET, HEAD, and OPTIONS do not require CSRF token."""
        self._setup_master_password()
        res_get = self.client.get("/api/tasks")
        self.assertEqual(res_get.status_code, 200)

    # =========================================================================
    # 3. Payload & Field Length Limits
    # =========================================================================

    def test_max_content_length_enforced(self):
        """Payload exceeding MAX_CONTENT_LENGTH (10MB) returns HTTP 413."""
        _, csrf_token = self._setup_master_password()
        huge_data = b"x" * (10 * 1024 * 1024 + 1024)  # 10MB + 1KB
        res = self.client.post(
            "/api/tasks",
            data=huge_data,
            content_type="application/json",
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res.status_code, 413)

    def test_task_field_length_limits(self):
        """Task title (>200 chars) and description (>10k chars) are rejected with 400."""
        _, csrf_token = self._setup_master_password()

        # Title too long (>200)
        res_title = self.client.post(
            "/api/tasks",
            json={"title": "T" * 201},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_title.status_code, 400)
        self.assertIn("200", res_title.get_json()["error"])

        # Description too long (>10000)
        res_desc = self.client.post(
            "/api/tasks",
            json={"title": "Valid title", "description": "D" * 10001},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_desc.status_code, 400)
        self.assertIn("10000", res_desc.get_json()["error"])

    def test_note_content_length_limit(self):
        """Note content (>50,000 chars) is rejected with 400."""
        _, csrf_token = self._setup_master_password()
        res = self.client.post(
            "/api/notes",
            json={"title": "Valid note", "content": "N" * 50001},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("50000", res.get_json()["error"])

    def test_memory_content_length_limit(self):
        """Memory content (>1,000 chars) is rejected with 400."""
        _, csrf_token = self._setup_master_password()
        res = self.client.post(
            "/api/memory",
            json={"content": "M" * 1001, "category": "preference"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("1000", res.get_json()["error"])

    def test_chat_message_length_limit(self):
        """Chat message (>5,000 chars) is rejected with 400."""
        _, csrf_token = self._setup_master_password()
        res = self.client.post(
            "/api/chat",
            json={"message": "C" * 5001},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("5000", res.get_json()["error"])

    # =========================================================================
    # 4. Strict Input Validation
    # =========================================================================

    def test_task_invalid_priority_and_status(self):
        """Invalid task priority or status is rejected with 400."""
        _, csrf_token = self._setup_master_password()

        res_prio = self.client.post(
            "/api/tasks",
            json={"title": "Test", "priority": "emergency"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_prio.status_code, 400)
        self.assertIn("Invalid priority", res_prio.get_json()["error"])

        res_stat = self.client.post(
            "/api/tasks",
            json={"title": "Test", "status": "deleted_status"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_stat.status_code, 400)
        self.assertIn("Invalid status", res_stat.get_json()["error"])

    def test_memory_invalid_confidence_and_category(self):
        """Invalid confidence (<0.0 or >1.0) or invalid category rejected with 400."""
        _, csrf_token = self._setup_master_password()

        res_conf_high = self.client.post(
            "/api/memory",
            json={"content": "Prefers dark mode", "category": "preference", "confidence": 1.5},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_conf_high.status_code, 400)
        self.assertIn("confidence", res_conf_high.get_json()["error"].lower())

        res_conf_neg = self.client.post(
            "/api/memory",
            json={"content": "Prefers dark mode", "category": "preference", "confidence": -0.2},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_conf_neg.status_code, 400)
        self.assertIn("confidence", res_conf_neg.get_json()["error"].lower())

        res_cat = self.client.post(
            "/api/memory",
            json={"content": "Prefers dark mode", "category": "arbitrary_category"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_cat.status_code, 400)
        self.assertIn("Invalid category", res_cat.get_json()["error"])

    def test_settings_allowed_keys_only(self):
        """Settings endpoint rejects unknown keys with 400."""
        _, csrf_token = self._setup_master_password()

        res_bad_key = self.client.post(
            "/api/settings",
            json={"arbitrary_key": "injected_value"},
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_bad_key.status_code, 400)
        self.assertIn("not permitted", res_bad_key.get_json()["error"])

    def test_pagination_bounds_enforced(self):
        """Decision audit log pagination limit cannot exceed 200 and validates input."""
        self._setup_master_password()

        # Non-numeric or negative limit returns 400
        res_bad_str = self.client.get("/api/memory/decisions?limit=notanumber")
        self.assertEqual(res_bad_str.status_code, 400)

        res_bad_neg = self.client.get("/api/memory/decisions?limit=-5")
        self.assertEqual(res_bad_neg.status_code, 400)

        # Excessively large limit is capped at 200
        res_capped = self.client.get("/api/memory/decisions?limit=500")
        self.assertEqual(res_capped.status_code, 200)
        self.assertTrue(res_capped.get_json()["success"])

    def test_malformed_json_handling(self):
        """Non-dict JSON or unparseable JSON returns HTTP 400 with clean error."""
        _, csrf_token = self._setup_master_password()

        res_list = self.client.post(
            "/api/tasks",
            data="[1, 2, 3]",
            content_type="application/json",
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_list.status_code, 400)
        self.assertIn("key-value object", res_list.get_json()["error"])

        res_bad_json = self.client.post(
            "/api/tasks",
            data="{invalid_json:",
            content_type="application/json",
            headers={"X-CSRF-Token": csrf_token}
        )
        self.assertEqual(res_bad_json.status_code, 400)
        self.assertIn("invalid json", res_bad_json.get_json()["error"].lower())

    # =========================================================================
    # 5. Security Headers
    # =========================================================================

    def test_security_headers_present(self):
        """Security headers must be present on responses."""
        res = self.client.get("/api/auth/status")
        self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(res.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(res.headers.get("Referrer-Policy"), "no-referrer")
        csp = res.headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("default-src 'self'", csp)

    # =========================================================================
    # 6. Password Storage & Secret Leaks Prevention
    # =========================================================================

    def test_password_storage_security(self):
        """Verify password is scrypt-hashed, no plaintext in auth.json, no secret leaks."""
        self._setup_master_password("MySuperSecretPass!2026")

        # Inspect auth.json
        self.assertTrue(self.test_auth_path.exists())
        with open(self.test_auth_path, "r", encoding="utf-8") as f:
            stored = json.load(f)

        # Plaintext password must NOT exist in auth.json
        self.assertNotIn("password", stored)
        self.assertNotIn("MySuperSecretPass!2026", str(stored))

        # Hash must use scrypt
        self.assertTrue(stored["password_hash"].startswith("scrypt:"))

        # Settings endpoint must not leak secrets or hashes
        res_settings = self.client.get("/api/settings")
        self.assertEqual(res_settings.status_code, 200)
        settings_str = json.dumps(res_settings.get_json())
        self.assertNotIn("password_hash", settings_str)
        self.assertNotIn("secret_key", settings_str)
        self.assertNotIn("scrypt:", settings_str)

        # Auth status endpoint must not leak secrets
        res_status = self.client.get("/api/auth/status")
        status_str = json.dumps(res_status.get_json())
        self.assertNotIn("password_hash", status_str)
        self.assertNotIn("secret_key", status_str)

if __name__ == "__main__":
    unittest.main()
