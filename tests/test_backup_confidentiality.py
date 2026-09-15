"""
Phase 3F Tests: Backup Confidentiality Hardening

Comprehensive test suite verifying:
1. Successful encrypted export.
2. Successful encrypted import.
3. Correct passphrase.
4. Wrong passphrase.
5. Tampered ciphertext.
6. Tampered authentication tag.
7. Tampered nonce.
8. Tampered salt.
9. Tampered KDF metadata.
10. Unsupported encryption version.
11. Unsupported cipher.
12. Invalid/malformed encrypted envelope.
13. Missing passphrase.
14. Invalid passphrase type.
15. Excessive passphrase length.
16. Excessive/malicious KDF parameters.
17. Decrypted payload with invalid Phase 3E checksum.
18. Decrypted payload with invalid Phase 3E schema.
19. Verify failed authentication/checksum occurs before DB mutation.
20. Verify failed authentication/checksum occurs before safety snapshot.
21. Successful import preserves existing Phase 3D safety snapshot behavior.
22. Vector rebuild still occurs after successful import.
23. Vector sync failure preserves SQLite-authoritative behavior.
24. Existing plaintext Phase 3E backup remains importable.
25. Plaintext import produces the intended warning/status.
26. Export does not create a plaintext temporary backup file.
27. Encrypted backup plaintext is not present outside ciphertext.
28. Fresh random salt is generated for separate exports.
29. Fresh random nonce is generated for separate encryption operations.
30. Existing regression suite remains green.
"""

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.backup_crypto import (
    encrypt_backup,
    decrypt_backup,
    validate_encrypted_envelope,
    is_encrypted_backup,
    ENCRYPTED_BACKUP_FORMAT,
    ENCRYPTED_BACKUP_VERSION,
    CIPHER_ALGORITHM,
    KDF_ALGORITHM,
    DEFAULT_SCRYPT_N,
    DEFAULT_SCRYPT_R,
    DEFAULT_SCRYPT_P,
)
from app.database import get_db
from app.validation import compute_backup_checksum
from app.vector_store import VectorStore
from app.models import TaskModel, NoteModel, MemoryModel, SettingsModel
import gc


