# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .constants import APP_ROOT
from .database import Database
from .logger import RunLogger, format_elapsed
from .platform_api import PlatformAdapter

SLOW_PROBE_LOG_SECONDS = 5.0

def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "N/A" or value == "":
            return None
        return int(value)
    except Exception:
        return None

def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "N/A" or value == "":
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except Exception:
        return None

def safe_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "N/A":
        return None
    return text

def parse_rational(value: Any) -> tuple[Optional[int], Optional[int], Optional[float]]:
    text = safe_str(value)
    if not text or "/" not in text:
        return None, None, safe_float(text)
    left, right = text.split("/", 1)
    num = safe_int(left)
    den = safe_int(right)
    if num is None or den in (None, 0):
        return num, den, None
    return num, den, num / den

def resolution_category(width: Optional[int], height: Optional[int]) -> str:
    if width is None and height is None:
        return "UNKNOWN"
    w = width or 0
    h = height or 0
    # Deliberately OR-based, matching the existing MediaCatalog categorization design.
    if w >= 7680 or h >= 4320:
        return "8K"
    if w >= 3840 or h >= 2160:
        return "4K"
    if w >= 2560 or h >= 1440:
        return "1440p"
    if w >= 1920 or h >= 1080:
        return "1080p"
    if w >= 1280 or h >= 720:
        return "720p"
    return "SD"

def normalize_container(format_name: Optional[str]) -> Optional[str]:
    if not format_name:
        return None
    values = [x.strip().casefold() for x in format_name.split(",") if x.strip()]
    if not values:
        return None
    joined = ",".join(values)
    if "matroska" in values and "webm" in values:
        # FFprobe commonly reports the shared demuxer name "matroska,webm".
        # Do not guess which container family member it is from that string alone.
        return "Matroska/WebM"
    if "matroska" in values:
        return "Matroska"
    if "webm" in values:
        return "WebM"
    if "mov" in values and "mp4" in values:
        return "MP4/MOV"
    if "mpegts" in values:
        return "MPEG-TS"
    if "mpeg" in values:
        return "MPEG"
    if "avi" in values:
        return "AVI"
    if "asf" in values:
        return "ASF/WMV"
    if "flv" in values:
        return "FLV"
    if "rm" in values:
        return "RealMedia"
    return values[0].upper() if len(values[0]) <= 6 else values[0]

def derive_bit_depth(stream: dict[str, Any]) -> Optional[int]:
    direct = safe_int(stream.get("bits_per_raw_sample"))
    if direct and direct > 0:
        return direct
    pix_fmt = (safe_str(stream.get("pix_fmt")) or "").casefold()
    if not pix_fmt:
        return None
    # Explicit bit-depth markers used by common FFmpeg pixel formats.
    for depth in (16, 14, 12, 10, 9):
        patterns = (f"p{depth}", f"{depth}le", f"{depth}be", f"{depth}p")
        if any(p in pix_fmt for p in patterns):
            return depth
    common_8 = (
        "yuv420p", "yuv422p", "yuv444p", "yuvj420p", "yuvj422p", "yuvj444p",
        "nv12", "nv21", "rgb24", "bgr24", "rgba", "bgra", "gray", "pal8",
    )
    if pix_fmt in common_8 or pix_fmt.endswith("p"):
        return 8
    return None

def frame_rate_mode(r_num: Optional[int], r_den: Optional[int], a_num: Optional[int], a_den: Optional[int]) -> str:
    if None in (r_num, r_den, a_num, a_den) or r_den == 0 or a_den == 0:
        return "UNKNOWN"
    r = r_num / r_den
    a = a_num / a_den
    if r <= 0 or a <= 0:
        return "UNKNOWN"
    return "CFR" if math.isclose(r, a, rel_tol=1e-4, abs_tol=1e-4) else "VFR"

def classify_hdr(stream: dict[str, Any], bit_depth: Optional[int]) -> str:
    transfer = (safe_str(stream.get("color_transfer")) or "").casefold()
    primaries = (safe_str(stream.get("color_primaries")) or "").casefold()
    space = (safe_str(stream.get("color_space")) or "").casefold()
    if transfer == "smpte2084":
        return "HDR10"
    if transfer == "arib-std-b67":
        return "HLG"
    if "bt2020" in primaries or "bt2020" in space:
        return "HDR"
    known_sdr_transfers = {
        "bt709", "smpte170m", "gamma22", "gamma28", "iec61966-2-1", "bt470bg", "bt470m",
    }
    if transfer in known_sdr_transfers:
        return "SDR"
    # Do not silently assume missing color metadata means SDR.
    return "UNKNOWN"

