# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..config import CONFIG_SCHEMA_VERSION, ConfigManager
from ..constants import PROGRAM_VERSION
from ..database import DATABASE_SCHEMA_VERSION
from ..datetime_helpers import ns_to_local_naive, utc_iso_to_local_naive
from ..logger import RunLogger, format_elapsed
from .report_common import REPORT_SCHEMA_VERSION, bool_text, format_compact_runtime, format_size_dynamic, scope_summary, size_category


class ExcelDependencyError(RuntimeError):
    pass

def write_excel_report(
    path: Path,
    packages: list[dict[str, Any]],
    scope_name: str,
    online_map: dict[int, bool],
    config: ConfigManager,
    ffprobe_version: Optional[str],
    logger: RunLogger,
) -> None:
    from .report_json import JSON_SCHEMA_VERSION

    try:
        import xlsxwriter
    except ImportError as exc:
        raise ExcelDependencyError("XlsxWriter package is not installed.") from exc

    theme = config.data["excel"]["theme"]
    font_name = config.data["excel"]["font_name"]
    global_max = float(config.data["excel"]["max_width_global"])

    def color(value: str) -> str:
        value = value.strip()
        return value if value.startswith("#") else f"#{value}"

    partial = path.with_name(path.name + ".partial")
    workbook = xlsxwriter.Workbook(
        str(partial),
        {"strings_to_urls": False, "strings_to_formulas": False},
    )

    border_color = color(theme["border"])
    header_background = color(theme["header_background"])
    header_text = color(theme["header_text"])
    zebra_a_background = color(theme["zebra_a_background"])
    zebra_a_text = color(theme["zebra_a_text"])
    zebra_b_background = color(theme["zebra_b_background"])
    zebra_b_text = color(theme["zebra_b_text"])
    summary_background = color(theme["summary_section_fill"])
    summary_text = color(theme["summary_section_text"])

    title_format = workbook.add_format({
        "font_name": font_name,
        "font_size": 14,
        "bold": True,
        "align": "center",
        "valign": "vcenter",
    })
    header_format = workbook.add_format({
        "font_name": font_name,
        "font_size": 10,
        "bold": True,
        "font_color": header_text,
        "bg_color": header_background,
        "bottom": 1,
        "bottom_color": border_color,
        "align": "left",
        "valign": "vcenter",
    })
    summary_section_format = workbook.add_format({
        "font_name": font_name,
        "font_size": 10,
        "bold": True,
        "font_color": summary_text,
        "bg_color": summary_background,
    })
    summary_card_label_format = workbook.add_format({
        "font_name": font_name,
        "font_size": 10,
        "bold": True,
        "font_color": summary_text,
        "bg_color": summary_background,
        "bottom": 1,
        "bottom_color": border_color,
        "align": "center",
        "valign": "vcenter",
    })
    info_label_format = workbook.add_format({"font_name": font_name, "font_size": 10, "bold": True})
    info_value_format = workbook.add_format({"font_name": font_name, "font_size": 10})
    summary_value_formats: dict[int, Any] = {}
    row_formats: dict[tuple[int, Optional[str]], Any] = {}

    def get_summary_value_format(font_size: int) -> Any:
        fmt = summary_value_formats.get(font_size)
        if fmt is None:
            fmt = workbook.add_format({
                "font_name": font_name,
                "font_size": font_size,
                "bold": True,
                "font_color": summary_text,
                "bg_color": summary_background,
                "bottom": 1,
                "bottom_color": border_color,
                "align": "center",
                "valign": "vcenter",
            })
            summary_value_formats[font_size] = fmt
        return fmt

    def get_row_format(parity: int, number_format: Optional[str] = None) -> Any:
        key = (parity, number_format)
        fmt = row_formats.get(key)
        if fmt is None:
            if parity == 0:
                bg = zebra_a_background
                fg = zebra_a_text
            else:
                bg = zebra_b_background
                fg = zebra_b_text
            props: dict[str, Any] = {
                "font_name": font_name,
                "font_size": 10,
                "font_color": fg,
                "bg_color": bg,
                "bottom": 1,
                "bottom_color": border_color,
                "valign": "vcenter",
            }
            if number_format:
                props["num_format"] = number_format
            fmt = workbook.add_format(props)
            row_formats[key] = fmt
        return fmt

    combined = len(packages) > 1
    summary = scope_summary(packages)

    try:
        # ------------------------------------------------------------------ SUMMARY
        ws = workbook.add_worksheet("SUMMARY")
        ws.hide_gridlines(2)
        ws.merge_range(0, 0, 0, 13, f"MediaCatalog v{PROGRAM_VERSION} — {scope_name}", title_format)
        ws.set_row(0, 24)

        cards = [
            ("TOTAL FILES", f"{summary['total_files']:,}"),
            ("TOTAL SIZE", format_size_dynamic(summary["total_size_bytes"])),
            ("SUCCESSFUL", f"{summary['successful']:,}"),
            ("FAILED", f"{summary['failed']:,}"),
            ("MISSING", f"{summary['missing']:,}"),
            ("TOTAL RUNTIME", format_compact_runtime(summary["total_runtime_seconds"])),
        ]
        card_cols = [(0, 3), (5, 8), (10, 13)]
        for idx, (label, value) in enumerate(cards):
            row_base = 2 if idx < 3 else 6
            col_start, col_end = card_cols[idx % 3]
            ws.merge_range(row_base, col_start, row_base, col_end, label, summary_card_label_format)
            size = 18 if len(str(value)) <= 14 else 15 if len(str(value)) <= 22 else 12
            ws.merge_range(row_base + 1, col_start, row_base + 2, col_end, value, get_summary_value_format(size))

        latest_statuses = [pkg["latest_scan"]["status"] if pkg["latest_scan"] else "NONE" for pkg in packages]
        overall_catalog_state = "INCOMPLETE" if any(x == "INCOMPLETE" for x in latest_statuses) else (latest_statuses[0] if len(set(latest_statuses)) == 1 else "MIXED")
        info = [
            ("Scope", scope_name),
            ("Generated", dt.datetime.now().astimezone().isoformat(timespec="seconds")),
            ("Catalog State", overall_catalog_state),
            ("MediaCatalog Version", PROGRAM_VERSION),
            ("Database Schema", DATABASE_SCHEMA_VERSION),
            ("Config Schema", CONFIG_SCHEMA_VERSION),
            ("JSON Schema", JSON_SCHEMA_VERSION),
            ("Report Schema", REPORT_SCHEMA_VERSION),
            ("FFprobe Version", ffprobe_version or "UNKNOWN"),
        ]
        if len(packages) == 1:
            pkg = packages[0]
            info.append(("Media Source", pkg["source"]["display_path"]))
            info.append(("Last Completed Scan", (pkg["completed_scan"] or {}).get("finished_at_utc") or "NONE"))
            info.append(("Latest Scan Status", (pkg["latest_scan"] or {}).get("status") or "NONE"))

        info_row = 11
        for i, (label, value) in enumerate(info):
            r = info_row + i
            ws.write(r, 0, label, info_label_format)
            ws.write(r, 1, value, info_value_format)
        current_row = info_row + len(info) + 2

        if combined:
            ws.merge_range(current_row, 0, current_row, 2, "SOURCES", summary_section_format)
            current_row += 1
            for pkg in packages:
                ws.write(current_row, 0, pkg["source"]["canonical_path"], info_value_format)
                current_row += 1
            current_row += 2

        successful = [f for pkg in packages for f in pkg["successful"]]
        videos = [v for f in successful for v in f.get("video_streams", [])]
        audios = [a for f in successful for a in f.get("audio_streams", [])]
        subtitles = [s for f in successful for s in f.get("subtitle_streams", [])]

        stat_sections: list[tuple[str, Counter[Any]]] = [
            ("FILES — SIZE DISTRIBUTION", Counter(size_category(f.get("size_bytes")) for f in successful)),
            ("FILES — CONTAINER", Counter(f.get("container") or "UNKNOWN" for f in successful)),
            ("VIDEO — RESOLUTION", Counter(v.get("resolution_category") or "UNKNOWN" for v in videos)),
            ("VIDEO — CODEC", Counter(v.get("codec_name") or "UNKNOWN" for v in videos)),
            ("VIDEO — HDR/SDR", Counter(v.get("hdr_type") or "UNKNOWN" for v in videos)),
            ("VIDEO — BIT DEPTH", Counter(v.get("bit_depth") if v.get("bit_depth") is not None else "UNKNOWN" for v in videos)),
            ("VIDEO — FRAME RATE MODE", Counter(v.get("frame_rate_mode") or "UNKNOWN" for v in videos)),
            ("AUDIO — CODEC", Counter(a.get("codec_name") or "UNKNOWN" for a in audios)),
            ("AUDIO — LANGUAGE", Counter(a.get("language") or "UNKNOWN" for a in audios)),
            ("AUDIO — CHANNEL LAYOUT", Counter(a.get("channel_layout") or "UNKNOWN" for a in audios)),
            ("SUBTITLES — CODEC", Counter(s.get("codec_name") or "UNKNOWN" for s in subtitles)),
            ("SUBTITLES — LANGUAGE", Counter(s.get("language") or "UNKNOWN" for s in subtitles)),
            ("SUBTITLES — TYPE", Counter(s.get("subtitle_type") or "UNKNOWN" for s in subtitles)),
        ]
        percentage_format = workbook.add_format({"num_format": "0.00%"})
        for title, counter in stat_sections:
            ws.merge_range(current_row, 0, current_row, 2, title, summary_section_format)
            current_row += 1
            for col, value in enumerate(("Category", "Count", "Percentage")):
                ws.write(current_row, col, value, header_format)
            current_row += 1
            total = sum(counter.values())
            if total == 0:
                ws.write(current_row, 0, "None")
                ws.write(current_row, 1, 0)
                ws.write(current_row, 2, 0.0, percentage_format)
                current_row += 1
            else:
                for key, count in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
                    ws.write(current_row, 0, "UNKNOWN" if key in (None, "") else str(key))
                    ws.write(current_row, 1, count)
                    ws.write(current_row, 2, count / total, percentage_format)
                    current_row += 1
            current_row += 2

        ws.set_column(0, 13, 14)
        ws.set_column(0, 0, 30)
        ws.set_column(1, 1, 36)

        # -------------------------------------------------------------- data helper
        def create_data_sheet(name: str, headers: list[str], rows: list[tuple[int, list[Any]]], formats: Optional[dict[str, str]] = None) -> Any:
            sheet = workbook.add_worksheet(name)
            sheet.hide_gridlines(2)
            sheet.freeze_panes(1, 0)
            sheet.set_row(0, 20)
            for col_idx, header in enumerate(headers):
                sheet.write(0, col_idx, header, header_format)

            group_parity: dict[int, int] = {}
            next_parity = 0
            for row_idx, (group_key, values) in enumerate(rows, 1):
                if group_key not in group_parity:
                    group_parity[group_key] = next_parity
                    next_parity = 1 - next_parity
                parity = group_parity[group_key]
                sheet.set_row(row_idx, 18)
                for col_idx, value in enumerate(values):
                    header = headers[col_idx]
                    number_format = formats.get(header) if formats and value is not None else None
                    sheet.write(row_idx, col_idx, value, get_row_format(parity, number_format))

            if headers:
                sheet.autofilter(0, 0, max(0, len(rows)), len(headers) - 1)

            for col_idx, header in enumerate(headers):
                max_len = len(str(header))
                for _, values in rows:
                    value = values[col_idx]
                    if value is None:
                        continue
                    if isinstance(value, dt.datetime):
                        text_len = 19
                    else:
                        text_len = len(str(value))
                    max_len = max(max_len, text_len)
                data_width = max_len + 2
                header_width = len(str(header)) + 3.0
                sheet.set_column(col_idx, col_idx, min(max(data_width, header_width), global_max))
            return sheet

        source_prefix = ["Source"] if combined else []

        # FILES
        file_headers = source_prefix + [
            "File Name", "Full Path", "Extension", "Size", "Size Category", "Duration", "Container", "Overall Bitrate",
            "Encoder", "Video Streams", "Audio Tracks", "Subtitle Tracks", "Chapters", "Modified", "Probe Status", "Metadata State",
        ]
        file_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["successful"]:
                values: list[Any] = ([source_name] if combined else []) + [
                    f.get("file_name"), f.get("canonical_path"), f.get("extension"),
                    (f.get("size_bytes") / (1024 ** 3)) if f.get("size_bytes") is not None else None,
                    size_category(f.get("size_bytes")),
                    (f.get("duration_seconds") / 86400.0) if f.get("duration_seconds") is not None else None,
                    f.get("container") or "UNKNOWN",
                    (f.get("overall_bitrate_bps") / 1000.0) if f.get("overall_bitrate_bps") is not None else None,
                    f.get("encoder") or "UNKNOWN", f.get("video_stream_count"), f.get("audio_stream_count"),
                    f.get("subtitle_stream_count"), f.get("chapter_count"), ns_to_local_naive(f.get("mtime_ns")),
                    f.get("probe_status"), f.get("metadata_state"),
                ]
                file_rows.append((int(f["file_id"]), values))
        create_data_sheet("FILES", file_headers, file_rows, {
            "Size": '0.00 "GB"', "Duration": "[HH]:MM:SS", "Overall Bitrate": '0 "kb/s"', "Modified": "yyyy-mm-dd hh:mm:ss",
        })

        # VIDEO / HDR
        video_headers = source_prefix + [
            "File Name", "Full Path", "Stream", "Primary", "Codec", "Profile", "Level", "Codec Tag", "Width", "Height",
            "Resolution", "Resolution Category", "SAR", "DAR", "Pixel Format", "Bit Depth", "Frame Rate", "Frame Rate Mode",
            "Bitrate", "Estimated Stream Size", "HDR", "Color Range", "Color Space", "Transfer", "Primaries", "Field Order",
        ]
        video_rows: list[tuple[int, list[Any]]] = []
        hdr_headers = source_prefix + [
            "File Name", "Full Path", "Stream", "HDR Type", "Codec", "Profile", "Resolution", "Bit Depth", "Pixel Format",
            "Frame Rate", "Bitrate", "Color Range", "Color Space", "Transfer", "Primaries", "Mastering Primaries",
            "Mastering White Point", "Mastering Min Luminance", "Mastering Max Luminance", "MaxCLL", "MaxFALL",
            "Dolby Vision", "Dolby Vision Profile", "HDR10+", "HDR Metadata Status",
        ]
        hdr_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["successful"]:
                for v in f.get("video_streams", []):
                    resolution = f"{v.get('width')}x{v.get('height')}" if v.get("width") is not None and v.get("height") is not None else "UNKNOWN"
                    base = ([source_name] if combined else []) + [
                        f.get("file_name"), f.get("canonical_path"), v.get("stream_index"), bool_text(v.get("is_primary")),
                        v.get("codec_name") or "UNKNOWN", v.get("profile") or "UNKNOWN", v.get("level"), v.get("codec_tag") or "UNKNOWN",
                        v.get("width"), v.get("height"), resolution, v.get("resolution_category") or "UNKNOWN",
                        v.get("sample_aspect_ratio") or "UNKNOWN", v.get("display_aspect_ratio") or "UNKNOWN",
                        v.get("pixel_format") or "UNKNOWN", v.get("bit_depth"), v.get("frame_rate"),
                        v.get("frame_rate_mode") or "UNKNOWN", (v.get("bitrate_bps") / 1000.0) if v.get("bitrate_bps") is not None else None,
                        (v.get("estimated_stream_size_bytes") / (1024 ** 3)) if v.get("estimated_stream_size_bytes") is not None else None,
                        v.get("hdr_type") or "UNKNOWN", v.get("color_range") or "UNKNOWN", v.get("color_space") or "UNKNOWN",
                        v.get("color_transfer") or "UNKNOWN", v.get("color_primaries") or "UNKNOWN", v.get("field_order") or "UNKNOWN",
                    ]
                    video_rows.append((int(f["file_id"]), base))
                    if v.get("hdr_type") in ("HDR10", "HLG", "HDR"):
                        hdr_values = ([source_name] if combined else []) + [
                            f.get("file_name"), f.get("canonical_path"), v.get("stream_index"), v.get("hdr_type"),
                            v.get("codec_name") or "UNKNOWN", v.get("profile") or "UNKNOWN", resolution, v.get("bit_depth"),
                            v.get("pixel_format") or "UNKNOWN", v.get("frame_rate"),
                            (v.get("bitrate_bps") / 1000.0) if v.get("bitrate_bps") is not None else None,
                            v.get("color_range") or "UNKNOWN", v.get("color_space") or "UNKNOWN", v.get("color_transfer") or "UNKNOWN",
                            v.get("color_primaries") or "UNKNOWN", v.get("mastering_display_primaries") or "UNKNOWN",
                            v.get("mastering_display_white_point") or "UNKNOWN", v.get("mastering_min_luminance"),
                            v.get("mastering_max_luminance"), v.get("max_cll"), v.get("max_fall"),
                            bool_text(v.get("dolby_vision_present")), v.get("dolby_vision_profile") or "UNKNOWN",
                            bool_text(v.get("hdr10_plus_present")), v.get("hdr_metadata_status") or "UNKNOWN",
                        ]
                        hdr_rows.append((int(f["file_id"]), hdr_values))
        create_data_sheet("VIDEO", video_headers, video_rows, {
            "Frame Rate": "0.000", "Bitrate": '0 "kb/s"', "Estimated Stream Size": '0.00 "GB"',
        })
        create_data_sheet("HDR", hdr_headers, hdr_rows, {
            "Frame Rate": "0.000", "Bitrate": '0 "kb/s"', "Mastering Min Luminance": "0.0000",
            "Mastering Max Luminance": "0.00",
        })

        # AUDIO
        audio_headers = source_prefix + [
            "File Name", "Full Path", "Stream", "Codec", "Profile", "Language", "Title", "Channels", "Channel Layout",
            "Sample Rate", "Bitrate", "Bit Depth", "Estimated Stream Size", "Default", "Forced", "Original", "Commentary",
            "Hearing Impaired", "Visual Impaired", "Dub",
        ]
        audio_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["successful"]:
                for a in f.get("audio_streams", []):
                    values = ([source_name] if combined else []) + [
                        f.get("file_name"), f.get("canonical_path"), a.get("stream_index"), a.get("codec_name") or "UNKNOWN",
                        a.get("profile") or "UNKNOWN", a.get("language") or "UNKNOWN", a.get("title") or "UNKNOWN",
                        a.get("channels"), a.get("channel_layout") or "UNKNOWN", a.get("sample_rate_hz"),
                        (a.get("bitrate_bps") / 1000.0) if a.get("bitrate_bps") is not None else None, a.get("bit_depth"),
                        (a.get("estimated_stream_size_bytes") / (1024 ** 3)) if a.get("estimated_stream_size_bytes") is not None else None,
                        bool_text(a.get("is_default")), bool_text(a.get("is_forced")), bool_text(a.get("is_original")),
                        bool_text(a.get("is_commentary")), bool_text(a.get("is_hearing_impaired")), bool_text(a.get("is_visual_impaired")),
                        bool_text(a.get("is_dub")),
                    ]
                    audio_rows.append((int(f["file_id"]), values))
        create_data_sheet("AUDIO", audio_headers, audio_rows, {
            "Sample Rate": '0 "Hz"', "Bitrate": '0 "kb/s"', "Estimated Stream Size": '0.00 "GB"',
        })

        # SUBTITLES
        subtitle_headers = source_prefix + [
            "File Name", "Full Path", "Stream", "Codec", "Type", "Language", "Title", "Duration", "Default", "Forced", "Hearing Impaired",
        ]
        subtitle_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["successful"]:
                for s in f.get("subtitle_streams", []):
                    values = ([source_name] if combined else []) + [
                        f.get("file_name"), f.get("canonical_path"), s.get("stream_index"), s.get("codec_name") or "UNKNOWN",
                        s.get("subtitle_type") or "UNKNOWN", s.get("language") or "UNKNOWN", s.get("title") or "UNKNOWN",
                        (s.get("duration_seconds") / 86400.0) if s.get("duration_seconds") is not None else None,
                        bool_text(s.get("is_default")), bool_text(s.get("is_forced")), bool_text(s.get("is_hearing_impaired")),
                    ]
                    subtitle_rows.append((int(f["file_id"]), values))
        create_data_sheet("SUBTITLES", subtitle_headers, subtitle_rows, {"Duration": "[HH]:MM:SS"})

        # FAILED
        failed_headers = source_prefix + [
            "File Name", "Full Path", "Size", "Modified", "Error Type", "Error Detail", "Exit Code", "Attempts", "Last Attempt", "Metadata State",
        ]
        failed_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["failed"]:
                err = f.get("latest_error") or {}
                values = ([source_name] if combined else []) + [
                    f.get("file_name"), f.get("canonical_path"),
                    (f.get("size_bytes") / (1024 ** 3)) if f.get("size_bytes") is not None else None,
                    ns_to_local_naive(f.get("mtime_ns")), err.get("error_type") or "UNKNOWN", err.get("error_message") or "UNKNOWN",
                    err.get("ffprobe_exit_code"), err.get("attempt_number"), utc_iso_to_local_naive(err.get("occurred_at_utc")),
                    f.get("metadata_state") or "UNKNOWN",
                ]
                failed_rows.append((int(f["file_id"]), values))
        create_data_sheet("FAILED", failed_headers, failed_rows, {
            "Size": '0.00 "GB"', "Modified": "yyyy-mm-dd hh:mm:ss", "Last Attempt": "yyyy-mm-dd hh:mm:ss",
        })

        # MISSING
        missing_headers = source_prefix + [
            "File Name", "Last Known Full Path", "Size", "Container", "Duration", "Last Seen", "Missing Since", "Last Probe Status", "Metadata State",
        ]
        missing_rows: list[tuple[int, list[Any]]] = []
        for pkg in packages:
            source_name = pkg["source"]["display_path"]
            for f in pkg["missing"]:
                values = ([source_name] if combined else []) + [
                    f.get("file_name"), f.get("canonical_path"),
                    (f.get("size_bytes") / (1024 ** 3)) if f.get("size_bytes") is not None else None,
                    f.get("container") or "UNKNOWN", (f.get("duration_seconds") / 86400.0) if f.get("duration_seconds") is not None else None,
                    utc_iso_to_local_naive(f.get("last_seen_at_utc")), utc_iso_to_local_naive(f.get("missing_since_utc")),
                    f.get("probe_status") or "UNKNOWN", f.get("metadata_state") or "UNKNOWN",
                ]
                missing_rows.append((int(f["file_id"]), values))
        create_data_sheet("MISSING", missing_headers, missing_rows, {
            "Size": '0.00 "GB"', "Duration": "[HH]:MM:SS", "Last Seen": "yyyy-mm-dd hh:mm:ss", "Missing Since": "yyyy-mm-dd hh:mm:ss",
        })


        workbook.close()
        os.replace(partial, path)
        logger.info("EXCEL", f"Created Excel report: {path}")
    except Exception:
        try:
            if not getattr(workbook, "fileclosed", False):
                workbook.close()
        except Exception:
            pass
        try:
            partial.unlink(missing_ok=True)
        except Exception:
            pass
        raise

def detect_xlsxwriter(logger: RunLogger, component: str = "STARTUP") -> bool:
    try:
        import xlsxwriter
        version = str(getattr(xlsxwriter, "__version__", "UNKNOWN"))
        logger.info(component, f"XlsxWriter import available version={version}")
        return True
    except Exception as exc:
        logger.warning(component, f"XlsxWriter import unavailable: {exc}")
        return False


def refresh_xlsxwriter_state(logger: RunLogger, component: str = "REPORT") -> bool:
    started = time.perf_counter()
    available = detect_xlsxwriter(logger, component)
    logger.info(
        component,
        f"Fresh XlsxWriter dependency check available={available} elapsed={format_elapsed(time.perf_counter() - started)}",
    )
    return available
