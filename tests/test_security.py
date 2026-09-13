import os
import json
import unittest
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock
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

    def test_adapter_bypasses_proxy_environment(self):
        proxy_env = {
            "HTTP_PROXY": "http://10.255.255.1:8080",
            "HTTPS_PROXY": "http://10.255.255.1:8080",
            "ALL_PROXY": "http://10.255.255.1:8080",
            "http_proxy": "http://10.255.255.1:8080",
            "https_proxy": "http://10.255.255.1:8080",
            "all_proxy": "http://10.255.255.1:8080",
        }
        with patch.dict(os.environ, proxy_env):
            # Default opener inherits proxy handlers from environment
            default_opener = urllib.request.build_opener()
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) for h in default_opener.handle_open.get("http", [])))

            # OllamaAdapter explicitly bypasses environment proxies
            adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in adapter._opener.handle_open.get("http", [])))

    def test_adapter_request_path_bypasses_proxy_environment(self):
        """
        Verify the actual request path uses the proxy-disabled opener and
        does not set proxy routing even when proxy environment variables are set.
        """
        proxy_env = {
            "HTTP_PROXY": "http://198.51.100.1:8888",
            "HTTPS_PROXY": "http://198.51.100.1:8888",
            "ALL_PROXY": "http://198.51.100.1:8888",
            "http_proxy": "http://198.51.100.1:8888",
            "https_proxy": "http://198.51.100.1:8888",
            "all_proxy": "http://198.51.100.1:8888",
        }
        with patch.dict(os.environ, proxy_env):
            # 1. Verify that standard/default urllib sees the configured proxy
            proxies_seen = urllib.request.getproxies()
            self.assertIn("http", proxies_seen)
            self.assertEqual(proxies_seen["http"], "http://198.51.100.1:8888")

            default_opener = urllib.request.build_opener()
            self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) for h in default_opener.handle_open.get("http", [])))

            # 2. Instantiate OllamaAdapter and verify it uses its proxy-disabled opener
            adapter = OllamaAdapter(base_url="http://127.0.0.1:11434")

            # Mock the opener.open method on the adapter to avoid any real network call
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"response": "proxy-bypassed reply"}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None

            with patch.object(adapter._opener, "open", return_value=mock_resp) as mock_open:
                # Test is_available()
                available = adapter.is_available()
                self.assertTrue(available)
                mock_open.assert_called_once()
                sent_req = mock_open.call_args[0][0]
                self.assertEqual(sent_req.full_url, "http://127.0.0.1:11434/api/tags")
                self.assertFalse(sent_req.has_proxy())
                self.assertEqual(sent_req.host, "127.0.0.1:11434")

            # Test _query_ollama()
            with patch.object(adapter._opener, "open", return_value=mock_resp) as mock_open:
                reply = adapter._query_ollama("Hello local agent")
                self.assertEqual(reply, "proxy-bypassed reply")
                mock_open.assert_called_once()
                sent_req = mock_open.call_args[0][0]
                self.assertEqual(sent_req.full_url, "http://127.0.0.1:11434/api/generate")
                self.assertFalse(sent_req.has_proxy())
                self.assertEqual(sent_req.host, "127.0.0.1:11434")

    def test_adapter_accepts_all_valid_loopback_urls(self):
        valid_cases = [
            ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
            ("http://localhost:11434", "http://localhost:11434"),
            ("http://[::1]:11434", "http://[::1]:11434"),
        ]
        for url, expected in valid_cases:
            with self.subTest(url=url):
                adapter = OllamaAdapter(base_url=url)
                self.assertEqual(adapter.base_url, expected)

    def test_adapter_rejects_non_loopback_destinations(self):
        invalid_cases = [
            "http://8.8.8.8:11434",
            "http://192.168.1.10:11434",
            "http://10.0.0.5:11434",
            "http://172.16.0.1:11434",
            "http://0.0.0.0:11434",
            "https://127.0.0.1:11434",
            "http://attacker.example:11434",
        ]
        for url in invalid_cases:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    OllamaAdapter(base_url=url)


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


class TestOfflineFirewallSecurity(unittest.TestCase):
    """
    Focused tests for run.py's loopback firewall and ipaddress validation.
    """
    def test_is_loopback_host_valid(self):
        from run import is_loopback_host
        valid_hosts = ["127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2", "127.1.2.3"]
        for host in valid_hosts:
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))

    def test_is_loopback_host_invalid(self):
        from run import is_loopback_host
        invalid_hosts = [
            "0.0.0.0",
            "8.8.8.8",
            "192.168.1.1",
            "10.0.0.1",
            "172.16.0.1",
            "127.fake.com",
            "127.0.0.1.attacker.com",
            "attacker.example",
            "",
            None
        ]
        for host in invalid_hosts:
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_firewall_raises_connection_refused_on_non_loopback(self):
        import socket
        from run import enforce_offline_firewall

        # Save original getaddrinfo
        orig = socket.getaddrinfo
        try:
            enforce_offline_firewall()
            with self.assertRaises(ConnectionRefusedError):
                socket.getaddrinfo("8.8.8.8", 80)
            with self.assertRaises(ConnectionRefusedError):
                socket.getaddrinfo("attacker.example", 443)
            with self.assertRaises(ConnectionRefusedError):
                socket.getaddrinfo("127.fake.com", 80)
        finally:
            # Always restore original getaddrinfo
            socket.getaddrinfo = orig


if __name__ == "__main__":
    unittest.main()
