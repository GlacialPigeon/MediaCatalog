# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..config import CONFIG_SCHEMA_VERSION
from ..constants import PROGRAM_VERSION
from ..database import DATABASE_SCHEMA_VERSION
from ..datetime_helpers import utc_now_iso
from ..logger import RunLogger
from .report_common import REPORT_SCHEMA_VERSION, scope_summary, source_scope_state

JSON_SCHEMA_VERSION = 1

def _clean_json_stream(row: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in row.items():
        if key.endswith("_stream_id") or key == "file_id":
            continue
        if key.startswith("is_") or key in ("dolby_vision_present", "hdr10_plus_present"):
            result[key] = None if value is None else bool(value)
        else:
            result[key] = value
    return result

def json_file_object(file: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "file_name", "canonical_path", "extension", "size_bytes", "mtime_ns",
        "first_seen_at_utc", "last_seen_at_utc", "presence_status", "probe_status", "metadata_state",
        "last_probe_at_utc", "last_successful_probe_at_utc", "format_name", "container", "duration_seconds",
        "overall_bitrate_bps", "encoder", "video_stream_count", "audio_stream_count", "subtitle_stream_count", "chapter_count",
    ]
    result = {key: file.get(key) for key in keys}
    result["full_path"] = result.pop("canonical_path")
    result["video_streams"] = [_clean_json_stream(x) for x in file.get("video_streams", [])]
    result["audio_streams"] = [_clean_json_stream(x) for x in file.get("audio_streams", [])]
    result["subtitle_streams"] = [_clean_json_stream(x) for x in file.get("subtitle_streams", [])]
    return result

def build_json_document(packages: list[dict[str, Any]], scope_name: str, online_map: dict[int, bool]) -> dict[str, Any]:
    sources_json = []
    for pkg in packages:
        src = pkg["source"]
        sid = int(src["source_id"])
        sources_json.append({
            "source": {
                "source_id": sid,
                "canonical_path": src["canonical_path"],
                "display_path": src["display_path"],
                "storage_identity_type": src["identity_type"],
                "storage_identity_key": src["identity_key"],
                "source_root_relative_path": src["source_root_relative_path"],
            },
            "catalog_state": source_scope_state(pkg, online_map.get(sid)),
            "files": [json_file_object(f) for f in pkg["active"]],
            "missing_files": [
                {
                    "file_name": f.get("file_name"),
                    "last_known_full_path": f.get("canonical_path"),
                    "size_bytes": f.get("size_bytes"),
                    "container": f.get("container"),
                    "duration_seconds": f.get("duration_seconds"),
                    "last_seen_at_utc": f.get("last_seen_at_utc"),
                    "missing_since_utc": f.get("missing_since_utc"),
                    "probe_status": f.get("probe_status"),
                    "metadata_state": f.get("metadata_state"),
                }
                for f in pkg["missing"]
            ],
        })

    return {
        "program": {
            "name": "MediaCatalog",
            "version": PROGRAM_VERSION,
        },
        "schemas": {
            "database": DATABASE_SCHEMA_VERSION,
            "config": CONFIG_SCHEMA_VERSION,
            "json": JSON_SCHEMA_VERSION,
            "report": REPORT_SCHEMA_VERSION,
        },
        "generated_at_utc": utc_now_iso(),
        "scope": {"name": scope_name},
        "summary": scope_summary(packages),
        "sources": sources_json,
    }

def write_json_report(path: Path, document: dict[str, Any], logger: RunLogger) -> None:
    partial = path.with_name(path.name + ".partial")
    try:
        with open(partial, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
        logger.info("JSON", f"Created JSON report: {path}")
    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        raise
