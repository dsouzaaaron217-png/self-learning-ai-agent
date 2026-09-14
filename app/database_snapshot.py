"""
Cognito Phase 3D: SQLite Snapshot & Recovery Hardening
Implements reliable, consistent local SQLite database snapshots and recovery
using Python's standard-library sqlite3.Connection.backup() Online Backup API.

Guarantees:
- 100% offline, on-device operation with zero external dependencies
- SQLite remains the authoritative source of truth
- Fully compatible with WAL mode (transparently reads uncheckpointed WAL pages)
- Atomicity: temporary files are used and validated before publishing
- Safety: pre-restore validated safety snapshot taken before restoring active DB
- Integration: triggers Phase 3B/3C ensure_valid_index() to synchronize vector store
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Any, Dict, Set

from app import config

# Core Cognito tables that must exist for a snapshot to be valid
REQUIRED_TABLES: Set[str] = {
    "tasks",
    "notes",
    "memories",
    "memory_decision_logs",
    "feedback_events",
    "settings",
}


class SnapshotError(Exception):
    """Base exception for database snapshot and recovery operations."""
    pass


class SnapshotValidationError(SnapshotError):
    """Exception raised when snapshot integrity or schema validation fails."""
    pass


class SnapshotRestoreError(SnapshotError):
    """Exception raised when snapshot restoration fails."""
    pass


class RestoreResult:
    """
    Structured result object for restore operations.
    Evaluates to True if and only if both SQLite and vector store synchronized successfully.
    Supports dictionary access and attribute access.
    """
    def __init__(
        self,
        success: bool,
        sqlite_restored: bool,
        vector_synced: bool,
        safety_snapshot_path: Optional[Path] = None,
        error: Optional[str] = None
    ):
        self.success = bool(success)
        self.sqlite_restored = bool(sqlite_restored)
        self.vector_synced = bool(vector_synced)
        self.safety_snapshot_path = Path(safety_snapshot_path) if safety_snapshot_path else None
        self.error = error

    def __bool__(self) -> bool:
        return self.success

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "sqlite_restored": self.sqlite_restored,
            "vector_synced": self.vector_synced,
            "safety_snapshot_path": str(self.safety_snapshot_path) if self.safety_snapshot_path else None,
            "error": self.error,
        }

    def __repr__(self) -> str:
        return (
            f"<RestoreResult success={self.success} "
            f"sqlite_restored={self.sqlite_restored} "
            f"vector_synced={self.vector_synced} "
            f"error={self.error}>"
        )


def _cleanup_temp_artifacts(temp_path: Path):
    """Safely cleans up a temporary database file and any related SQLite journal/WAL files."""
    for ext in ("", "-wal", "-shm", "-journal"):
        p = Path(f"{temp_path}{ext}")
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def validate_snapshot(snapshot_path: Path) -> Tuple[bool, str]:
    """
    Validates a database snapshot file:
    - Verifies file exists and is non-empty
    - Opens read-only using SQLite URI mode
    - Executes PRAGMA quick_check and PRAGMA integrity_check
    - Verifies all required Cognito tables exist
    Returns:
        (True, "Snapshot integrity valid") if completely sound
        (False, error_reason) if invalid or corrupt
    """
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.is_file():
        return False, f"Snapshot file does not exist: {snapshot_path}"

    try:
        if snapshot_path.stat().st_size == 0:
            return False, "Snapshot file is empty (0 bytes)"
    except OSError as e:
        return False, f"Cannot access snapshot file stats: {e}"

    conn = None
    try:
        # Open in read-only mode using SQLite URI format
        resolved = snapshot_path.resolve().as_posix()
        uri = f"file:{resolved}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        cursor = conn.cursor()

        # 1. Quick check (fast structural verification)
        cursor.execute("PRAGMA quick_check")
        qc_rows = cursor.fetchall()
        if not qc_rows or qc_rows[0][0] != "ok":
            return False, f"PRAGMA quick_check failed: {qc_rows}"

        # 2. Integrity check (exhaustive B-tree and index cross-check)
        cursor.execute("PRAGMA integrity_check")
        ic_rows = cursor.fetchall()
        if not ic_rows or ic_rows[0][0] != "ok":
            return False, f"PRAGMA integrity_check failed: {ic_rows}"

        # 3. Schema verification (core tables exist)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {r[0] for r in cursor.fetchall()}
        missing_tables = REQUIRED_TABLES - existing_tables
        if missing_tables:
            return False, f"Snapshot missing required tables: {sorted(missing_tables)}"

        return True, "Snapshot integrity valid"

    except sqlite3.DatabaseError as e:
        return False, f"SQLite database validation error: {e}"
    except Exception as e:
        return False, f"Unexpected validation error: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def create_snapshot(
    target_path: Optional[Path] = None,
    source_path: Optional[Path] = None
) -> Path:
    """
    Creates a consistent, point-in-time snapshot of the SQLite database using
    sqlite3.Connection.backup().

    - In WAL mode, transparently reads uncheckpointed WAL frames without requiring
      a prior manual checkpoint or file copy.
    - Writes to a temporary file in the same directory, validates via PRAGMA integrity_check,
      and atomically replaces into final destination via os.replace().
    - Cleans up temporary files on any failure, leaving the source database completely untouched.

    Returns:
        Path to the validated, finalized snapshot file.
    Raises:
        SnapshotError: If snapshot creation or validation fails.
    """
    source_db = Path(source_path or config.DB_PATH).resolve()
    if not source_db.is_file():
        raise SnapshotError(f"Source database file does not exist: {source_db}")

    if target_path is None:
        backup_dir = Path(config.BACKUP_DIR).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:6]
        final_path = backup_dir / f"cognito_snapshot_{timestamp}_{unique_suffix}.db"
    else:
        final_path = Path(target_path).resolve()
        final_path.parent.mkdir(parents=True, exist_ok=True)

    # Unique temporary path in the SAME directory to allow atomic os.replace()
    temp_path = final_path.parent / f"{final_path.stem}_{uuid.uuid4().hex[:8]}.tmp.db"

    source_conn = None
    target_conn = None

    try:
        # Open source connection
        source_conn = sqlite3.connect(str(source_db), timeout=15.0)
        source_conn.execute("PRAGMA foreign_keys = ON")

        # Open destination temporary database connection
        target_conn = sqlite3.connect(str(temp_path), timeout=15.0)

        # Execute online backup via SQLite C API binding
        # pages=-1 backs up all pages atomically in the backup operation
        source_conn.backup(target_conn, pages=-1)

        # Ensure all pages are committed and connections are closed before validation
        target_conn.close()
        target_conn = None
        source_conn.close()
        source_conn = None

        # Validate the generated snapshot file
        is_valid, reason = validate_snapshot(temp_path)
        if not is_valid:
            raise SnapshotValidationError(f"Generated snapshot failed integrity check: {reason}")

        # Atomically publish to final path
        os.replace(temp_path, final_path)
        return final_path

    except Exception as e:
        _cleanup_temp_artifacts(temp_path)
        if isinstance(e, SnapshotError):
            raise
        raise SnapshotError(f"Snapshot creation failed: {e}") from e
    finally:
        if target_conn is not None:
            try:
                target_conn.close()
            except Exception:
                pass
        if source_conn is not None:
            try:
                source_conn.close()
            except Exception:
                pass


def restore_snapshot(
    snapshot_path: Path,
    target_db_path: Optional[Path] = None
) -> RestoreResult:
    """
    Safely restores the SQLite database from a validated snapshot file:
    1. Pre-validates the source snapshot file (aborts if invalid without touching active DB).
    2. Creates a validated emergency safety snapshot of current active DB.
    3. Restores data into the active database connection using sqlite3.backup().
    4. Runs post-restore validation on the restored database.
    5. Reconciles the vector store via existing Phase 3B/3C ensure_valid_index().

    If vector synchronization fails, SQLite is NOT corrupted or rolled back,
    and a RestoreResult with success=False, sqlite_restored=True, vector_synced=False is returned.

    Returns:
        RestoreResult object (evaluates to True if both SQLite and vector sync succeeded).
    """
    snapshot_path = Path(snapshot_path).resolve()

    # Step 1: Pre-validate candidate snapshot file
    is_valid, reason = validate_snapshot(snapshot_path)
    if not is_valid:
        return RestoreResult(
            success=False,
            sqlite_restored=False,
            vector_synced=False,
            error=f"Candidate snapshot failed pre-validation: {reason}"
        )

    target_db = Path(target_db_path or config.DB_PATH).resolve()
    safety_snapshot_path: Optional[Path] = None

    # Step 2: Create safety snapshot of current active DB if it exists and has content
    if target_db.is_file() and target_db.stat().st_size > 0:
        backup_dir = Path(config.BACKUP_DIR).resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_suffix = uuid.uuid4().hex[:6]
        safety_path = backup_dir / f"pre_restore_safety_{timestamp}_{unique_suffix}.db"
        try:
            safety_snapshot_path = create_snapshot(
                target_path=safety_path,
                source_path=target_db
            )
        except Exception as e:
            return RestoreResult(
                success=False,
                sqlite_restored=False,
                vector_synced=False,
                error=f"Failed to create pre-restore safety snapshot of active DB: {e}"
            )

    # Step 3: Restore using SQLite Backup API (snapshot -> active DB)
    snap_conn = None
    active_conn = None
    try:
        snap_uri = f"file:{snapshot_path.as_posix()}?mode=ro"
        snap_conn = sqlite3.connect(snap_uri, uri=True, timeout=15.0)

        active_conn = sqlite3.connect(str(target_db), timeout=15.0)
        active_conn.execute("PRAGMA foreign_keys = ON")
        active_conn.execute("PRAGMA journal_mode = WAL")

        # Online restore via SQLite backup API
        snap_conn.backup(active_conn, pages=-1)

        active_conn.close()
        active_conn = None
        snap_conn.close()
        snap_conn = None

    except Exception as e:
        if active_conn is not None:
            try:
                active_conn.close()
            except Exception:
                pass
        if snap_conn is not None:
            try:
                snap_conn.close()
            except Exception:
                pass

        return RestoreResult(
            success=False,
            sqlite_restored=False,
            vector_synced=False,
            safety_snapshot_path=safety_snapshot_path,
            error=f"SQLite backup restore failed: {e}"
        )

    # Step 4: Post-restore validation on active database
    is_post_valid, post_reason = validate_snapshot(target_db)
    if not is_post_valid:
        # Attempt emergency rollback to safety snapshot if available
        if safety_snapshot_path and safety_snapshot_path.is_file():
            try:
                rollback_src = sqlite3.connect(f"file:{safety_snapshot_path.as_posix()}?mode=ro", uri=True)
                rollback_tgt = sqlite3.connect(str(target_db), timeout=15.0)
                rollback_src.backup(rollback_tgt, pages=-1)
                rollback_tgt.close()
                rollback_src.close()
            except Exception as rb_err:
                print(f"[DatabaseSnapshot] Critical: failed to rollback to safety snapshot: {rb_err}")

        return RestoreResult(
            success=False,
            sqlite_restored=False,
            vector_synced=False,
            safety_snapshot_path=safety_snapshot_path,
            error=f"Restored database failed post-validation: {post_reason}"
        )

    # Step 5: Vector Store Synchronization via Phase 3B/3C ensure_valid_index()
    vector_synced = True
    try:
        from app.vector_store import global_vector_store
        # Authoritative SQLite recovery & synchronization
        global_vector_store.ensure_valid_index()
    except Exception as vs_err:
        vector_synced = False
        print(f"[DatabaseSnapshot] Warning: vector store synchronization failed post-restore: {vs_err}. SQLite remains authoritative.")

    overall_success = vector_synced

    return RestoreResult(
        success=overall_success,
        sqlite_restored=True,
        vector_synced=vector_synced,
        safety_snapshot_path=safety_snapshot_path,
        error=None if overall_success else "SQLite restored successfully, but vector store synchronization failed."
    )
