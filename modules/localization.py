# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import json
from pathlib import Path
from string import Formatter
from typing import Any, Optional

from .constants import LOCALES_DIR
from .logger import RunLogger

CANONICAL_LOCALE = "en-US"

REQUIRED_CANONICAL_KEYS = frozenset({
    'common.back',
    'common.cancel',
    'common.choice',
    'common.choice_default',
    'common.continue',
    'common.elapsed',
    'common.error_detail',
    'common.full_details_location',
    'common.invalid_choice',
    'common.log_location',
    'common.logging_unavailable',
    'common.press_enter_continue',
    'common.press_enter_exit',
    'common.readme_install_instructions',
    'common.source_path',
    'config.invalid_backup',
    'config.invalid_replaced',
    'config.invalid_salvaged_sources',
    'excel_support.check_again',
    'excel_support.now_available',
    'excel_support.package_missing_error',
    'excel_support.still_unavailable',
    'excel_support.title',
    'excel_support.unavailable',
    'fatal.title',
    'fatal.unexpected',
    'ffprobe.retries',
    'ffprobe.workers_name',
    'ffprobe.timeout_name',
    'ffprobe.retries_name',
    'ffprobe.new_value',
    'ffprobe.current_value',
    'ffprobe.retries_invalid',
    'ffprobe.retries_prompt',
    'ffprobe.timeout',
    'ffprobe.timeout_invalid',
    'ffprobe.timeout_prompt',
    'ffprobe.workers',
    'ffprobe.workers_invalid',
    'ffprobe.workers_prompt',
    'label.add_source',
    'label.configuration',
    'label.export_sources',
    'label.ffprobe_settings',
    'label.force_reprobe',
    'label.generate_reports',
    'label.import_sources',
    'label.language',
    'label.main_menu',
    'label.media_sources',
    'label.paths',
    'label.remove_source',
    'label.scan_media',
    'label.scanning',
    'label.select_source',
    'label.start_scan',
    'language.current',
    'main.exit',
    'paths.export_sources_to_file',
    'paths.import_sources_from_file',
    'paths.sources.empty',
    'preflight.database.heading',
    'preflight.database.schema',
    'preflight.database.status_ok',
    'preflight.ffprobe.expected',
    'preflight.ffprobe.heading',
    'preflight.ffprobe.not_found',
    'preflight.ffprobe.required',
    'preflight.ffprobe.version',
    'preflight.no_sources_available',
    'preflight.scan_available_sources',
    'preflight.sources.available',
    'preflight.sources.total',
    'preflight.sources.unavailable',
    'preflight.sources.unavailable_heading',
    'preflight.title',
    'report.blocked.message',
    'report.blocked.no_completed_scan_item',
    'report.blocked.title',
    'report.complete.created_successfully',
    'report.complete.folder',
    'report.complete.generate_another',
    'report.complete.no_media_files',
    'report.complete.output_error',
    'report.complete.some_outputs_failed',
    'report.complete.title',
    'report.error.folder_not_writable',
    'report.error.title',
    'report.format.all',
    'report.format.excel',
    'report.format.json',
    'report.format.txt',
    'report.progress.generating',
    'report.progress.scope',
    'report.progress.state.complete',
    'report.progress.state.failed',
    'report.progress.state.generating',
    'report.progress.state.skipped',
    'report.progress.state.waiting',
    'report.progress.title',
    'report.source.all_combined',
    'report.source.all_separate',
    'report.source.title',
    'report.warning.latest_scan_status',
    'report.warning.message',
    'report.warning.source_unavailable',
    'report.warning.title',
    'scan.cache_heading',
    'scan.catalog_heading',
    'scan.complete.cached_not_video',
    'scan.complete.changed',
    'scan.complete.excluded_diagnostic',
    'scan.complete.incomplete_warning',
    'scan.complete.missing',
    'scan.complete.mode',
    'scan.complete.new',
    'scan.complete.probed',
    'scan.complete.sources',
    'scan.complete.status',
    'scan.complete.title',
    'scan.completed_sources_heading',
    'scan.configure_sources_hint',
    'scan.current_source_heading',
    'scan.error.database_core',
    'scan.error.title',
    'scan.force_reprobe.all_sources',
    'scan.mode',
    'scan.mode.force_reprobe',
    'scan.mode.normal',
    'scan.probe_results_heading',
    'scan.progress.cached_not_video',
    'scan.progress.excluded',
    'scan.progress.need_probe',
    'scan.progress.phase_enumerating',
    'scan.progress.phase_enumeration_complete',
    'scan.progress.phase_probing',
    'scan.progress.probing',
    'scan.progress.stop_hint',
    'scan.source.status',
    'scan.sources.empty',
    'scan.stats.candidates',
    'scan.stats.failed',
    'scan.stats.files_seen',
    'scan.stats.not_video',
    'scan.stats.probe_successful',
    'scan.stats.reused',
    'scan.stop.emergency_requested',
    'scan.stop.graceful_requested',
    'scan.stop.return_to_main',
    'scan.stop.summary',
    'scan.stop.title',
    'source.add.already_configured',
    'source.add.already_configured_as',
    'source.add.already_covered',
    'source.add.cancelled',
    'source.add.error',
    'source.add.folder_not_found',
    'source.add.instructions',
    'source.add.overlap_unverified',
    'source.add.path_empty',
    'source.add.path_prompt',
    'source.add.reactivated',
    'source.add.success',
    'source.export.failed',
    'source.export.success',
    'source.identity.read_only_warning',
    'source.import.file_not_found',
    'source.import.path_prompt',
    'source.import.read_failed',
    'source.network_shortcut.unresolved',
    'source.none_configured',
    'source.overlap.already_configured_heading',
    'source.overlap.already_covered_heading',
    'source.overlap.already_covered_item',
    'source.overlap.catalog_reconcile',
    'source.overlap.continue_valid',
    'source.overlap.duplicate_item',
    'source.overlap.duplicates_heading',
    'source.overlap.invalid_heading',
    'source.overlap.invalid_item',
    'source.overlap.new_source',
    'source.overlap.replace_effect',
    'source.overlap.replace_heading',
    'source.overlap.replace_source',
    'source.overlap.replace_sources',
    'source.overlap.replaced_multiple',
    'source.overlap.replaced_single',
    'source.overlap.selected_covered_heading',
    'source.overlap.selected_covered_item',
    'source.overlap.title',
    'source.overlap.unavailable_heading',
    'source.overlap.unavailable_item',
    'source.remove.action',
    'source.remove.all_action',
    'source.remove.all_completed',
    'source.remove.all_confirmation',
    'source.remove.all_title',
    'source.remove.completed',
    'source.remove.empty',
    'source.remove.single_confirmation',
    'source.resolution.input',
    'source.resolution.resolved',
    'source.resolution.resolved_heading',
    'source.resolution.same_storage',
    'source.resolution.same_storage_heading',
    'source.resolution.title',
    'source.resolution.unc_note_multiple',
    'source.resolution.unc_note_single',
    'source.summary.added_heading',
    'source.summary.no_changes',
    'source.summary.removed_heading',
    'startup.already_running',
    'startup.app_folder_not_writable',
    'startup.config_init_failed',
    'startup.data_folder_not_writable',
    'startup.database_open_failed',
    'startup.database_recovered',
    'startup.database_recovered_backup',
    'startup.database_recovered_preserved',
    'startup.recovery.title',
    'startup.recovery.database_heading',
    'startup.recovery.configuration_heading',
    'startup.database_recovered_wal_backup',
    'startup.database_recovered_shm_backup',
    'startup.details',
    'startup.logging_unavailable_warning',
    'startup.python_too_old',
    'startup.windows_only',
})



