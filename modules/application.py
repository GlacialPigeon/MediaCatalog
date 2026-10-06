# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import os
import platform as py_platform
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import CONFIG_SCHEMA_VERSION, ConfigManager, build_default_config, peek_logging_level
from .constants import APP_NAME, APP_ROOT, CONFIG_PATH, DATA_DIR, DB_PATH, LOGS_DIR, PROGRAM_VERSION
from .database import DATABASE_SCHEMA_VERSION, Database
from .datetime_helpers import local_timestamp_compact
from .localization import CANONICAL_LOCALE, LOCALES, REQUIRED_CANONICAL_KEYS
from .logger import RunLogger, compute_build_fingerprint, format_elapsed, sha256_file, unique_log_path
from .platform_api import InstanceLock, PlatformAdapter
from .probe import ProbeService
from .sources import SourceManager
from .ui import (
    ConsoleUI,
    show_combined_startup_recovery,
    show_config_init_failed,
    show_config_invalid_replaced,
    show_database_open_failed,
    show_database_recovered,
    show_fatal,
    show_logging_unavailable,
    show_python_too_old,
    show_startup_app_root_error,
    show_startup_data_dir_error,
)


def _verify_app_root_writable() -> tuple[bool, Optional[str]]:
    test_path = APP_ROOT / f".__mediacatalog_write_test_{uuid.uuid4().hex}.tmp"
    try:
        with open(test_path, "xb") as handle:
            handle.write(b"MediaCatalog write test\n")
            handle.flush()
            os.fsync(handle.fileno())
        test_path.unlink()
        return True, None
    except Exception as exc:
        try:
            if test_path.exists():
                test_path.unlink()
        except Exception:
            pass
        return False, str(exc)


