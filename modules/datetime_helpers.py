# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import datetime as dt
from typing import Optional


def local_timestamp_compact() -> str:
    return dt.datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


def utc_iso_to_local_naive(value: Optional[str]) -> Optional[dt.datetime]:
    obj = parse_utc_iso(value)
    if obj is None:
        return None
    return obj.astimezone().replace(tzinfo=None)


def ns_to_local_naive(mtime_ns: Optional[int]) -> Optional[dt.datetime]:
    if mtime_ns is None:
        return None
    try:
        return dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000)
    except Exception:
        return None
