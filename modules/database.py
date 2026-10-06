# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .constants import BACKUP_DIR, PROGRAM_VERSION
from .datetime_helpers import local_timestamp_compact, utc_now_iso
from .logger import RunLogger

DATABASE_SCHEMA_VERSION = 2

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS storages (
    storage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_type TEXT NOT NULL,
    identity_key TEXT NOT NULL UNIQUE,
    canonical_root TEXT NOT NULL,
    volume_guid TEXT,
    volume_serial TEXT,
    filesystem TEXT,
    volume_label TEXT,
    remote_volume_serial INTEGER,
    remote_file_id TEXT,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_id INTEGER NOT NULL REFERENCES storages(storage_id),
    source_root_relative_path TEXT NOT NULL,
    source_root_key TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    user_path TEXT NOT NULL,
    display_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    superseded_by_source_id INTEGER REFERENCES sources(source_id),
    created_at_utc TEXT NOT NULL,
    last_scan_started_at_utc TEXT,
    last_scan_completed_at_utc TEXT,
    UNIQUE(storage_id, source_root_key)
);

CREATE TABLE IF NOT EXISTS source_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    alias_path TEXT NOT NULL,
    alias_key TEXT NOT NULL UNIQUE,
    alias_type TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 1,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(source_id),
    scan_type TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    status TEXT NOT NULL,
    files_seen INTEGER NOT NULL DEFAULT 0,
    files_excluded INTEGER NOT NULL DEFAULT 0,
    files_candidates INTEGER NOT NULL DEFAULT 0,
    files_reused INTEGER NOT NULL DEFAULT 0,
    files_probed INTEGER NOT NULL DEFAULT 0,
    files_failed INTEGER NOT NULL DEFAULT 0,
    files_not_video INTEGER NOT NULL DEFAULT 0,
    files_new INTEGER NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    missing_marked INTEGER NOT NULL DEFAULT 0,
    workers INTEGER,
    ffprobe_timeout_sec REAL,
    ffprobe_version TEXT,
    program_version TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_id INTEGER NOT NULL REFERENCES storages(storage_id),
    source_id INTEGER NOT NULL REFERENCES sources(source_id),
    storage_relative_path TEXT NOT NULL,
    storage_relative_key TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    extension TEXT,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    last_seen_scan_id INTEGER REFERENCES scan_runs(scan_id),
    presence_status TEXT NOT NULL,
    missing_since_utc TEXT,
    probe_status TEXT NOT NULL,
    metadata_state TEXT NOT NULL,
    last_probe_at_utc TEXT,
    last_probe_scan_id INTEGER REFERENCES scan_runs(scan_id),
    last_successful_probe_at_utc TEXT,
    format_name TEXT,
    container TEXT,
    duration_seconds REAL,
    overall_bitrate_bps INTEGER,
    encoder TEXT,
    video_stream_count INTEGER,
    audio_stream_count INTEGER,
    subtitle_stream_count INTEGER,
    chapter_count INTEGER,
    UNIQUE(storage_id, storage_relative_key)
);

CREATE TABLE IF NOT EXISTS video_streams (
    video_stream_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES media_files(file_id) ON DELETE CASCADE,
    stream_index INTEGER,
    is_primary INTEGER NOT NULL DEFAULT 0,
    codec_name TEXT,
    profile TEXT,
    level INTEGER,
    codec_tag TEXT,
    width INTEGER,
    height INTEGER,
    resolution_category TEXT,
    sample_aspect_ratio TEXT,
    display_aspect_ratio TEXT,
    pixel_format TEXT,
    bit_depth INTEGER,
    r_frame_rate_num INTEGER,
    r_frame_rate_den INTEGER,
    avg_frame_rate_num INTEGER,
    avg_frame_rate_den INTEGER,
    frame_rate REAL,
    frame_rate_mode TEXT,
    bitrate_bps INTEGER,
    estimated_stream_size_bytes INTEGER,
    field_order TEXT,
    color_range TEXT,
    color_space TEXT,
    color_transfer TEXT,
    color_primaries TEXT,
    hdr_type TEXT,
    hdr_metadata_status TEXT,
    mastering_display_primaries TEXT,
    mastering_display_white_point TEXT,
    mastering_min_luminance REAL,
    mastering_max_luminance REAL,
    max_cll INTEGER,
    max_fall INTEGER,
    dolby_vision_present INTEGER,
    dolby_vision_profile TEXT,
    hdr10_plus_present INTEGER
);

