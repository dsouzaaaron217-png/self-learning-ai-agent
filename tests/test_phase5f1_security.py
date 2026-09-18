import unittest
import json
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from app.vector_store import VectorStore
from app.auth import (
    is_auth_configured,
    setup_password,
    change_password,
    verify_password,
    get_auth_version,
    login_rate_limiter,
    LoginRateLimiter
)
from app.security import validate_bind_host

class TestPhase5F1SecurityHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_sec5f1.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_sec5f1_vec.json"
        self.test_auth_path = Path(self.temp_dir.name) / "test_sec5f1_auth.json"

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

        login_rate_limiter.reset()

    def tearDown(self):
        login_rate_limiter.reset()
        self.p_auth.stop()
        self.p_db.stop()
        self.p_vec.stop()
        self.temp_dir.cleanup()

    def _setup_master_password(self, password="initial-master-password"):
        res = self.client.post("/api/auth/setup", json={
            "password": password,
            "confirm_password": password
        })
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        return data.get("csrf_token")

    # =========================================================================
    # 1. Password Rotation Service & API Tests
    # =========================================================================

    def test_change_password_service_success(self):
        """change_password updates hash, bumps version, and preserves secret key."""
        setup_password("firstpassword123")
        initial_version = get_auth_version()
        self.assertEqual(initial_version, 1)

        new_version = change_password("firstpassword123", "newpassword456")
        self.assertEqual(new_version, 2)
        self.assertEqual(get_auth_version(), 2)

        # Old password no longer verifies
        self.assertFalse(verify_password("firstpassword123"))
        # New password verifies
        self.assertTrue(verify_password("newpassword456"))

        # auth.json has no plaintext passwords
        with open(self.test_auth_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["password_version"], 2)
        self.assertNotIn("firstpassword123", str(data))
        self.assertNotIn("newpassword456", str(data))

    def test_change_password_service_validations(self):
        """change_password validates wrong current password, short password, and unconfigured state."""
        # Unconfigured
        with self.assertRaises(ValueError) as ctx:
            change_password("any", "newpassword123")
        self.assertIn("not configured", str(ctx.exception).lower())

        setup_password("existingpass123")

        # Wrong current password
        with self.assertRaises(ValueError) as ctx:
            change_password("wrongpass123", "newpassword123")
        self.assertIn("current password is incorrect", str(ctx.exception).lower())

        # Short new password (< 8 chars)
        with self.assertRaises(ValueError) as ctx:
            change_password("existingpass123", "short")
        self.assertIn("at least 8 characters", str(ctx.exception).lower())

    def test_api_change_password_unauthenticated_blocked(self):
        """POST /api/auth/change-password requires authentication."""
        self._setup_master_password()
        unauth_client = self.app.test_client()

        res = unauth_client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "new_password": "new-master-password",
            "confirm_password": "new-master-password"
        })
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json()["success"])

    def test_api_change_password_csrf_enforced(self):
        """POST /api/auth/change-password requires a valid CSRF token."""
        self._setup_master_password()

        # Missing CSRF
        res_no_csrf = self.client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "new_password": "new-master-password",
            "confirm_password": "new-master-password"
        })
        self.assertEqual(res_no_csrf.status_code, 403)

        # Invalid CSRF
        res_bad_csrf = self.client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "new_password": "new-master-password",
            "confirm_password": "new-master-password"
        }, headers={"X-CSRF-Token": "bogus-csrf-token"})
        self.assertEqual(res_bad_csrf.status_code, 403)

    def test_api_change_password_input_validation(self):
        """POST /api/auth/change-password validates fields, lengths, and matches."""
        csrf_token = self._setup_master_password()

        # Missing current_password
        res1 = self.client.post("/api/auth/change-password", json={
            "new_password": "newpassword123",
            "confirm_password": "newpassword123"
        }, headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res1.status_code, 400)
        self.assertIn("Current password is required", res1.get_json()["error"])

        # Missing new_password
        res2 = self.client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "confirm_password": "newpassword123"
        }, headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res2.status_code, 400)
        self.assertIn("New password is required", res2.get_json()["error"])

        # Short new_password
        res3 = self.client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "new_password": "short",
            "confirm_password": "short"
        }, headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res3.status_code, 400)
        self.assertIn("at least 8 characters", res3.get_json()["error"])

        # Confirmation mismatch
        res4 = self.client.post("/api/auth/change-password", json={
            "current_password": "initial-master-password",
            "new_password": "validnewpass123",
            "confirm_password": "differentpass123"
        }, headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res4.status_code, 400)
        self.assertIn("do not match", res4.get_json()["error"])

        # Incorrect current password
        res5 = self.client.post("/api/auth/change-password", json={
            "current_password": "wrong-current-password",
            "new_password": "validnewpass123",
            "confirm_password": "validnewpass123"
        }, headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(res5.status_code, 401)
        self.assertIn("incorrect", res5.get_json()["error"].lower())

    def test_api_change_password_success_flow_and_session_invalidation(self):
        """
        Successful password change:
        1. Keeps current user logged in with rotated CSRF token.
        2. Invalidates prior sessions from other clients.
        3. Never leaks hash or secrets.
        """
        client_a = self.client
        csrf_a = self._setup_master_password("originalpass123")

        # Client B logs in with original password
        client_b = self.app.test_client()
        res_b_login = client_b.post("/api/auth/login", json={"password": "originalpass123"})
        self.assertEqual(res_b_login.status_code, 200)
        csrf_b = res_b_login.get_json()["csrf_token"]

        # Both clients can make authenticated calls
        self.assertEqual(client_a.get("/api/tasks").status_code, 200)
        self.assertEqual(client_b.get("/api/tasks").status_code, 200)

        # Client A changes password
        res_change = client_a.post("/api/auth/change-password", json={
            "current_password": "originalpass123",
            "new_password": "updatedpass123",
            "confirm_password": "updatedpass123"
        }, headers={"X-CSRF-Token": csrf_a})
        self.assertEqual(res_change.status_code, 200)
        change_data = res_change.get_json()
        self.assertTrue(change_data["success"])
        new_csrf_a = change_data["csrf_token"]
        self.assertIsNotNone(new_csrf_a)
        self.assertNotEqual(new_csrf_a, csrf_a)

        # No secret leakage in response
        res_str = json.dumps(change_data)
        self.assertNotIn("password_hash", res_str)
        self.assertNotIn("secret_key", res_str)
        self.assertNotIn("scrypt:", res_str)

        # Client A remains authenticated and can perform actions with new CSRF token
        res_a_tasks = client_a.get("/api/tasks")
        self.assertEqual(res_a_tasks.status_code, 200)

        res_a_post = client_a.post("/api/tasks", json={"title": "Session A task"}, headers={"X-CSRF-Token": new_csrf_a})
        self.assertEqual(res_a_post.status_code, 201)

        # Old CSRF token is rejected for Client A
        res_a_old_csrf = client_a.post("/api/tasks", json={"title": "Should fail"}, headers={"X-CSRF-Token": csrf_a})
        self.assertEqual(res_a_old_csrf.status_code, 403)

        # Client B (prior session) is now INVALIDATED
        res_b_tasks = client_b.get("/api/tasks")
        self.assertEqual(res_b_tasks.status_code, 401)
        self.assertIn("password change", res_b_tasks.get_json()["error"].lower())

        # Client B cannot perform POST actions either
        res_b_post = client_b.post("/api/tasks", json={"title": "Session B task"}, headers={"X-CSRF-Token": csrf_b})
        self.assertEqual(res_b_post.status_code, 401)

        # Client B can log in with new password
        res_b_relogin = client_b.post("/api/auth/login", json={"password": "updatedpass123"})
        self.assertEqual(res_b_relogin.status_code, 200)
        self.assertEqual(client_b.get("/api/tasks").status_code, 200)

    # =========================================================================
    # 2. Login Rate Limiter & Lockout Tests
    # =========================================================================

    def test_login_rate_limiter_unit(self):
        """Unit test LoginRateLimiter for lockout, remaining time, and success reset."""
        limiter = LoginRateLimiter(max_attempts=3, lockout_duration=30, window_seconds=60)
        ip = "127.0.0.1"

        self.assertEqual(limiter.get_lockout_remaining(ip), 0)

        # 1st failure
        locked, count = limiter.record_failure(ip, now=100.0)
        self.assertFalse(locked)
        self.assertEqual(count, 1)
        self.assertEqual(limiter.get_lockout_remaining(ip, now=100.0), 0)

        # 2nd failure
        locked, count = limiter.record_failure(ip, now=110.0)
        self.assertFalse(locked)
        self.assertEqual(count, 2)

        # 3rd failure -> triggers lockout
        locked, secs = limiter.record_failure(ip, now=120.0)
        self.assertTrue(locked)
        self.assertEqual(secs, 30)
        self.assertEqual(limiter.get_lockout_remaining(ip, now=120.0), 30)
        self.assertEqual(limiter.get_lockout_remaining(ip, now=135.0), 15)
        self.assertEqual(limiter.get_lockout_remaining(ip, now=151.0), 0)

        # Success clears history
        limiter.record_failure(ip, now=160.0)
        limiter.record_success(ip)
        self.assertEqual(limiter.get_lockout_remaining(ip, now=160.0), 0)
        self.assertEqual(len(limiter._failures.get(ip, [])), 0)

    def test_login_rate_limiting_api_lockout_and_retry_after(self):
        """5 failed login attempts trigger HTTP 429 with Retry-After header."""
        self._setup_master_password("correctpassword123")
        client = self.app.test_client()

        # 4 failed attempts -> 401
        for i in range(4):
            res = client.post("/api/auth/login", json={"password": "wrongpassword"})
            self.assertEqual(res.status_code, 401, f"Attempt {i+1} should return 401")
            self.assertFalse(res.get_json()["success"])

        # 5th failed attempt -> 429 Too Many Requests
        res_locked = client.post("/api/auth/login", json={"password": "wrongpassword"})
        self.assertEqual(res_locked.status_code, 429)
        self.assertIn("Retry-After", res_locked.headers)
        retry_after = int(res_locked.headers["Retry-After"])
        self.assertGreater(retry_after, 0)
        self.assertLessEqual(retry_after, 60)
        self.assertIn("Too many failed login attempts", res_locked.get_json()["error"])

        # 6th attempt even with CORRECT password is blocked while locked out
        res_blocked = client.post("/api/auth/login", json={"password": "correctpassword123"})
        self.assertEqual(res_blocked.status_code, 429)
        self.assertIn("Retry-After", res_blocked.headers)

    def test_login_rate_limiting_per_client_isolation(self):
        """Lockout on one client IP does not affect another client IP."""
        self._setup_master_password("correctpassword123")
        client_attacker = self.app.test_client()
        client_legit = self.app.test_client()

        # Attacker fails 5 times from 10.0.0.1
        for _ in range(5):
            res = client_attacker.post(
                "/api/auth/login",
                json={"password": "bad"},
                environ_base={"REMOTE_ADDR": "10.0.0.1"}
            )
        self.assertEqual(res.status_code, 429)

        # Legitimate user from 10.0.0.2 can still log in successfully
        res_legit = client_legit.post(
            "/api/auth/login",
            json={"password": "correctpassword123"},
            environ_base={"REMOTE_ADDR": "10.0.0.2"}
        )
        self.assertEqual(res_legit.status_code, 200)
        self.assertTrue(res_legit.get_json()["success"])

    def test_login_rate_limiter_resets_on_success(self):
        """A successful login clears the failed attempt count."""
        self._setup_master_password("correctpassword123")
        client = self.app.test_client()

        # 3 failed attempts
        for _ in range(3):
            client.post("/api/auth/login", json={"password": "wrong"})

        # Successful login resets the counter
        res_ok = client.post("/api/auth/login", json={"password": "correctpassword123"})
        self.assertEqual(res_ok.status_code, 200)

        # 3 more failed attempts should not trigger lockout (since counter was reset)
        for _ in range(3):
            res = client.post("/api/auth/login", json={"password": "wrong"})
            self.assertEqual(res.status_code, 401)

    # =========================================================================
    # 3. Session Expiration & Inactivity Timeout Tests
    # =========================================================================

    def test_session_permanent_lifetime_configured(self):
        """app.config['PERMANENT_SESSION_LIFETIME'] is configured to 24 hours."""
        self.assertEqual(self.app.config["PERMANENT_SESSION_LIFETIME"], timedelta(hours=24))

    def test_session_inactivity_timeout_enforced(self):
        """Requests with session inactive for > 2 hours (7200s) are rejected with 401."""
        csrf = self._setup_master_password()

        # Normal request within active window succeeds
        res_ok = self.client.get("/api/tasks")
        self.assertEqual(res_ok.status_code, 200)

        # Fast-forward last_activity by 7205 seconds
        stale_time = datetime.now(timezone.utc).timestamp() - 7205
        with self.client.session_transaction() as sess:
            sess["last_activity"] = stale_time

        # Next request should be rejected due to inactivity
        res_expired = self.client.get("/api/tasks")
        self.assertEqual(res_expired.status_code, 401)
        self.assertIn("inactivity", res_expired.get_json()["error"].lower())

        # Session should be cleared
        res_status = self.client.get("/api/auth/status")
        self.assertFalse(res_status.get_json()["authenticated"])

    def test_active_requests_refresh_inactivity_timestamp(self):
        """Active requests continuously update session['last_activity']."""
        self._setup_master_password()

        with self.client.session_transaction() as sess:
            initial_activity = sess.get("last_activity")
        self.assertIsNotNone(initial_activity)

        time.sleep(0.01)
        res = self.client.get("/api/tasks")
        self.assertEqual(res.status_code, 200)

        with self.client.session_transaction() as sess:
            updated_activity = sess.get("last_activity")
        self.assertGreaterEqual(updated_activity, initial_activity)

    # =========================================================================
    # 4. Host Binding Security Tests
    # =========================================================================

    def test_validate_bind_host_strict_mode(self):
        """Strict offline mode permits only loopback and rejects any external binding."""
        # Loopback permitted
        self.assertEqual(validate_bind_host("127.0.0.1", strict_mode=True), "127.0.0.1")
        self.assertEqual(validate_bind_host("localhost", strict_mode=True), "localhost")
        self.assertEqual(validate_bind_host("::1", strict_mode=True), "::1")

        # Non-loopback rejected
        prohibited_hosts = [
            "0.0.0.0",
            "::",
            "192.168.1.50",
            "10.0.0.1",
            "172.16.0.1",
            "8.8.8.8",
            "example.com"
        ]
        for host in prohibited_hosts:
            with self.subTest(host=host):
                with self.assertRaises(ValueError) as ctx:
                    validate_bind_host(host, strict_mode=True)
                self.assertIn("loopback", str(ctx.exception).lower())

if __name__ == "__main__":
    unittest.main()
