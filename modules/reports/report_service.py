# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from ..config import ConfigManager
from ..constants import REPORTS_DIR
from ..database import Database
from ..datetime_helpers import local_timestamp_compact
from ..logger import RunLogger, format_elapsed
from ..platform_api import PlatformAdapter
from ..probe import ProbeService
from .report_common import scope_summary
from .report_data import load_source_report_data, row_to_dict
from ..sources import SourceManager


class ReportProgressSink(Protocol):
    def begin(self) -> None: ...
    def end(self) -> None: ...
    def set_source(self, source: str) -> None: ...
    def start_format(self, fmt: str) -> None: ...
    def finish_format(self, fmt: str, status: str, elapsed: float) -> None: ...


@dataclass(frozen=True)
class ReportEligibilityIssue:
    kind: str
    source_id: int
    source_path: str
    status: Optional[str] = None


@dataclass
class ReportEligibilityResult:
    blockers: list[ReportEligibilityIssue] = field(default_factory=list)
    warnings: list[ReportEligibilityIssue] = field(default_factory=list)
    online_map: dict[int, bool] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


@dataclass
class ReportOperationResult:
    output_dir: Optional[Path]
    results: list[tuple[str, Optional[Path], Optional[str]]]
    elapsed: float
    error: Optional[str] = None
    operation_id: str = ""
    ui_wait: float = 0.0
    wall_elapsed: float = 0.0