CREATE TABLE IF NOT EXISTS audio_streams (
    audio_stream_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES media_files(file_id) ON DELETE CASCADE,
    stream_index INTEGER,
    codec_name TEXT,
    profile TEXT,
    language TEXT,
    title TEXT,
    channels INTEGER,
    channel_layout TEXT,
    sample_rate_hz INTEGER,
    bitrate_bps INTEGER,
    bit_depth INTEGER,
    estimated_stream_size_bytes INTEGER,
    is_default INTEGER,
    is_forced INTEGER,
    is_original INTEGER,
    is_commentary INTEGER,
    is_hearing_impaired INTEGER,
    is_visual_impaired INTEGER,
    is_dub INTEGER
);

CREATE TABLE IF NOT EXISTS subtitle_streams (
    subtitle_stream_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES media_files(file_id) ON DELETE CASCADE,
    stream_index INTEGER,
    codec_name TEXT,
    subtitle_type TEXT,
    language TEXT,
    title TEXT,
    duration_seconds REAL,
    is_default INTEGER,
    is_forced INTEGER,
    is_hearing_impaired INTEGER
);

CREATE TABLE IF NOT EXISTS non_video_cache (
    non_video_id INTEGER PRIMARY KEY AUTOINCREMENT,
    storage_id INTEGER NOT NULL REFERENCES storages(storage_id),
    storage_relative_path TEXT NOT NULL,
    storage_relative_key TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    reason TEXT NOT NULL,
    first_seen_at_utc TEXT NOT NULL,
    last_seen_at_utc TEXT NOT NULL,
    UNIQUE(storage_id, storage_relative_key)
);

CREATE TABLE IF NOT EXISTS probe_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER REFERENCES media_files(file_id) ON DELETE SET NULL,
    scan_id INTEGER REFERENCES scan_runs(scan_id),
    attempt_number INTEGER,
    error_type TEXT,
    error_message TEXT,
    ffprobe_exit_code INTEGER,
    ffprobe_stderr TEXT,
    occurred_at_utc TEXT NOT NULL,
    was_timeout INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_storage ON sources(storage_id);
