# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .datetime_helpers import local_timestamp_compact
from .localization import CANONICAL_LOCALE
from .logger import RunLogger

CONFIG_SCHEMA_VERSION = 4
LOGGING_LEVELS = (
    "error_only",
    "normal",
    "full",
    "full_with_excluded_file",
)

COMMON_DEFAULT_EXCLUDED_EXTENSIONS = [
    ".nfo", ".txt", ".log", ".xml", ".json", ".md", ".ini",
    ".pdf", ".csv", ".tsv", ".rtf",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
    ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup",
    ".mp3", ".flac", ".wav", ".aac", ".m4a", ".opus", ".ac3", ".eac3", ".dts", ".wma",
    ".cue", ".url",
]



def peek_logging_level(path: Path) -> str:
    """Read only a valid requested logging level without mutating config state."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return "normal"
        section = raw.get("logging")
        if not isinstance(section, dict):
            return "normal"
        level = section.get("level")
        return str(level) if isinstance(level, str) and level in LOGGING_LEVELS else "normal"
    except Exception:
        return "normal"

COMMON_DEFAULT_CONFIG: dict[str, Any] = {
    "ConfigSchemaVersion": CONFIG_SCHEMA_VERSION,
    "locale": "en-US",
    "sources": [],
    "scan": {
        "workers": 8,
        "timeout_seconds": 30,
        "retries": 1,
        "queue_capacity": 256,
        "progress_refresh_seconds": 0.5,
        "follow_reparse_points": False,
        "excluded_directories": [],
        "excluded_non_video_extensions": COMMON_DEFAULT_EXCLUDED_EXTENSIONS,
    },
    "logging": {
        "level": "normal",
    },
    "excel": {
        "max_width_global": 80,
        "font_name": "Arial",
        "theme": {
            "header_background": "#1F4E78",
            "header_text": "#FFFFFF",
            "zebra_a_background": "#F2F2F2",
            "zebra_a_text": "#000000",
            "zebra_b_background": "#FFFFFF",
            "zebra_b_text": "#000000",
            "summary_section_fill": "#D9EAF7",
            "summary_section_text": "#000000",
            "border": "#D9D9D9",
        },
    },
}


def _merge_unique(base: Iterable[str], additions: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in [*base, *additions]:
        value = str(raw)
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def build_default_config(
    platform_excluded_directories: Iterable[str],
    platform_excluded_extensions: Iterable[str],
) -> dict[str, Any]:
    data = copy.deepcopy(COMMON_DEFAULT_CONFIG)
    data["scan"]["excluded_directories"] = _merge_unique([], platform_excluded_directories)
    data["scan"]["excluded_non_video_extensions"] = _merge_unique(
        COMMON_DEFAULT_EXCLUDED_EXTENSIONS,
        platform_excluded_extensions,
    )
    return data


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    try:
        with open(partial, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    except Exception:
        try:
            if partial.exists():
                partial.unlink()
        except Exception:
            pass
        raise


def atomic_write_json(path: Path, data: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    else:
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    atomic_write_text(path, text)


def _hex_color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value) is not None


def _get_nested(data: dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def _set_nested(data: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    cur = data
    for key in keys[:-1]:
        if not isinstance(cur.get(key), dict):
            cur[key] = {}
        cur = cur[key]
    cur[keys[-1]] = value


class ConfigSchemaError(ValueError):
    pass


def _schema_error(path: str, detail: str) -> ConfigSchemaError:
    return ConfigSchemaError(f"{path}: {detail}")


def _validate_exact_keys(data: dict[str, Any], expected: dict[str, Any], path: str) -> None:
    actual_keys = set(data)
    expected_keys = set(expected)
    unknown = sorted(actual_keys - expected_keys)
    if unknown:
        key = unknown[0]
        full = f"{path}.{key}" if path else key
        raise _schema_error(full, "unknown key")
    missing = sorted(expected_keys - actual_keys)
    if missing:
        key = missing[0]
        full = f"{path}.{key}" if path else key
        raise _schema_error(full, "missing required key")


def _valid_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and bool(item.strip()) for item in value)


def _validated_sources_for_salvage(raw: Any) -> list[str] | None:
    if not isinstance(raw, dict) or "sources" not in raw:
        return None
    sources = raw.get("sources")
    if not _valid_string_list(sources):
        return None
    return [item.strip() for item in sources]


def validate_config(
    config: dict[str, Any],
    defaults: dict[str, Any],
    logger: RunLogger,
) -> tuple[dict[str, Any], bool, bool]:
    """Validate schema 4 strictly; return normalized runtime config, changed, newer_schema(False)."""
    if not isinstance(config, dict):
        raise _schema_error("config", "top-level value must be a JSON object")

    _validate_exact_keys(config, defaults, "")

    schema = config.get("ConfigSchemaVersion")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _schema_error("ConfigSchemaVersion", "must be an integer")
    if schema != CONFIG_SCHEMA_VERSION:
        raise _schema_error(
            "ConfigSchemaVersion",
            f"unsupported schema {schema}; expected {CONFIG_SCHEMA_VERSION}",
        )

    locale = config.get("locale")
    if not isinstance(locale, str) or not locale.strip():
        raise _schema_error("locale", "must be a non-empty string")

    sources = config.get("sources")
    if not _valid_string_list(sources):
        raise _schema_error("sources", "must be a list of non-empty strings")

    scan = config.get("scan")
    if not isinstance(scan, dict):
        raise _schema_error("scan", "must be an object")
    _validate_exact_keys(scan, defaults["scan"], "scan")

    workers = scan.get("workers")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
        raise _schema_error("scan.workers", "must be a positive integer")
    timeout = scan.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise _schema_error("scan.timeout_seconds", "must be a number greater than 0")
    retries = scan.get("retries")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise _schema_error("scan.retries", "must be 0 or a positive integer")
    queue_capacity = scan.get("queue_capacity")
    if not isinstance(queue_capacity, int) or isinstance(queue_capacity, bool) or queue_capacity <= 0:
        raise _schema_error("scan.queue_capacity", "must be a positive integer")
    refresh = scan.get("progress_refresh_seconds")
    if not isinstance(refresh, (int, float)) or isinstance(refresh, bool) or refresh <= 0:
        raise _schema_error("scan.progress_refresh_seconds", "must be a number greater than 0")
    if not isinstance(scan.get("follow_reparse_points"), bool):
        raise _schema_error("scan.follow_reparse_points", "must be true or false")
    if not _valid_string_list(scan.get("excluded_directories")) and scan.get("excluded_directories") != []:
        raise _schema_error("scan.excluded_directories", "must be a list of non-empty strings")
    if not _valid_string_list(scan.get("excluded_non_video_extensions")) and scan.get("excluded_non_video_extensions") != []:
        raise _schema_error("scan.excluded_non_video_extensions", "must be a list of non-empty strings")

    logging_section = config.get("logging")
    if not isinstance(logging_section, dict):
        raise _schema_error("logging", "must be an object")
    _validate_exact_keys(logging_section, defaults["logging"], "logging")
    level = logging_section.get("level")
    if not isinstance(level, str) or level not in LOGGING_LEVELS:
        raise _schema_error("logging.level", "must be one of: " + ", ".join(LOGGING_LEVELS))

    excel = config.get("excel")
    if not isinstance(excel, dict):
        raise _schema_error("excel", "must be an object")
    _validate_exact_keys(excel, defaults["excel"], "excel")
    max_width = excel.get("max_width_global")
    if not isinstance(max_width, (int, float)) or isinstance(max_width, bool) or max_width <= 0:
        raise _schema_error("excel.max_width_global", "must be a number greater than 0")
    font_name = excel.get("font_name")
    if not isinstance(font_name, str) or not font_name.strip():
        raise _schema_error("excel.font_name", "must be a non-empty string")

    theme = excel.get("theme")
    if not isinstance(theme, dict):
        raise _schema_error("excel.theme", "must be an object")
    _validate_exact_keys(theme, defaults["excel"]["theme"], "excel.theme")
    for key in defaults["excel"]["theme"]:
        if not _hex_color(theme.get(key)):
            raise _schema_error(f"excel.theme.{key}", "must be a #RRGGBB color")

    # The schema is valid. Apply only harmless canonical formatting; source hierarchy
    # normalization is deliberately performed later by SourceManager at startup.
    data = copy.deepcopy(config)
    changed = False

    trimmed_locale = locale.strip()
    if trimmed_locale != locale:
        data["locale"] = trimmed_locale
        changed = True

    normalized_exts: list[str] = []
    for item in scan["excluded_non_video_extensions"]:
        value = item.strip().casefold()
        if value and not value.startswith("."):
            value = "." + value
        if value and value not in normalized_exts:
            normalized_exts.append(value)
    if normalized_exts != scan["excluded_non_video_extensions"]:
        data["scan"]["excluded_non_video_extensions"] = normalized_exts
        changed = True

    return data, changed, False


@dataclass(frozen=True)
class ConfigLoadResult:
    invalid_replaced: bool = False
    invalid_backup: Path | None = None
    invalid_error: str | None = None
    salvaged_sources: int = 0


class ConfigManager:
    def __init__(self, path: Path, defaults: dict[str, Any], logger: RunLogger) -> None:
        self.path = path
        self.defaults = copy.deepcopy(defaults)
        self.logger = logger
        self.data: dict[str, Any] = copy.deepcopy(self.defaults)
        self.newer_schema = False

    def load(self) -> ConfigLoadResult:
        if not self.path.exists():
            self.data = copy.deepcopy(self.defaults)
            atomic_write_json(self.path, self.data)
            self.logger.info("CONFIG", "Created default config.json.")
            return ConfigLoadResult()

        raw: Any = None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ConfigSchemaError("config: top-level value must be a JSON object")
            validated, changed, self.newer_schema = validate_config(raw, self.defaults, self.logger)
        except Exception as exc:
            stamp = local_timestamp_compact()
            backup = self.path.with_name(f"config.invalid_{stamp}.json")
            salvage_sources = _validated_sources_for_salvage(raw)
            try:
                shutil.move(str(self.path), str(backup))
            except Exception as backup_exc:
                self.logger.critical(
                    "CONFIG",
                    f"Could not preserve invalid config at {backup}: {backup_exc}",
                    exc_info=True,
                )
                raise RuntimeError(f"Could not preserve invalid config.json: {backup_exc}") from backup_exc

            self.data = copy.deepcopy(self.defaults)
            if salvage_sources is not None:
                self.data["sources"] = salvage_sources
            atomic_write_json(self.path, self.data)
            salvage_note = (
                f"; salvaged_sources={len(salvage_sources)}"
                if salvage_sources is not None
                else "; salvaged_sources=0"
            )
            self.logger.error(
                "CONFIG",
                f"Invalid config was replaced with defaults: {exc}; backup={backup}{salvage_note}",
            )
            return ConfigLoadResult(True, backup, str(exc), len(salvage_sources or []))

        self.data = validated
        if changed:
            atomic_write_json(self.path, self.data)
            self.logger.info("CONFIG", "Normalized config.json formatting.")
        return ConfigLoadResult()

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    @property
    def language(self) -> str:
        return str(self.data.get("locale") or CANONICAL_LOCALE)

    @property
    def sources(self) -> list[str]:
        return [str(x) for x in self.data.get("sources", [])]

    def set_sources(self, sources: Sequence[str]) -> None:
        self.data["sources"] = [str(value) for value in sources]
        self.save()

    @property
    def logging_level(self) -> str:
        value = str(_get_nested(self.data, "logging", "level") or "normal")
        return value if value in LOGGING_LEVELS else "normal"

    @property
    def workers(self) -> int:
        return int(self.data["scan"]["workers"])

    @property
    def timeout(self) -> float:
        return float(self.data["scan"]["timeout_seconds"])

    @property
    def retries(self) -> int:
        return int(self.data["scan"]["retries"])

    @property
    def max_attempts(self) -> int:
        return self.retries + 1

    @property
    def queue_capacity(self) -> int:
        return int(self.data["scan"]["queue_capacity"])

    @property
    def refresh_seconds(self) -> float:
        return float(self.data["scan"]["progress_refresh_seconds"])

    @property
    def follow_reparse_points(self) -> bool:
        return bool(self.data["scan"]["follow_reparse_points"])

    @property
    def excluded_extensions(self) -> set[str]:
        return {str(x).casefold() for x in self.data["scan"]["excluded_non_video_extensions"]}

    @property
    def excluded_directories(self) -> set[str]:
        return {str(x).casefold() for x in self.data["scan"]["excluded_directories"]}
