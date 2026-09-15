"""
Backup Confidentiality Hardening (Phase 3F)
Provides authenticated encryption for Cognito logical backups using AES-256-GCM
and key derivation via hashlib.scrypt.

Security guarantees:
1. AES-256-GCM authenticated encryption (confidentiality + authenticity + integrity).
2. Key derived with scrypt from a user-supplied passphrase, fresh random salt per export.
3. Fresh random 12-byte nonce per encryption operation (NIST SP 800-38D).
4. Envelope metadata (format, version, KDF params, cipher params) cryptographically bound
   as Additional Authenticated Data (AAD) to prevent envelope header tampering.
5. Strict bounds on scrypt parameters to prevent resource-exhaustion / DoS attacks.
6. Safe, generic error messages on decryption/authentication failure.
7. Plaintext data is never written to disk as a temporary artifact.
"""

import base64
import json
import os
import secrets
from typing import Dict, Any, Tuple
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
import hashlib

from app.database import utc_now_iso

# Format and Algorithm Constants
ENCRYPTED_BACKUP_FORMAT = "cognito_backup_encrypted"
ENCRYPTED_BACKUP_VERSION = "1.0.0"
SUPPORTED_APPLICATION_NAME = "Cognito Offline Agent"
CIPHER_ALGORITHM = "AES-256-GCM"
KDF_ALGORITHM = "scrypt"

# Default Cryptographic Parameters
DEFAULT_SCRYPT_N = 16384  # 2^14 CPU/memory cost
DEFAULT_SCRYPT_R = 8      # Block size
DEFAULT_SCRYPT_P = 1      # Parallelization
SALT_BYTES = 32           # 256-bit random salt
NONCE_BYTES = 12          # 96-bit random nonce for GCM
KEY_BYTES = 32            # 256-bit key for AES-256
MAX_MEM_BYTES = 64 * 1024 * 1024  # 64 MB max memory for scrypt

# Validation and Resource-Exhaustion Bounds
MIN_PASSPHRASE_LEN = 8
MAX_PASSPHRASE_LEN = 256
MIN_SCRYPT_N = 1024       # 2^10
MAX_SCRYPT_N = 65536      # 2^16 (prevents memory exhaustion)
MIN_SCRYPT_R = 1
MAX_SCRYPT_R = 16
MIN_SCRYPT_P = 1
MAX_SCRYPT_P = 16
MIN_SALT_BYTES = 16
MAX_SALT_BYTES = 64
EXPECTED_NONCE_BYTES = 12
GCM_TAG_BYTES = 16
MAX_ENCRYPTED_PAYLOAD_SIZE = 25 * 1024 * 1024  # 25MB safety cap


def is_encrypted_backup(payload: Any) -> bool:
    """Returns True if the payload matches the encrypted backup envelope identifier."""
    return isinstance(payload, dict) and payload.get("format") == ENCRYPTED_BACKUP_FORMAT


def validate_passphrase(passphrase: Any, is_export: bool = False) -> str:
    """
    Validates the user-supplied backup passphrase.
    For export: requires non-empty string, length between MIN_PASSPHRASE_LEN and MAX_PASSPHRASE_LEN.
    For import: requires non-empty string, length up to MAX_PASSPHRASE_LEN.
    """
    if passphrase is None:
        raise ValueError("Backup passphrase is required.")
    if not isinstance(passphrase, str):
        raise ValueError("Backup passphrase must be a string.")
    if not passphrase:
        raise ValueError("Backup passphrase is required.")
    if len(passphrase) > MAX_PASSPHRASE_LEN:
        raise ValueError(f"Backup passphrase exceeds maximum allowed length of {MAX_PASSPHRASE_LEN} characters.")
    if is_export and len(passphrase) < MIN_PASSPHRASE_LEN:
        raise ValueError(f"Backup passphrase must be at least {MIN_PASSPHRASE_LEN} characters.")
    return passphrase


def derive_key(passphrase: str, salt: bytes, n: int = DEFAULT_SCRYPT_N, r: int = DEFAULT_SCRYPT_R, p: int = DEFAULT_SCRYPT_P) -> bytes:
    """
    Derives a 32-byte AES key from the passphrase using hashlib.scrypt.
    Zero persistent caching of the derived key.
    """
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=MAX_MEM_BYTES,
        dklen=KEY_BYTES
    )