CREATE INDEX IF NOT EXISTS idx_media_source_presence ON media_files(source_id, presence_status);
CREATE INDEX IF NOT EXISTS idx_video_file ON video_streams(file_id);
CREATE INDEX IF NOT EXISTS idx_audio_file ON audio_streams(file_id);
CREATE INDEX IF NOT EXISTS idx_subtitle_file ON subtitle_streams(file_id);
CREATE INDEX IF NOT EXISTS idx_probe_errors_file ON probe_errors(file_id);
"""


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatabaseOpenResult:
    recovered_corruption: bool = False
    invalid_database_backup: Optional[Path] = None
    invalid_wal_backup: Optional[Path] = None
    invalid_shm_backup: Optional[Path] = None
    corruption_error: Optional[str] = None


def _is_confirmed_sqlite_corruption(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    corrupt_codes = {
        getattr(sqlite3, "SQLITE_CORRUPT", 11),
        getattr(sqlite3, "SQLITE_NOTADB", 26),
    }
    if code in corrupt_codes:
        return True
    message = str(exc).casefold()
    markers = (
        "database disk image is malformed",
        "file is not a database",
        "database schema is corrupt",
        "malformed database schema",
    )
    return any(marker in message for marker in markers)


class Database:
    def __init__(self, path: Path, logger: RunLogger) -> None:
        self.path = path
        self.logger = logger
        self.conn: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _open_once(self, *, new_db: bool) -> None:
        conn = self._connect()
        self.conn = conn
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(SCHEMA_SQL)
            conn.commit()
            schema = self.get_metadata_int("database_schema_version")
            if schema is None:
                self.set_metadata("database_schema_version", str(DATABASE_SCHEMA_VERSION))
                self.set_metadata("created_by_version", PROGRAM_VERSION)
                self.set_metadata("created_at_utc", utc_now_iso())
                schema = DATABASE_SCHEMA_VERSION
            if schema > DATABASE_SCHEMA_VERSION:
                raise DatabaseError(f"Database schema {schema} is newer than supported schema {DATABASE_SCHEMA_VERSION}.")
            if schema < DATABASE_SCHEMA_VERSION:
                self._migrate(schema, DATABASE_SCHEMA_VERSION)
            self.set_metadata("last_opened_by_version", PROGRAM_VERSION)
            self._mark_abandoned_runs_incomplete()
            if new_db:
                self.logger.info("DB", f"Created database: {self.path}")
            else:
                self.logger.info("DB", f"Opened database: {self.path}")
        except Exception:
            try:
                conn.close()
            finally:
                self.conn = None
            raise

    def _snapshot_existing_sidecars(self) -> dict[Path, Path]:
        """Protect pre-open WAL/SHM bytes so SQLite cannot erase recovery evidence."""
        snapshots: dict[Path, Path] = {}
        for source in (Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if not source.exists():
                continue
            guard = source.with_name(f".{source.name}.recovery_guard_{uuid.uuid4().hex}")
            try:
                shutil.copy2(source, guard)
            except Exception as exc:
                for existing_guard in snapshots.values():
                    try:
                        existing_guard.unlink(missing_ok=True)
                    except Exception:
                        pass
                raise DatabaseError(f"Could not protect SQLite sidecar before database open: {source}: {exc}") from exc
            snapshots[source] = guard
        return snapshots

    @staticmethod
    def _cleanup_sidecar_snapshots(snapshots: dict[Path, Path]) -> None:
        for guard in snapshots.values():
            try:
                guard.unlink(missing_ok=True)
            except Exception:
                pass

    def _preserve_corrupt_database(
        self,
        sidecar_snapshots: Optional[dict[Path, Path]] = None,
    ) -> tuple[Path, Optional[Path], Optional[Path]]:
        snapshots = sidecar_snapshots or {}
        stamp = local_timestamp_compact()
        backup_db = self.path.with_name(f"{self.path.stem}.invalid_{stamp}{self.path.suffix}")
        source_wal = Path(str(self.path) + "-wal")
        source_shm = Path(str(self.path) + "-shm")

        wal_input = snapshots.get(source_wal, source_wal if source_wal.exists() else None)
        shm_input = snapshots.get(source_shm, source_shm if source_shm.exists() else None)
        backup_wal = Path(str(backup_db) + "-wal") if wal_input is not None else None
        backup_shm = Path(str(backup_db) + "-shm") if shm_input is not None else None

        # Each move records the normal-path destination to use if rollback is needed.
        moves: list[tuple[Path, Path, Path]] = [(self.path, backup_db, self.path)]
        if wal_input is not None and backup_wal is not None:
            moves.append((wal_input, backup_wal, source_wal))
        if shm_input is not None and backup_shm is not None:
            moves.append((shm_input, backup_shm, source_shm))

        completed: list[tuple[Path, Path, Path]] = []
        try:
            for source, target, rollback_source in moves:
                os.replace(source, target)
                completed.append((source, target, rollback_source))

            # When a guard snapshot supplied the preserved bytes, any sidecar SQLite
            # left at the live DB name is stale and must not be consumed by the new DB.
            for live_source in (source_wal, source_shm):
                if live_source in snapshots and live_source.exists():
                    live_source.unlink()
        except Exception as exc:
            rollback_errors: list[str] = []
            for _source, target, rollback_source in reversed(completed):
                try:
                    if target.exists():
                        if rollback_source.exists():
                            rollback_source.unlink()
                        os.replace(target, rollback_source)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{target} -> {rollback_source}: {rollback_exc}")
            self._cleanup_sidecar_snapshots(snapshots)
            detail = f"Could not preserve corrupt database; recovery aborted: {exc}"
            if rollback_errors:
                detail += "; rollback errors: " + " | ".join(rollback_errors)
            raise DatabaseError(detail) from exc

        self._cleanup_sidecar_snapshots(snapshots)
        return backup_db, backup_wal, backup_shm

    def open(self) -> DatabaseOpenResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_db = not self.path.exists()
        sidecar_snapshots: dict[Path, Path] = {}
        if not new_db:
            sidecar_snapshots = self._snapshot_existing_sidecars()
        try:
            self._open_once(new_db=new_db)
            self._cleanup_sidecar_snapshots(sidecar_snapshots)
            return DatabaseOpenResult()
        except Exception as exc:
            if new_db or not _is_confirmed_sqlite_corruption(exc):
                self._cleanup_sidecar_snapshots(sidecar_snapshots)
                raise

            # Confirmed SQLite corruption only. Access/permission/lock failures never
            # move the existing database. The failed connection is already closed.
            self.logger.error("DB", f"Confirmed database corruption detected: {exc}")
            backup_db, backup_wal, backup_shm = self._preserve_corrupt_database(sidecar_snapshots)
            self.logger.warning("DB", f"Preserved invalid database: {backup_db}")
            if backup_wal is not None:
                self.logger.warning("DB", f"Preserved invalid WAL: {backup_wal}")
            if backup_shm is not None:
                self.logger.warning("DB", f"Preserved invalid SHM: {backup_shm}")

            try:
                self._open_once(new_db=True)
            except Exception as replacement_exc:
                raise DatabaseError(
                    f"Corrupt database was preserved at {backup_db}, but replacement database creation failed: "
                    f"{replacement_exc}"
                ) from replacement_exc

            self.logger.warning("DB", f"Created replacement database after corruption recovery: {self.path}")
            return DatabaseOpenResult(
                recovered_corruption=True,
                invalid_database_backup=backup_db,
                invalid_wal_backup=backup_wal,
                invalid_shm_backup=backup_shm,
                corruption_error=str(exc),
            )

    def _migrate(self, old_version: int, new_version: int) -> None:
        assert self.conn is not None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup_path = BACKUP_DIR / f"MediaCatalog_before_schema_{new_version}_{local_timestamp_compact()}.db"
        partial = backup_path.with_name(backup_path.name + ".partial")
        self.logger.info("DB", f"Creating pre-migration backup: {backup_path}")
        try:
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            dest = sqlite3.connect(str(partial))
            try:
                self.conn.backup(dest)
                dest.commit()
            finally:
                dest.close()
            os.replace(partial, backup_path)
        except Exception as exc:
            try:
                partial.unlink(missing_ok=True)
            except Exception:
                pass
            raise DatabaseError(f"Database backup failed; migration aborted: {exc}") from exc

        if old_version == 1 and new_version == 2:
            obsolete_indexes = (
                "idx_media_probe",
                "idx_media_last_seen_scan",
                "idx_video_codec",
                "idx_video_resolution",
                "idx_video_hdr",
                "idx_video_bit_depth",
                "idx_audio_codec",
                "idx_audio_language",
                "idx_subtitle_codec",
                "idx_subtitle_language",
                "idx_probe_errors_scan",
                "idx_non_video_identity",
            )
            with self.conn:
                for index_name in obsolete_indexes:
                    self.conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')
                self.conn.execute(
                    "INSERT INTO metadata(key,value) VALUES('database_schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(new_version),),
                )
            self.logger.info("DB", "Database schema migrated from 1 to 2; obsolete indexes removed.")
            return

        raise DatabaseError(f"No migration path implemented from schema {old_version} to {new_version}.")

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.commit()
                self.conn.close()
            finally:
                self.conn = None

    def read_connection(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.execute("PRAGMA query_only=ON")
        return conn

    def snapshot_connection(self) -> sqlite3.Connection:
        return self._connect()

    def writer_connection(self) -> sqlite3.Connection:
        conn = self._connect()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def set_metadata(self, key: str, value: Optional[str]) -> None:
        assert self.conn is not None
        with self.conn:
            self.conn.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_metadata(self, key: str) -> Optional[str]:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def get_metadata_int(self, key: str) -> Optional[int]:
        value = self.get_metadata(key)
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    def _mark_abandoned_runs_incomplete(self) -> None:
        assert self.conn is not None
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                "UPDATE scan_runs SET status='INCOMPLETE', finished_at_utc=COALESCE(finished_at_utc, ?) WHERE status='RUNNING'",
                (now,),
            )
        if cur.rowcount:
            self.logger.warning("DB", f"Marked {cur.rowcount} abandoned RUNNING scan(s) as INCOMPLETE.")

    def list_sources(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        assert self.conn is not None
        sql = """
            SELECT s.*, st.identity_type, st.identity_key, st.canonical_root,
                   st.volume_guid, st.volume_serial, st.filesystem, st.volume_label,
                   st.remote_volume_serial, st.remote_file_id
            FROM sources s
            JOIN storages st ON st.storage_id=s.storage_id
        """
        params: tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE s.status='ACTIVE'"
        sql += " ORDER BY s.display_path COLLATE NOCASE"
        return list(self.conn.execute(sql, params).fetchall())

    def get_source(self, source_id: int) -> Optional[sqlite3.Row]:
        assert self.conn is not None
        return self.conn.execute(
            """
            SELECT s.*, st.identity_type, st.identity_key, st.canonical_root,
                   st.volume_guid, st.volume_serial, st.filesystem, st.volume_label,
                   st.remote_volume_serial, st.remote_file_id
            FROM sources s JOIN storages st ON st.storage_id=s.storage_id
            WHERE s.source_id=?
            """,
            (source_id,),
        ).fetchone()

    def get_source_aliases(self, source_id: int, *, verified_only: bool = False) -> list[sqlite3.Row]:
        assert self.conn is not None
        sql = "SELECT * FROM source_aliases WHERE source_id=?"
        params: list[Any] = [source_id]
        if verified_only:
            sql += " AND verified=1"
        sql += " ORDER BY alias_id"
        return list(self.conn.execute(sql, params).fetchall())

    def start_scan_run(self, source_id: int, scan_type: str, workers: int, timeout: float, ffprobe_version: Optional[str]) -> int:
        assert self.conn is not None
        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO scan_runs(source_id,scan_type,started_at_utc,status,workers,ffprobe_timeout_sec,ffprobe_version,program_version)
                VALUES(?,?,?,'RUNNING',?,?,?,?)
                """,
                (source_id, scan_type, now, workers, timeout, ffprobe_version, PROGRAM_VERSION),
            )
            self.conn.execute("UPDATE sources SET last_scan_started_at_utc=? WHERE source_id=?", (now, source_id))
        return int(cur.lastrowid)

    def finish_scan_run(self, scan_id: int, source_id: int, status: str, counters: "ScanCounters", missing_marked: int = 0) -> None:
        assert self.conn is not None
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE scan_runs SET
                    finished_at_utc=?, status=?, files_seen=?, files_excluded=?, files_candidates=?,
                    files_reused=?, files_probed=?, files_failed=?, files_not_video=?,
                    files_new=?, files_changed=?, missing_marked=?
                WHERE scan_id=?
                """,
                (
                    now, status, counters.files_seen, counters.files_excluded, counters.files_candidates,
                    counters.reused, counters.probed, counters.failed, counters.not_video,
                    counters.new, counters.changed, missing_marked, scan_id,
                ),
            )
            if status == "COMPLETED":
                self.conn.execute("UPDATE sources SET last_scan_completed_at_utc=? WHERE source_id=?", (now, source_id))

    def mark_scan_incomplete_best_effort(self, scan_id: int) -> None:
        if self.conn is None:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE scan_runs SET status='INCOMPLETE', finished_at_utc=? WHERE scan_id=? AND status='RUNNING'",
                    (utc_now_iso(), scan_id),
                )
        except Exception:
            pass

    def latest_scan(self, source_id: int) -> Optional[sqlite3.Row]:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM scan_runs WHERE source_id=? ORDER BY scan_id DESC LIMIT 1",
            (source_id,),
        ).fetchone()

    def latest_completed_scan(self, source_id: int) -> Optional[sqlite3.Row]:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM scan_runs WHERE source_id=? AND status='COMPLETED' ORDER BY scan_id DESC LIMIT 1",
            (source_id,),
        ).fetchone()
