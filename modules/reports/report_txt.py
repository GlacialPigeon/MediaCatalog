# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from ..config import atomic_write_text
from ..constants import PROGRAM_VERSION
from ..logger import RunLogger
from .report_common import bool_text, format_compact_runtime, format_size_dynamic, scope_summary, size_category

TXT_HEADER_WIDTH = 72

def txt_line_value(value: Any, unknown: str = "UNKNOWN") -> str:
    return unknown if value is None or value == "" else str(value)

def write_txt_report(path: Path, packages: list[dict[str, Any]], scope_name: str, logger: RunLogger) -> None:
    summary = scope_summary(packages)
    successful = [f for pkg in packages for f in pkg["successful"]]
    failed = [(pkg, f) for pkg in packages for f in pkg["failed"]]
    missing = [(pkg, f) for pkg in packages for f in pkg["missing"]]
    videos = [v for f in successful for v in f.get("video_streams", [])]

    lines: list[str] = []
    lines += ["=" * TXT_HEADER_WIDTH, f"MEDIACATALOG v{PROGRAM_VERSION}", "=" * TXT_HEADER_WIDTH, ""]
    lines += [f"Generated: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}", f"Scope: {scope_name}", ""]
    lines += ["-" * TXT_HEADER_WIDTH, "SUMMARY", "-" * TXT_HEADER_WIDTH]
    lines += [
        f"Total Files: {summary['total_files']:,}",
        f"Total Size: {format_size_dynamic(summary['total_size_bytes'])}",
        f"Successful: {summary['successful']:,}",
        f"Failed: {summary['failed']:,}",
        f"Missing: {summary['missing']:,}",
        f"Total Runtime: {format_compact_runtime(summary['total_runtime_seconds'])}",
        "",
    ]

    def add_counter(title: str, values: Iterable[Any]) -> None:
        counter = Counter("UNKNOWN" if v in (None, "") else str(v) for v in values)
        lines.extend(["-" * TXT_HEADER_WIDTH, title, "-" * TXT_HEADER_WIDTH])
        for label, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"{label}: {count:,}")
        lines.append("")

    add_counter("RESOLUTION", (v.get("resolution_category") for v in videos))
    add_counter("VIDEO CODEC", (v.get("codec_name") for v in videos))
    add_counter("HDR", (v.get("hdr_type") for v in videos))
    add_counter("BIT DEPTH", (v.get("bit_depth") for v in videos))
    add_counter("SIZE", (size_category(f.get("size_bytes")) for f in successful))

    lines.extend(["-" * TXT_HEADER_WIDTH, "FAILED", "-" * TXT_HEADER_WIDTH])
    if not failed:
        lines.append("None")
    else:
        for pkg, f in failed:
            err = f.get("latest_error") or {}
            lines.append(f"FILE: {f.get('file_name')}")
            lines.append(f"PATH: {f.get('canonical_path')}")
            if len(packages) > 1:
                lines.append(f"SOURCE: {pkg['source']['display_path']}")
            lines.append(f"ERROR: {txt_line_value(err.get('error_type'))} | {txt_line_value(err.get('error_message'))}")
            lines.append("")
    lines.append("")

    lines.extend(["-" * TXT_HEADER_WIDTH, "MISSING", "-" * TXT_HEADER_WIDTH])
    if not missing:
        lines.append("None")
    else:
        for pkg, f in missing:
            lines.append(f"FILE: {f.get('file_name')}")
            lines.append(f"PATH: {f.get('canonical_path')}")
            if len(packages) > 1:
                lines.append(f"SOURCE: {pkg['source']['display_path']}")
            lines.append(f"MISSING SINCE: {txt_line_value(f.get('missing_since_utc'))}")
            lines.append("")
    lines.append("")

    lines.extend(["-" * TXT_HEADER_WIDTH, "FILES", "-" * TXT_HEADER_WIDTH])
    for pkg in packages:
        for f in pkg["successful"]:
            if len(packages) > 1:
                lines.append(f"SOURCE: {pkg['source']['display_path']}")
            lines.append(f"FILE: {f.get('file_name')}")
            lines.append(f"PATH: {f.get('canonical_path')}")
            if f.get("size_bytes") is not None:
                lines.append(f"SIZE: {f['size_bytes'] / (1024 ** 3):.2f} GB")
            else:
                lines.append("SIZE: UNKNOWN")
            lines.append(f"DURATION: {format_compact_runtime(f.get('duration_seconds') or 0)}")
            lines.append(f"CONTAINER: {txt_line_value(f.get('container'))}")
            lines.append("")
            lines.append("VIDEO")
            if not f.get("video_streams"):
                lines.append("  None")
            for v in f.get("video_streams", []):
                res = f"{v.get('width')}x{v.get('height')}" if v.get("width") and v.get("height") else "UNKNOWN"
                depth = f"{v.get('bit_depth')}-bit" if v.get("bit_depth") is not None else "UNKNOWN"
                fps = f"{v.get('frame_rate'):.3f} fps" if v.get("frame_rate") is not None else "UNKNOWN"
                lines.append(f"  [{txt_line_value(v.get('stream_index'))}] {txt_line_value(v.get('codec_name'))} | {res} | {depth} | {txt_line_value(v.get('hdr_type'))} | {fps}")
            lines.append("")
            lines.append("AUDIO")
            if not f.get("audio_streams"):
                lines.append("  None")
            for a in f.get("audio_streams", []):
                br = f"{a.get('bitrate_bps') / 1000:.0f} kb/s" if a.get("bitrate_bps") else "UNKNOWN"
                lines.append(f"  [{txt_line_value(a.get('stream_index'))}] {txt_line_value(a.get('codec_name'))} | {txt_line_value(a.get('language'))} | {txt_line_value(a.get('channels'))} ch | {br}")
            lines.append("")
            lines.append("SUBTITLES")
            if not f.get("subtitle_streams"):
                lines.append("  None")
            for s in f.get("subtitle_streams", []):
                lines.append(f"  [{txt_line_value(s.get('stream_index'))}] {txt_line_value(s.get('codec_name'))} | {txt_line_value(s.get('language'))} | Forced: {bool_text(s.get('is_forced'))}")
            lines += ["", ""]

    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")
    logger.info("TXT", f"Created TXT report: {path}")