def compute_envelope_aad(envelope_meta: Dict[str, Any]) -> bytes:
    """
    Computes a canonical byte representation of the envelope metadata to use
    as Additional Authenticated Data (AAD) for AES-GCM.
    Binds format, version, application, KDF parameters, and cipher parameters.
    """
    aad_structure = {
        "application": envelope_meta.get("application", SUPPORTED_APPLICATION_NAME),
        "cipher": {
            "algorithm": envelope_meta.get("cipher", {}).get("algorithm"),
            "nonce": envelope_meta.get("cipher", {}).get("nonce")
        },
        "format": envelope_meta.get("format"),
        "kdf": {
            "algorithm": envelope_meta.get("kdf", {}).get("algorithm"),
            "n": envelope_meta.get("kdf", {}).get("n"),
            "p": envelope_meta.get("kdf", {}).get("p"),
            "r": envelope_meta.get("kdf", {}).get("r"),
            "salt": envelope_meta.get("kdf", {}).get("salt")
        },
        "version": envelope_meta.get("version")
    }
    return json.dumps(aad_structure, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_encrypted_envelope(envelope: Any) -> Tuple[bytes, bytes, int, int, int, bytes]:
    """
    Validates an encrypted backup envelope against schema and resource bounds.
    Returns decoded (salt, nonce, n, r, p, ciphertext_with_tag).
    Raises ValueError on invalid schema, unsupported version/cipher, or out-of-bound parameters.
    """
    if not isinstance(envelope, dict):
        raise ValueError("Encrypted backup payload must be a JSON object.")

    fmt = envelope.get("format")
    if fmt != ENCRYPTED_BACKUP_FORMAT:
        raise ValueError(f"Invalid backup envelope format '{fmt}'. Expected '{ENCRYPTED_BACKUP_FORMAT}'.")

    version = envelope.get("version")
    if not isinstance(version, str) or not version.startswith("1."):
        raise ValueError(f"Unsupported encrypted backup version '{version}'. Expected version 1.x.")

    app = envelope.get("application")
    if app and app != SUPPORTED_APPLICATION_NAME:
        raise ValueError(f"Unsupported application '{app}'. Expected '{SUPPORTED_APPLICATION_NAME}'.")

    # Validate KDF block
    kdf = envelope.get("kdf")
    if not isinstance(kdf, dict):
        raise ValueError("Malformed envelope: 'kdf' metadata object is missing.")

    kdf_algo = kdf.get("algorithm")
    if kdf_algo != KDF_ALGORITHM:
        raise ValueError(f"Unsupported KDF algorithm '{kdf_algo}'. Expected '{KDF_ALGORITHM}'.")

    n = kdf.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < MIN_SCRYPT_N or n > MAX_SCRYPT_N or (n & (n - 1)) != 0:
        raise ValueError(f"KDF parameter 'n' ({n}) is outside acceptable bounds [{MIN_SCRYPT_N}, {MAX_SCRYPT_N}].")

    r = kdf.get("r")
    if not isinstance(r, int) or isinstance(r, bool) or r < MIN_SCRYPT_R or r > MAX_SCRYPT_R:
        raise ValueError(f"KDF parameter 'r' ({r}) is outside acceptable bounds [{MIN_SCRYPT_R}, {MAX_SCRYPT_R}].")

    p = kdf.get("p")
    if not isinstance(p, int) or isinstance(p, bool) or p < MIN_SCRYPT_P or p > MAX_SCRYPT_P:
        raise ValueError(f"KDF parameter 'p' ({p}) is outside acceptable bounds [{MIN_SCRYPT_P}, {MAX_SCRYPT_P}].")

    salt_b64 = kdf.get("salt")
    if not isinstance(salt_b64, str) or not salt_b64.strip():
        raise ValueError("Malformed envelope: 'kdf.salt' is required.")
    try:
        salt = base64.b64decode(salt_b64.strip(), validate=True)
    except Exception:
        raise ValueError("Malformed envelope: 'kdf.salt' contains invalid base64.")
    if len(salt) < MIN_SALT_BYTES or len(salt) > MAX_SALT_BYTES:
        raise ValueError(f"KDF salt length ({len(salt)} bytes) is outside bounds [{MIN_SALT_BYTES}, {MAX_SALT_BYTES}].")

    # Validate Cipher block
    cipher = envelope.get("cipher")
    if not isinstance(cipher, dict):
        raise ValueError("Malformed envelope: 'cipher' metadata object is missing.")

    cipher_algo = cipher.get("algorithm")
    if cipher_algo != CIPHER_ALGORITHM:
        raise ValueError(f"Unsupported cipher algorithm '{cipher_algo}'. Expected '{CIPHER_ALGORITHM}'.")

    nonce_b64 = cipher.get("nonce")
    if not isinstance(nonce_b64, str) or not nonce_b64.strip():
        raise ValueError("Malformed envelope: 'cipher.nonce' is required.")
    try:
        nonce = base64.b64decode(nonce_b64.strip(), validate=True)
    except Exception:
        raise ValueError("Malformed envelope: 'cipher.nonce' contains invalid base64.")
    if len(nonce) != EXPECTED_NONCE_BYTES:
        raise ValueError(f"Cipher nonce length ({len(nonce)} bytes) is invalid. Expected {EXPECTED_NONCE_BYTES} bytes.")

    # Validate Ciphertext
    ct_b64 = envelope.get("ciphertext")
    if not isinstance(ct_b64, str) or not ct_b64.strip():
        raise ValueError("Malformed envelope: 'ciphertext' is required.")
    if len(ct_b64) > MAX_ENCRYPTED_PAYLOAD_SIZE:
        raise ValueError("Encrypted backup payload exceeds maximum allowable size limit.")
    try:
        ciphertext = base64.b64decode(ct_b64.strip(), validate=True)
    except Exception:
        raise ValueError("Malformed envelope: 'ciphertext' contains invalid base64.")
    if len(ciphertext) < GCM_TAG_BYTES:
        raise ValueError("Malformed envelope: 'ciphertext' is shorter than minimum authentication tag length.")

    return salt, nonce, n, r, p, ciphertext


def encrypt_backup(payload: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """
    Encrypts a logical backup payload dictionary using AES-256-GCM.

    Steps:
    1. Validates passphrase length and type.
    2. Generates fresh random 32-byte salt and 12-byte nonce.
    3. Derives 32-byte AES key via scrypt.
    4. Prepares envelope structure and AAD.
    5. Encrypts JSON serialized payload with GCM tag.
    6. Returns complete encrypted envelope dictionary.
    """
    clean_passphrase = validate_passphrase(passphrase, is_export=True)

    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)

    key = derive_key(clean_passphrase, salt, DEFAULT_SCRYPT_N, DEFAULT_SCRYPT_R, DEFAULT_SCRYPT_P)

    salt_b64 = base64.b64encode(salt).decode("ascii")
    nonce_b64 = base64.b64encode(nonce).decode("ascii")

    envelope_meta = {
        "format": ENCRYPTED_BACKUP_FORMAT,
        "version": ENCRYPTED_BACKUP_VERSION,
        "application": SUPPORTED_APPLICATION_NAME,
        "kdf": {
            "algorithm": KDF_ALGORITHM,
            "n": DEFAULT_SCRYPT_N,
            "r": DEFAULT_SCRYPT_R,
            "p": DEFAULT_SCRYPT_P,
            "salt": salt_b64
        },
        "cipher": {
            "algorithm": CIPHER_ALGORITHM,
            "nonce": nonce_b64
        }
    }

    aad_bytes = compute_envelope_aad(envelope_meta)
    plaintext_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    aesgcm = AESGCM(key)
    ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext_bytes, aad_bytes)

    ciphertext_b64 = base64.b64encode(ciphertext_with_tag).decode("ascii")

    envelope = {
        "format": ENCRYPTED_BACKUP_FORMAT,
        "version": ENCRYPTED_BACKUP_VERSION,
        "application": SUPPORTED_APPLICATION_NAME,
        "created_at": utc_now_iso(),
        "kdf": envelope_meta["kdf"],
        "cipher": envelope_meta["cipher"],
        "ciphertext": ciphertext_b64
    }

    return envelope


