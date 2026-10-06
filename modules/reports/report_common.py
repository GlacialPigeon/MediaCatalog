# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

from typing import Any, Optional

REPORT_SCHEMA_VERSION = 1

def scope_summary(source_packages: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [f for pkg in source_packages for f in pkg["successful"]]
    failed = [f for pkg in source_packages for f in pkg["failed"]]
    missing = [f for pkg in source_packages for f in pkg["missing"]]
    active = [f for pkg in source_packages for f in pkg["active"]]
    total_size = sum(int(f["size_bytes"] or 0) for f in active)
    runtime = sum(float(f["duration_seconds"] or 0.0) for f in successful)
    return {
        "total_files": len(active),
        "total_size_bytes": total_size,
        "successful": len(successful),
        "failed": len(failed),
        "missing": len(missing),
        "total_runtime_seconds": runtime,
    }

def format_compact_runtime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minute, sec = divmod(seconds, 60)
    hour, minute = divmod(minute, 60)
    day, hour = divmod(hour, 24)
    year, day = divmod(day, 365)
    parts = []
    if year:
        parts.append(f"{year}y")
    if day or parts:
        parts.append(f"{day}d")
    if hour or parts:
        parts.append(f"{hour}h")
    if minute or parts:
        parts.append(f"{minute}m")
    parts.append(f"{sec}s")
    return " ".join(parts)

def format_size_dynamic(size_bytes: int) -> str:
    gib = size_bytes / (1024 ** 3)
    tib = size_bytes / (1024 ** 4)
    pib = size_bytes / (1024 ** 5)
    if pib >= 1:
        return f"{pib:.2f} PB"
    if tib >= 1:
        return f"{tib:.2f} TB"
    return f"{gib:.2f} GB"

def bool_text(value: Any) -> str:
    if value is None:
        return "UNKNOWN"
    return "Yes" if bool(value) else "No"

def source_scope_state(package: dict[str, Any], online: Optional[bool] = None) -> dict[str, Any]:
    latest = package["latest_scan"]
    completed = package["completed_scan"]
    return {
        "latest_scan_status": latest.get("status") if latest else None,
        "latest_scan_id": latest.get("scan_id") if latest else None,
        "last_completed_scan_id": completed.get("scan_id") if completed else None,
        "last_completed_scan_at_utc": completed.get("finished_at_utc") if completed else None,
        "currently_online": online,
    }

def size_category(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "UNKNOWN"
    gib = size_bytes / (1024 ** 3)
    if gib < 1:
        return "<1 GB"
    if gib < 5:
        return "1-5 GB"
    if gib < 10:
        return "5-10 GB"
    if gib < 20:
        return "10-20 GB"
    return "20+ GB"