class LocaleError(Exception):
    pass


def _json_no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LocaleError(f"Duplicate locale key: {key}")
        result[key] = value
    return result


def _placeholder_names(value: str) -> frozenset[str]:
    names: set[str] = set()
    try:
        for _, field_name, _, _ in Formatter().parse(value):
            if field_name is None:
                continue
            if not field_name:
                raise LocaleError("Empty placeholder name")
            names.add(field_name)
    except ValueError as exc:
        raise LocaleError(f"Malformed placeholder syntax: {exc}") from exc
    return frozenset(names)


class LocaleManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.canonical: dict[str, str] = {}
        self.active: dict[str, str] = {}
        self.active_locale = CANONICAL_LOCALE
        self.canonical_name = ""
        self.active_name = ""
        self.canonical_error: Optional[str] = None
        self.canonical_meta_version = ""
        self.canonical_path = self.directory / f"{CANONICAL_LOCALE}.json"
        self._canonical_placeholders: dict[str, frozenset[str]] = {}
        self.logger: Optional[RunLogger] = None

    def set_logger(self, logger: Optional[RunLogger]) -> None:
        self.logger = logger

    def _log_warning(self, component: str, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(component, message)

    def _log_info(self, component: str, message: str) -> None:
        if self.logger is not None:
            self.logger.info(component, message)

    def _read_json(self, path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle, object_pairs_hook=_json_no_duplicate_pairs)
        if not isinstance(data, dict):
            raise LocaleError("Top-level locale value must be a JSON object")
        return data

    def _validate_metadata(self, data: dict[str, Any], path: Path) -> tuple[str, str, str]:
        code = data.get("_meta.code")
        locale = data.get("_meta.locale")
        name = data.get("_meta.name")
        if not all(isinstance(v, str) and v.strip() for v in (code, locale, name)):
            raise LocaleError("Locale metadata is missing or invalid")
        if path.name != f"{locale}.json":
            raise LocaleError(f"Filename does not match _meta.locale ({locale})")
        return code.strip(), locale.strip(), name.strip()

    def load_canonical(self) -> bool:
        path = self.directory / f"{CANONICAL_LOCALE}.json"
        self.canonical_path = path
        try:
            data = self._read_json(path)
            _, locale, name = self._validate_metadata(data, path)
            if locale != CANONICAL_LOCALE:
                raise LocaleError("Canonical locale tag must be en-US")

            missing = sorted(key for key in REQUIRED_CANONICAL_KEYS if key not in data)
            if missing:
                raise LocaleError("Missing required canonical locale keys: " + ", ".join(missing))

            invalid_types = sorted(
                key for key in REQUIRED_CANONICAL_KEYS
                if key in data and not isinstance(data[key], str)
            )
            if invalid_types:
                raise LocaleError(
                    "Required canonical locale values must be strings: " + ", ".join(invalid_types)
                )

            canonical: dict[str, str] = {}
            placeholders: dict[str, frozenset[str]] = {}
            for key, value in data.items():
                if key.startswith("_meta.") or not isinstance(value, str):
                    continue
                placeholders[key] = _placeholder_names(value)
                canonical[key] = value

            self.canonical = canonical
            self._canonical_placeholders = placeholders
            self.active = dict(canonical)
            self.active_locale = CANONICAL_LOCALE
            self.canonical_name = name
            self.active_name = name
            meta_version = data.get("_meta.version")
            self.canonical_meta_version = meta_version.strip() if isinstance(meta_version, str) else ""
            self.canonical_error = None
            return True
        except Exception as exc:
            self.canonical = {}
            self._canonical_placeholders = {}
            self.active = {}
            self.active_locale = CANONICAL_LOCALE
            self.canonical_name = ""
            self.active_name = ""
            self.canonical_meta_version = ""
            self.canonical_error = str(exc)
            return False

    def _load_override(self, locale: str, *, log_warnings: bool) -> Optional[tuple[dict[str, str], str]]:
        path = self.directory / f"{locale}.json"
        try:
            data = self._read_json(path)
            _, file_locale, name = self._validate_metadata(data, path)
            if file_locale != locale:
                raise LocaleError(f"Locale tag mismatch: requested {locale}, file declares {file_locale}")
        except Exception as exc:
            if log_warnings:
                self._log_warning("LOCALE", f"Locale unavailable locale={locale}: {exc}")
            return None

        overrides: dict[str, str] = {}
        for key, value in data.items():
            if key.startswith("_meta."):
                continue
            if key not in self.canonical:
                if log_warnings:
                    self._log_warning("LOCALE", f"Unknown locale key ignored locale={locale} key={key}")
                continue
            if not isinstance(value, str):
                if log_warnings:
                    self._log_warning("LOCALE", f"Invalid locale value ignored locale={locale} key={key}")
                continue
            if not value.strip():
                continue
            try:
                actual = _placeholder_names(value)
            except LocaleError as exc:
                if log_warnings:
                    self._log_warning("LOCALE", f"Invalid placeholder syntax ignored locale={locale} key={key}: {exc}")
                continue
            expected = self._canonical_placeholders[key]
            if actual != expected:
                if log_warnings:
                    self._log_warning(
                        "LOCALE",
                        f"Placeholder mismatch ignored locale={locale} key={key} expected={sorted(expected)} actual={sorted(actual)}",
                    )
                continue
            overrides[key] = value
        return overrides, name

    def activate(self, locale: str, *, log_warnings: bool = True) -> bool:
        requested = str(locale or "").strip() or CANONICAL_LOCALE
        if requested == CANONICAL_LOCALE:
            self.active = dict(self.canonical)
            self.active_locale = CANONICAL_LOCALE
            self.active_name = self.canonical_name
            return True
        loaded = self._load_override(requested, log_warnings=log_warnings)
        if loaded is None:
            self.active = dict(self.canonical)
            self.active_locale = CANONICAL_LOCALE
            self.active_name = self.canonical_name
            return False
        overrides, name = loaded
        merged = dict(self.canonical)
        merged.update(overrides)
        self.active = merged
        self.active_locale = requested
        self.active_name = name
        if log_warnings:
            self._log_info(
                "LOCALE",
                f"Loaded locale={requested} overrides={len(overrides)} fallback={len(self.canonical) - len(overrides)}",
            )
        return True

    def discover(self) -> list[tuple[str, str]]:
        found: dict[str, str] = {CANONICAL_LOCALE: self.canonical_name}
        try:
            paths = sorted(self.directory.glob("*.json"), key=lambda p: p.name.casefold())
        except Exception as exc:
            self._log_warning("LOCALE", f"Locale discovery failed: {exc}")
            return [(CANONICAL_LOCALE, self.canonical_name)]
        for path in paths:
            if path.name == f"{CANONICAL_LOCALE}.json":
                continue
            locale = path.stem
            loaded = self._load_override(locale, log_warnings=True)
            if loaded is None:
                continue
            _, name = loaded
            if locale in found:
                self._log_warning("LOCALE", f"Duplicate locale ignored locale={locale} path={path}")
                continue
            found[locale] = name
        others = sorted(
            ((locale, name) for locale, name in found.items() if locale != CANONICAL_LOCALE),
            key=lambda item: item[1].casefold(),
        )
        return [(CANONICAL_LOCALE, found[CANONICAL_LOCALE]), *others]

    def text(self, key: str, **values: Any) -> str:
        template = self.active.get(key) or self.canonical.get(key)
        if template is None:
            raise LocaleError(f"Unknown canonical locale key: {key}")
        try:
            return template.format(**values)
        except Exception as exc:
            fallback = self.canonical.get(key)
            if fallback is None:
                raise
            self._log_warning("LOCALE", f"Locale formatting failed key={key}: {exc}; using en-US")
            return fallback.format(**values)


LOCALES = LocaleManager(LOCALES_DIR)


def t(key: str, **values: Any) -> str:
    return LOCALES.text(key, **values)
