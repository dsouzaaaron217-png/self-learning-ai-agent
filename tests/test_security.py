import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from app.security import validate_ollama_url, validate_bind_host
from app.reasoning.ollama_adapter import OllamaAdapter
from app.reasoning.local_reasoner import LocalReasoner
from app.vector_store import VectorStore

class TestOllamaUrlValidation(unittest.TestCase):
    """
    Focused security tests for Ollama URL validation under OFFLINE_STRICT_MODE=True.
    """

    def test_valid_urls(self):
        valid_cases = [
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
            ("http://localhost:11434", "http://localhost:11434"),
            ("http://localhost:11434/", "http://localhost:11434"),
            ("http://[::1]:11434", "http://[::1]:11434"),
            ("http://[::1]:11434/", "http://[::1]:11434"),
        ]
        for input_url, expected in valid_cases:
            with self.subTest(url=input_url):
                canonical = validate_ollama_url(input_url, strict_mode=True)
                self.assertEqual(canonical, expected)

    def test_invalid_urls_required_by_spec(self):
        invalid_cases = [
            "http://8.8.8.8:11434",
            "http://192.168.1.10:11434",
            "http://10.0.0.5:11434",
            "http://172.16.0.10:11434",
            "http://0.0.0.0:11434",
            "https://127.0.0.1:11434",
            "http://attacker.example:11434",
            "http://user:password@127.0.0.1:11434",
        ]
        for url in invalid_cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_ollama_url(url, strict_mode=True)

    def test_invalid_urls_edge_cases(self):
        edge_cases = [
            "http://127.0.0.2:11434",            # No 127.* prefix bypass
            "http://127.1.2.3:11434",            # No 127.* prefix bypass
            "http://127.0.0.1:11434/api/remote", # Endpoint path alteration
            "http://127.0.0.1:11434/evil",       # Endpoint path alteration
            "http://127.0.0.1:8000",             # Wrong port
            "http://127.0.0.1",                  # Missing port
            "ftp://127.0.0.1:11434",             # Non-HTTP scheme
            "http://127.0.0.1:11434?query=evil", # Query parameter prohibited
            "http://127.0.0.1:11434#fragment",   # Fragment prohibited
            "",                                  # Empty string
            "   ",                               # Whitespace only
        ]
        for url in edge_cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_ollama_url(url, strict_mode=True)


class TestBindHostValidation(unittest.TestCase):
    """
    Focused security tests for Flask server bind host under OFFLINE_STRICT_MODE=True.
    """

    def test_valid_bind_hosts(self):
        valid_hosts = ["127.0.0.1", "localhost", "::1", "[::1]"]
        for host in valid_hosts:
            with self.subTest(host=host):
                result = validate_bind_host(host, strict_mode=True)
                self.assertIn(result.strip("[]").lower(), {"127.0.0.1", "localhost", "::1"})

    def test_invalid_bind_hosts(self):
        invalid_hosts = [
            "0.0.0.0",
            "192.168.1.10",
            "10.0.0.1",
            "172.16.0.1",
            "8.8.8.8",
            "example.com",
            "",
        ]
        for host in invalid_hosts:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    validate_bind_host(host, strict_mode=True)


class TestOllamaAdapterSecurity(unittest.TestCase):
    """
    Tests for OllamaAdapter strict boundary and fallback behavior.
    """

    def test_adapter_rejects_remote_url(self):
        with self.assertRaises(ValueError):
            OllamaAdapter(base_url="http://8.8.8.8:11434")

    def test_adapter_accepts_local_url(self):
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")
        self.assertEqual(adapter.base_url, "http://127.0.0.1:11434")

    def test_adapter_blocks_corrupted_base_url_at_runtime(self):
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")
        # Simulate base_url mutation attempt
        adapter.base_url = "http://192.168.1.50:11434"
        
        # is_available and _query_ollama must refuse to connect
        self.assertFalse(adapter.is_available())
        self.assertIsNone(adapter._query_ollama("Hello"))

    def test_adapter_fallback_to_local_reasoner(self):
        # When Ollama daemon is unreachable on local port, fallback is seamless
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")
        self.assertIsInstance(adapter.fallback, LocalReasoner)
        
        # Suggest task enhancements falls back smoothly
        res = adapter.suggest_task_enhancements("Review pull request", "", [])
        self.assertIn("suggested_priority", res)
        self.assertIn("subtasks", res)


class TestSettingsApiSecurity(unittest.TestCase):
    """
    Tests for /api/settings endpoint strict validation.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_db_path = Path(self.temp_dir.name) / "test_sec_api.db"
        self.test_vec_path = Path(self.temp_dir.name) / "test_sec_vec.json"
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

        # Set up auth and obtain CSRF token
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

    def test_settings_rejects_remote_ollama_url(self):
        remote_urls = [
            "http://8.8.8.8:11434",
            "http://192.168.1.100:11434",
            "https://127.0.0.1:11434",
            "http://0.0.0.0:11434",
            "http://attacker.example:11434",
        ]
        for url in remote_urls:
            with self.subTest(url=url):
                res = self.client.post(
                    "/api/settings",
                    json={"ollama_url": url},
                    headers={"X-CSRF-Token": self.csrf_token}
                )
                self.assertEqual(res.status_code, 400)
                data = res.get_json()
                self.assertFalse(data["success"])
                self.assertIn("Invalid Ollama URL", data["error"])

    def test_settings_accepts_valid_ollama_url(self):
        valid_urls = [
            "http://127.0.0.1:11434",
            "http://localhost:11434",
            "http://[::1]:11434",
        ]
        for url in valid_urls:
            with self.subTest(url=url):
                res = self.client.post(
                    "/api/settings",
                    json={"ollama_url": url},
                    headers={"X-CSRF-Token": self.csrf_token}
                )
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertTrue(data["success"])


if __name__ == "__main__":
    unittest.main()
