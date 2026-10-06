# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import hashlib
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

LOG_LEVEL_ERROR_ONLY = "error_only"
LOG_LEVEL_NORMAL = "normal"
LOG_LEVEL_FULL = "full"
LOG_LEVEL_FULL_WITH_EXCLUDED = "full_with_excluded_file"
LOG_LEVELS = {
    LOG_LEVEL_ERROR_ONLY,
    LOG_LEVEL_NORMAL,
    LOG_LEVEL_FULL,
    LOG_LEVEL_FULL_WITH_EXCLUDED,
}


@dataclass(frozen=True)
class UIWaitToken:
    action: str
    started: float


def format_elapsed(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def unique_log_path(directory: Path, timestamp: str) -> Path:
    candidate = directory / f"MediaCatalog_{timestamp}.log"
    index = 2
    while candidate.exists():
        candidate = directory / f"MediaCatalog_{timestamp}_{index}.log"
        index += 1
    return candidate


def excluded_path_for_log(log_path: Path) -> Path:
    return log_path.with_name(f"{log_path.stem}_excluded.txt")


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def compute_build_fingerprint(app_root: Path) -> tuple[str, int]:
    """Return deterministic SHA-256 for shipped Python + locale content."""
    candidates: list[Path] = []
    main = app_root / "main.py"
    if main.is_file():
        candidates.append(main)
    modules = app_root / "modules"
    if modules.is_dir():
        candidates.extend(path for path in modules.rglob("*.py") if path.is_file())
    locales = app_root / "locales"
    if locales.is_dir():
        candidates.extend(path for path in locales.glob("*.json") if path.is_file())

    ordered = sorted(candidates, key=lambda path: path.relative_to(app_root).as_posix())
    digest = hashlib.sha256()
    for path in ordered:
        relative = path.relative_to(app_root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest(), len(ordered)


class RunLogger:
    def __init__(self, path: Optional[Path], *, level: str = LOG_LEVEL_NORMAL) -> None:
        self.path = path
        self.open_error: Optional[Exception] = None
        self._handle: Optional[Any] = None
        self._lock = threading.Lock()
        self._level = self._normalize_level(level)
        self.session_id = uuid.uuid4().hex
        self._session_started_perf = time.perf_counter()
        self._ui_wait_seconds = 0.0
        self._operation_counter = 0

        if path is not None:
            try:
                self._handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
            except Exception as exc:
                self._handle = None
                self.open_error = exc
                self.path = None

    @staticmethod
    def _normalize_level(level: str) -> str:
        value = str(level).strip().casefold()
        return value if value in LOG_LEVELS else LOG_LEVEL_NORMAL

    @property
    def available(self) -> bool:
        return self._handle is not None

    @property
    def level(self) -> str:
        return self._level

    @property
    def ui_wait_seconds(self) -> float:
        return self._ui_wait_seconds

    @property
    def session_wall_seconds(self) -> float:
        return max(0.0, time.perf_counter() - self._session_started_perf)

    @property
    def session_active_seconds(self) -> float:
        return max(0.0, self.session_wall_seconds - self._ui_wait_seconds)

    @property
    def full_enabled(self) -> bool:
        return self._level in {LOG_LEVEL_FULL, LOG_LEVEL_FULL_WITH_EXCLUDED}

    @property
    def excluded_file_enabled(self) -> bool:
        return self._level == LOG_LEVEL_FULL_WITH_EXCLUDED

    def set_level(self, level: str) -> None:
        self._level = self._normalize_level(level)

    def next_operation_id(self, prefix: str) -> str:
        with self._lock:
            self._operation_counter += 1
            number = self._operation_counter
        clean = "".join(ch for ch in str(prefix).upper() if ch.isalnum() or ch in "_-") or "OP"
        return f"{clean}-{number:04d}"

    def _should_write(self, level: str) -> bool:
        level = level.upper()
        if self._level == LOG_LEVEL_ERROR_ONLY:
            # Keep warnings because access/dependency/recovery failures are intentionally
            # represented as WARNING in several subsystems.
            return level in {"WARNING", "ERROR", "CRITICAL"}
        if self._level == LOG_LEVEL_NORMAL:
            return level in {"INFO", "WARNING", "ERROR", "CRITICAL"}
        return level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    def log(
        self,
        level: str,
        component: str,
        message: str,
        *,
        exc_info: bool = False,
        force: bool = False,
    ) -> None:
        level = level.upper()
        if not force and not self._should_write(level):
            return
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        component = component.upper()
        prefix = f"{stamp} [{level}] [{component}] "
        lines = str(message).splitlines() or [""]
        if exc_info:
            trace = traceback.format_exc().rstrip()
            if trace and trace != "NoneType: None":
                lines.extend(trace.splitlines())
        if self._handle is not None:
            with self._lock:
                try:
                    for line in lines:
                        self._handle.write(prefix + line + "\n")
                    self._handle.flush()
                except Exception:
                    pass

    def debug(self, component: str, message: str) -> None:
        self.log("DEBUG", component, message)

    def info(self, component: str, message: str) -> None:
        self.log("INFO", component, message)

    def essential_info(self, component: str, message: str) -> None:
        """Write minimal diagnostic context regardless of configured verbosity."""
        self.log("INFO", component, message, force=True)

    def identity(self, component: str, message: str) -> None:
        """Write essential build/session identity regardless of configured verbosity."""
        self.essential_info(component, message)

    def warning(self, component: str, message: str) -> None:
        self.log("WARNING", component, message)

    def error(self, component: str, message: str, *, exc_info: bool = False) -> None:
        self.log("ERROR", component, message, exc_info=exc_info)

    def critical(self, component: str, message: str, *, exc_info: bool = False) -> None:
        self.log("CRITICAL", component, message, exc_info=exc_info)

    def ui_selection(self, *, screen: str, action: str, **values: Any) -> None:
        details = " ".join(f"{key}={value!r}" for key, value in values.items())
        suffix = f" {details}" if details else ""
        self.info("UI", f"Selection screen={screen} action={action}{suffix}")

    def setting_changed(self, key: str, old: Any, new: Any) -> None:
        self.info("UI", f"Setting changed key={key} old={old!r} new={new!r}")

    def invalid_input(self, *, screen: str, value: Any) -> None:
        self.debug("UI", f"Invalid input screen={screen} value={value!r}")

    def begin_ui_wait(self, action: str) -> UIWaitToken:
        token = UIWaitToken(str(action), time.perf_counter())
        self.info("UI", f"Waiting action={token.action}")
        return token

    def end_ui_wait(self, token: UIWaitToken, *, result: Optional[str] = None) -> float:
        elapsed = max(0.0, time.perf_counter() - token.started)
        with self._lock:
            self._ui_wait_seconds += elapsed
        suffix = f" result={result}" if result is not None else ""
        self.info("UI", f"Wait finished action={token.action}{suffix} wait={format_elapsed(elapsed)}")
        return elapsed

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
                self._handle.close()
            except Exception:
                pass
            self._handle = None