class TestBackupConfidentiality(unittest.TestCase):
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
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _auth_headers(self, extra=None):
        h = {"X-CSRF-Token": self.csrf_token}
        if extra:
            h.update(extra)
        return h

    def _post(self, path, json=None, headers=None):
        return self.client.post(path, json=json, headers=self._auth_headers(headers))

    def _create_sample_payload(self):
        """Creates a valid, complete backup payload with matching canonical checksum."""
        data = {
            "tasks": [
                {
                    "id": 1,
                    "title": "Confidential Security Audit",
                    "description": "Evaluate backup encryption mechanisms",
                    "priority": "urgent",
                    "status": "todo",
                    "tags": "security,crypto",
                    "due_date": "2026-10-01",
                    "subtasks": [{"id": 1, "title": "Test AES-GCM", "completed": True}],
                    "created_at": "2026-09-15T10:00:00+00:00",
                    "updated_at": "2026-09-15T10:00:00+00:00"
                }
            ],
            "notes": [
                {
                    "id": 1,
                    "title": "Cryptographic Key Architecture",
                    "content": "AES-256-GCM authenticated encryption with scrypt KDF.",
                    "tags": "crypto,architecture",
                    "pinned": 1,
                    "created_at": "2026-09-15T10:00:00+00:00",
                    "updated_at": "2026-09-15T10:00:00+00:00"
                }
            ],
            "memories": [
                {
                    "id": 1,
                    "category": "preference",
                    "content": "User prefers authenticated encryption for all backups.",
                    "confidence_weight": 0.95,
                    "status": "active",
                    "source_context": "Phase 3F setup",
                    "superseded_by": None,
                    "update_count": 0,
                    "created_at": "2026-09-15T10:00:00+00:00",
                    "updated_at": "2026-09-15T10:00:00+00:00"
                }
            ],
            "decision_logs": [
                {
                    "id": 1,
                    "memory_id": 1,
                    "action": "ADD",
                    "reasoning": "Learned from security specification",
                    "previous_content": None,
                    "new_content": "User prefers authenticated encryption for all backups.",
                    "triggered_by": "user",
                    "timestamp": "2026-09-15T10:00:00+00:00"
                }
            ],
            "feedback_events": [
                {
                    "id": 1,
                    "suggestion_type": "task_suggestion",
                    "item_id": 1,
                    "memory_id_applied": 1,
                    "original_suggestion": "Suggested urgent priority",
                    "user_action": "accepted",
                    "correction_detail": None,
                    "timestamp": "2026-09-15T10:00:00+00:00"
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
            "exported_at": "2026-09-15T10:00:00+00:00",
            "checksum_sha256": checksum,
            "data": data
        }

    # =========================================================================
    # 1. Successful encrypted export
    # =========================================================================
    def test_encrypted_export_success(self):
        """POST /api/backup/export with passphrase returns AES-256-GCM encrypted envelope."""
        TaskModel.create(title="Sensitive Task", priority="high")
        NoteModel.create(title="Private Journal", content="Personal thoughts never to be leaked")

        res = self._post("/api/backup/export", json={"passphrase": "StrongPassphrase123!"})
        self.assertEqual(res.status_code, 200)

        disposition = res.headers.get("Content-Disposition", "")
        self.assertIn(".enc.json", disposition)

        envelope = json.loads(res.data.decode("utf-8"))
        self.assertEqual(envelope.get("format"), ENCRYPTED_BACKUP_FORMAT)
        self.assertEqual(envelope.get("version"), ENCRYPTED_BACKUP_VERSION)
        self.assertEqual(envelope.get("application"), "Cognito Offline Agent")

        # Verify KDF and Cipher metadata
        self.assertEqual(envelope.get("kdf", {}).get("algorithm"), KDF_ALGORITHM)
        self.assertEqual(envelope.get("kdf", {}).get("n"), DEFAULT_SCRYPT_N)
        self.assertEqual(envelope.get("cipher", {}).get("algorithm"), CIPHER_ALGORITHM)
        self.assertTrue(envelope.get("cipher", {}).get("nonce"))
        self.assertTrue(envelope.get("ciphertext"))

        # Requirement 27: Plaintext data must not be present outside ciphertext
        self.assertNotIn("data", envelope)
        self.assertNotIn("tasks", envelope)
        self.assertNotIn("notes", envelope)
        self.assertNotIn("checksum_sha256", envelope)
        self.assertNotIn("Private Journal", res.data.decode("utf-8"))

        # Decrypt envelope with correct passphrase and verify full content
        decrypted = decrypt_backup(envelope, "StrongPassphrase123!")
        self.assertIn("data", decrypted)
        self.assertIn("checksum_sha256", decrypted)
        self.assertEqual(compute_backup_checksum(decrypted["data"]), decrypted["checksum_sha256"])

    def test_query_string_passphrase_ignored_does_not_produce_encrypted_export(self):
        """CWE-598 regression: Authenticated GET with ?passphrase=... does NOT produce an encrypted export."""
        TaskModel.create(title="Regression Test Item")
        res = self.client.get("/api/backup/export?passphrase=SensitiveQueryPassphrase123!")
        self.assertEqual(res.status_code, 200)

        disposition = res.headers.get("Content-Disposition", "")
        self.assertNotIn(".enc.json", disposition)
        self.assertIn(".json", disposition)

        payload = json.loads(res.data.decode("utf-8"))
        # Must not be an encrypted envelope
        self.assertNotEqual(payload.get("format"), ENCRYPTED_BACKUP_FORMAT)
        self.assertNotIn("ciphertext", payload)
        # Must be the standard plaintext backup format
        self.assertIn("data", payload)
        self.assertIn("checksum_sha256", payload)

        # POST with only query-string passphrase must also be rejected
        res_post = self._post("/api/backup/export?passphrase=SensitiveQueryPassphrase123!", json={})
        self.assertEqual(res_post.status_code, 400)
        self.assertIn("passphrase is required", res_post.get_json()["error"].lower())

    def test_encrypted_export_via_header_passphrase(self):
        """Encrypted export accepts passphrase securely via X-Backup-Passphrase header."""
        res = self.client.get("/api/backup/export", headers={"X-Backup-Passphrase": "HeaderPassphrase123!"})
        self.assertEqual(res.status_code, 200)

        disposition = res.headers.get("Content-Disposition", "")
        self.assertIn(".enc.json", disposition)

        envelope = json.loads(res.data.decode("utf-8"))
        self.assertEqual(envelope.get("format"), ENCRYPTED_BACKUP_FORMAT)
        decrypted = decrypt_backup(envelope, "HeaderPassphrase123!")
        self.assertIn("data", decrypted)

    # =========================================================================
    # 2. Salt and Nonce Freshness
    # =========================================================================
    def test_fresh_salt_and_nonce_per_export(self):
        """Requirements 28 & 29: Separate exports must use fresh random salt and nonce."""
        passphrase = "IdenticalPassphrase123!"
        res1 = self._post("/api/backup/export", json={"passphrase": passphrase})
        res2 = self._post("/api/backup/export", json={"passphrase": passphrase})

        env1 = json.loads(res1.data.decode("utf-8"))
        env2 = json.loads(res2.data.decode("utf-8"))

        # Salts must differ
        salt1 = env1["kdf"]["salt"]
        salt2 = env2["kdf"]["salt"]
        self.assertNotEqual(salt1, salt2)

        # Nonces must differ
        nonce1 = env1["cipher"]["nonce"]
        nonce2 = env2["cipher"]["nonce"]
        self.assertNotEqual(nonce1, nonce2)

        # Ciphertexts must differ
        self.assertNotEqual(env1["ciphertext"], env2["ciphertext"])

    # =========================================================================
    # 3. No plaintext temporary file on export
    # =========================================================================
    def test_export_does_not_create_temporary_file(self):
        """Requirement 26: Export must not create a plaintext temporary backup file on disk."""
        initial_files = set(self.backup_dir.glob("*"))
        res = self._post("/api/backup/export", json={"passphrase": "StrongPassphrase123!"})
        self.assertEqual(res.status_code, 200)

        post_files = set(self.backup_dir.glob("*"))
        # No files added to backups directory during export
        self.assertEqual(initial_files, post_files)

    # =========================================================================
    # 4. Passphrase Validation (Missing, Invalid Type, Length)
    # =========================================================================
    def test_export_missing_passphrase(self):
        """Requirement 13: POST export without passphrase is rejected."""
        res = self._post("/api/backup/export", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("passphrase is required", res.get_json()["error"].lower())

    def test_export_invalid_passphrase_type(self):
        """Requirement 14: Passphrase must be a string."""
        res = self._post("/api/backup/export", json={"passphrase": 12345678})
        self.assertEqual(res.status_code, 400)
        self.assertIn("must be a string", res.get_json()["error"].lower())

    def test_export_passphrase_too_short(self):
        """Export passphrase must be at least 8 characters."""
        res = self._post("/api/backup/export", json={"passphrase": "short"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("at least 8 characters", res.get_json()["error"].lower())

    def test_export_passphrase_excessive_length(self):
        """Requirement 15: Passphrase exceeding 256 characters is rejected."""
        long_pass = "A" * 300
        res = self._post("/api/backup/export", json={"passphrase": long_pass})
        self.assertEqual(res.status_code, 400)
        self.assertIn("exceeds maximum allowed length", res.get_json()["error"].lower())

    # =========================================================================
    # 5. Successful Encrypted Import
    # =========================================================================
    def test_encrypted_import_success(self):
        """Requirements 2 & 3: Valid encrypted backup imports cleanly with correct passphrase."""
        # Setup initial item that will be replaced
        TaskModel.create(title="Old Task to be Replaced")

        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["sqlite_restored"])
        self.assertTrue(data["is_encrypted"])
        self.assertIsNone(data["warning"])
        self.assertEqual(data["restored"]["tasks"], 1)
        self.assertEqual(data["restored"]["notes"], 1)
        self.assertEqual(data["restored"]["memories"], 1)

        # Verify SQLite has new records
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM tasks")
            tasks = [r[0] for r in cur.fetchall()]
            self.assertEqual(tasks, ["Confidential Security Audit"])

            cur.execute("SELECT title FROM notes")
            notes = [r[0] for r in cur.fetchall()]
            self.assertEqual(notes, ["Cryptographic Key Architecture"])

        # Requirement 22: Vector store was rebuilt
        vs_results = self.vs.search("Cryptographic", limit=5)
        self.assertTrue(len(vs_results) > 0)

        # Requirement 21: Pre-restore safety snapshot was created
        snapshots = list(self.backup_dir.glob("cognito_snapshot_*.db"))
        self.assertTrue(len(snapshots) >= 1)

    def test_encrypted_import_with_passphrase_in_header(self):
        """Encrypted import accepts passphrase via X-Backup-Passphrase header."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "HeaderPassphrase123!")

        res = self._post("/api/backup/import",
                         json={"backup": envelope},
                         headers={"X-Backup-Passphrase": "HeaderPassphrase123!"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

    def test_encrypted_import_with_flat_envelope_body(self):
        """Encrypted import accepts envelope directly at root with passphrase field."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "FlatPassphrase123!")
        envelope["passphrase"] = "FlatPassphrase123!"

        res = self._post("/api/backup/import", json=envelope)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

    # =========================================================================
    # 6. Authentication Failure: Wrong Passphrase
    # =========================================================================
    def test_encrypted_import_wrong_passphrase(self):
        """Requirements 4, 19, 20: Wrong passphrase fails with safe error, no DB touch, no snapshot."""
        TaskModel.create(title="Unmodified Original Task")
        initial_snapshots = list(self.backup_dir.glob("*.db"))

        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "CorrectPassphrase123!")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "WrongPassphrase999!"
        })
        self.assertEqual(res.status_code, 400)
        # Safe generic error message
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

        # Database must NOT be modified
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM tasks")
            tasks = [r[0] for r in cur.fetchall()]
            self.assertEqual(tasks, ["Unmodified Original Task"])

        # No safety snapshot should have been created
        post_snapshots = list(self.backup_dir.glob("*.db"))
        self.assertEqual(initial_snapshots, post_snapshots)

    # =========================================================================
    # 7. Tampering Tests: Ciphertext, Tag, Nonce, Salt, KDF
    # =========================================================================
    def test_encrypted_import_tampered_ciphertext(self):
        """Requirement 5: Tampered ciphertext fails authentication with generic error."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        raw_ct = bytearray(base64.b64decode(envelope["ciphertext"]))
        raw_ct[0] ^= 0xFF  # Flip bits in ciphertext body
        envelope["ciphertext"] = base64.b64encode(raw_ct).decode("ascii")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

    def test_encrypted_import_tampered_authentication_tag(self):
        """Requirement 6: Tampered GCM tag fails authentication with generic error."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        raw_ct = bytearray(base64.b64decode(envelope["ciphertext"]))
        raw_ct[-1] ^= 0xFF  # Flip bits in authentication tag (last 16 bytes)
        envelope["ciphertext"] = base64.b64encode(raw_ct).decode("ascii")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

    def test_encrypted_import_tampered_nonce(self):
        """Requirement 7: Tampered nonce fails GCM tag verification."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        raw_nonce = bytearray(base64.b64decode(envelope["cipher"]["nonce"]))
        raw_nonce[0] ^= 0xFF
        envelope["cipher"]["nonce"] = base64.b64encode(raw_nonce).decode("ascii")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

    def test_encrypted_import_tampered_salt(self):
        """Requirement 8: Tampered salt fails key derivation / tag verification."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        raw_salt = bytearray(base64.b64decode(envelope["kdf"]["salt"]))
        raw_salt[0] ^= 0xFF
        envelope["kdf"]["salt"] = base64.b64encode(raw_salt).decode("ascii")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

    def test_encrypted_import_tampered_kdf_metadata(self):
        """Requirement 9: Tampered KDF metadata (e.g. changing r) fails authentication."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        envelope["kdf"]["r"] = 16  # Valid bound, but altered from original 8

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"], "Invalid backup passphrase or corrupted encrypted backup.")

    # =========================================================================
    # 8. Envelope Validation (Version, Cipher, Malformed, Bounds)
    # =========================================================================
    def test_encrypted_import_unsupported_version(self):
        """Requirement 10: Unsupported envelope version is rejected."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")
        envelope["version"] = "2.0.0"

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("unsupported encrypted backup version", res.get_json()["error"].lower())

    def test_encrypted_import_unsupported_cipher(self):
        """Requirement 11: Unsupported cipher algorithm is rejected."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")
        envelope["cipher"]["algorithm"] = "ChaCha20-Poly1305"

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("unsupported cipher algorithm", res.get_json()["error"].lower())

    def test_encrypted_import_malformed_envelope(self):
        """Requirement 12: Missing required envelope fields is rejected."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")
        del envelope["ciphertext"]

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("ciphertext", res.get_json()["error"].lower())

    def test_encrypted_import_missing_passphrase(self):
        """Requirement 13: Encrypted import with missing passphrase is rejected."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        res = self._post("/api/backup/import", json={"backup": envelope})
        self.assertEqual(res.status_code, 400)
        self.assertIn("passphrase is required", res.get_json()["error"].lower())

    def test_encrypted_import_excessive_kdf_parameters_rejected(self):
        """Requirement 16: Maliciously high KDF parameters are rejected to prevent DoS."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        # Set N above maximum allowed (65536)
        envelope["kdf"]["n"] = 131072

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("outside acceptable bounds", res.get_json()["error"].lower())

    def test_encrypted_import_non_power_of_two_kdf_n(self):
        """Non-power-of-two KDF N parameter is rejected."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")
        envelope["kdf"]["n"] = 15000

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("outside acceptable bounds", res.get_json()["error"].lower())

    # =========================================================================
    # 9. Phase 3E Canonical Checksum & Schema Validation after Decryption
    # =========================================================================
    def test_decrypted_payload_invalid_checksum_rejected(self):
        """Requirement 17: Decrypted payload with tampered Phase 3E checksum is rejected."""
        payload = self._create_sample_payload()
        # Corrupt the internal checksum inside the payload before encrypting
        payload["checksum_sha256"] = "0" * 64

        envelope = encrypt_backup(payload, "TestPassphrase123!")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("checksum verification failed", res.get_json()["error"].lower())

    def test_decrypted_payload_invalid_schema_rejected(self):
        """Requirement 18: Decrypted payload with duplicate IDs or invalid FKs is rejected."""
        payload = self._create_sample_payload()
        # Introduce duplicate task ID
        payload["data"]["tasks"].append({
            "id": 1,
            "title": "Duplicate Task",
            "priority": "low",
            "status": "todo"
        })
        # Recompute checksum so checksum check passes but schema validation fails
        payload["checksum_sha256"] = compute_backup_checksum(payload["data"])

        envelope = encrypt_backup(payload, "TestPassphrase123!")

        res = self._post("/api/backup/import", json={
            "backup": envelope,
            "passphrase": "TestPassphrase123!"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("duplicate task id 1", res.get_json()["error"].lower())

    # =========================================================================
    # 10. Vector Sync Resilience during Encrypted Import
    # =========================================================================
    def test_vector_sync_failure_preserves_authoritative_sqlite_import(self):
        """Requirement 23: If vector sync fails post-import, SQLite restore is still preserved."""
        payload = self._create_sample_payload()
        envelope = encrypt_backup(payload, "TestPassphrase123!")

        from app import vector_store
        with patch.object(vector_store.global_vector_store, "rebuild_from_db", side_effect=Exception("Disk full")):
            res = self._post("/api/backup/import", json={
                "backup": envelope,
                "passphrase": "TestPassphrase123!"
            })
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["sqlite_restored"])
            self.assertFalse(data["vector_synced"])
            self.assertIn("vector store synchronization failed", data["error"].lower())

        # Verify SQLite data was restored despite vector failure
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM tasks")
            tasks = [r[0] for r in cur.fetchall()]
            self.assertEqual(tasks, ["Confidential Security Audit"])

    # =========================================================================
    # 11. Legacy Plaintext Phase 3E Backward Compatibility
    # =========================================================================
    def test_legacy_plaintext_phase_3e_import_with_warning(self):
        """Requirements 24 & 25: Plaintext Phase 3E backup remains importable with warning."""
        payload = self._create_sample_payload()

        res = self._post("/api/backup/import", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["sqlite_restored"])
        self.assertFalse(data["is_encrypted"])
        self.assertIsNotNone(data["warning"])
        self.assertIn("unencrypted legacy backup", data["warning"].lower())

        # Verify SQLite data was restored
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT title FROM tasks")
            tasks = [r[0] for r in cur.fetchall()]
            self.assertEqual(tasks, ["Confidential Security Audit"])


if __name__ == "__main__":
    unittest.main()
