# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .config import ConfigManager, atomic_write_text
from .constants import APP_NAME, PATHLISTS_DIR, PROGRAM_VERSION
from .database import DATABASE_SCHEMA_VERSION, Database
from .localization import LOCALES, t
from .logger import RunLogger, format_elapsed
from .platform_api import PlatformAdapter, SourceDescriptor
from .probe import ProbeService
from .sources import SourceBatchPlan, SourceError, SourceManager, strip_surrounding_quotes

UI_HEADER_WIDTH = 72


def make_header(title: str) -> str:
    title = title.upper()
    line = "=" * UI_HEADER_WIDTH
    middle = title.center(UI_HEADER_WIDTH)
    return f"{line}\n{middle}\n{line}"


def make_subheader(title: str) -> str:
    return f"--- {title.upper()} ---"


def ui_header(key: str, **values: Any) -> str:
    return make_header(f"{APP_NAME} v{PROGRAM_VERSION} | {t(key, **values)}")


def format_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def pause_enter(message: Optional[str] = None) -> None:
    try:
        input(message if message is not None else t("common.press_enter_continue"))
    except (EOFError, KeyboardInterrupt):
        pass


class ScanProgressRenderer:
    def __init__(
        self,
        refresh_seconds: float,
        *,
        mode: str = "NORMAL",
        unavailable_paths: Optional[Sequence[str]] = None,
        clear_screen: Optional[Callable[[], None]] = None,
    ) -> None:
        self.refresh_seconds = max(0.05, float(refresh_seconds))
        self.mode = mode
        self.unavailable_paths = list(unavailable_paths or [])
        self.clear_screen = clear_screen
        self._last = 0.0
        self._active = False
        self._rendered_lines = 0
        self._interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._spinner_index = 0
        self._completed: list[dict[str, Any]] = [
            {"path": path, "status": "UNAVAILABLE", "position": None, "total": None, "counters": None, "missing": 0, "elapsed": 0.0}
            for path in self.unavailable_paths
        ]
        self._current_position = 0
        self._current_total = 0
        self._current_path = ""
        self._current_lines: list[str] = []
        # v2.1.2: multi-source scans keep completed output append-only. Single-source
        # scans retain the established v2.1.1 full-dashboard renderer.
        self._append_only: Optional[bool] = None
        self._append_dashboard_initialized = False

    def begin_operation(self) -> None:
        if self.clear_screen is not None:
            self.clear_screen()
        print(ui_header("label.scanning"))
        print()
        if self._interactive:
            print("\x1b[?25l", end="", flush=True)
            self._active = True
        # Until the source count is known, keep the v2.1.1 initial dashboard.
        # Multi-source mode replaces it once, when the first source context arrives.
        self._render(force=True)

    def end_operation(self) -> None:
        if self._append_only:
            self._erase_live_region()
        else:
            self._render(force=True)
        if self._interactive:
            print("\x1b[?25h", end="", flush=True)
        self._active = False
        self._rendered_lines = 0
        print()

    # ScanService still brackets enumeration/probing with begin/end. The dashboard
    # owns the cursor for the whole operation, so phase-level begin/end are no-ops.
    def begin(self) -> None:
        return

    def end(self) -> None:
        return

    def set_source_context(self, position: int, total: int, path: str, mode: str) -> None:
        if self._append_only is None:
            self._append_only = total > 1
            if self._append_only:
                self._initialize_append_dashboard()
        self._current_position = position
        self._current_total = total
        self._current_path = path
        self.mode = mode
        self._current_lines = []
        self._render(force=True)

    def source_completed(self, position: int, total: int, path: str, result: Any, elapsed: float) -> None:
        item = {
            "path": path,
            "status": result.status,
            "position": position,
            "total": total,
            "counters": result.counters,
            "missing": result.missing_marked,
            "elapsed": elapsed,
        }
        self._completed.append(item)
        if self._append_only:
            # Remove only the live/current-source region, then commit the finished
            # source once to terminal history. Previously completed sources are never
            # included in later refreshes.
            self._erase_live_region()
            self._print_completed_item(item)
            self._current_path = ""
            self._current_lines = []
            self._last = 0.0
            return
        self._current_path = ""
        self._current_lines = []
        self._render(force=True)

    def _mode_text(self) -> str:
        return t("scan.mode.force_reprobe") if self.mode == "FORCE_REPROBE" else t("scan.mode.normal")

    def _completed_item_lines(self, item: dict[str, Any]) -> list[str]:
        prefix = f"[{item['position']}/{item['total']}] " if item["position"] is not None else ""
        lines = [prefix + item["path"]]
        lines.append("      " + t("scan.source.status", status=item["status"]))
        counters = item["counters"]
        if counters is not None:
            lines.append(
                "      "
                + " | ".join((
                    t("scan.stats.files_seen", count=f"{counters.files_seen:,}"),
                    t("scan.stats.candidates", count=f"{counters.files_candidates:,}"),
                    t("scan.stats.reused", count=f"{counters.reused:,}"),
                    t("scan.complete.probed", count=f"{counters.probed:,}"),
                ))
            )
            lines.append(
                "      "
                + " | ".join((
                    t("scan.stats.probe_successful", count=f"{counters.successful:,}"),
                    t("scan.stats.failed", count=f"{counters.failed:,}"),
                    t("scan.stats.not_video", count=f"{counters.not_video:,}"),
                    t("scan.complete.missing", count=f"{item['missing']:,}"),
                ))
            )
            lines.append("      " + t("common.elapsed", elapsed=format_hms(item["elapsed"])))
        lines.append("")
        return lines

    def _completed_lines(self) -> list[str]:
        if not self._completed:
            return []
        lines = [make_subheader(t("scan.completed_sources_heading")), ""]
        for item in self._completed:
            lines.extend(self._completed_item_lines(item))
        return lines

    def _current_source_lines(self) -> list[str]:
        if not self._current_path:
            return []
        return [
            make_subheader(t("scan.current_source_heading")),
            "",
            f"[{self._current_position}/{self._current_total}] {self._current_path}",
            "",
            *self._current_lines,
        ]

    def _lines(self) -> list[str]:
        lines = [t("scan.mode", mode=self._mode_text()), ""]
        lines.extend(self._completed_lines())
        lines.extend(self._current_source_lines())
        return lines

    def _initialize_append_dashboard(self) -> None:
        if self._append_dashboard_initialized:
            return
        # begin_operation rendered the legacy initial frame before total source count
        # was known. Replace that frame once with the append-only multi-source layout.
        if self.clear_screen is not None:
            self.clear_screen()
        print(ui_header("label.scanning"))
        print()
        print(t("scan.mode", mode=self._mode_text()))
        print()
        print(make_subheader(t("scan.completed_sources_heading")))
        print()
        for item in self._completed:
            self._print_completed_item(item)
        self._rendered_lines = 0
        self._last = 0.0
        self._append_dashboard_initialized = True

    def _print_completed_item(self, item: dict[str, Any]) -> None:
        for line in self._completed_item_lines(item):
            print(line)

    def _erase_live_region(self) -> None:
        if not (self._interactive and self._active and self._rendered_lines):
            self._rendered_lines = 0
            return
        # Move upward one rendered line at a time and erase it in place.
        # IMPORTANT: do not emit newlines here. A newline while clearing a live
        # region becomes permanent blank scrollback in Windows Terminal/copy-paste.
        # Starting from the blank line immediately below the live region, this
        # leaves the cursor at column 1 of the former first live line.
        sys.stdout.write("\x1b[1F\x1b[2K" * self._rendered_lines)
        sys.stdout.flush()
        self._rendered_lines = 0

    def _render_append_current(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < self.refresh_seconds:
            return
        self._last = now
        # Redirected/non-interactive output is intentionally append-only as well:
        # live refresh frames are omitted and only completed source blocks are emitted.
        if not (self._interactive and self._active):
            return
        lines = self._current_source_lines()
        self._erase_live_region()
        for line in lines:
            print("\x1b[2K" + line)
        self._rendered_lines = len(lines)
        print("\x1b[2K", end="", flush=True)

    def _render(self, *, force: bool = False) -> None:
        if self._append_only:
            self._render_append_current(force=force)
            return
        now = time.monotonic()
        if not force and now - self._last < self.refresh_seconds:
            return
        self._last = now
        lines = self._lines()
        if self._interactive and self._active:
            if self._rendered_lines:
                print(f"\x1b[{self._rendered_lines}F", end="")
            total_lines = max(self._rendered_lines, len(lines))
            for index in range(total_lines):
                value = lines[index] if index < len(lines) else ""
                print("\x1b[2K" + value)
            self._rendered_lines = len(lines)
            if total_lines > len(lines):
                print(f"\x1b[{total_lines - len(lines)}F", end="")
            print("\x1b[2K", end="", flush=True)
        else:
            print(" | ".join(line.strip() for line in lines if line.strip())[:500], flush=True)

    def enumeration(self, source_path: str, counters: Any, elapsed: float, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._last < self.refresh_seconds:
            return
        spinner = "|/-\\"[self._spinner_index % 4]
        self._spinner_index += 1
        self._current_lines = [
            t("scan.progress.phase_enumerating", spinner=spinner),
            "",
            t("scan.stats.files_seen", count=f"{counters.files_seen:,}"),
            t("scan.progress.excluded", count=f"{counters.files_excluded:,}"),
            t("scan.stats.candidates", count=f"{counters.files_candidates:,}"),
            t("scan.stats.reused", count=f"{counters.reused:,}"),
            t("scan.progress.cached_not_video", count=f"{counters.cached_not_video:,}"),
            t("scan.progress.need_probe", count=f"{counters.need_probe:,}"),
            "",
            t("common.elapsed", elapsed=format_hms(elapsed)),
            "",
            t("scan.progress.stop_hint"),
        ]
        self._render(force=force)

    def enumeration_complete(self, source_path: str, counters: Any, elapsed: float) -> None:
        self._current_lines = [
            t("scan.progress.phase_enumeration_complete"),
            "",
            t("scan.stats.files_seen", count=f"{counters.files_seen:,}"),
            t("scan.progress.excluded", count=f"{counters.files_excluded:,}"),
            t("scan.stats.candidates", count=f"{counters.files_candidates:,}"),
            "",
            make_subheader(t("scan.cache_heading")),
            t("scan.stats.reused", count=f"{counters.reused:,}"),
            t("scan.progress.cached_not_video", count=f"{counters.cached_not_video:,}"),
            t("scan.progress.need_probe", count=f"{counters.need_probe:,}"),
            "",
            t("common.elapsed", elapsed=format_hms(elapsed)),
        ]
        self._render(force=True)

    def probing(self, source_path: str, counters: Any, total_probe: int, workers: int, elapsed: float, *, force: bool = False) -> None:
        done = counters.probed
        pct = (done / total_probe * 100.0) if total_probe else 100.0
        width = 26
        filled = width if total_probe == 0 else min(width, int((done / total_probe) * width))
        bar = "#" * filled + "-" * (width - filled)
        self._current_lines = [
            t("scan.progress.phase_probing"),
            t("scan.progress.probing", done=f"{done:,}", total=f"{total_probe:,}", percent=f"{pct:5.1f}"),
            f"[{bar}]",
            "",
            make_subheader(t("scan.cache_heading")),
            t("scan.stats.reused", count=f"{counters.reused:,}"),
            t("scan.progress.cached_not_video", count=f"{counters.cached_not_video:,}"),
            "",
            make_subheader(t("scan.probe_results_heading")),
            t("scan.complete.probed", count=f"{counters.probed:,}"),
            t("scan.stats.probe_successful", count=f"{counters.successful:,}"),
            t("scan.stats.failed", count=f"{counters.failed:,}"),
            t("scan.stats.not_video", count=f"{counters.not_video:,}"),
            "",
            t("ffprobe.workers", count=f"{workers:,}"),
            t("common.elapsed", elapsed=format_hms(elapsed)),
            "",
            t("scan.progress.stop_hint"),
        ]
        self._render(force=force)


class ReportProgressRenderer:
    def __init__(self, scope_name: str, formats: Sequence[str], refresh_seconds: float, clear_screen: Callable[[], None]) -> None:
        self.scope_name = scope_name
        self.formats = list(formats)
        self.refresh_seconds = max(0.2, float(refresh_seconds))
        self.clear_screen = clear_screen
        self.current_source: Optional[str] = None
        self.states: dict[str, str] = {fmt: "WAITING" for fmt in self.formats}
        self.started_at: dict[str, float] = {}
        self.finished_elapsed: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._interactive = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._active = False
        self._rendered_lines = 0

    def _elapsed_text(self, seconds: float) -> str:
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _state_text(self, state: str, elapsed: Optional[float] = None) -> str:
        if state == "GENERATING":
            return t("report.progress.state.generating", elapsed=self._elapsed_text(elapsed or 0.0))
        key = {
            "WAITING": "report.progress.state.waiting",
            "COMPLETE": "report.progress.state.complete",
            "FAILED": "report.progress.state.failed",
            "SKIPPED": "report.progress.state.skipped",
        }.get(state)
        return t(key) if key is not None else state

    def _lines(self) -> list[str]:
        with self._lock:
            now = time.perf_counter()
            lines = [t("report.progress.generating"), "", t("report.progress.scope", scope=self.scope_name)]
            if self.current_source:
                lines.append(t("common.source_path", path=self.current_source))
            lines.append("")
            labels = {"excel": t("report.format.excel"), "json": t("report.format.json"), "txt": t("report.format.txt")}
            for fmt in self.formats:
                state = self.states.get(fmt, "WAITING")
                if state == "GENERATING":
                    value = self._state_text(state, now - self.started_at.get(fmt, now))
                elif fmt in self.finished_elapsed:
                    value = f"{self._state_text(state):<10} {self._elapsed_text(self.finished_elapsed[fmt])}"
                else:
                    value = self._state_text(state)
                lines.append(f"{labels.get(fmt, fmt.upper()):<6} {value}")
            return lines

    def _render(self, *, force: bool = False) -> None:
        if not self._interactive and not force:
            return
        lines = self._lines()
        if self._interactive and self._active:
            if self._rendered_lines:
                print(f"\x1b[{self._rendered_lines}F", end="")
            previous_lines = self._rendered_lines
            total_lines = max(previous_lines, len(lines))
            for index in range(total_lines):
                text = lines[index] if index < len(lines) else ""
                print("\x1b[2K" + text)
            self._rendered_lines = len(lines)
            if total_lines > len(lines):
                print(f"\x1b[{total_lines - len(lines)}F", end="")
            print("\x1b[2K", end="", flush=True)
        elif force:
            print(" | ".join(line for line in lines if line)[:300], flush=True)

    def _run(self) -> None:
        while not self._stop.wait(self.refresh_seconds):
            self._render()

    def begin(self) -> None:
        self.clear_screen()
        print(ui_header("report.progress.title"))
        print()
        if self._interactive:
            print("\x1b[?25l", end="", flush=True)
            self._active = True
            self._rendered_lines = 0
        self._render(force=True)
        self._thread = threading.Thread(target=self._run, name="MediaCatalog-ReportProgress", daemon=True)
        self._thread.start()

    def set_source(self, source: Optional[str]) -> None:
        with self._lock:
            self.current_source = source
            for fmt in self.formats:
                self.states[fmt] = "WAITING"
                self.started_at.pop(fmt, None)
                self.finished_elapsed.pop(fmt, None)
        self._render(force=True)

    def start_format(self, fmt: str) -> None:
        with self._lock:
            self.states[fmt] = "GENERATING"
            self.started_at[fmt] = time.perf_counter()
            self.finished_elapsed.pop(fmt, None)
        self._render(force=True)

    def finish_format(self, fmt: str, state: str, elapsed: float) -> None:
        with self._lock:
            self.states[fmt] = state
            self.finished_elapsed[fmt] = max(0.0, elapsed)
            self.started_at.pop(fmt, None)
        self._render(force=True)

    def end(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._render(force=True)
        if self._interactive:
            print("\x1b[?25h", end="", flush=True)
        self._active = False
        self._rendered_lines = 0
        print()


class ConsoleUI:
    def __init__(
        self,
        platform: PlatformAdapter,
        db: Database,
        config: ConfigManager,
        logger: RunLogger,
        source_manager: SourceManager,
        probe_service: ProbeService,
        *,
        xlsxwriter_available: bool,
    ) -> None:
        self.platform = platform
        self.db = db
        self.config = config
        self.logger = logger
        self.source_manager = source_manager
        self.probe_service = probe_service
        self.xlsxwriter_available = xlsxwriter_available
        self.source_manager.set_ui_callbacks(
            notice_callback=self._source_notice,
            overlap_confirm_callback=None,
        )

    def clear_screen(self) -> None:
        self.platform.clear_screen()

    def _prompt(self, prompt: str, wait_action: str) -> str:
        token = self.logger.begin_ui_wait(wait_action)
        try:
            return input(prompt).strip()
        finally:
            self.logger.end_ui_wait(token)

    def _pause(self, wait_action: str = "CONTINUE_ACK", message: Optional[str] = None) -> None:
        token = self.logger.begin_ui_wait(wait_action)
        try:
            pause_enter(message)
        finally:
            self.logger.end_ui_wait(token, result="CONTINUE")

    def _source_notice(self, code: str) -> None:
        if code == "READ_ONLY_IDENTITY":
            print(t("source.identity.read_only_warning"))
        elif code == "NETWORK_SHORTCUT_UNRESOLVED":
            print(t("source.network_shortcut.unresolved", app_name=APP_NAME) + "\n")

    def _render_source_resolution(self, plan: SourceBatchPlan) -> None:
        grouped_ids = {id(item) for _, group in plan.same_storage_groups for item in group}
        changed = [item for item in plan.resolutions if item.changed and id(item) not in grouped_ids]
        if changed:
            print("\n" + make_subheader(t("source.resolution.resolved_heading")) + "\n")
            for item in changed:
                print(t("source.resolution.input"))
                print(item.input_path)
                print(t("source.resolution.resolved"))
                print(item.resolved_path + "\n")

        if plan.same_storage_groups:
            print("\n" + make_subheader(t("source.resolution.same_storage_heading")) + "\n")
            for root, items in plan.same_storage_groups:
                print(t("source.resolution.same_storage"))
                print(root + "\n")
                for item in items:
                    print(t("source.resolution.input"))
                    print(item.input_path)
                    print(t("source.resolution.resolved"))
                    print(item.resolved_path + "\n")

        resolved_network = [item for item in plan.resolutions if item.storage_type == "UNC" and item.changed]
        if resolved_network:
            key = "source.resolution.unc_note_single" if len(resolved_network) == 1 else "source.resolution.unc_note_multiple"
            print(t(key) + "\n")

    def _render_source_overlap(self, plan: SourceBatchPlan) -> None:
        if plan.replacements:
            print("\n" + make_subheader(t("source.overlap.replace_heading")) + "\n")
            for replacement in plan.replacements:
                print(t("source.overlap.new_source"))
                print(replacement.new_path + "\n")
                key = "source.overlap.replaced_single" if len(replacement.replaced_paths) == 1 else "source.overlap.replaced_multiple"
                print(t(key))
                for path in replacement.replaced_paths:
                    print(path)
                print()
            print(t("source.overlap.replace_effect"))
            print(t("source.overlap.catalog_reconcile") + "\n")

        if plan.selected_covered:
            print("\n" + make_subheader(t("source.overlap.selected_covered_heading")) + "\n")
            for source, parent in plan.selected_covered:
                print(t("source.overlap.selected_covered_item", source=source, parent=parent) + "\n")

        if plan.already_covered:
            print("\n" + make_subheader(t("source.overlap.already_covered_heading")) + "\n")
            for source, parent in plan.already_covered:
                print(t("source.overlap.already_covered_item", source=source, parent=parent) + "\n")

        if plan.already_configured:
            print("\n" + make_subheader(t("source.overlap.already_configured_heading")) + "\n")
            for path in plan.already_configured:
                print(path)

        if plan.duplicate_inputs:
            print("\n" + make_subheader(t("source.overlap.duplicates_heading")) + "\n")
            for raw, path in plan.duplicate_inputs:
                print(t("source.overlap.duplicate_item", input=raw, path=path) + "\n")

        if plan.unavailable:
            print("\n" + make_subheader(t("source.overlap.unavailable_heading")) + "\n")
            for issue in plan.unavailable:
                print(t("source.overlap.unavailable_item", path=issue.input_path, detail=issue.detail) + "\n")

        if plan.invalid:
            print("\n" + make_subheader(t("source.overlap.invalid_heading")) + "\n")
            for issue in plan.invalid:
                print(t("source.overlap.invalid_item", path=issue.input_path, detail=issue.detail) + "\n")

    def _show_source_batch_review(self, plan: SourceBatchPlan, *, title_key: str) -> bool:
        has_review_details = plan.has_resolution_summary or plan.has_overlap_summary
        if not has_review_details and plan.configuration_changed:
            return True

        self.clear_screen()
        print(ui_header(title_key))

        if plan.has_resolution_summary:
            print("\n" + make_subheader(t("source.resolution.title")) + "\n")
            self._render_source_resolution(plan)
        if plan.has_overlap_summary:
            print("\n" + make_subheader(t("source.overlap.title")) + "\n")
            self._render_source_overlap(plan)

        if not plan.configuration_changed:
            print("\n" + t("source.summary.no_changes") + "\n")
            self._pause("SOURCE_BATCH_NO_CHANGES_ACK")
            return False

        if plan.unavailable or plan.invalid:
            action_text = t("source.overlap.continue_valid")
            action_name = "CONTINUE_VALID_SOURCES"
        elif plan.replacements:
            replacement_count = sum(len(item.replaced_paths) for item in plan.replacements)
            action_text = t("source.overlap.replace_source" if replacement_count == 1 else "source.overlap.replace_sources")
            action_name = "REPLACE_SOURCES"
        else:
            action_text = t("common.continue")
            action_name = "CONTINUE"

        print(f"\n[1] {action_text}")
        print(f"[0] {t('common.cancel')}")
        answer = self._prompt("\n" + t("common.choice_default") + " ", "SOURCE_BATCH_CONFIRMATION") or "0"
        decision = answer == "1"
        self.logger.ui_selection(screen="SOURCE_BATCH_REVIEW", action=action_name if decision else "CANCEL")
        return decision

    def _execute_source_batch(self, values: Sequence[str], *, mode: str, title_key: str) -> None:
        plan = self.source_manager.plan_batch(values, mode=mode)
        if not self._show_source_batch_review(plan, title_key=title_key):
            return
        if plan.configuration_changed:
            self.source_manager.apply_batch_plan(plan)

    def choose_single_source(self, sources: list[sqlite3.Row]) -> Optional[sqlite3.Row]:
        while True:
            self.clear_screen()
            print(ui_header("label.select_source"))
            print()
            for idx, row in enumerate(sources, 1):
                print(f"[{idx}] {row['display_path']}")
            print(f"[0] {t('common.back')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "SELECT_SOURCE") or "0"
            if answer == "0":
                self.logger.ui_selection(screen="SELECT_SOURCE", action="BACK")
                return None
            try:
                index = int(answer)
                if 1 <= index <= len(sources):
                    selected = sources[index - 1]
                    self.logger.ui_selection(screen="SELECT_SOURCE", action="SELECT", path=str(selected["display_path"]))
                    return selected
            except ValueError:
                pass
            self.logger.invalid_input(screen="SELECT_SOURCE", value=answer)
            print("\n" + t("common.invalid_choice"))
            self._pause("INVALID_INPUT_ACK")

    def _edit_ffprobe_setting(self, setting: str) -> None:
        defaults = self.config.defaults["scan"]
        while True:
            if setting == "workers":
                name = t("ffprobe.workers_name")
                current_text = str(self.config.workers)
                default_value: Any = int(defaults["workers"])
                wait_action = "FFPROBE_WORKERS_VALUE"
                invalid_screen = "FFPROBE_WORKERS"
                invalid_message = t("ffprobe.workers_invalid", minimum=1, maximum=32)
            elif setting == "timeout_seconds":
                name = t("ffprobe.timeout_name")
                current_text = f"{self.config.timeout:g} s"
                default_value = defaults["timeout_seconds"]
                wait_action = "FFPROBE_TIMEOUT_VALUE"
                invalid_screen = "FFPROBE_TIMEOUT"
                invalid_message = t("ffprobe.timeout_invalid")
            elif setting == "retries":
                name = t("ffprobe.retries_name")
                current_text = str(self.config.retries)
                default_value = int(defaults["retries"])
                wait_action = "FFPROBE_RETRIES_VALUE"
                invalid_screen = "FFPROBE_RETRIES"
                invalid_message = t("ffprobe.retries_invalid")
            else:
                raise ValueError(f"Unsupported FFprobe setting: {setting}")

            self.clear_screen()
            print(ui_header("label.ffprobe_settings"))
            print(f"\n{name}\n")
            print(t("ffprobe.current_value", value=current_text))
            raw = self._prompt("\n" + t("ffprobe.new_value", default=default_value) + " ", wait_action)
            value_text = raw or str(default_value)

            try:
                if setting == "workers":
                    value = int(value_text)
                    if not 1 <= value <= 32:
                        raise ValueError
                    old = self.config.workers
                    new_value: Any = value
                elif setting == "timeout_seconds":
                    value = float(value_text)
                    if value <= 0:
                        raise ValueError
                    old = self.config.timeout
                    new_value = int(value) if value.is_integer() else value
                else:
                    value = int(value_text)
                    if value < 0:
                        raise ValueError
                    old = self.config.retries
                    new_value = value
            except ValueError:
                self.logger.invalid_input(screen=invalid_screen, value=value_text)
                print("\n" + invalid_message)
                self._pause("INVALID_INPUT_ACK")
                continue

            self.config.data["scan"][setting] = new_value
            self.config.save()
            self.logger.setting_changed(f"scan.{setting}", old, new_value)
            return

    def ffprobe_settings_menu(self) -> None:
        while True:
            self.clear_screen()
            print(ui_header("label.ffprobe_settings"))
            print(f"\n[1] {t('ffprobe.workers', count=self.config.workers)}")
            print(f"[2] {t('ffprobe.timeout', seconds=f'{self.config.timeout:g}')}")
            print(f"[3] {t('ffprobe.retries', count=self.config.retries)}")
            print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "FFPROBE_SETTINGS_MENU") or "0"
            if choice == "0":
                self.logger.ui_selection(screen="FFPROBE_SETTINGS", action="BACK")
                return
            if choice == "1":
                self.logger.ui_selection(screen="FFPROBE_SETTINGS", action="SET_WORKERS")
                self._edit_ffprobe_setting("workers")
            elif choice == "2":
                self.logger.ui_selection(screen="FFPROBE_SETTINGS", action="SET_TIMEOUT")
                self._edit_ffprobe_setting("timeout_seconds")
            elif choice == "3":
                self.logger.ui_selection(screen="FFPROBE_SETTINGS", action="SET_RETRIES")
                self._edit_ffprobe_setting("retries")
            else:
                self.logger.invalid_input(screen="FFPROBE_SETTINGS", value=choice)
                print(t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK")

    def remove_source_menu(self) -> None:
        while True:
            sources = list(self.config.sources)
            self.clear_screen(); print(ui_header("label.remove_source"))
            if not sources:
                print("\n" + t("source.remove.empty"))
                print(f"\n[0] {t('common.back')}")
                answer = self._prompt("\n" + t("common.choice_default") + " ", "REMOVE_SOURCE_EMPTY") or "0"
                if answer == "0":
                    return
                self.logger.invalid_input(screen="REMOVE_SOURCE", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK")
                continue
            print()
            for idx, path in enumerate(sources, 1): print(f"[{idx}] {path}")
            print(f"[A] {t('source.remove.all_action')}"); print(f"[0] {t('common.back')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "REMOVE_SOURCE_SELECTION") or "0"
            if answer == "0": return
            if answer.casefold() == "a":
                self.clear_screen(); print(ui_header("source.remove.all_title"))
                print("\n" + t("source.remove.all_confirmation", count=len(sources)) + "\n")
                print(f"[1] {t('source.remove.all_action')}"); print(f"[0] {t('common.cancel')}")
                confirm = self._prompt("\n" + t("common.choice_default") + " ", "REMOVE_ALL_CONFIRMATION") or "0"
                if confirm == "1":
                    self.logger.ui_selection(screen="REMOVE_SOURCE", action="REMOVE_ALL")
                    self.source_manager.remove_all_sources()
                    return
                continue
            try:
                idx = int(answer)
                if not 1 <= idx <= len(sources): raise ValueError
            except ValueError:
                self.logger.invalid_input(screen="REMOVE_SOURCE", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause(); continue
            path = sources[idx - 1]
            self.clear_screen(); print(ui_header("label.remove_source"))
            print("\n" + t("source.remove.single_confirmation", path=path) + "\n")
            print(f"[1] {t('source.remove.action')}"); print(f"[0] {t('common.cancel')}")
            confirm = self._prompt("\n" + t("common.choice_default") + " ", "REMOVE_SOURCE_CONFIRMATION") or "0"
            if confirm == "1":
                self.logger.ui_selection(screen="REMOVE_SOURCE", action="REMOVE", path=path)
                self.source_manager.remove_config_path(path)

    def import_sources_from_file(self) -> None:
        self.clear_screen(); print(ui_header("label.import_sources"))
        raw = self._prompt("\n" + t("source.import.path_prompt") + " ", "IMPORT_SOURCE_FILE_PATH")
        if not raw: return
        path = Path(self.platform.resolve_user_path(raw))
        self.logger.info("UI", f"Source import file selected path={str(path)!r}")
        if not path.is_file():
            print("\n" + t("source.import.file_not_found", path=path)); self._pause(); return
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
        except Exception as exc:
            print("\n" + t("source.import.read_failed", error=exc)); self._pause(); return
        values = self.source_manager.normalize_import_values(lines)
        self.logger.ui_selection(screen="IMPORT_SOURCES", action="IMPORT_FROM_TXT", inputs=len(values))
        self._execute_source_batch(values, mode="REPLACE", title_key="label.import_sources")

    def export_sources_to_file(self) -> None:
        path = PATHLISTS_DIR / "media_sources.txt"
        try:
            PATHLISTS_DIR.mkdir(parents=True, exist_ok=True)
            lines = list(self.config.sources)
            atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
            self.clear_screen(); print(ui_header("label.export_sources")); print("\n" + t("source.export.success", path=path) + "\n")
            self.logger.info("SOURCE", f"Exported configured sources count={len(lines)} path={path}")
        except Exception as exc:
            self.clear_screen(); print(ui_header("label.export_sources")); print("\n" + t("source.export.failed", error=exc) + "\n")
            self.logger.error("SOURCE", f"Source export failed: {exc}", exc_info=True)
        self._pause()

    def paths_menu(self) -> None:
        while True:
            sources = list(self.config.sources)
            self.clear_screen(); print(ui_header("label.paths")); print("\n" + make_subheader(t("label.media_sources")) + "\n")
            if sources:
                for path in sources: print(path)
            else: print(t("paths.sources.empty"))
            print(f"\n[1] {t('label.add_source')}"); print(f"[2] {t('label.remove_source')}")
            print(f"[3] {t('paths.import_sources_from_file')}"); print(f"[4] {t('paths.export_sources_to_file')}"); print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "PATHS_MENU") or "0"
            if choice == "0": return
            if choice == "1":
                self.logger.ui_selection(screen="PATHS", action="ADD_SOURCE")
                self.clear_screen(); print(ui_header("label.add_source")); print("\n" + t("source.add.instructions") + "\n")
                pending: list[str] = []
                while True:
                    raw = self._prompt(t("source.add.path_prompt") + " ", "ADD_SOURCE_PATH")
                    if not raw: break
                    pending.append(raw)
                    self.logger.info("UI", f"Source input method=ADD_SOURCE path={strip_surrounding_quotes(raw)!r}")
                if not pending: continue
                self._execute_source_batch(pending, mode="ADD", title_key="label.add_source")
            elif choice == "2":
                self.logger.ui_selection(screen="PATHS", action="REMOVE_SOURCE"); self.remove_source_menu()
            elif choice == "3":
                self.logger.ui_selection(screen="PATHS", action="IMPORT_FROM_TXT"); self.import_sources_from_file()
            elif choice == "4":
                self.logger.ui_selection(screen="PATHS", action="EXPORT_TO_TXT"); self.export_sources_to_file()
            else:
                self.logger.invalid_input(screen="PATHS", value=choice)
                print("\n" + t("common.invalid_choice")); self._pause()

    def language_menu(self) -> None:
        while True:
            locales = LOCALES.discover()
            self.clear_screen(); print(ui_header("label.language")); print("\n" + t("language.current", name=LOCALES.active_name) + "\n")
            for idx, (_, name) in enumerate(locales, 1): print(f"[{idx}] {name}")
            print(f"\n[0] {t('common.back')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "LANGUAGE_MENU") or "0"
            if answer == "0":
                self.logger.ui_selection(screen="LANGUAGE", action="BACK")
                return
            try:
                index = int(answer)
                if not 1 <= index <= len(locales): raise ValueError
            except ValueError:
                self.logger.invalid_input(screen="LANGUAGE", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK"); continue
            locale, _ = locales[index - 1]
            self.logger.ui_selection(screen="LANGUAGE", action="SELECT", locale=locale)
            if locale == LOCALES.active_locale: continue
            if not LOCALES.activate(locale, log_warnings=True): continue
            if self.config.language != locale:
                old_locale = self.config.language
                self.config.data["locale"] = locale; self.config.save()
                self.logger.setting_changed("locale", old_locale, locale)

    def configuration_menu(self) -> None:
        while True:
            self.clear_screen(); print(ui_header("label.configuration"))
            print(f"\n[1] {t('label.ffprobe_settings')}"); print(f"[2] {t('label.paths')}"); print(f"[3] {t('label.language')}"); print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "CONFIGURATION_MENU") or "0"
            if choice == "0":
                self.logger.ui_selection(screen="CONFIGURATION", action="BACK")
                return
            if choice == "1":
                self.logger.ui_selection(screen="CONFIGURATION", action="FFPROBE_SETTINGS"); self.ffprobe_settings_menu()
            elif choice == "2":
                self.logger.ui_selection(screen="CONFIGURATION", action="PATHS"); self.paths_menu()
            elif choice == "3":
                self.logger.ui_selection(screen="CONFIGURATION", action="LANGUAGE"); self.language_menu()
            else:
                self.logger.invalid_input(screen="CONFIGURATION", value=choice)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK")

    def _scan_stop_notice(self, kind: str) -> None:
        if kind == "GRACEFUL":
            print("\n" + t("scan.stop.graceful_requested"), flush=True)
        elif kind == "EMERGENCY":
            print("\n" + t("scan.stop.emergency_requested", app_name=APP_NAME), flush=True)

    def show_preflight(
        self,
        sources: list[sqlite3.Row],
        *,
        configured_paths: Optional[Sequence[str]] = None,
    ) -> Optional[list[tuple[sqlite3.Row, str]]]:
        from .scan import ScanService
        refresh_started = time.perf_counter()
        runtime = self.probe_service.validate_runtime()
        self.logger.info(
            "FFPROBE",
            f"Fresh preflight validation available={runtime.available} version={runtime.version or 'NOT FOUND'} "
            f"elapsed={format_elapsed(time.perf_counter() - refresh_started)}",
        )
        service = ScanService(self.db, self.config, self.logger, self.platform, self.probe_service, stop_notify=self._scan_stop_notice)
        available, unavailable_rows = service.scan_preflight(sources, self.source_manager.resolve_access_path)
        configured = list(configured_paths if configured_paths is not None else [str(row["display_path"]) for row in sources])
        known_paths = [str(row["canonical_path"]) for row in sources]
        unresolved = [path for path in configured if not any(self.platform.paths_equal(path, known) for known in known_paths)]
        unavailable_paths = [str(row["display_path"]) for row in unavailable_rows] + unresolved
        self._last_preflight_source_order = configured
        self._last_preflight_unavailable_paths = unavailable_paths

        self.logger.info(
            "PREFLIGHT",
            f"Scan preflight sources={len(configured)} available={len(available)} unavailable={len(unavailable_paths)} "
            f"workers={self.config.workers} timeout={self.config.timeout:g} retries={self.config.retries} "
            f"max_attempts={self.config.max_attempts} database_status=OK schema={DATABASE_SCHEMA_VERSION}",
        )
        self.logger.info(
            "PREFLIGHT",
            f"FFprobe available={runtime.available} version={runtime.version or 'NOT FOUND'} "
            f"path={str(self.probe_service.ffprobe_path)!r}",
        )
        for row in unavailable_rows:
            self.logger.warning(
                "PREFLIGHT",
                f"Unavailable source_id={int(row['source_id'])} path={str(row['display_path'])!r} "
                "reason=UNAVAILABLE_OR_INACCESSIBLE",
            )
        for path in unresolved:
            self.logger.warning(
                "PREFLIGHT",
                f"Unavailable configured source path={path!r} reason=UNRESOLVED_CONFIG_SOURCE",
            )

        self.clear_screen(); print(ui_header("preflight.title")); print("\n" + make_subheader(t("label.media_sources")) + "\n")
        print(t("preflight.sources.total", count=len(configured)))
        print(t("preflight.sources.available", count=len(available)))
        print(t("preflight.sources.unavailable", count=len(unavailable_paths)))
        if unavailable_paths:
            print("\n" + t("preflight.sources.unavailable_heading"))
            for path in unavailable_paths: print(f"- {path}")
        print("\n" + make_subheader(t("preflight.ffprobe.heading")) + "\n")
        print(t("preflight.ffprobe.version", version=runtime.version or t("preflight.ffprobe.not_found")))
        print(t("ffprobe.workers", count=self.config.workers))
        print(t("ffprobe.timeout", seconds=f"{self.config.timeout:g}"))
        print(t("ffprobe.retries", count=self.config.retries))
        print("\n" + make_subheader(t("preflight.database.heading")) + "\n")
        print(t("preflight.database.status_ok")); print(t("preflight.database.schema", version=DATABASE_SCHEMA_VERSION))
        if not runtime.available:
            print("\n" + t("preflight.ffprobe.required")); print(t("preflight.ffprobe.expected", path=self.probe_service.ffprobe_path)); print("\n" + t("common.readme_install_instructions") + "\n"); self._pause("PREFLIGHT_FFPROBE_ACK"); return None
        if not available:
            print("\n" + t("preflight.no_sources_available") + "\n"); self._pause("PREFLIGHT_NO_SOURCES_ACK"); return None
        label = t("preflight.scan_available_sources") if unavailable_paths else t("label.start_scan")
        print(f"\n[1] {label}"); print(f"[0] {t('common.back')}")
        answer = self._prompt("\n" + t("common.choice_default") + " ", "PREFLIGHT_CONFIRMATION") or "0"
        if answer == "1":
            self.logger.ui_selection(screen="PREFLIGHT", action="START_SCAN")
            return available
        self.logger.ui_selection(screen="PREFLIGHT", action="BACK")
        return None

    def _run_scan_operation(self, selected: list[tuple[sqlite3.Row, str]], *, force_reprobe: bool, scope: str) -> None:
        from .scan import ScanService
        service = ScanService(self.db, self.config, self.logger, self.platform, self.probe_service, stop_notify=self._scan_stop_notice)
        mode = "FORCE_REPROBE" if force_reprobe else "NORMAL"
        renderer = ScanProgressRenderer(
            self.config.refresh_seconds,
            mode=mode,
            unavailable_paths=getattr(self, "_last_preflight_unavailable_paths", []),
            clear_screen=self.clear_screen,
        )
        renderer.begin_operation()
        try:
            result = service.run_scan_operation(
                selected,
                force_reprobe=force_reprobe,
                renderer_factory=lambda _refresh: renderer,
                scope=scope,
            )
        finally:
            renderer.end_operation()

        if result.status == "FATAL":
            self.clear_screen(); print(ui_header("scan.error.title")); print("\n" + t("scan.error.database_core"))
            if result.error: print("\n" + t("common.error_detail", error=result.error))
            detail_path = self.logger.path if self.logger.path is not None else t("common.logging_unavailable")
            print("\n" + t("common.full_details_location", path=detail_path) + "\n"); self._pause("SCAN_FATAL_ACK"); return
        if result.status == "INCOMPLETE" and service.stop_controller.stop_event.is_set():
            print(ui_header("scan.stop.title")); print("\n" + t("scan.stop.summary", status="INCOMPLETE") + "\n")
            print(t("scan.stop.return_to_main", main_menu=t("label.main_menu")))
            self._pause("SCAN_STOP_ACK", "")
            return

        c = result.counters
        total_sources = len(getattr(self, "_last_preflight_source_order", selected))
        completed_sources = len(result.source_statuses)
        mode_text = t("scan.mode.force_reprobe") if force_reprobe else t("scan.mode.normal")
        print(ui_header("scan.complete.title"))
        print("\n" + t("scan.complete.status", status=result.status))
        print(t("scan.complete.mode", mode=mode_text))
        print(t("scan.complete.sources", completed=completed_sources, total=total_sources))
        print("\n" + t("scan.stats.files_seen", count=f"{c.files_seen:,}"))
        print(t("scan.stats.candidates", count=f"{c.files_candidates:,}"))
        print("\n" + make_subheader(t("scan.cache_heading")) + "\n")
        print(t("scan.stats.reused", count=f"{c.reused:,}"))
        print(t("scan.complete.cached_not_video", count=f"{c.cached_not_video:,}"))
        print("\n" + make_subheader(t("scan.probe_results_heading")) + "\n")
        print(t("scan.complete.probed", count=f"{c.probed:,}"))
        print(t("scan.stats.probe_successful", count=f"{c.successful:,}"))
        print(t("scan.stats.failed", count=f"{c.failed:,}"))
        print(t("scan.stats.not_video", count=f"{c.not_video:,}"))
        print("\n" + make_subheader(t("scan.catalog_heading")) + "\n")
        print(t("scan.complete.new", count=f"{c.new:,}"))
        print(t("scan.complete.changed", count=f"{c.changed:,}"))
        print(t("scan.complete.missing", count=f"{result.missing_marked:,}"))
        if "INCOMPLETE" in result.source_statuses: print("\n" + t("scan.complete.incomplete_warning"))
        print("\n" + t("common.elapsed", elapsed=format_hms(result.elapsed)) + "\n")
        if result.excluded_diagnostic_path is not None: print(t("scan.complete.excluded_diagnostic", path=result.excluded_diagnostic_path) + "\n")
        print(f"[1] {t('label.generate_reports')}"); print(f"[0] {t('label.main_menu')}")
        answer = self._prompt("\n" + t("common.choice_default") + " ", "SCAN_COMPLETE_ACTION") or "0"
        if answer == "1":
            self.logger.ui_selection(screen="SCAN_COMPLETE", action="GENERATE_REPORTS")
            self.generate_reports_menu()
        else:
            self.logger.ui_selection(screen="SCAN_COMPLETE", action="MAIN_MENU")

    def force_reprobe_menu(self) -> None:
        source_rows = self.source_manager.configured_source_rows()
        configured_paths = list(self.config.sources)
        if not configured_paths:
            self.clear_screen(); print(ui_header("label.force_reprobe")); print("\n" + t("source.none_configured") + "\n"); self._pause(); return
        while True:
            self.clear_screen(); print(ui_header("label.force_reprobe")); print(f"\n[1] {t('scan.force_reprobe.all_sources')}"); print(f"[2] {t('label.select_source')}"); print(f"[0] {t('common.back')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "FORCE_REPROBE_SCOPE") or "0"
            if answer == "0": return
            if answer == "1":
                self.logger.ui_selection(screen="FORCE_REPROBE", action="ALL_SOURCES")
                selected_sources = source_rows
                selected_paths = configured_paths
                scope = "ALL_SOURCES"
            elif answer == "2":
                if not source_rows:
                    print("\n" + t("preflight.no_sources_available") + "\n"); self._pause(); return
                selected = self.choose_single_source(source_rows)
                if selected is None: continue
                self.logger.ui_selection(screen="FORCE_REPROBE", action="SINGLE_SOURCE", path=selected["display_path"])
                selected_sources = [selected]
                selected_paths = [str(selected["display_path"])]
                scope = "SINGLE_SOURCE"
            else:
                self.logger.invalid_input(screen="FORCE_REPROBE", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause(); continue
            available = self.show_preflight(selected_sources, configured_paths=selected_paths)
            if available is not None:
                self._run_scan_operation(available, force_reprobe=True, scope=scope)
            return

    def scan_media_menu(self) -> None:
        while True:
            configured_paths = list(self.config.sources)
            source_rows = self.source_manager.configured_source_rows()
            self.clear_screen(); print(ui_header("label.scan_media")); print("\n" + make_subheader(t("label.media_sources")) + "\n")
            if configured_paths:
                for path in configured_paths: print(path)
            else: print(t("scan.sources.empty"))
            print(f"\n[1] {t('label.start_scan')}"); print(f"[2] {t('label.force_reprobe')}"); print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "SCAN_MEDIA_MENU") or "0"
            if choice == "0": return
            if choice == "1":
                self.logger.ui_selection(screen="SCAN_MEDIA", action="START_SCAN")
                if not configured_paths:
                    print("\n" + t("source.none_configured")); print(t("scan.configure_sources_hint", configuration=t("label.configuration"), paths=t("label.paths")) + "\n"); self._pause(); continue
                available = self.show_preflight(source_rows, configured_paths=configured_paths)
                if available is not None:
                    self._run_scan_operation(available, force_reprobe=False, scope="ALL_SOURCES")
                return
            if choice == "2":
                self.logger.ui_selection(screen="SCAN_MEDIA", action="FORCE_REPROBE")
                self.force_reprobe_menu(); return
            self.logger.invalid_input(screen="SCAN_MEDIA", value=choice)
            print("\n" + t("common.invalid_choice")); self._pause()

    def _refresh_xlsxwriter(self, component: str = "REPORT") -> bool:
        from .reports.report_excel import refresh_xlsxwriter_state
        self.xlsxwriter_available = refresh_xlsxwriter_state(self.logger, component)
        return self.xlsxwriter_available

    def excel_dependency_recovery(self) -> None:
        while True:
            self.clear_screen(); print(ui_header("excel_support.title")); print("\n" + t("excel_support.unavailable") + "\n"); print(t("common.readme_install_instructions") + "\n")
            print(f"[1] {t('excel_support.check_again')}"); print(f"[0] {t('common.back')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "EXCEL_DEPENDENCY_RECOVERY") or "0"
            if answer == "0":
                self.logger.ui_selection(screen="EXCEL_SUPPORT", action="BACK")
                return
            if answer != "1":
                self.logger.invalid_input(screen="EXCEL_SUPPORT", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK"); continue
            self.logger.ui_selection(screen="EXCEL_SUPPORT", action="CHECK_AGAIN")
            if self._refresh_xlsxwriter("REPORT"):
                print("\n" + t("excel_support.now_available") + "\n"); self._pause("EXCEL_AVAILABLE_ACK"); return
            print("\n" + t("excel_support.still_unavailable") + "\n"); self._pause("EXCEL_UNAVAILABLE_ACK")

    def report_scope_menu(self, sources: list[sqlite3.Row]) -> Optional[tuple[str, list[sqlite3.Row]]]:
        while True:
            self.clear_screen(); print(ui_header("report.source.title")); print(f"\n[1] {t('report.source.all_combined')}"); print(f"[2] {t('report.source.all_separate')}"); print(f"[3] {t('label.select_source')}"); print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "REPORT_SCOPE_MENU") or "0"
            if choice == "0":
                self.logger.ui_selection(screen="REPORT_SCOPE", action="BACK")
                return None
            if choice == "1":
                self.logger.ui_selection(screen="REPORT_SCOPE", action="ALL_COMBINED")
                return "combined", sources
            if choice == "2":
                self.logger.ui_selection(screen="REPORT_SCOPE", action="ALL_SEPARATE")
                return "separate", sources
            if choice == "3":
                self.logger.ui_selection(screen="REPORT_SCOPE", action="SELECT_SOURCE")
                selected = self.choose_single_source(sources)
                if selected is not None: return "single", [selected]
                continue
            self.logger.invalid_input(screen="REPORT_SCOPE", value=choice)
            print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK")

    def _show_report_eligibility(self, eligibility: Any) -> bool:
        if eligibility.blockers:
            self.clear_screen(); print(ui_header("report.blocked.title"))
            lines=[]
            for issue in eligibility.blockers:
                lines.append(t("report.blocked.no_completed_scan_item", source=issue.source_path))
            print("\n" + t("report.blocked.message", items="\n".join(f"- {line}" for line in lines)) + "\n"); self._pause("REPORT_BLOCKED_ACK"); return False
        if eligibility.warnings:
            self.clear_screen(); print(ui_header("report.warning.title")); lines=[]
            for issue in eligibility.warnings:
                if issue.kind == "LATEST_SCAN_STATUS": lines.append(t("report.warning.latest_scan_status", source=issue.source_path, status=issue.status))
                elif issue.kind == "SOURCE_UNAVAILABLE": lines.append(t("report.warning.source_unavailable", source=issue.source_path))
            print("\n" + t("report.warning.message", warnings="\n".join(f"- {line}" for line in lines)) + "\n")
            print(f"[1] {t('common.continue')}"); print(f"[0] {t('common.cancel')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "REPORT_WARNING_CONFIRMATION") or "0"
            decision = answer == "1"
            self.logger.ui_selection(screen="REPORT_WARNING", action="CONTINUE" if decision else "CANCEL")
            return decision
        return True

    def generate_reports_menu(self) -> None:
        from .reports.report_service import ReportService
        active = self.source_manager.configured_source_rows()
        if not active:
            self.clear_screen(); print(ui_header("label.generate_reports")); print("\n" + t("source.none_configured") + "\n"); self._pause("REPORT_NO_SOURCES_ACK"); return
        service = ReportService(self.db, self.logger, self.platform, self.source_manager, self.config, self.probe_service)
        while True:
            self.clear_screen(); print(ui_header("label.generate_reports")); print(f"\n[1] {t('report.format.all')}"); print(f"[2] {t('report.format.excel')}"); print(f"[3] {t('report.format.json')}"); print(f"[4] {t('report.format.txt')}"); print(f"[0] {t('common.back')}")
            choice = self._prompt("\n" + t("common.choice_default") + " ", "REPORT_FORMAT_MENU") or "0"
            if choice == "0":
                self.logger.ui_selection(screen="GENERATE_REPORTS", action="BACK")
                return
            format_map={"1":["excel","json","txt"],"2":["excel"],"3":["json"],"4":["txt"]}
            if choice not in format_map:
                self.logger.invalid_input(screen="GENERATE_REPORTS", value=choice)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK"); continue
            formats=format_map[choice]
            self.logger.ui_selection(screen="GENERATE_REPORTS", action="SELECT_FORMATS", formats=",".join(formats))
            if "excel" in formats and not self._refresh_xlsxwriter("REPORT"):
                self.excel_dependency_recovery(); continue
            scope=self.report_scope_menu(active)
            if scope is None: continue
            scope_mode, selected=scope
            eligibility=service.report_eligibility(selected)
            if not self._show_report_eligibility(eligibility): continue
            if scope_mode == "combined": progress_scope=t("report.source.all_combined")
            elif scope_mode == "separate": progress_scope=t("report.source.all_separate")
            else: progress_scope=str(selected[0]["display_path"])
            progress=ReportProgressRenderer(progress_scope, formats, self.config.refresh_seconds, self.clear_screen)
            result=service.generate(scope_mode, selected, formats, eligibility.online_map, progress)
            if result.output_dir is None:
                self.clear_screen(); print(ui_header("report.error.title")); print("\n" + t("report.error.folder_not_writable", error=result.error) + "\n"); self._pause("REPORT_ERROR_ACK"); continue
            self.clear_screen(); print(ui_header("report.complete.title"))
            created=[path for _,path,err in result.results if path is not None and err is None]
            errors=[(fmt,err) for fmt,path,err in result.results if err and fmt != "EMPTY"]
            empty_sources=[]
            for fmt,_,detail in result.results:
                if fmt == "EMPTY" and detail:
                    for source_path in detail.splitlines():
                        if source_path and source_path not in empty_sources: empty_sources.append(source_path)
            if created:
                print("\n" + t("report.complete.created_successfully") + "\n")
                for created_path in created: print(created_path)
            if empty_sources:
                print("\n" + t("report.complete.no_media_files") + "\n")
                for source_path in empty_sources: print(source_path)
            if errors:
                print("\n" + t("report.complete.some_outputs_failed") + "\n")
                for fmt,err in errors: print("- " + t("report.complete.output_error", format=fmt.upper(), error=err))
            print("\n" + t("report.complete.folder", path=result.output_dir) + "\n")
            print(f"[1] {t('report.complete.generate_another')}"); print(f"[0] {t('label.main_menu')}")
            answer = self._prompt("\n" + t("common.choice_default") + " ", "REPORT_COMPLETE_ACTION") or "0"
            if answer == "1":
                self.logger.ui_selection(screen="REPORT_COMPLETE", action="GENERATE_ANOTHER")
                continue
            self.logger.ui_selection(screen="REPORT_COMPLETE", action="MAIN_MENU")
            return

    def main_menu(self) -> None:
        while True:
            self.clear_screen(); print(ui_header("label.main_menu")); print(f"\n[1] {t('label.scan_media')}"); print(f"[2] {t('label.generate_reports')}"); print(f"[3] {t('label.configuration')}"); print(f"[4] {t('main.exit')}")
            answer = self._prompt("\n" + t("common.choice") + " ", "MAIN_MENU")
            if not answer:
                self.logger.invalid_input(screen="MAIN_MENU", value=answer)
                continue
            if answer == "1":
                self.logger.ui_selection(screen="MAIN_MENU", action="SCAN_MEDIA"); self.scan_media_menu()
            elif answer == "2":
                self.logger.ui_selection(screen="MAIN_MENU", action="GENERATE_REPORTS"); self.generate_reports_menu()
            elif answer == "3":
                self.logger.ui_selection(screen="MAIN_MENU", action="CONFIGURATION"); self.configuration_menu()
            elif answer == "4":
                self.logger.ui_selection(screen="MAIN_MENU", action="EXIT")
                return
            else:
                self.logger.invalid_input(screen="MAIN_MENU", value=answer)
                print("\n" + t("common.invalid_choice")); self._pause("INVALID_INPUT_ACK")


def _logged_pause(logger: Optional[RunLogger], action: str, message: Optional[str] = None, *, result: str = "CONTINUE") -> None:
    token = logger.begin_ui_wait(action) if logger is not None else None
    try:
        pause_enter(message)
    finally:
        if token is not None:
            logger.end_ui_wait(token, result=result)


def _print_config_recovery(result: Any) -> None:
    print(t("config.invalid_replaced"))
    if result.invalid_error:
        print(t("startup.details", error=result.invalid_error))
    if result.invalid_backup is not None:
        print(t("config.invalid_backup", path=result.invalid_backup))
    if getattr(result, "salvaged_sources", 0):
        print(t("config.invalid_salvaged_sources", count=result.salvaged_sources))


def _print_database_recovery(result: Any) -> None:
    print(t("startup.database_recovered"))
    if result.invalid_database_backup is not None:
        print(t("startup.database_recovered_backup", path=result.invalid_database_backup))
    if getattr(result, "invalid_wal_backup", None) is not None:
        print(t("startup.database_recovered_wal_backup", path=result.invalid_wal_backup))
    if getattr(result, "invalid_shm_backup", None) is not None:
        print(t("startup.database_recovered_shm_backup", path=result.invalid_shm_backup))
    print(t("startup.database_recovered_preserved"))


def show_config_invalid_replaced(result: Any, logger: Optional[RunLogger] = None) -> None:
    _print_config_recovery(result)
    _logged_pause(logger, "CONFIG_INVALID_ACK")


def show_database_recovered(result: Any, logger: RunLogger) -> None:
    _print_database_recovery(result)
    _logged_pause(logger, "DATABASE_RECOVERY_ACK")


def show_combined_startup_recovery(config_result: Any, db_result: Any, logger: RunLogger) -> None:
    print(ui_header("startup.recovery.title"))
    print("\n" + make_subheader(t("startup.recovery.configuration_heading")) + "\n")
    _print_config_recovery(config_result)
    print("\n" + make_subheader(t("startup.recovery.database_heading")) + "\n")
    _print_database_recovery(db_result)
    print()
    _logged_pause(logger, "STARTUP_RECOVERY_ACK")


def show_startup_app_root_error(path: Path, error: Optional[str], logger: Optional[RunLogger] = None) -> None:
    print(t("startup.app_folder_not_writable", app_name=APP_NAME, path=path))
    if error:
        print("\n" + t("startup.details", error=error))
    _logged_pause(logger, "APP_ROOT_FATAL_ACK", t("common.press_enter_exit"), result="EXIT")


def show_startup_data_dir_error(path: Path, logger: Optional[RunLogger] = None) -> None:
    print(t("startup.data_folder_not_writable", app_name=APP_NAME, path=path))
    _logged_pause(logger, "DATA_DIR_FATAL_ACK", t("common.press_enter_exit"), result="EXIT")


def show_logging_unavailable(error: Optional[BaseException] = None) -> None:
    print(t("startup.logging_unavailable_warning"))
    if error is not None:
        print(t("startup.details", error=error))


def show_python_too_old(current_version: str, logger: Optional[RunLogger] = None) -> None:
    print(t("startup.python_too_old", app_name=APP_NAME, version=PROGRAM_VERSION, minimum_version="3.11", current_version=current_version))
    _logged_pause(logger, "PYTHON_VERSION_FATAL_ACK", t("common.press_enter_exit"), result="EXIT")


def show_config_init_failed(error: BaseException, logger: Optional[RunLogger] = None) -> None:
    print(t("startup.config_init_failed", error=error))
    _logged_pause(logger, "CONFIG_INIT_FATAL_ACK", t("common.press_enter_exit"), result="EXIT")


def show_database_open_failed(logger: RunLogger, error: BaseException) -> None:
    print(t("startup.database_open_failed", app_name=APP_NAME))
    print(str(error))
    log_value = logger.path if logger.path is not None else t("common.logging_unavailable")
    print("\n" + t("common.log_location", path=log_value) + "\n")
    _logged_pause(logger, "DATABASE_OPEN_FATAL_ACK", t("common.press_enter_exit"), result="EXIT")


def show_fatal(platform: PlatformAdapter, logger: RunLogger, error: BaseException) -> None:
    platform.clear_screen()
    print(ui_header("fatal.title"))
    print("\n" + t("fatal.unexpected", app_name=APP_NAME))
    print("\n" + t("common.error_detail", error=error))
    detail_path = logger.path if logger.path is not None else t("common.logging_unavailable")
    print("\n" + t("common.full_details_location", path=detail_path) + "\n")
    _logged_pause(logger, "UNEXPECTED_FATAL_ACK", result="EXIT")