def decrypt_backup(envelope: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
    """
    Decrypts an encrypted backup envelope using AES-256-GCM.

    Steps:
    1. Validates passphrase presence, type, and length bounds.
    2. Validates envelope schema, version, cipher, and KDF bounds.
    3. Derives 32-byte key via scrypt.
    4. Computes AAD and verifies GCM authentication tag.
    5. Rejects any tag/ciphertext/metadata mismatch with a safe, generic error.
    6. Parses decrypted JSON bytes into memory and returns the payload.
    """
    clean_passphrase = validate_passphrase(passphrase, is_export=False)
    salt, nonce, n, r, p, ciphertext_with_tag = validate_encrypted_envelope(envelope)

    key = derive_key(clean_passphrase, salt, n, r, p)
    aad_bytes = compute_envelope_aad(envelope)

    aesgcm = AESGCM(key)
    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_with_tag, aad_bytes)
    except (InvalidTag, Exception):
        raise ValueError("Invalid backup passphrase or corrupted encrypted backup.")

    try:
        payload = json.loads(plaintext_bytes.decode("utf-8"))
    except Exception:
        raise ValueError("Invalid backup passphrase or corrupted encrypted backup.")

    if not isinstance(payload, dict):
        raise ValueError("Invalid backup passphrase or corrupted encrypted backup.")

    return payload