def stable_short_suffix(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def unique_report_path(directory: Path, basename: str, extension: str) -> Path:
    extension = extension.lstrip(".")
    candidate = directory / f"{basename}.{extension}"
    index = 2
    while candidate.exists() or candidate.with_name(candidate.name + ".partial").exists():
        candidate = directory / f"{basename}_{index}.{extension}"
        index += 1
    return candidate


def create_report_operation_dir(timestamp: str, scope_label: str) -> Path:
    date_part, time_part = timestamp.split("_", 1)
    parent = REPORTS_DIR / date_part
    parent.mkdir(parents=True, exist_ok=True)
    base = f"{time_part}_{scope_label}"
    candidate = parent / base
    index = 2
    while candidate.exists():
        candidate = parent / f"{base}_{index}"
        index += 1
    candidate.mkdir()
    return candidate


def report_basename_for_source(platform: PlatformAdapter, source: dict[str, Any]) -> str:
    return platform.sanitize_filename_from_path(str(source["canonical_path"]))


def selected_sources_by_ids(db: Database, ids: Sequence[int]) -> list[sqlite3.Row]:
    rows = []
    for sid in ids:
        row = db.get_source(int(sid))
        if row is not None and row["status"] == "ACTIVE":
            rows.append(row)
    return rows


class ReportService:
    def __init__(
        self,
        db: Database,
        logger: RunLogger,
        platform: PlatformAdapter,
        source_manager: SourceManager,
        config: ConfigManager,
        probe_service: ProbeService,
    ) -> None:
        self.db = db
        self.logger = logger
        self.platform = platform
        self.source_manager = source_manager
        self.config = config
        self.probe_service = probe_service

    def report_eligibility(self, sources: list[sqlite3.Row]) -> ReportEligibilityResult:
        result = ReportEligibilityResult()
        for source in sources:
            sid = int(source["source_id"])
            completed = self.db.latest_completed_scan(sid)
            latest = self.db.latest_scan(sid)
            if completed is None:
                result.blockers.append(ReportEligibilityIssue("NO_COMPLETED_SCAN", sid, str(source["display_path"])))
                continue
            access = self.source_manager.resolve_access_path(source)
            online = access is not None
            result.online_map[sid] = online
            if latest is not None and latest["status"] != "COMPLETED":
                result.warnings.append(
                    ReportEligibilityIssue("LATEST_SCAN_STATUS", sid, str(source["display_path"]), str(latest["status"]))
                )
            if not online:
                result.warnings.append(ReportEligibilityIssue("SOURCE_UNAVAILABLE", sid, str(source["display_path"])))
        return result

    def _log_snapshot_summary(self, operation_id: str, *, mode: str, packages: list[dict[str, Any]], elapsed: float) -> None:
        summary = scope_summary(packages)
        self.logger.info(
            "REPORT",
            f"Snapshot loaded operation_id={operation_id} mode={mode} sources={len(packages)} "
            f"records={summary['total_files']} successful={summary['successful']} failed={summary['failed']} "
            f"missing={summary['missing']} total_size_bytes={summary['total_size_bytes']} "
            f"elapsed={format_elapsed(elapsed)}",
        )

    def _create_reports_for_packages(
        self,
        packages: list[dict[str, Any]],
        formats: list[str],
        basename: str,
        scope_name: str,
        online_map: dict[int, bool],
        output_dir: Path,
        progress: ReportProgressSink,
        operation_id: str,
    ) -> list[tuple[str, Optional[Path], Optional[str]]]:
        summary = scope_summary(packages)
        if summary["total_files"] == 0 and summary["missing"] == 0:
            for fmt in formats:
                progress.finish_format(fmt, "SKIPPED", 0.0)
            empty_sources = "\n".join(str(pkg["source"]["display_path"]) for pkg in packages)
            return [("EMPTY", None, empty_sources)]

        results: list[tuple[str, Optional[Path], Optional[str]]] = []
        for fmt in formats:
            progress.start_format(fmt)
            started = time.perf_counter()
            path: Optional[Path] = None
            try:
                if fmt == "excel":
                    from .report_excel import write_excel_report
                    path = unique_report_path(output_dir, basename, "xlsx")
                    write_excel_report(
                        path, packages, scope_name, online_map, self.config,
                        self.probe_service.runtime.version, self.logger,
                    )
                elif fmt == "json":
                    from .report_json import build_json_document, write_json_report
                    path = unique_report_path(output_dir, basename, "json")
                    doc = build_json_document(packages, scope_name, online_map)
                    write_json_report(path, doc, self.logger)
                elif fmt == "txt":
                    from .report_txt import write_txt_report
                    path = unique_report_path(output_dir, basename, "txt")
                    write_txt_report(path, packages, scope_name, self.logger)
                else:
                    raise ValueError(f"Unknown report format: {fmt}")
                elapsed = time.perf_counter() - started
                progress.finish_format(fmt, "COMPLETE", elapsed)
                self.logger.info(
                    "REPORT",
                    f"Report format finished operation_id={operation_id} format={fmt.upper()} scope={scope_name!r} output={path} "
                    f"elapsed={format_elapsed(elapsed)}",
                )
                results.append((fmt, path, None))
            except Exception as exc:
                elapsed = time.perf_counter() - started
                progress.finish_format(fmt, "FAILED", elapsed)
                self.logger.error(
                    "REPORT",
                    f"Report format failed operation_id={operation_id} format={fmt.upper()} scope={scope_name!r} "
                    f"elapsed={format_elapsed(elapsed)} error={exc}",
                    exc_info=True,
                )
                results.append((fmt, None, str(exc)))
        return results

    def generate(
        self,
        scope_mode: str,
        selected: list[sqlite3.Row],
        formats: list[str],
        online_map: dict[int, bool],
        progress: ReportProgressSink,
    ) -> ReportOperationResult:
        selected_ids = [int(row["source_id"]) for row in selected]
        selected = selected_sources_by_ids(self.db, selected_ids)
        timestamp = local_timestamp_compact()
        if scope_mode == "combined":
            scope_display = "All Sources - Combined"
            folder_scope = "All_Combined"
        elif scope_mode == "separate":
            scope_display = "All Sources - Separate Files"
            folder_scope = "All_Separate"
        else:
            scope_display = str(selected[0]["display_path"])
            folder_scope = report_basename_for_source(self.platform, row_to_dict(selected[0]))

        operation_id = self.logger.next_operation_id("REPORT")
        wall_started = time.perf_counter()
        ui_wait_started = self.logger.ui_wait_seconds
        try:
            operation_dir = create_report_operation_dir(timestamp, folder_scope)
        except Exception as exc:
            wall_elapsed = time.perf_counter() - wall_started
            ui_wait = max(0.0, self.logger.ui_wait_seconds - ui_wait_started)
            active_elapsed = max(0.0, wall_elapsed - ui_wait)
            self.logger.error(
                "REPORT",
                f"Operation failed operation_id={operation_id} mode={scope_mode} reason=OUTPUT_FOLDER_CREATE_FAILED "
                f"active_elapsed={format_elapsed(active_elapsed)} ui_wait={format_elapsed(ui_wait)} "
                f"wall_elapsed={format_elapsed(wall_elapsed)} error={exc}",
                exc_info=True,
            )
            return ReportOperationResult(None, [], active_elapsed, str(exc), operation_id, ui_wait, wall_elapsed)

        operation_started = time.perf_counter()
        self.logger.essential_info(
            "REPORT",
            f"Operation started operation_id={operation_id} mode={scope_mode} scope={scope_display!r} "
            f"sources={len(selected)} formats={','.join(formats)} output_folder={operation_dir}",
        )
        results: list[tuple[str, Optional[Path], Optional[str]]] = []
        read_conn = self.db.snapshot_connection()
        progress.begin()
        try:
            read_conn.execute("BEGIN")
            if scope_mode == "combined":
                load_started = time.perf_counter()
                packages = [load_source_report_data(read_conn, int(row["source_id"])) for row in selected]
                self._log_snapshot_summary(
                    operation_id, mode="combined", packages=packages,
                    elapsed=time.perf_counter() - load_started,
                )
                results.extend(self._create_reports_for_packages(
                    packages, formats, "MediaCatalog_All", "All Sources - Combined",
                    online_map, operation_dir, progress, operation_id,
                ))
            else:
                basenames: dict[str, list[sqlite3.Row]] = defaultdict(list)
                for row in selected:
                    basenames[report_basename_for_source(self.platform, row_to_dict(row))].append(row)
                for row in selected:
                    source_started = time.perf_counter()
                    progress.set_source(str(row["display_path"]))
                    load_started = time.perf_counter()
                    pkg = load_source_report_data(read_conn, int(row["source_id"]))
                    load_elapsed = time.perf_counter() - load_started
                    self._log_snapshot_summary(operation_id, mode="single_source", packages=[pkg], elapsed=load_elapsed)
                    self.logger.info(
                        "REPORT",
                        f"Source snapshot operation_id={operation_id} source_id={row['source_id']} "
                        f"path={row['display_path']!r} elapsed={format_elapsed(load_elapsed)}",
                    )
                    base = report_basename_for_source(self.platform, pkg["source"])
                    if len(basenames[base]) > 1:
                        base += "_" + stable_short_suffix(
                            str(pkg["source"]["identity_key"]) + "|" + str(pkg["source"]["source_root_key"])
                        )
                    scope_name = pkg["source"]["display_path"]
                    results.extend(self._create_reports_for_packages(
                        [pkg], formats, base, scope_name, online_map, operation_dir, progress, operation_id,
                    ))
                    self.logger.info(
                        "REPORT",
                        f"Source report finished operation_id={operation_id} source_id={row['source_id']} "
                        f"path={row['display_path']!r} elapsed={format_elapsed(time.perf_counter() - source_started)}",
                    )
            read_conn.rollback()
        except Exception as exc:
            self.logger.error("REPORT", f"Operation failed operation_id={operation_id} mode={scope_mode} error={exc}", exc_info=True)
            try:
                read_conn.rollback()
            except Exception:
                pass
            results.append(("REPORT", None, str(exc)))
        finally:
            read_conn.close()
            progress.end()

        wall_elapsed = time.perf_counter() - wall_started
        ui_wait = max(0.0, self.logger.ui_wait_seconds - ui_wait_started)
        active_elapsed = max(0.0, wall_elapsed - ui_wait)
        failed_outputs = sum(1 for _fmt, path, err in results if err is not None and path is None)
        created_outputs = sum(1 for fmt, path, err in results if fmt != "EMPTY" and path is not None and err is None)
        status = "COMPLETED" if failed_outputs == 0 else "COMPLETED_WITH_ERRORS"
        self.logger.essential_info(
            "REPORT",
            f"Operation finished operation_id={operation_id} status={status} mode={scope_mode} "
            f"created_outputs={created_outputs} failed_outputs={failed_outputs} output_folder={operation_dir} "
            f"active_elapsed={format_elapsed(active_elapsed)} ui_wait={format_elapsed(ui_wait)} "
            f"wall_elapsed={format_elapsed(wall_elapsed)}",
        )
        return ReportOperationResult(operation_dir, results, active_elapsed, None, operation_id, ui_wait, wall_elapsed)