def subtitle_type(codec_name: Optional[str]) -> str:
    codec = (codec_name or "").casefold()
    text_codecs = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "ttml"}
    bitmap_codecs = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
    if codec in text_codecs:
        return "TEXT"
    if codec in bitmap_codecs:
        return "BITMAP"
    return "UNKNOWN"

def disposition_flag(stream: dict[str, Any], key: str) -> int:
    disp = stream.get("disposition") or {}
    value = disp.get(key)
    return 1 if value in (1, True, "1") else 0

def get_tags(stream_or_format: dict[str, Any]) -> dict[str, Any]:
    tags = stream_or_format.get("tags")
    return tags if isinstance(tags, dict) else {}

def get_tag_ci(tags: dict[str, Any], *names: str) -> Optional[str]:
    lower = {str(k).casefold(): v for k, v in tags.items()}
    for name in names:
        if name.casefold() in lower:
            return safe_str(lower[name.casefold()])
    return None

class FFprobeRuntime:
    available: bool = False
    version: Optional[str] = None
    full_version: Optional[str] = None
    file_size: Optional[int] = None
    mtime_ns: Optional[int] = None

class ActiveProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[Any]] = set()

    def add(self, proc: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.add(proc)

    def remove(self, proc: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.discard(proc)

    def kill_all(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for proc in processes:
            try:
                proc.kill()
            except Exception:
                pass

def short_ffprobe_version(raw: Optional[str]) -> Optional[str]:
    """Return a compact display label without inventing build information."""
    if not raw:
        return None
    value = raw.strip()
    match = re.match(r"^ffprobe\s+version\s+([^\s]+)", value, re.IGNORECASE)
    if not match:
        return value
    token = match.group(1)
    suffix = "-www.gyan.dev"
    if token.casefold().endswith(suffix):
        token = token[:-len(suffix)]
    return f"ffmpeg-{token}"

def _side_data_type(item: dict[str, Any]) -> str:
    return str(item.get("side_data_type") or item.get("type") or "").casefold()

def _parse_ratio_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    text = safe_str(value)
    if not text:
        return None
    if "/" in text:
        _, _, result = parse_rational(text)
        return result
    return safe_float(text)

def merge_hdr_side_data(target: dict[str, Any], side_data_list: Any) -> None:
    if not isinstance(side_data_list, list):
        return
    for item in side_data_list:
        if not isinstance(item, dict):
            continue
        typ = _side_data_type(item)
        if "mastering display metadata" in typ:
            parts = []
            for name in ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y"):
                if item.get(name) is not None:
                    parts.append(f"{name}={item.get(name)}")
            if parts:
                target["mastering_display_primaries"] = "; ".join(parts)
            wp = []
            for name in ("white_point_x", "white_point_y"):
                if item.get(name) is not None:
                    wp.append(f"{name}={item.get(name)}")
            if wp:
                target["mastering_display_white_point"] = "; ".join(wp)
            min_lum = _parse_ratio_float(item.get("min_luminance"))
            max_lum = _parse_ratio_float(item.get("max_luminance"))
            if min_lum is not None:
                target["mastering_min_luminance"] = min_lum
            if max_lum is not None:
                target["mastering_max_luminance"] = max_lum
        if "content light level metadata" in typ:
            max_content = safe_int(item.get("max_content") or item.get("max_cll"))
            max_average = safe_int(item.get("max_average") or item.get("max_fall"))
            if max_content is not None:
                target["max_cll"] = max_content
            if max_average is not None:
                target["max_fall"] = max_average
        if "dovi" in typ or "dolby vision" in typ:
            target["dolby_vision_present"] = True
            profile = item.get("dv_profile") or item.get("profile")
            if profile is not None:
                target["dolby_vision_profile"] = str(profile)
        if "smpte2094-40" in typ or "hdr dynamic metadata" in typ or "hdr10+" in typ:
            target["hdr10_plus_present"] = True

def run_process_capture(
    args: list[str],
    timeout: float,
    *,
    active_processes: ActiveProcessRegistry,
    subprocess_kwargs: dict[str, Any],
) -> tuple[int, str, str, bool]:
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_kwargs,
    )
    active_processes.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return int(proc.returncode or 0), stdout, stderr, False
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            stdout, stderr = proc.communicate()
            return int(proc.returncode if proc.returncode is not None else -1), stdout, stderr, True
    finally:
        active_processes.remove(proc)

def validate_ffprobe_runtime(
    db: Database,
    ffprobe_path: Path,
    logger: RunLogger,
    run_process: Callable[[list[str], float], tuple[int, str, str, bool]],
) -> FFprobeRuntime:
    runtime = FFprobeRuntime()
    if not ffprobe_path.is_file():
        logger.warning("FFPROBE", f"FFprobe not found at expected path: {ffprobe_path}")
        return runtime
    try:
        stat = ffprobe_path.stat()
        runtime.file_size = int(stat.st_size)
        runtime.mtime_ns = int(stat.st_mtime_ns)
    except Exception as exc:
        logger.error("FFPROBE", f"Could not stat FFprobe: {exc}")
        return runtime

    cached_size = db.get_metadata("ffprobe_file_size")
    cached_mtime = db.get_metadata("ffprobe_mtime_ns")
    cached_version = db.get_metadata("ffprobe_version")
    cached_capabilities = db.get_metadata("ffprobe_capability_state")
    if (
        cached_size == str(runtime.file_size)
        and cached_mtime == str(runtime.mtime_ns)
        and cached_version
        and cached_capabilities == "VALID"
    ):
        runtime.available = True
        runtime.full_version = cached_version
        runtime.version = short_ffprobe_version(cached_version)
        logger.info("FFPROBE", f"Using cached FFprobe validation: {runtime.version}")
        if runtime.version != cached_version:
            logger.debug("FFPROBE", f"Raw cached FFprobe version: {cached_version}")
        return runtime

    try:
        code, stdout, stderr, timed_out = run_process([str(ffprobe_path), "-version"], timeout=10)
        if timed_out or code != 0:
            logger.error("FFPROBE", f"FFprobe validation failed. exit={code} timeout={timed_out} stderr={stderr.strip()}")
            return runtime
        first_line = stdout.splitlines()[0].strip() if stdout.splitlines() else "ffprobe"

        # Capability validation is performed only when the executable is new or
        # changed. This makes the persistent cache meaningful while still
        # verifying that the bundled/user-supplied build exposes every feature
        # MediaCatalog relies on.
        help_code, help_out, help_err, help_timeout = run_process(
            [str(ffprobe_path), "-h", "full"], timeout=10
        )
        help_text = (help_out + "\n" + help_err).casefold()
        required_options = (
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-show_frames",
            "-read_intervals",
            "-select_streams",
            "-show_entries",
            "-print_format",
        )
        missing_options = [option for option in required_options if option.casefold() not in help_text]
        if help_timeout or help_code != 0 or missing_options:
            detail = ", ".join(missing_options) if missing_options else "FFprobe help/capability query failed"
            logger.error(
                "FFPROBE",
                f"FFprobe capability validation failed: {detail}. exit={help_code} timeout={help_timeout}",
            )
            db.set_metadata("ffprobe_capability_state", "INVALID")
            return runtime

        runtime.available = True
        runtime.full_version = first_line
        runtime.version = short_ffprobe_version(first_line)
        db.set_metadata("ffprobe_path", str(ffprobe_path))
        db.set_metadata("ffprobe_file_size", str(runtime.file_size))
        db.set_metadata("ffprobe_mtime_ns", str(runtime.mtime_ns))
        db.set_metadata("ffprobe_version", first_line)
        db.set_metadata("ffprobe_capability_state", "VALID")
        logger.info("FFPROBE", f"Validated FFprobe and required capabilities: {runtime.version}")
        if runtime.version != first_line:
            logger.debug("FFPROBE", f"Raw FFprobe version: {first_line}")
    except Exception as exc:
        logger.error("FFPROBE", f"FFprobe validation exception: {exc}", exc_info=True)
    return runtime

def ffprobe_json(
    path: str,
    timeout: float,
    ffprobe_path: Path,
    run_process: Callable[[list[str], float], tuple[int, str, str, bool]],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    args = [
        str(ffprobe_path),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        path,
    ]
    code, stdout, stderr, timed_out = run_process(args, timeout)
    if timed_out:
        return None, {
            "type": "TIMEOUT",
            "message": f"FFprobe timed out after {timeout:g} seconds.",
            "exit_code": code,
            "stderr": stderr,
            "was_timeout": 1,
        }
    if code != 0:
        return None, {
            "type": "FFPROBE_ERROR",
            "message": stderr.strip() or f"FFprobe exited with code {code}.",
            "exit_code": code,
            "stderr": stderr,
            "was_timeout": 0,
        }
    try:
        parsed = json.loads(stdout)
        if not isinstance(parsed, dict):
            raise ValueError("Top-level FFprobe JSON is not an object.")
        return parsed, None
    except Exception as exc:
        return None, {
            "type": "INVALID_JSON",
            "message": str(exc),
            "exit_code": code,
            "stderr": stderr,
            "was_timeout": 0,
        }

def hdr_second_pass(
    path: str,
    video_ordinal: int,
    duration: Optional[float],
    timeout: float,
    ffprobe_path: Path,
    run_process: Callable[[list[str], float], tuple[int, str, str, bool]],
    logger: RunLogger,
) -> tuple[dict[str, Any], str]:
    result: dict[str, Any] = {
        "mastering_display_primaries": None,
        "mastering_display_white_point": None,
        "mastering_min_luminance": None,
        "mastering_max_luminance": None,
        "max_cll": None,
        "max_fall": None,
        "dolby_vision_present": False,
        "dolby_vision_profile": None,
        "hdr10_plus_present": False,
    }
    had_error = False
    sampled = 0
    total_started = time.perf_counter()

    def all_positive_targets_found() -> bool:
        # Absence of DV/HDR10+ cannot be proven from a single frame, therefore the
        # true early-exit condition is intentionally strict.
        return (
            result["mastering_display_primaries"] is not None
            and result["mastering_display_white_point"] is not None
            and result["mastering_min_luminance"] is not None
            and result["mastering_max_luminance"] is not None
            and result["max_cll"] is not None
            and result["max_fall"] is not None
            and result["dolby_vision_present"]
            and result["hdr10_plus_present"]
        )

    def sample_single(position: float, label: str) -> bool:
        nonlocal had_error, sampled
        interval = f"{max(position, 0.0):.3f}%+#1"
        args = [
            str(ffprobe_path),
            "-v", "error",
            "-select_streams", f"v:{video_ordinal}",
            "-read_intervals", interval,
            "-show_frames",
            "-print_format", "json",
            path,
        ]
        started = time.perf_counter()
        code, stdout, stderr, timed_out = run_process(args, timeout)
        elapsed = time.perf_counter() - started
        sampled += 1
        if timed_out or code != 0:
            had_error = True
            logger.warning(
                "HDR",
                f"HDR sample failed path={path!r} sample={label} position={position:.3f} "
                f"exit_code={code} timeout={timed_out} elapsed={format_elapsed(elapsed)} stderr={stderr.strip()}",
            )
            return False
        try:
            data = json.loads(stdout)
            frames = [x for x in (data.get("frames") or []) if isinstance(x, dict)]
            if not frames:
                had_error = True
                logger.warning(
                    "HDR",
                    f"HDR sample returned no frame path={path!r} sample={label} "
                    f"position={position:.3f} elapsed={format_elapsed(elapsed)}",
                )
                return False
            merge_hdr_side_data(result, frames[0].get("side_data_list"))
            if elapsed >= SLOW_PROBE_LOG_SECONDS:
                logger.info(
                    "HDR",
                    f"Slow HDR sample path={path!r} sample={label} elapsed={format_elapsed(elapsed)}",
                )
            return True
        except Exception as exc:
            had_error = True
            logger.warning(
                "HDR",
                f"HDR JSON parse failed path={path!r} sample={label} "
                f"elapsed={format_elapsed(elapsed)} error={exc}",
            )
            return False

    def sample_combined(samples: list[tuple[float, str]]) -> bool:
        nonlocal sampled
        intervals = ",".join(f"{max(position, 0.0):.3f}%+#1" for position, _ in samples)
        args = [
            str(ffprobe_path),
            "-v", "error",
            "-select_streams", f"v:{video_ordinal}",
            "-read_intervals", intervals,
            "-show_frames",
            "-print_format", "json",
            path,
        ]
        started = time.perf_counter()
        code, stdout, stderr, timed_out = run_process(args, timeout)
        elapsed = time.perf_counter() - started
        if timed_out or code != 0:
            logger.debug(
                "HDR",
                f"Combined HDR sampling fallback path={path!r} exit_code={code} timeout={timed_out} "
                f"elapsed={format_elapsed(elapsed)} stderr={stderr.strip()}",
            )
            return False
        try:
            data = json.loads(stdout)
            frames = [x for x in (data.get("frames") or []) if isinstance(x, dict)]
        except Exception as exc:
            logger.debug(
                "HDR",
                f"Combined HDR sampling fallback path={path!r} elapsed={format_elapsed(elapsed)} error={exc}",
            )
            return False

        # One packet interval normally yields one decoded frame. Require an exact
        # one-to-one result so the combined path remains equivalent to the old
        # per-sample flow; any unusual decoder/seek behavior falls back safely.
        if len(frames) != len(samples):
            logger.debug(
                "HDR",
                f"Combined HDR sampling fallback path={path!r} expected_frames={len(samples)} "
                f"returned_frames={len(frames)} elapsed={format_elapsed(elapsed)}",
            )
            return False

        for frame in frames:
            sampled += 1
            merge_hdr_side_data(result, frame.get("side_data_list"))
            if all_positive_targets_found():
                break
        if elapsed >= SLOW_PROBE_LOG_SECONDS:
            logger.info(
                "HDR",
                f"Slow combined HDR sample path={path!r} intervals={len(samples)} elapsed={format_elapsed(elapsed)}",
            )
        return True

    if duration and duration > 0:
        planned_samples = [
            (0.0, "first"),
            (duration * 0.25, "25%"),
            (duration * 0.50, "50%"),
            (duration * 0.75, "75%"),
        ]
        if not sample_combined(planned_samples):
            # Optimization failures must not alter probe semantics. Fall back to
            # the proven per-sample path and preserve its fail-soft behavior.
            sampled = 0
            for position, label in planned_samples:
                sample_single(position, label)
                if all_positive_targets_found():
                    break
    else:
        sample_single(0.0, "first")
        # Without a usable duration the planned 25/50/75% samples cannot run.
        had_error = True

    status = "PARTIAL" if had_error else "COMPLETE"
    logger.debug(
        "HDR",
        f"HDR second pass finished path={path!r} status={status} samples={sampled} "
        f"elapsed={format_elapsed(time.perf_counter() - total_started)}",
    )
    return result, status

def parse_probe_data(
    data: dict[str, Any],
    path: str,
    size_bytes: Optional[int],
    *,
    run_hdr: bool,
    timeout: float,
    hdr_runner: Callable[[str, int, Optional[float], float], tuple[dict[str, Any], str]],
) -> tuple[Optional[dict[str, Any]], bool]:
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    chapters = data.get("chapters") if isinstance(data.get("chapters"), list) else None

    duration = safe_float(format_info.get("duration"))
    format_name = safe_str(format_info.get("format_name"))
    overall_bitrate = safe_int(format_info.get("bit_rate"))
    format_tags = get_tags(format_info)
    encoder = get_tag_ci(format_tags, "encoder", "writing_application", "writing library", "writing_library")

    normal_video_streams: list[dict[str, Any]] = []
    audio_streams: list[dict[str, Any]] = []
    subtitle_streams: list[dict[str, Any]] = []

    all_video_ordinal = 0
    primary_assigned = False
    for raw in streams:
        if not isinstance(raw, dict):
            continue
        codec_type = (safe_str(raw.get("codec_type")) or "").casefold()
        if codec_type == "video":
            attached = disposition_flag(raw, "attached_pic") == 1
            current_ordinal = all_video_ordinal
            all_video_ordinal += 1
            if attached:
                continue
            width = safe_int(raw.get("width"))
            height = safe_int(raw.get("height"))
            bit_depth = derive_bit_depth(raw)
            r_num, r_den, r_fps = parse_rational(raw.get("r_frame_rate"))
            a_num, a_den, a_fps = parse_rational(raw.get("avg_frame_rate"))
            fps = a_fps if a_fps and a_fps > 0 else r_fps
            bitrate = safe_int(raw.get("bit_rate"))
            estimated = None
            if bitrate is not None and duration is not None and duration >= 0:
                estimated = int((bitrate * duration) / 8)
            hdr = classify_hdr(raw, bit_depth)
            item = {
                "stream_index": safe_int(raw.get("index")),
                "is_primary": 0 if primary_assigned else 1,
                "codec_name": safe_str(raw.get("codec_name")),
                "profile": safe_str(raw.get("profile")),
                "level": safe_int(raw.get("level")),
                "codec_tag": safe_str(raw.get("codec_tag_string")),
                "width": width,
                "height": height,
                "resolution_category": resolution_category(width, height),
                "sample_aspect_ratio": safe_str(raw.get("sample_aspect_ratio")),
                "display_aspect_ratio": safe_str(raw.get("display_aspect_ratio")),
                "pixel_format": safe_str(raw.get("pix_fmt")),
                "bit_depth": bit_depth,
                "r_frame_rate_num": r_num,
                "r_frame_rate_den": r_den,
                "avg_frame_rate_num": a_num,
                "avg_frame_rate_den": a_den,
                "frame_rate": fps,
                "frame_rate_mode": frame_rate_mode(r_num, r_den, a_num, a_den),
                "bitrate_bps": bitrate,
                "estimated_stream_size_bytes": estimated,
                "field_order": safe_str(raw.get("field_order")),
                "color_range": safe_str(raw.get("color_range")),
                "color_space": safe_str(raw.get("color_space")),
                "color_transfer": safe_str(raw.get("color_transfer")),
                "color_primaries": safe_str(raw.get("color_primaries")),
                "hdr_type": hdr,
                "hdr_metadata_status": "NOT_APPLICABLE",
                "mastering_display_primaries": None,
                "mastering_display_white_point": None,
                "mastering_min_luminance": None,
                "mastering_max_luminance": None,
                "max_cll": None,
                "max_fall": None,
                "dolby_vision_present": 0,
                "dolby_vision_profile": None,
                "hdr10_plus_present": 0,
                "_video_ordinal": current_ordinal,
            }
            merge_hdr_side_data(item, raw.get("side_data_list"))
            if item.get("dolby_vision_present") or item.get("hdr10_plus_present"):
                if item["hdr_type"] in ("UNKNOWN", "SDR"):
                    item["hdr_type"] = "HDR"
            if item.get("dolby_vision_present"):
                item["dolby_vision_present"] = 1
            if item.get("hdr10_plus_present"):
                item["hdr10_plus_present"] = 1
            normal_video_streams.append(item)
            primary_assigned = True

        elif codec_type == "audio":
            tags = get_tags(raw)
            bitrate = safe_int(raw.get("bit_rate"))
            estimated = None
            if bitrate is not None and duration is not None and duration >= 0:
                estimated = int((bitrate * duration) / 8)
            audio_streams.append({
                "stream_index": safe_int(raw.get("index")),
                "codec_name": safe_str(raw.get("codec_name")),
                "profile": safe_str(raw.get("profile")),
                "language": get_tag_ci(tags, "language"),
                "title": get_tag_ci(tags, "title"),
                "channels": safe_int(raw.get("channels")),
                "channel_layout": safe_str(raw.get("channel_layout")),
                "sample_rate_hz": safe_int(raw.get("sample_rate")),
                "bitrate_bps": bitrate,
                "bit_depth": safe_int(raw.get("bits_per_raw_sample")),
                "estimated_stream_size_bytes": estimated,
                "is_default": disposition_flag(raw, "default"),
                "is_forced": disposition_flag(raw, "forced"),
                "is_original": disposition_flag(raw, "original"),
                "is_commentary": disposition_flag(raw, "comment"),
                "is_hearing_impaired": disposition_flag(raw, "hearing_impaired"),
                "is_visual_impaired": disposition_flag(raw, "visual_impaired"),
                "is_dub": disposition_flag(raw, "dub"),
            })

        elif codec_type == "subtitle":
            tags = get_tags(raw)
            subtitle_streams.append({
                "stream_index": safe_int(raw.get("index")),
                "codec_name": safe_str(raw.get("codec_name")),
                "subtitle_type": subtitle_type(safe_str(raw.get("codec_name"))),
                "language": get_tag_ci(tags, "language"),
                "title": get_tag_ci(tags, "title"),
                "duration_seconds": safe_float(raw.get("duration")),
                "is_default": disposition_flag(raw, "default"),
                "is_forced": disposition_flag(raw, "forced"),
                "is_hearing_impaired": disposition_flag(raw, "hearing_impaired"),
            })

    if not normal_video_streams:
        return None, False

    if run_hdr:
        for item in normal_video_streams:
            if item["hdr_type"] not in ("HDR10", "HLG", "HDR"):
                continue
            hdr_data, hdr_status = hdr_runner(
                path,
                int(item.pop("_video_ordinal")),
                duration,
                timeout,
            )
            item.update({
                "mastering_display_primaries": hdr_data.get("mastering_display_primaries") or item.get("mastering_display_primaries"),
                "mastering_display_white_point": hdr_data.get("mastering_display_white_point") or item.get("mastering_display_white_point"),
                "mastering_min_luminance": hdr_data.get("mastering_min_luminance") if hdr_data.get("mastering_min_luminance") is not None else item.get("mastering_min_luminance"),
                "mastering_max_luminance": hdr_data.get("mastering_max_luminance") if hdr_data.get("mastering_max_luminance") is not None else item.get("mastering_max_luminance"),
                "max_cll": hdr_data.get("max_cll") if hdr_data.get("max_cll") is not None else item.get("max_cll"),
                "max_fall": hdr_data.get("max_fall") if hdr_data.get("max_fall") is not None else item.get("max_fall"),
                "dolby_vision_present": 1 if (hdr_data.get("dolby_vision_present") or item.get("dolby_vision_present")) else 0,
                "dolby_vision_profile": hdr_data.get("dolby_vision_profile") or item.get("dolby_vision_profile"),
                "hdr10_plus_present": 1 if (hdr_data.get("hdr10_plus_present") or item.get("hdr10_plus_present")) else 0,
                "hdr_metadata_status": hdr_status,
            })
        for item in normal_video_streams:
            item.pop("_video_ordinal", None)
    else:
        for item in normal_video_streams:
            item.pop("_video_ordinal", None)

    result = {
        "format_name": format_name,
        "container": normalize_container(format_name),
        "duration_seconds": duration,
        "overall_bitrate_bps": overall_bitrate,
        "encoder": encoder,
        "video_stream_count": len(normal_video_streams),
        "audio_stream_count": len(audio_streams),
        "subtitle_stream_count": len(subtitle_streams),
        "chapter_count": len(chapters) if chapters is not None else None,
        "video_streams": normal_video_streams,
        "audio_streams": audio_streams,
        "subtitle_streams": subtitle_streams,
    }
    return result, True

class ProbeService:
    def __init__(
        self,
        db: Database,
        logger: RunLogger,
        platform: PlatformAdapter,
        app_root: Path = APP_ROOT,
    ) -> None:
        self.db = db
        self.logger = logger
        self.platform = platform
        self.ffprobe_path = platform.ffprobe_path(app_root)
        self.active_processes = ActiveProcessRegistry()
        self.runtime = FFprobeRuntime()

    def _run_process_capture(self, args: list[str], timeout: float) -> tuple[int, str, str, bool]:
        return run_process_capture(
            args,
            timeout,
            active_processes=self.active_processes,
            subprocess_kwargs=self.platform.subprocess_creation_kwargs(),
        )

    def validate_runtime(self) -> FFprobeRuntime:
        self.runtime = validate_ffprobe_runtime(
            self.db,
            self.ffprobe_path,
            self.logger,
            self._run_process_capture,
        )
        return self.runtime

    def ffprobe_json(
        self,
        path: str,
        timeout: float,
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        return ffprobe_json(path, timeout, self.ffprobe_path, self._run_process_capture)

    def hdr_second_pass(
        self,
        path: str,
        video_ordinal: int,
        duration: Optional[float],
        timeout: float,
    ) -> tuple[dict[str, Any], str]:
        return hdr_second_pass(
            path,
            video_ordinal,
            duration,
            timeout,
            self.ffprobe_path,
            self._run_process_capture,
            self.logger,
        )

    def parse_probe_data(
        self,
        data: dict[str, Any],
        path: str,
        size_bytes: Optional[int],
        *,
        run_hdr: bool,
        timeout: float,
    ) -> tuple[Optional[dict[str, Any]], bool]:
        return parse_probe_data(
            data,
            path,
            size_bytes,
            run_hdr=run_hdr,
            timeout=timeout,
            hdr_runner=self.hdr_second_pass,
        )