def _ensure_local_directories() -> dict[str, Optional[Exception]]:
    results: dict[str, Optional[Exception]] = {}
    for name, path in (("data", DATA_DIR), ("logs", LOGS_DIR)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            results[name] = None
        except Exception as exc:
            results[name] = exc
    return results


def _log_session_environment(logger: RunLogger) -> None:
    try:
        fingerprint, file_count = compute_build_fingerprint(APP_ROOT)
    except Exception as exc:
        fingerprint, file_count = f"UNAVAILABLE:{exc}", 0
    logger.identity("SESSION", f"Start session_id={logger.session_id}")
    logger.identity("SESSION", f"MediaCatalog={PROGRAM_VERSION}")
    logger.identity("SESSION", f"Build fingerprint=SHA256:{fingerprint}")
    logger.identity("SESSION", f"Build files={file_count}")
    logger.identity("SESSION", f"Logging level={logger.level}")
    logger.essential_info("SESSION", f"Python={sys.version.split()[0]}")
    logger.essential_info("SESSION", f"OS={py_platform.platform()}")
    logger.essential_info("SESSION", f"Architecture={py_platform.machine() or 'UNKNOWN'}")
    logger.info("SESSION", f"Executable={sys.executable}")
    logger.essential_info("SESSION", f"CWD={os.getcwd()}")
    logger.info("SESSION", f"PID={os.getpid()}")
    logger.info("SESSION", f"Timezone={time.tzname[0] if time.tzname else 'UNKNOWN'}")


def run_application(platform: PlatformAdapter) -> int:
    startup_started = time.perf_counter()
    mutex: Optional[InstanceLock] = None
    logger = RunLogger(None)
    db: Optional[Database] = None
    session_status = "ERROR"

    try:
        platform.configure_console()

        mutex_started = time.perf_counter()
        mutex = platform.create_instance_lock()
        if not mutex.acquire():
            print("MediaCatalog is already running.")
            print("Only one instance can run at a time.")
            return 1
        mutex_elapsed = time.perf_counter() - mutex_started

        app_root_started = time.perf_counter()
        writable, app_root_error = _verify_app_root_writable()
        app_root_elapsed = time.perf_counter() - app_root_started

        folders_started = time.perf_counter()
        folder_results = _ensure_local_directories()
        folders_elapsed = time.perf_counter() - folders_started

        log_path: Optional[Path]
        if folder_results.get("logs") is None:
            log_path = unique_log_path(LOGS_DIR, local_timestamp_compact())
        else:
            log_path = None
        logger = RunLogger(log_path, level=peek_logging_level(CONFIG_PATH))
        LOCALES.set_logger(logger)
        _log_session_environment(logger)

        if not LOCALES.load_canonical():
            logger.critical("LOCALE", f"Canonical locale validation failed: {LOCALES.canonical_error or 'unknown error'}")
            print("ERROR: Default language file en-US.json is missing or invalid.")
            if LOCALES.canonical_error:
                print(f"\nDetails: {LOCALES.canonical_error}")
            print("\nPlease restore a valid en-US.json file or download MediaCatalog again.")
            token = logger.begin_ui_wait("CANONICAL_LOCALE_FATAL_ACK")
            try:
                input("Press ENTER to exit.")
            except (EOFError, KeyboardInterrupt):
                pass
            finally:
                logger.end_ui_wait(token, result="EXIT")
            return 1

        logger.info("STARTUP", f"{APP_NAME} v{PROGRAM_VERSION} starting. APP_ROOT={APP_ROOT}")
        logger.info("STARTUP", f"Global instance lock acquired elapsed={format_elapsed(mutex_elapsed)}")
        logger.info("STARTUP", f"APP_ROOT write validation finished elapsed={format_elapsed(app_root_elapsed)}")
        logger.info("STARTUP", f"Local folder initialization finished elapsed={format_elapsed(folders_elapsed)}")
        logger.info(
            "LOCALE",
            f"Canonical validation passed path={LOCALES.canonical_path} locale={CANONICAL_LOCALE} "
            f"meta_version={LOCALES.canonical_meta_version or 'UNKNOWN'} required_keys={len(REQUIRED_CANONICAL_KEYS)} "
            f"present={len(LOCALES.canonical)} missing=0",
        )

        if sys.version_info < (3, 11):
            show_python_too_old(sys.version.split()[0], logger)
            return 1
        if not writable:
            show_startup_app_root_error(APP_ROOT, app_root_error, logger)
            return 1
        if folder_results.get("data") is not None:
            show_startup_data_dir_error(DATA_DIR, logger)
            return 1
        if folder_results.get("logs") is not None:
            show_logging_unavailable()
        elif log_path is not None and not logger.available:
            show_logging_unavailable(logger.open_error)

        defaults = build_default_config(
            platform.default_excluded_directories(),
            platform.default_excluded_extensions(),
        )
        config = ConfigManager(CONFIG_PATH, defaults, logger)
        config_started = time.perf_counter()
        try:
            config_result = config.load()
        except Exception as exc:
            logger.critical("CONFIG", f"Configuration initialization failed: {exc}", exc_info=True)
            show_config_init_failed(exc, logger)
            return 1
        logger.set_level(config.logging_level)
        logger.essential_info(
            "CONFIG",
            f"Status=OK path={CONFIG_PATH} schema={CONFIG_SCHEMA_VERSION} locale={config.language} "
            f"sources={len(config.sources)} workers={config.workers} timeout={config.timeout:g} "
            f"retries={config.retries} max_attempts={config.max_attempts} logging={config.logging_level}",
        )
        logger.info("STARTUP", f"Configuration load finished elapsed={format_elapsed(time.perf_counter() - config_started)}")

        requested_locale = config.language
        if not LOCALES.activate(requested_locale, log_warnings=True) and requested_locale != CANONICAL_LOCALE:
            logger.warning("LOCALE", f"Selected locale unavailable locale={requested_locale}; using {CANONICAL_LOCALE}")
        logger.info("LOCALE", f"Active locale={LOCALES.active_locale} name={LOCALES.active_name!r}")

        db = Database(DB_PATH, logger)
        db_started = time.perf_counter()
        try:
            db_result = db.open()
        except Exception as exc:
            logger.critical("DB", f"Database initialization failed: {exc}", exc_info=True)
            show_database_open_failed(logger, exc)
            return 1
        logger.essential_info(
            "DB",
            f"Path={DB_PATH} status=OK schema={DATABASE_SCHEMA_VERSION} sqlite_runtime={sqlite3.sqlite_version}",
        )
        logger.info("STARTUP", f"Database open/schema check finished elapsed={format_elapsed(time.perf_counter() - db_started)}")
        if config_result.invalid_replaced and db_result.recovered_corruption:
            platform.clear_screen()
            show_combined_startup_recovery(config_result, db_result, logger)
        elif config_result.invalid_replaced:
            show_config_invalid_replaced(config_result, logger)
        elif db_result.recovered_corruption:
            show_database_recovered(db_result, logger)

        source_manager = SourceManager(db, logger, platform, config)
        source_manager.normalize_config_sources_for_startup()
        source_manager.sync_config_sources()

        probe_service = ProbeService(db, logger, platform)
        ffprobe_started = time.perf_counter()
        runtime = probe_service.validate_runtime()
        ffprobe_path = platform.ffprobe_path(APP_ROOT)
        ffprobe_hash = "NOT_FOUND"
        if ffprobe_path.is_file():
            try:
                ffprobe_hash = sha256_file(ffprobe_path)
            except Exception as exc:
                ffprobe_hash = f"UNAVAILABLE:{exc}"
        logger.essential_info("FFPROBE", f"Path={ffprobe_path}")
        logger.essential_info("FFPROBE", f"Short version={runtime.version or 'NOT FOUND'}")
        logger.essential_info("FFPROBE", f"Full version={runtime.full_version or 'NOT FOUND'}")
        logger.essential_info("FFPROBE", f"SHA-256={ffprobe_hash}")
        logger.info("STARTUP", f"FFprobe validation finished elapsed={format_elapsed(time.perf_counter() - ffprobe_started)}")

        xlsx_started = time.perf_counter()
        from .reports.report_excel import detect_xlsxwriter
        xlsxwriter_available = detect_xlsxwriter(logger, "STARTUP")
        xlsx_version = "NOT AVAILABLE"
        if xlsxwriter_available:
            try:
                import xlsxwriter
                xlsx_version = str(getattr(xlsxwriter, "__version__", "UNKNOWN"))
            except Exception:
                xlsx_version = "UNKNOWN"
        logger.essential_info("STARTUP", f"XlsxWriter available={xlsxwriter_available} version={xlsx_version}")
        logger.info(
            "STARTUP",
            f"XLSX dependency check finished available={xlsxwriter_available} elapsed={format_elapsed(time.perf_counter() - xlsx_started)}",
        )
        if not xlsxwriter_available:
            logger.warning("STARTUP", "XlsxWriter is not installed; Excel export is unavailable.")

        ui = ConsoleUI(
            platform,
            db,
            config,
            logger,
            source_manager,
            probe_service,
            xlsxwriter_available=xlsxwriter_available,
        )
        startup_wall = time.perf_counter() - startup_started
        logger.info(
            "STARTUP",
            f"Startup completed active_elapsed={format_elapsed(max(0.0, startup_wall - logger.ui_wait_seconds))} "
            f"ui_wait={format_elapsed(logger.ui_wait_seconds)} wall_elapsed={format_elapsed(startup_wall)}",
        )
        ui.main_menu()
        session_status = "NORMAL"
        return 0
    except KeyboardInterrupt:
        session_status = "INTERRUPTED"
        return 130
    except Exception as exc:
        session_status = "ERROR"
        logger.critical("STARTUP", f"Unhandled top-level exception: {exc}", exc_info=True)
        try:
            if LOCALES.canonical:
                show_fatal(platform, logger, exc)
        except Exception:
            pass
        return 1
    finally:
        shutdown_started = time.perf_counter()
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        try:
            logger.info("STARTUP", f"Shutdown cleanup finished elapsed={format_elapsed(time.perf_counter() - shutdown_started)}")
            logger.essential_info(
                "SESSION",
                f"End status={session_status} session_id={logger.session_id} active_elapsed={format_elapsed(logger.session_active_seconds)} "
                f"ui_wait={format_elapsed(logger.ui_wait_seconds)} wall_elapsed={format_elapsed(logger.session_wall_seconds)}",
            )
            logger.close()
        except Exception:
            pass
        if mutex is not None:
            mutex.release()
