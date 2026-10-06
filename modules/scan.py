# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import os
import queue
import signal
import sqlite3
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .config import ConfigManager
from .constants import LOGS_DIR, PROGRAM_VERSION
from .database import Database, DatabaseError
from .datetime_helpers import local_timestamp_compact, utc_now_iso
from .logger import RunLogger, excluded_path_for_log, format_elapsed
from .platform_api import PlatformAdapter
from .probe import ProbeService, SLOW_PROBE_LOG_SECONDS


class ScanProgressSink(Protocol):
    def begin(self) -> None: ...
    def end(self) -> None: ...
    def enumeration(self, source_path: str, counters: "ScanCounters", elapsed: float, *, force: bool = False) -> None: ...
    def enumeration_complete(self, source_path: str, counters: "ScanCounters", elapsed: float) -> None: ...
    def probing(self, source_path: str, counters: "ScanCounters", total_probe: int, workers: int, elapsed: float, *, force: bool = False) -> None: ...

class ExcludedDiagnosticWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._temp_dir = Path(tempfile.mkdtemp(prefix=".mediacatalog_excluded_", dir=str(self.path.parent)))
        self._groups: dict[tuple[str, str], Path] = {}

    @staticmethod
    def _safe_group_name(index: int) -> str:
        return f"group_{index:06d}.txt"

    def record(self, source: str, path: str, extension: str) -> None:
        extension = extension or "<none>"
        key = (str(source), str(extension))
        with self._lock:
            spool = self._groups.get(key)
            if spool is None:
                spool = self._temp_dir / self._safe_group_name(len(self._groups) + 1)
                self._groups[key] = spool
            with open(spool, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(str(path) + "\n")

    def close(self) -> None:
        try:
            with self._lock:
                with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(f"MediaCatalog v{PROGRAM_VERSION} - Excluded Files Diagnostic\n")
                    handle.write(f"Created: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n\n")
                    current_source: Optional[str] = None
                    ordered = sorted(
                        self._groups.items(),
                        key=lambda item: (item[0][0].casefold(), item[0][1].casefold()),
                    )
                    for (source, extension), spool in ordered:
                        if source != current_source:
                            if current_source is not None:
                                handle.write("\n")
                            handle.write(f"SOURCE: {source}\n\n")
                            current_source = source
                        handle.write(f"[{extension}]\n")
                        try:
                            with open(spool, "r", encoding="utf-8", errors="replace") as source_handle:
                                shutil_copyfileobj(source_handle, handle)
                        except FileNotFoundError:
                            pass
                        handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        finally:
            try:
                for spool in self._temp_dir.iterdir():
                    try:
                        spool.unlink()
                    except Exception:
                        pass
                self._temp_dir.rmdir()
            except Exception:
                pass


def shutil_copyfileobj(source: Any, target: Any, length: int = 1024 * 1024) -> None:
    while True:
        chunk = source.read(length)
        if not chunk:
            return
        target.write(chunk)

@dataclass
class ScanCounters:
    files_seen: int = 0
    files_excluded: int = 0
    files_candidates: int = 0
    reused: int = 0
    need_probe: int = 0
    probed: int = 0
    successful: int = 0
    failed: int = 0
    not_video: int = 0
    new: int = 0
    changed: int = 0
    skipped: int = 0
    cached_not_video: int = 0

@dataclass
class FileJob:
    scan_id: int
    source_id: int
    storage_id: int
    storage_relative_path: str
    storage_relative_key: str
    canonical_path: str
    file_name: str
    extension: str
    size_bytes: Optional[int]
    mtime_ns: Optional[int]
    existing_file_id: Optional[int]
    existing_probe_status: Optional[str]
    existing_metadata_state: Optional[str]
    existing_size_bytes: Optional[int]
    existing_mtime_ns: Optional[int]
    is_new: bool
    is_changed: bool
    pre_error: Optional[dict[str, Any]] = None

@dataclass
class DBResult:
    kind: str
    job: FileJob
    metadata: Optional[dict[str, Any]] = None
    error: Optional[dict[str, Any]] = None
    attempt_number: Optional[int] = None
    attempt_errors: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class EnumerationResult:
    source: sqlite3.Row
    access_path: str
    scan_id: int
    safe_complete: bool = True
    source_available: bool = True
    interrupted: bool = False
    jobs: list[FileJob] = field(default_factory=list)
    reuse_results: list[FileJob] = field(default_factory=list)
    cached_not_video_results: list[FileJob] = field(default_factory=list)
    counters: ScanCounters = field(default_factory=ScanCounters)
    excluded_by_extension: Counter[str] = field(default_factory=Counter)
    enumeration_elapsed: float = 0.0
    probing_elapsed: float = 0.0

@dataclass
class SourceScanResult:
    status: str
    counters: ScanCounters
    missing_marked: int = 0
    error: Optional[BaseException] = None
    reason: str = "NORMAL"


@dataclass
class ScanOperationResult:
    status: str
    counters: ScanCounters
    missing_marked: int
    elapsed: float
    source_statuses: list[str] = field(default_factory=list)
    error: Optional[BaseException] = None
    excluded_diagnostic_path: Optional[Path] = None
    operation_id: str = ""
    reason: str = "NORMAL"
    ui_wait: float = 0.0
    wall_elapsed: float = 0.0

class ScanStopController:
    def __init__(
        self,
        logger: RunLogger,
        kill_active_processes: Callable[[], None],
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.logger = logger
        self.kill_active_processes = kill_active_processes
        self.notify = notify
        self.stop_event = threading.Event()
        self.emergency_event = threading.Event()
        self._press_count = 0
        self._lock = threading.Lock()
        self._installed = False
        self._old_handler: Any = None

    def reset(self) -> None:
        self.stop_event.clear()
        self.emergency_event.clear()
        with self._lock:
            self._press_count = 0

    def install(self) -> None:
        if self._installed:
            return
        self._old_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self._handle)
        self._installed = True

    def restore(self) -> None:
        if self._installed:
            try:
                signal.signal(signal.SIGINT, self._old_handler)
            except Exception:
                pass
        self._installed = False

    def _handle(self, signum: int, frame: Any) -> None:
        with self._lock:
            self._press_count += 1
            count = self._press_count
        if count == 1:
            self.stop_event.set()
            if self.notify is not None:
                try:
                    self.notify("GRACEFUL")
                except Exception:
                    pass
            self.logger.warning("SCAN", "First Ctrl+C received; graceful stop requested.")
            self.logger.info("UI", "Interrupt requested type=GRACEFUL_STOP")
            return

        self.emergency_event.set()
        self.kill_active_processes()
        self.logger.critical("SCAN", "Second Ctrl+C received; emergency process termination requested.")
        self.logger.info("UI", "Interrupt requested type=EMERGENCY_TERMINATE")
        if self.notify is not None:
            try:
                self.notify("EMERGENCY")
            except Exception:
                pass
        os._exit(130)


def file_extension(name: str) -> str:
    return Path(name).suffix.casefold()

def enumerate_source(
    source: sqlite3.Row,
    access_path: str,
    scan_id: int,
    *,
    force_reprobe: bool,
    renderer: ScanProgressSink,
    excluded_writer: Optional[ExcludedDiagnosticWriter],
    db: Database,
    config: ConfigManager,
    logger: RunLogger,
    platform: PlatformAdapter,
    stop_controller: ScanStopController,
) -> EnumerationResult:
    result = EnumerationResult(source=source, access_path=access_path, scan_id=scan_id)
    start = time.perf_counter()
    excluded_exts = config.excluded_extensions
    excluded_dirs = config.excluded_directories
    follow_reparse = config.follow_reparse_points

    read_conn = db.read_connection()
    file_lookup = read_conn.cursor()
    non_video_lookup = read_conn.cursor()

    def on_walk_error(exc: OSError) -> None:
        result.safe_complete = False
        logger.warning("SCAN", f"Enumeration error source={access_path!r}: {exc}")

    try:
        if not os.path.isdir(access_path):
            result.source_available = False
            result.safe_complete = False
            return result

        for root, dirs, files in os.walk(access_path, topdown=True, followlinks=follow_reparse, onerror=on_walk_error):
            if stop_controller.stop_event.is_set():
                result.interrupted = True
                result.safe_complete = False
                break

            filtered_dirs: list[str] = []
            for dirname in dirs:
                full_dir = os.path.join(root, dirname)
                if dirname.casefold() in excluded_dirs:
                    logger.debug("SCAN", f"Excluded directory: {full_dir}")
                    continue
                if not follow_reparse and platform.is_directory_link(full_dir):
                    logger.debug("SCAN", f"Skipped reparse-point directory: {full_dir}")
                    continue
                filtered_dirs.append(dirname)
            dirs[:] = filtered_dirs

            for name in files:
                if stop_controller.stop_event.is_set():
                    result.interrupted = True
                    result.safe_complete = False
                    break
                result.counters.files_seen += 1
                full_path = os.path.join(root, name)
                ext = file_extension(name)
                if ext in excluded_exts:
                    result.counters.files_excluded += 1
                    result.excluded_by_extension[ext or "<none>"] += 1
                    if excluded_writer is not None:
                        excluded_writer.record(access_path, full_path, ext or "<none>")
                    renderer.enumeration(access_path, result.counters, time.perf_counter() - start)
                    continue

                result.counters.files_candidates += 1
                rel_from_source = os.path.relpath(full_path, access_path)
                storage_rel = platform.storage_relative_path(str(source["source_root_relative_path"] or ""), rel_from_source)
                storage_key = platform.relative_key(storage_rel)

                existing = file_lookup.execute(
                    "SELECT * FROM media_files WHERE storage_id=? AND storage_relative_key=?",
                    (source["storage_id"], storage_key),
                ).fetchone()
                cached_non_video = non_video_lookup.execute(
                    "SELECT * FROM non_video_cache WHERE storage_id=? AND storage_relative_key=?",
                    (source["storage_id"], storage_key),
                ).fetchone()

                try:
                    st = os.stat(full_path)
                    size_bytes = int(st.st_size)
                    mtime_ns = int(st.st_mtime_ns)
                    pre_error = None
                except FileNotFoundError:
                    result.counters.skipped += 1
                    logger.info("SCAN", f"File disappeared during enumeration: {full_path}")
                    renderer.enumeration(access_path, result.counters, time.perf_counter() - start)
                    continue
                except PermissionError as exc:
                    size_bytes = None
                    mtime_ns = None
                    pre_error = {
                        "type": "ACCESS_DENIED",
                        "message": str(exc),
                        "exit_code": None,
                        "stderr": None,
                        "was_timeout": 0,
                    }
                except OSError as exc:
                    size_bytes = None
                    mtime_ns = None
                    pre_error = {
                        "type": "READ_ERROR",
                        "message": str(exc),
                        "exit_code": None,
                        "stderr": None,
                        "was_timeout": 0,
                    }

                unchanged_existing = bool(
                    existing
                    and existing["size_bytes"] == size_bytes
                    and existing["mtime_ns"] == mtime_ns
                )
                unchanged_non_video = bool(
                    cached_non_video
                    and cached_non_video["size_bytes"] == size_bytes
                    and cached_non_video["mtime_ns"] == mtime_ns
                )

                if not force_reprobe and pre_error is None and unchanged_existing and existing["probe_status"] == "OK" and existing["metadata_state"] == "CURRENT":
                    job = FileJob(
                        scan_id=scan_id,
                        source_id=int(source["source_id"]),
                        storage_id=int(source["storage_id"]),
                        storage_relative_path=storage_rel,
                        storage_relative_key=storage_key,
                        canonical_path=full_path,
                        file_name=name,
                        extension=ext,
                        size_bytes=size_bytes,
                        mtime_ns=mtime_ns,
                        existing_file_id=int(existing["file_id"]),
                        existing_probe_status=existing["probe_status"],
                        existing_metadata_state=existing["metadata_state"],
                        existing_size_bytes=existing["size_bytes"],
                        existing_mtime_ns=existing["mtime_ns"],
                        is_new=False,
                        is_changed=False,
                    )
                    result.reuse_results.append(job)
                    result.counters.reused += 1
                    renderer.enumeration(access_path, result.counters, time.perf_counter() - start)
                    continue

                if not force_reprobe and pre_error is None and unchanged_non_video:
                    job = FileJob(
                        scan_id=scan_id,
                        source_id=int(source["source_id"]),
                        storage_id=int(source["storage_id"]),
                        storage_relative_path=storage_rel,
                        storage_relative_key=storage_key,
                        canonical_path=full_path,
                        file_name=name,
                        extension=ext,
                        size_bytes=size_bytes,
                        mtime_ns=mtime_ns,
                        existing_file_id=int(existing["file_id"]) if existing else None,
                        existing_probe_status=existing["probe_status"] if existing else None,
                        existing_metadata_state=existing["metadata_state"] if existing else None,
                        existing_size_bytes=existing["size_bytes"] if existing else None,
                        existing_mtime_ns=existing["mtime_ns"] if existing else None,
                        is_new=existing is None,
                        is_changed=False,
                    )
                    result.cached_not_video_results.append(job)
                    result.counters.cached_not_video += 1
                    renderer.enumeration(access_path, result.counters, time.perf_counter() - start)
                    continue

                # A path already known through the persistent NOT_VIDEO cache is
                # not "new" merely because it is not a media_files row.
                is_new = existing is None and cached_non_video is None
                is_changed = bool(
                    (existing is not None and not unchanged_existing)
                    or (existing is None and cached_non_video is not None and not unchanged_non_video)
                )
                if is_new:
                    result.counters.new += 1
                if is_changed:
                    result.counters.changed += 1

                job = FileJob(
                    scan_id=scan_id,
                    source_id=int(source["source_id"]),
                    storage_id=int(source["storage_id"]),
                    storage_relative_path=storage_rel,
                    storage_relative_key=storage_key,
                    canonical_path=full_path,
                    file_name=name,
                    extension=ext,
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    existing_file_id=int(existing["file_id"]) if existing else None,
                    existing_probe_status=existing["probe_status"] if existing else None,
                    existing_metadata_state=existing["metadata_state"] if existing else None,
                    existing_size_bytes=existing["size_bytes"] if existing else None,
                    existing_mtime_ns=existing["mtime_ns"] if existing else None,
                    is_new=is_new,
                    is_changed=is_changed,
                    pre_error=pre_error,
                )
                result.jobs.append(job)
                result.counters.need_probe += 1
                renderer.enumeration(access_path, result.counters, time.perf_counter() - start)

            if stop_controller.stop_event.is_set():
                break

        # A source can disappear between os.walk iterations without an obvious callback.
        if not os.path.isdir(access_path):
            result.safe_complete = False
            result.source_available = False
            logger.warning("SCAN", f"Source became unavailable during enumeration: {access_path}")

        renderer.enumeration(access_path, result.counters, time.perf_counter() - start, force=True)
        return result
    except Exception as exc:
        result.safe_complete = False
        logger.error("SCAN", f"Unexpected enumeration failure for {access_path}: {exc}", exc_info=True)
        return result
    finally:
        result.enumeration_elapsed = time.perf_counter() - start
        for extension, count in sorted(result.excluded_by_extension.items()):
            logger.debug(
                "SCAN",
                f"Excluded by extension source_id={source['source_id']} extension={extension} count={count}",
            )
        logger.info(
            "SCAN",
            f"Enumeration finished source_id={source['source_id']} files_seen={result.counters.files_seen} "
            f"candidates={result.counters.files_candidates} excluded={result.counters.files_excluded} "
            f"reused={result.counters.reused} cached_not_video={result.counters.cached_not_video} "
            f"need_probe={result.counters.need_probe} "
            f"safe_complete={result.safe_complete} elapsed={format_elapsed(result.enumeration_elapsed)}",
        )
        read_conn.close()

class ScanDBWriter(threading.Thread):
    def __init__(
        self,
        result_queue: "queue.Queue[Optional[DBResult]]",
        counters: ScanCounters,
        counters_lock: threading.Lock,
        fatal_event: threading.Event,
        db: Database,
        logger: RunLogger,
    ) -> None:
        super().__init__(name="MediaCatalog-DBWriter", daemon=True)
        self.db = db
        self.logger = logger
        self.result_queue = result_queue
        self.counters = counters
        self.counters_lock = counters_lock
        self.fatal_event = fatal_event
        self.exception: Optional[BaseException] = None

    def run(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self.db.writer_connection()
            while True:
                item = self.result_queue.get()
                try:
                    if item is None:
                        return
                    self._commit_result(conn, item)
                finally:
                    self.result_queue.task_done()
        except BaseException as exc:
            self.exception = exc
            self.fatal_event.set()
            self.logger.critical("DB", f"DB writer failure: {exc}", exc_info=True)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _get_existing_file(self, conn: sqlite3.Connection, job: FileJob) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM media_files WHERE storage_id=? AND storage_relative_key=?",
            (job.storage_id, job.storage_relative_key),
        ).fetchone()

    def _upsert_failed_file(self, conn: sqlite3.Connection, job: FileJob, error: dict[str, Any], attempt_number: Optional[int]) -> int:
        now = utc_now_iso()
        existing = self._get_existing_file(conn, job)
        if existing:
            metadata_state = "STALE"
            conn.execute(
                """
                UPDATE media_files SET
                    source_id=?, storage_relative_path=?, canonical_path=?, file_name=?, extension=?,
                    size_bytes=?, mtime_ns=?, last_seen_at_utc=?, last_seen_scan_id=?,
                    presence_status='ACTIVE', missing_since_utc=NULL,
                    probe_status='FAILED', metadata_state=?, last_probe_at_utc=?, last_probe_scan_id=?
                WHERE file_id=?
                """,
                (
                    job.source_id, job.storage_relative_path, job.canonical_path, job.file_name, job.extension,
                    job.size_bytes, job.mtime_ns, now, job.scan_id,
                    metadata_state, now, job.scan_id, existing["file_id"],
                ),
            )
            file_id = int(existing["file_id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO media_files(
                    storage_id,source_id,storage_relative_path,storage_relative_key,canonical_path,file_name,extension,
                    size_bytes,mtime_ns,first_seen_at_utc,last_seen_at_utc,last_seen_scan_id,
                    presence_status,probe_status,metadata_state,last_probe_at_utc,last_probe_scan_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE','FAILED','STALE',?,?)
                """,
                (
                    job.storage_id, job.source_id, job.storage_relative_path, job.storage_relative_key,
                    job.canonical_path, job.file_name, job.extension, job.size_bytes, job.mtime_ns,
                    now, now, job.scan_id, now, job.scan_id,
                ),
            )
            file_id = int(cur.lastrowid)
        return file_id

    def _insert_probe_error(self, conn: sqlite3.Connection, file_id: int, job: FileJob, error: dict[str, Any], attempt_number: Optional[int]) -> None:
        conn.execute(
            """
            INSERT INTO probe_errors(file_id,scan_id,attempt_number,error_type,error_message,ffprobe_exit_code,ffprobe_stderr,occurred_at_utc,was_timeout)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                file_id, job.scan_id, attempt_number, error.get("type"), error.get("message"),
                error.get("exit_code"), error.get("stderr"), error.get("occurred_at_utc") or utc_now_iso(),
                int(bool(error.get("was_timeout"))),
            ),
        )

    def _replace_streams(self, conn: sqlite3.Connection, file_id: int, metadata: dict[str, Any]) -> None:
        conn.execute("DELETE FROM video_streams WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM audio_streams WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM subtitle_streams WHERE file_id=?", (file_id,))

        video_columns = [
            "stream_index", "is_primary", "codec_name", "profile", "level", "codec_tag", "width", "height",
            "resolution_category", "sample_aspect_ratio", "display_aspect_ratio", "pixel_format", "bit_depth",
            "r_frame_rate_num", "r_frame_rate_den", "avg_frame_rate_num", "avg_frame_rate_den", "frame_rate",
            "frame_rate_mode", "bitrate_bps", "estimated_stream_size_bytes", "field_order", "color_range", "color_space",
            "color_transfer", "color_primaries", "hdr_type", "hdr_metadata_status", "mastering_display_primaries",
            "mastering_display_white_point", "mastering_min_luminance", "mastering_max_luminance", "max_cll", "max_fall",
            "dolby_vision_present", "dolby_vision_profile", "hdr10_plus_present",
        ]
        for item in metadata.get("video_streams", []):
            vals = [item.get(col) for col in video_columns]
            placeholders = ",".join("?" for _ in range(len(video_columns) + 1))
            conn.execute(
                f"INSERT INTO video_streams(file_id,{','.join(video_columns)}) VALUES({placeholders})",
                [file_id] + vals,
            )

        audio_columns = [
            "stream_index", "codec_name", "profile", "language", "title", "channels", "channel_layout",
            "sample_rate_hz", "bitrate_bps", "bit_depth", "estimated_stream_size_bytes", "is_default", "is_forced",
            "is_original", "is_commentary", "is_hearing_impaired", "is_visual_impaired", "is_dub",
        ]
        for item in metadata.get("audio_streams", []):
            placeholders = ",".join("?" for _ in range(len(audio_columns) + 1))
            conn.execute(
                f"INSERT INTO audio_streams(file_id,{','.join(audio_columns)}) VALUES({placeholders})",
                [file_id] + [item.get(col) for col in audio_columns],
            )

        subtitle_columns = [
            "stream_index", "codec_name", "subtitle_type", "language", "title", "duration_seconds",
            "is_default", "is_forced", "is_hearing_impaired",
        ]
        for item in metadata.get("subtitle_streams", []):
            placeholders = ",".join("?" for _ in range(len(subtitle_columns) + 1))
            conn.execute(
                f"INSERT INTO subtitle_streams(file_id,{','.join(subtitle_columns)}) VALUES({placeholders})",
                [file_id] + [item.get(col) for col in subtitle_columns],
            )

    def _commit_result(self, conn: sqlite3.Connection, result: DBResult) -> None:
        job = result.job
        with conn:
            if result.kind in {"DEFERRED", "SKIPPED", "FAILED", "NOT_VIDEO", "SUCCESS"}:
                with self.counters_lock:
                    self.counters.probed += 1

            if result.kind == "DEFERRED":
                # File still exists but changed while it was being probed. Mark it
                # seen/ACTIVE so completed reconciliation cannot falsely mark it
                # MISSING. Existing metadata is retained but is no longer current.
                existing = self._get_existing_file(conn, job)
                if existing:
                    conn.execute(
                        """
                        UPDATE media_files SET
                            source_id=?, storage_relative_path=?, canonical_path=?, file_name=?, extension=?,
                            size_bytes=?, mtime_ns=?, last_seen_at_utc=?, last_seen_scan_id=?,
                            presence_status='ACTIVE', missing_since_utc=NULL, metadata_state='STALE'
                        WHERE file_id=?
                        """,
                        (
                            job.source_id, job.storage_relative_path, job.canonical_path, job.file_name, job.extension,
                            job.size_bytes, job.mtime_ns, utc_now_iso(), job.scan_id, existing["file_id"],
                        ),
                    )
                with self.counters_lock:
                    self.counters.skipped += 1
                return

            if result.kind == "SKIPPED":
                with self.counters_lock:
                    self.counters.skipped += 1
                return

            if result.kind == "FAILED":
                assert result.error is not None
                file_id = self._upsert_failed_file(conn, job, result.error, result.attempt_number)

                # A failed current probe supersedes any older NOT_VIDEO decision
                # for this exact file identity. Keeping that cache row could make
                # the next normal scan skip a required retry (especially after a
                # Force Reprobe of an unchanged NOT_VIDEO candidate).
                conn.execute(
                    "DELETE FROM non_video_cache WHERE storage_id=? AND storage_relative_key=?",
                    (job.storage_id, job.storage_relative_key),
                )

                history = list(result.attempt_errors)
                final_signature = (
                    result.error.get("type"), result.error.get("message"), result.error.get("exit_code"),
                    result.error.get("stderr"), int(bool(result.error.get("was_timeout"))),
                )
                history_signatures = {
                    (item.get("type"), item.get("message"), item.get("exit_code"), item.get("stderr"), int(bool(item.get("was_timeout"))))
                    for item in history
                }
                if final_signature not in history_signatures:
                    item = dict(result.error)
                    item["_attempt_number"] = result.attempt_number
                    history.append(item)
                if not history:
                    history = [dict(result.error, _attempt_number=result.attempt_number)]
                for item in history:
                    self._insert_probe_error(conn, file_id, job, item, item.get("_attempt_number"))
                with self.counters_lock:
                    self.counters.failed += 1
                return

            if result.kind == "NOT_VIDEO":
                # A path that is no longer video is removed from the video catalog and remembered in the NOT_VIDEO cache.
                existing = self._get_existing_file(conn, job)
                if existing:
                    conn.execute("DELETE FROM media_files WHERE file_id=?", (existing["file_id"],))
                now = utc_now_iso()
                conn.execute(
                    """
                    INSERT INTO non_video_cache(storage_id,storage_relative_path,storage_relative_key,canonical_path,size_bytes,mtime_ns,reason,first_seen_at_utc,last_seen_at_utc)
                    VALUES(?,?,?,?,?,?,'NOT_VIDEO',?,?)
                    ON CONFLICT(storage_id,storage_relative_key) DO UPDATE SET
                        storage_relative_path=excluded.storage_relative_path,
                        canonical_path=excluded.canonical_path,
                        size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns,
                        reason='NOT_VIDEO',
                        last_seen_at_utc=excluded.last_seen_at_utc
                    """,
                    (
                        job.storage_id, job.storage_relative_path, job.storage_relative_key, job.canonical_path,
                        job.size_bytes, job.mtime_ns, now, now,
                    ),
                )
                with self.counters_lock:
                    self.counters.not_video += 1
                return

            if result.kind != "SUCCESS" or result.metadata is None:
                raise DatabaseError(f"Unknown DB result kind: {result.kind}")

            metadata = result.metadata
            now = utc_now_iso()
            existing = self._get_existing_file(conn, job)
            if existing:
                file_id = int(existing["file_id"])
                conn.execute(
                    """
                    UPDATE media_files SET
                        source_id=?, storage_relative_path=?, canonical_path=?, file_name=?, extension=?,
                        size_bytes=?, mtime_ns=?, last_seen_at_utc=?, last_seen_scan_id=?,
                        presence_status='ACTIVE', missing_since_utc=NULL,
                        probe_status='OK', metadata_state='CURRENT', last_probe_at_utc=?, last_probe_scan_id=?,
                        last_successful_probe_at_utc=?, format_name=?, container=?, duration_seconds=?, overall_bitrate_bps=?, encoder=?,
                        video_stream_count=?, audio_stream_count=?, subtitle_stream_count=?, chapter_count=?
                    WHERE file_id=?
                    """,
                    (
                        job.source_id, job.storage_relative_path, job.canonical_path, job.file_name, job.extension,
                        job.size_bytes, job.mtime_ns, now, job.scan_id, now, job.scan_id, now,
                        metadata.get("format_name"), metadata.get("container"), metadata.get("duration_seconds"),
                        metadata.get("overall_bitrate_bps"), metadata.get("encoder"), metadata.get("video_stream_count"),
                        metadata.get("audio_stream_count"), metadata.get("subtitle_stream_count"), metadata.get("chapter_count"),
                        file_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO media_files(
                        storage_id,source_id,storage_relative_path,storage_relative_key,canonical_path,file_name,extension,
                        size_bytes,mtime_ns,first_seen_at_utc,last_seen_at_utc,last_seen_scan_id,
                        presence_status,probe_status,metadata_state,last_probe_at_utc,last_probe_scan_id,last_successful_probe_at_utc,
                        format_name,container,duration_seconds,overall_bitrate_bps,encoder,
                        video_stream_count,audio_stream_count,subtitle_stream_count,chapter_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE','OK','CURRENT',?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job.storage_id, job.source_id, job.storage_relative_path, job.storage_relative_key,
                        job.canonical_path, job.file_name, job.extension, job.size_bytes, job.mtime_ns,
                        now, now, job.scan_id, now, job.scan_id, now,
                        metadata.get("format_name"), metadata.get("container"), metadata.get("duration_seconds"),
                        metadata.get("overall_bitrate_bps"), metadata.get("encoder"), metadata.get("video_stream_count"),
                        metadata.get("audio_stream_count"), metadata.get("subtitle_stream_count"), metadata.get("chapter_count"),
                    ),
                )
                file_id = int(cur.lastrowid)

            conn.execute(
                "DELETE FROM non_video_cache WHERE storage_id=? AND storage_relative_key=?",
                (job.storage_id, job.storage_relative_key),
            )
            self._replace_streams(conn, file_id, metadata)
            for item in result.attempt_errors:
                self._insert_probe_error(conn, file_id, job, item, item.get("_attempt_number"))
            with self.counters_lock:
                self.counters.successful += 1

class ProbeWorker(threading.Thread):
    def __init__(
        self,
        worker_id: int,
        work_queue: "queue.Queue[Optional[FileJob]]",
        result_queue: "queue.Queue[Optional[DBResult]]",
        fatal_event: threading.Event,
        source_abort_event: threading.Event,
        source_root: str,
        config: ConfigManager,
        logger: RunLogger,
        probe_service: ProbeService,
        stop_controller: ScanStopController,
    ) -> None:
        super().__init__(name=f"MediaCatalog-Probe-{worker_id}", daemon=True)
        self.config = config
        self.logger = logger
        self.probe_service = probe_service
        self.stop_controller = stop_controller
        self.worker_id = worker_id
        self.work_queue = work_queue
        self.result_queue = result_queue
        self.fatal_event = fatal_event
        self.source_abort_event = source_abort_event
        self.source_root = source_root

    def _source_still_available(self) -> bool:
        if self.source_abort_event.is_set():
            return False
        try:
            available = os.path.isdir(self.source_root)
        except Exception:
            available = False
        if not available:
            if not self.source_abort_event.is_set():
                self.logger.warning("SCAN", f"Source became unavailable during probing: {self.source_root}")
            self.source_abort_event.set()
            return False
        return True

    def _put_result(self, result: DBResult) -> None:
        while not self.fatal_event.is_set() and not self.stop_controller.emergency_event.is_set():
            try:
                self.result_queue.put(result, timeout=0.2)
                return
            except queue.Full:
                continue

    def run(self) -> None:
        while True:
            try:
                job = self.work_queue.get(timeout=0.2)
            except queue.Empty:
                if self.fatal_event.is_set() or self.source_abort_event.is_set() or self.stop_controller.emergency_event.is_set():
                    return
                continue
            try:
                if job is None:
                    return
                if self.stop_controller.stop_event.is_set() or self.fatal_event.is_set() or self.source_abort_event.is_set():
                    continue
                self._process(job)
            finally:
                self.work_queue.task_done()

    def _process(self, job: FileJob) -> None:
        if job.pre_error is not None:
            self._put_result(DBResult(kind="FAILED", job=job, error=job.pre_error, attempt_number=1))
            return

        attempts = self.config.retries + 1
        last_error: Optional[dict[str, Any]] = None
        last_attempt = 0
        attempt_errors: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            if self.stop_controller.stop_event.is_set() or self.fatal_event.is_set() or self.source_abort_event.is_set():
                return
            last_attempt = attempt
            if attempt > 1:
                self.logger.debug(
                    "FFPROBE",
                    f"Retrying attempt={attempt}/{attempts} path={job.canonical_path!r}",
                )
            probe_started = time.perf_counter()
            data, error = self.probe_service.ffprobe_json(job.canonical_path, self.config.timeout)
            probe_elapsed = time.perf_counter() - probe_started
            if data is not None:
                if probe_elapsed >= SLOW_PROBE_LOG_SECONDS:
                    self.logger.info(
                        "FFPROBE",
                        f"Slow probe path={job.canonical_path!r} attempt={attempt}/{attempts} "
                        f"elapsed={format_elapsed(probe_elapsed)}",
                    )
                try:
                    metadata, is_video = self.probe_service.parse_probe_data(
                        data,
                        job.canonical_path,
                        job.size_bytes,
                        run_hdr=True,
                        timeout=self.config.timeout,
                    )
                except Exception as exc:
                    error = {
                        "type": "INTERNAL_ERROR",
                        "message": f"Probe parsing failed: {exc}",
                        "exit_code": None,
                        "stderr": traceback.format_exc(),
                        "was_timeout": 0,
                    }
                    last_error = error
                    item = dict(error)
                    item["_attempt_number"] = attempt
                    item["occurred_at_utc"] = utc_now_iso()
                    attempt_errors.append(item)
                    self.logger.error("FFPROBE", f"Parsing exception for {job.canonical_path}: {exc}", exc_info=True)
                    continue

                if not is_video or metadata is None:
                    self._put_result(DBResult(kind="NOT_VIDEO", job=job, attempt_number=attempt))
                    self.logger.debug("FFPROBE", f"NOT_VIDEO: {job.canonical_path}")
                    return

                try:
                    st = os.stat(job.canonical_path)
                    current_size = int(st.st_size)
                    current_mtime = int(st.st_mtime_ns)
                except FileNotFoundError:
                    if not self._source_still_available():
                        return
                    self._put_result(DBResult(kind="SKIPPED", job=job))
                    self.logger.info("SCAN", f"File disappeared before commit: {job.canonical_path}")
                    return
                except PermissionError as exc:
                    if not self._source_still_available():
                        return
                    last_error = {
                        "type": "ACCESS_DENIED",
                        "message": str(exc),
                        "exit_code": None,
                        "stderr": None,
                        "was_timeout": 0,
                    }
                    item = dict(last_error)
                    item["_attempt_number"] = attempt
                    item["occurred_at_utc"] = utc_now_iso()
                    attempt_errors.append(item)
                    continue
                except OSError as exc:
                    if not self._source_still_available():
                        return
                    last_error = {
                        "type": "READ_ERROR",
                        "message": str(exc),
                        "exit_code": None,
                        "stderr": None,
                        "was_timeout": 0,
                    }
                    item = dict(last_error)
                    item["_attempt_number"] = attempt
                    item["occurred_at_utc"] = utc_now_iso()
                    attempt_errors.append(item)
                    continue

                if current_size != job.size_bytes or current_mtime != job.mtime_ns:
                    self.logger.info("SCAN", f"File changed during probe; retrying once: {job.canonical_path}")
                    job.size_bytes = current_size
                    job.mtime_ns = current_mtime

                    # One stabilization retry is independent of configured FFprobe retries.
                    retry_started = time.perf_counter()
                    data2, error2 = self.probe_service.ffprobe_json(job.canonical_path, self.config.timeout)
                    retry_elapsed = time.perf_counter() - retry_started
                    if data2 is None:
                        self.logger.warning(
                            "FFPROBE",
                            f"Changed-file stabilization failed path={job.canonical_path!r} "
                            f"exit_code={error2.get('exit_code') if error2 else None} "
                            f"timeout={bool(error2.get('was_timeout')) if error2 else False} "
                            f"elapsed={format_elapsed(retry_elapsed)}",
                        )
                        if error2 is not None and not self._source_still_available():
                            return
                        retry_error = error2 or {
                            "type": "UNKNOWN",
                            "message": "Changed-file stabilization probe failed for an unknown reason.",
                            "exit_code": None,
                            "stderr": None,
                            "was_timeout": 0,
                        }
                        item = dict(retry_error)
                        item["_attempt_number"] = attempt
                        item["occurred_at_utc"] = utc_now_iso()
                        attempt_errors.append(item)
                        self._put_result(DBResult(
                            kind="FAILED", job=job, error=retry_error, attempt_number=attempt, attempt_errors=attempt_errors
                        ))
                        return

                    try:
                        metadata2, is_video2 = self.probe_service.parse_probe_data(
                            data2, job.canonical_path, current_size, run_hdr=True, timeout=self.config.timeout
                        )
                    except Exception as exc:
                        retry_error = {
                            "type": "INTERNAL_ERROR",
                            "message": f"Changed-file probe parsing failed: {exc}",
                            "exit_code": None,
                            "stderr": traceback.format_exc(),
                            "was_timeout": 0,
                        }
                        item = dict(retry_error)
                        item["_attempt_number"] = attempt
                        item["occurred_at_utc"] = utc_now_iso()
                        attempt_errors.append(item)
                        self._put_result(DBResult(
                            kind="FAILED", job=job, error=retry_error, attempt_number=attempt, attempt_errors=attempt_errors
                        ))
                        return

                    try:
                        st2 = os.stat(job.canonical_path)
                        size2 = int(st2.st_size)
                        mtime2 = int(st2.st_mtime_ns)
                    except FileNotFoundError:
                        if not self._source_still_available():
                            return
                        self._put_result(DBResult(kind="SKIPPED", job=job))
                        self.logger.info("SCAN", f"File disappeared during changed-file retry: {job.canonical_path}")
                        return
                    except PermissionError as exc:
                        if not self._source_still_available():
                            return
                        retry_error = {
                            "type": "ACCESS_DENIED", "message": str(exc), "exit_code": None,
                            "stderr": None, "was_timeout": 0,
                        }
                        self._put_result(DBResult(kind="FAILED", job=job, error=retry_error, attempt_number=attempt, attempt_errors=attempt_errors))
                        return
                    except OSError as exc:
                        if not self._source_still_available():
                            return
                        retry_error = {
                            "type": "READ_ERROR", "message": str(exc), "exit_code": None,
                            "stderr": None, "was_timeout": 0,
                        }
                        self._put_result(DBResult(kind="FAILED", job=job, error=retry_error, attempt_number=attempt, attempt_errors=attempt_errors))
                        return

                    if size2 != current_size or mtime2 != current_mtime:
                        job.size_bytes = size2
                        job.mtime_ns = mtime2
                        self._put_result(DBResult(kind="DEFERRED", job=job))
                        self.logger.warning("SCAN", f"File continued changing; deferred until next scan: {job.canonical_path}")
                        return

                    if is_video2 and metadata2 is not None:
                        self._put_result(DBResult(
                            kind="SUCCESS", job=job, metadata=metadata2, attempt_number=attempt, attempt_errors=attempt_errors
                        ))
                    else:
                        self._put_result(DBResult(kind="NOT_VIDEO", job=job, attempt_number=attempt))
                    return

                self._put_result(DBResult(kind="SUCCESS", job=job, metadata=metadata, attempt_number=attempt, attempt_errors=attempt_errors))
                return

            # A probe error caused by loss of the entire source is source-level,
            # not a per-file FAILED record. Stop assigning work for this source.
            if error is not None and not self._source_still_available():
                return

            last_error = error
            if error is not None:
                item = dict(error)
                item["_attempt_number"] = attempt
                item["occurred_at_utc"] = utc_now_iso()
                attempt_errors.append(item)
            if error is not None:
                error_message = str(error.get("message") or "Unknown error").replace("\r", "").replace("\n", "\n")
                self.logger.warning(
                    "FFPROBE",
                    f"Attempt {attempt}/{attempts} failed path={job.canonical_path!r} "
                    f"exit_code={error.get('exit_code')} timeout={bool(error.get('was_timeout'))} "
                    f"elapsed={format_elapsed(probe_elapsed)} error={error_message}",
                )
            else:
                self.logger.warning(
                    "FFPROBE",
                    f"Attempt {attempt}/{attempts} failed path={job.canonical_path!r} exit_code=None "
                    f"timeout=False elapsed={format_elapsed(probe_elapsed)} error=Unknown error",
                )

        if last_error is None:
            last_error = {
                "type": "UNKNOWN",
                "message": "FFprobe failed for an unknown reason.",
                "exit_code": None,
                "stderr": None,
                "was_timeout": 0,
            }
        self._put_result(DBResult(kind="FAILED", job=job, error=last_error, attempt_number=last_attempt, attempt_errors=attempt_errors))

def enqueue_with_stop(
    q: queue.Queue[Any],
    item: Any,
    fatal_event: threading.Event,
    abort_event: Optional[threading.Event] = None,
    *,
    stop_controller: ScanStopController,
) -> bool:
    while (
        not fatal_event.is_set()
        and not stop_controller.emergency_event.is_set()
        and not (abort_event is not None and abort_event.is_set())
    ):
        try:
            q.put(item, timeout=0.2)
            return True
        except queue.Full:
            if (stop_controller.stop_event.is_set() or (abort_event is not None and abort_event.is_set())) and item is not None:
                return False
            continue
    return False

def _commit_reuse_batch(enumeration: EnumerationResult, db: Database, logger: RunLogger) -> Optional[BaseException]:
    """Persist reused media and cached NOT_VIDEO bookkeeping in one transaction."""
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = db.writer_connection()

        reuse_params = [
            (
                job.source_id,
                job.storage_relative_path,
                job.canonical_path,
                job.file_name,
                job.extension,
                job.size_bytes,
                job.mtime_ns,
                utc_now_iso(),
                job.scan_id,
                job.storage_id,
                job.storage_relative_key,
            )
            for job in enumeration.reuse_results
        ]
        not_video_params = [
            (
                job.canonical_path,
                utc_now_iso(),
                job.storage_id,
                job.storage_relative_key,
            )
            for job in enumeration.cached_not_video_results
        ]

        # Reuse bookkeeping is independent from FFprobe results. Commit it in
        # one transaction so fully reused and mixed-resume scans do not fsync
        # once per unchanged file.
        with conn:
            if reuse_params:
                conn.executemany(
                    """
                    UPDATE media_files SET source_id=?, storage_relative_path=?, canonical_path=?, file_name=?, extension=?,
                                           size_bytes=?, mtime_ns=?, last_seen_at_utc=?, last_seen_scan_id=?,
                                           presence_status='ACTIVE', missing_since_utc=NULL
                    WHERE storage_id=? AND storage_relative_key=?
                    """,
                    reuse_params,
                )
            if not_video_params:
                conn.executemany(
                    """
                    UPDATE non_video_cache SET canonical_path=?, last_seen_at_utc=?
                    WHERE storage_id=? AND storage_relative_key=?
                    """,
                    not_video_params,
                )
        return None
    except BaseException as exc:
        logger.critical("DB", f"Reuse batch update failed: {exc}", exc_info=True)
        return exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def run_probe_phase(
    enumeration: EnumerationResult,
    renderer: ScanProgressSink,
    *,
    db: Database,
    config: ConfigManager,
    logger: RunLogger,
    probe_service: ProbeService,
    stop_controller: ScanStopController,
) -> tuple[bool, Optional[BaseException]]:
    counters = enumeration.counters
    start = time.perf_counter()
    renderer.probing(
        enumeration.access_path, counters, len(enumeration.jobs), config.workers, 0.0, force=True
    )

    if enumeration.reuse_results or enumeration.cached_not_video_results:
        batch_started = time.perf_counter()
        batch_exc = _commit_reuse_batch(enumeration, db, logger)
        batch_elapsed = time.perf_counter() - batch_started
        if batch_exc is not None:
            enumeration.probing_elapsed = time.perf_counter() - start
            renderer.probing(
                enumeration.access_path,
                counters,
                len(enumeration.jobs),
                config.workers,
                enumeration.probing_elapsed,
                force=True,
            )
            return False, batch_exc
        logger.info(
            "SCAN",
            f"Reuse bookkeeping committed source_id={enumeration.source['source_id']} "
            f"reused={len(enumeration.reuse_results)} cached_not_video={len(enumeration.cached_not_video_results)} "
            f"elapsed={format_elapsed(batch_elapsed)}",
        )

    if not enumeration.jobs:
        enumeration.probing_elapsed = time.perf_counter() - start
        renderer.probing(
            enumeration.access_path, counters, 0, config.workers, enumeration.probing_elapsed, force=True
        )
        logger.info(
            "SCAN",
            f"Probing finished source_id={enumeration.source['source_id']} probed={counters.probed} "
            f"successful={counters.successful} failed={counters.failed} not_video={counters.not_video} skipped={counters.skipped} "
            f"source_available={enumeration.source_available} elapsed={format_elapsed(enumeration.probing_elapsed)}",
        )
        return True, None

    counters_lock = threading.Lock()
    fatal_event = threading.Event()
    source_abort_event = threading.Event()
    work_queue: "queue.Queue[Optional[FileJob]]" = queue.Queue(maxsize=config.queue_capacity)
    result_queue: "queue.Queue[Optional[DBResult]]" = queue.Queue(maxsize=config.queue_capacity)

    writer = ScanDBWriter(result_queue, counters, counters_lock, fatal_event, db, logger)
    workers = [
        ProbeWorker(i + 1, work_queue, result_queue, fatal_event, source_abort_event, enumeration.access_path, config, logger, probe_service, stop_controller)
        for i in range(config.workers)
    ]
    writer.start()
    for worker in workers:
        worker.start()

    def feeder() -> None:
        try:
            for job in enumeration.jobs:
                if stop_controller.stop_event.is_set() or fatal_event.is_set() or source_abort_event.is_set():
                    break
                if not enqueue_with_stop(work_queue, job, fatal_event, source_abort_event, stop_controller=stop_controller):
                    break
        finally:
            for _ in workers:
                while True:
                    try:
                        work_queue.put(None, timeout=0.2)
                        break
                    except queue.Full:
                        if fatal_event.is_set() or source_abort_event.is_set() or stop_controller.emergency_event.is_set():
                            return

    feeder_thread = threading.Thread(target=feeder, name="MediaCatalog-Feeder", daemon=True)
    feeder_thread.start()

    while feeder_thread.is_alive() or any(worker.is_alive() for worker in workers):
        renderer.probing(enumeration.access_path, counters, len(enumeration.jobs), config.workers, time.perf_counter() - start)
        if fatal_event.is_set():
            stop_controller.stop_event.set()
        time.sleep(min(config.refresh_seconds, 0.2))

    feeder_thread.join()
    for worker in workers:
        worker.join()

    if source_abort_event.is_set():
        enumeration.safe_complete = False
        enumeration.source_available = False

    # Every worker result is now enqueued. Let the single DB writer drain completely.
    while result_queue.unfinished_tasks and not fatal_event.is_set():
        renderer.probing(enumeration.access_path, counters, len(enumeration.jobs), config.workers, time.perf_counter() - start)
        time.sleep(min(config.refresh_seconds, 0.2))

    if not fatal_event.is_set():
        result_queue.put(None)
    else:
        try:
            result_queue.put_nowait(None)
        except Exception:
            pass
    writer.join(timeout=30)
    enumeration.probing_elapsed = time.perf_counter() - start
    renderer.probing(enumeration.access_path, counters, len(enumeration.jobs), config.workers, enumeration.probing_elapsed, force=True)
    logger.info(
        "SCAN",
        f"Probing finished source_id={enumeration.source['source_id']} probed={counters.probed} "
        f"successful={counters.successful} failed={counters.failed} not_video={counters.not_video} skipped={counters.skipped} "
        f"source_available={enumeration.source_available} elapsed={format_elapsed(enumeration.probing_elapsed)}",
    )
    return (not fatal_event.is_set()), writer.exception

def reconcile_missing(source_id: int, scan_id: int, db: Database) -> int:
    assert db.conn is not None
    now = utc_now_iso()
    with db.conn:
        cur = db.conn.execute(
            """
            UPDATE media_files
            SET presence_status='MISSING', missing_since_utc=COALESCE(missing_since_utc, ?)
            WHERE source_id=? AND presence_status='ACTIVE'
              AND (last_seen_scan_id IS NULL OR last_seen_scan_id<>?)
            """,
            (now, source_id, scan_id),
        )
    return int(cur.rowcount)

def merge_counters(target: ScanCounters, src: ScanCounters) -> None:
    for name in ScanCounters.__dataclass_fields__:
        setattr(target, name, getattr(target, name) + getattr(src, name))

class ScanService:
    def __init__(
        self,
        db: Database,
        config: ConfigManager,
        logger: RunLogger,
        platform: PlatformAdapter,
        probe_service: ProbeService,
        *,
        stop_notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db = db
        self.config = config
        self.logger = logger
        self.platform = platform
        self.probe_service = probe_service
        self.stop_controller = ScanStopController(
            logger, probe_service.active_processes.kill_all, stop_notify
        )

    def scan_preflight(
        self,
        sources: list[sqlite3.Row],
        resolve_access_path: Callable[[sqlite3.Row], Optional[str]],
    ) -> tuple[list[tuple[sqlite3.Row, str]], list[sqlite3.Row]]:
        available: list[tuple[sqlite3.Row, str]] = []
        unavailable: list[sqlite3.Row] = []
        for source in sources:
            path = resolve_access_path(source)
            if path and os.path.isdir(path):
                available.append((source, path))
            else:
                unavailable.append(source)
        return available, unavailable

    def scan_single_source(
        self,
        source: sqlite3.Row,
        access_path: str,
        *,
        force_reprobe: bool,
        excluded_writer: Optional[ExcludedDiagnosticWriter],
        renderer: ScanProgressSink,
        phase_callback: Optional[Callable[[str, sqlite3.Row, EnumerationResult], None]] = None,
    ) -> SourceScanResult:
        scan_type = "FORCE_REPROBE" if force_reprobe else "NORMAL"
        scan_id = self.db.start_scan_run(
            int(source["source_id"]), scan_type, self.config.workers, self.config.timeout, self.probe_service.runtime.version
        )

        renderer.begin()
        try:
            enumeration = enumerate_source(
                source, access_path, scan_id, force_reprobe=force_reprobe,
                renderer=renderer, excluded_writer=excluded_writer,
                db=self.db, config=self.config, logger=self.logger, platform=self.platform,
                stop_controller=self.stop_controller,
            )
            renderer.enumeration_complete(access_path, enumeration.counters, enumeration.enumeration_elapsed)
        finally:
            renderer.end()

        if enumeration.interrupted or self.stop_controller.stop_event.is_set():
            self.db.finish_scan_run(scan_id, int(source["source_id"]), "INCOMPLETE", enumeration.counters, 0)
            return SourceScanResult("INCOMPLETE", enumeration.counters, reason="USER_INTERRUPT")

        if not enumeration.source_available:
            self.db.finish_scan_run(scan_id, int(source["source_id"]), "INCOMPLETE", enumeration.counters, 0)
            return SourceScanResult("INCOMPLETE", enumeration.counters, reason="SOURCE_UNAVAILABLE")

        renderer.begin()
        try:
            ok, writer_exc = run_probe_phase(
                enumeration, renderer, db=self.db, config=self.config, logger=self.logger,
                probe_service=self.probe_service, stop_controller=self.stop_controller,
            )
        finally:
            renderer.end()

        if self.stop_controller.stop_event.is_set():
            self.db.finish_scan_run(scan_id, int(source["source_id"]), "INCOMPLETE", enumeration.counters, 0)
            return SourceScanResult("INCOMPLETE", enumeration.counters, error=writer_exc, reason="USER_INTERRUPT")

        if not ok:
            self.db.mark_scan_incomplete_best_effort(scan_id)
            return SourceScanResult("FATAL", enumeration.counters, error=writer_exc, reason="DB_WRITER_FAILURE")

        try:
            if not os.path.isdir(access_path):
                enumeration.safe_complete = False
                enumeration.source_available = False
                self.logger.warning("SCAN", f"Source unavailable at completion check: {access_path}")
        except Exception:
            enumeration.safe_complete = False
            enumeration.source_available = False

        if enumeration.safe_complete:
            reconcile_started = time.perf_counter()
            missing = reconcile_missing(int(source["source_id"]), scan_id, self.db)
            reconcile_elapsed = time.perf_counter() - reconcile_started
            self.logger.info(
                "SCAN",
                f"Reconciliation finished source_id={source['source_id']} missing_marked={missing} "
                f"elapsed={format_elapsed(reconcile_elapsed)}",
            )
            self.db.finish_scan_run(scan_id, int(source["source_id"]), "COMPLETED", enumeration.counters, missing)
            return SourceScanResult("COMPLETED", enumeration.counters, missing, reason="NORMAL")

        self.db.finish_scan_run(scan_id, int(source["source_id"]), "INCOMPLETE", enumeration.counters, 0)
        return SourceScanResult("INCOMPLETE", enumeration.counters, reason="SOURCE_UNAVAILABLE")

    def run_scan_operation(
        self,
        selected: list[tuple[sqlite3.Row, str]],
        *,
        force_reprobe: bool,
        renderer_factory: Callable[[float], ScanProgressSink],
        phase_callback: Optional[Callable[[str, sqlite3.Row, EnumerationResult], None]] = None,
        scope: str = "ALL_SOURCES",
    ) -> ScanOperationResult:
        self.stop_controller.reset()
        self.stop_controller.install()
        total = ScanCounters()
        total_missing = 0
        started = time.perf_counter()
        ui_wait_started = self.logger.ui_wait_seconds
        statuses: list[str] = []
        operation_id = self.logger.next_operation_id("SCAN")
        mode = "FORCE_REPROBE" if force_reprobe else "NORMAL"
        self.logger.essential_info(
            "SCAN",
            f"Operation started operation_id={operation_id} mode={mode} scope={scope} sources={len(selected)} "
            f"workers={self.config.workers} timeout={self.config.timeout:g} retries={self.config.retries} "
            f"max_attempts={self.config.max_attempts}",
        )

        excluded_writer: Optional[ExcludedDiagnosticWriter] = None
        if self.logger.excluded_file_enabled:
            try:
                if self.logger.path is not None:
                    diagnostic_path = excluded_path_for_log(self.logger.path)
                else:
                    diagnostic_path = LOGS_DIR / f"MediaCatalog_{local_timestamp_compact()}_excluded.txt"
                excluded_writer = ExcludedDiagnosticWriter(diagnostic_path)
                self.logger.info("SCAN", f"Excluded-file diagnostic enabled: {excluded_writer.path}")
            except Exception as exc:
                self.logger.warning("SCAN", f"Could not create excluded-file diagnostic: {exc}")
                excluded_writer = None

        error: Optional[BaseException] = None
        status = "COMPLETED"
        reason = "NORMAL"
        try:
            for position, (source, path) in enumerate(selected, 1):
                if self.stop_controller.stop_event.is_set():
                    status = "INCOMPLETE"
                    reason = "USER_INTERRUPT"
                    break
                source_started = time.perf_counter()
                source_ui_wait_start = self.logger.ui_wait_seconds
                self.logger.info(
                    "SCAN",
                    f"Source started operation_id={operation_id} source={position}/{len(selected)} "
                    f"source_id={source['source_id']} mode={mode} path={path!r}",
                )
                if phase_callback is not None:
                    phase_callback("SOURCE_START", source, EnumerationResult(source, path, 0))
                renderer = renderer_factory(self.config.refresh_seconds)
                set_context = getattr(renderer, "set_source_context", None)
                if callable(set_context):
                    set_context(position, len(selected), path, mode)
                result = self.scan_single_source(
                    source, path, force_reprobe=force_reprobe, excluded_writer=excluded_writer,
                    renderer=renderer, phase_callback=phase_callback,
                )
                source_wall = time.perf_counter() - source_started
                source_ui_wait = max(0.0, self.logger.ui_wait_seconds - source_ui_wait_start)
                source_active = max(0.0, source_wall - source_ui_wait)
                merge_counters(total, result.counters)
                total_missing += result.missing_marked
                statuses.append(result.status)
                self.logger.info(
                    "SCAN",
                    f"Source finished operation_id={operation_id} source={position}/{len(selected)} "
                    f"source_id={source['source_id']} path={path!r} status={result.status} reason={result.reason} "
                    f"files_seen={result.counters.files_seen} candidates={result.counters.files_candidates} "
                    f"reused={result.counters.reused} cached_not_video={result.counters.cached_not_video} "
                    f"probed={result.counters.probed} probe_successful={result.counters.successful} "
                    f"failed={result.counters.failed} not_video={result.counters.not_video} missing={result.missing_marked} "
                    f"active_elapsed={format_elapsed(source_active)} ui_wait={format_elapsed(source_ui_wait)} "
                    f"wall_elapsed={format_elapsed(source_wall)}",
                )
                completed = getattr(renderer, "source_completed", None)
                if callable(completed):
                    completed(position, len(selected), path, result, source_active)
                if result.status == "FATAL":
                    status = "FATAL"
                    reason = result.reason
                    error = result.error
                    break
                if self.stop_controller.stop_event.is_set():
                    status = "INCOMPLETE"
                    reason = "USER_INTERRUPT"
                    break
                if result.status == "INCOMPLETE" and status == "COMPLETED":
                    status = "INCOMPLETE"
                    reason = result.reason

            wall_elapsed = time.perf_counter() - started
            ui_wait = max(0.0, self.logger.ui_wait_seconds - ui_wait_started)
            active_elapsed = max(0.0, wall_elapsed - ui_wait)
            self.logger.essential_info(
                "SCAN",
                f"Operation finished operation_id={operation_id} status={status} reason={reason} mode={mode} scope={scope} "
                f"sources_completed={len(statuses)}/{len(selected)} files_seen={total.files_seen} "
                f"candidates={total.files_candidates} reused={total.reused} cached_not_video={total.cached_not_video} "
                f"probed={total.probed} probe_successful={total.successful} failed={total.failed} "
                f"not_video={total.not_video} missing={total_missing} active_elapsed={format_elapsed(active_elapsed)} "
                f"ui_wait={format_elapsed(ui_wait)} wall_elapsed={format_elapsed(wall_elapsed)}",
            )
            return ScanOperationResult(
                status=status, counters=total, missing_marked=total_missing, elapsed=active_elapsed,
                source_statuses=statuses, error=error,
                excluded_diagnostic_path=excluded_writer.path if excluded_writer is not None else None,
                operation_id=operation_id, reason=reason, ui_wait=ui_wait, wall_elapsed=wall_elapsed,
            )
        finally:
            if excluded_writer is not None:
                excluded_writer.close()
            self.stop_controller.restore()

