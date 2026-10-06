# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}

def load_source_report_data(conn: sqlite3.Connection, source_id: int) -> dict[str, Any]:
    source_row = conn.execute(
        """
        SELECT s.*, st.identity_type, st.identity_key, st.canonical_root, st.volume_guid,
               st.volume_serial, st.filesystem, st.volume_label
        FROM sources s JOIN storages st ON st.storage_id=s.storage_id
        WHERE s.source_id=?
        """,
        (source_id,),
    ).fetchone()
    if source_row is None:
        raise ValueError(f"Unknown source_id={source_id}")

    latest_scan = conn.execute(
        "SELECT * FROM scan_runs WHERE source_id=? ORDER BY scan_id DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    completed_scan = conn.execute(
        "SELECT * FROM scan_runs WHERE source_id=? AND status='COMPLETED' ORDER BY scan_id DESC LIMIT 1",
        (source_id,),
    ).fetchone()

    active_rows = list(conn.execute(
        "SELECT * FROM media_files WHERE source_id=? AND presence_status='ACTIVE' ORDER BY canonical_path COLLATE NOCASE",
        (source_id,),
    ).fetchall())
    missing_rows = list(conn.execute(
        "SELECT * FROM media_files WHERE source_id=? AND presence_status='MISSING' ORDER BY canonical_path COLLATE NOCASE",
        (source_id,),
    ).fetchall())

    video_by_file: dict[int, list[dict[str, Any]]] = defaultdict(list)
    audio_by_file: dict[int, list[dict[str, Any]]] = defaultdict(list)
    subtitle_by_file: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for row in conn.execute(
        """
        SELECT vs.* FROM video_streams vs
        JOIN media_files mf ON mf.file_id=vs.file_id
        WHERE mf.source_id=? AND mf.presence_status='ACTIVE'
        ORDER BY mf.canonical_path COLLATE NOCASE, vs.stream_index
        """,
        (source_id,),
    ):
        video_by_file[int(row["file_id"])].append(row_to_dict(row))

    for row in conn.execute(
        """
        SELECT a.* FROM audio_streams a
        JOIN media_files mf ON mf.file_id=a.file_id
        WHERE mf.source_id=? AND mf.presence_status='ACTIVE'
        ORDER BY mf.canonical_path COLLATE NOCASE, a.stream_index
        """,
        (source_id,),
    ):
        audio_by_file[int(row["file_id"])].append(row_to_dict(row))

    for row in conn.execute(
        """
        SELECT s.* FROM subtitle_streams s
        JOIN media_files mf ON mf.file_id=s.file_id
        WHERE mf.source_id=? AND mf.presence_status='ACTIVE'
        ORDER BY mf.canonical_path COLLATE NOCASE, s.stream_index
        """,
        (source_id,),
    ):
        subtitle_by_file[int(row["file_id"])].append(row_to_dict(row))

    active: list[dict[str, Any]] = []
    for row in active_rows:
        item = row_to_dict(row)
        fid = int(row["file_id"])
        item["video_streams"] = video_by_file.get(fid, [])
        item["audio_streams"] = audio_by_file.get(fid, [])
        item["subtitle_streams"] = subtitle_by_file.get(fid, [])
        active.append(item)

    missing = [row_to_dict(row) for row in missing_rows]
    successful = [x for x in active if x["probe_status"] == "OK" and x["metadata_state"] == "CURRENT"]
    failed = [x for x in active if x["probe_status"] == "FAILED"]

    for item in failed:
        err = conn.execute(
            """
            SELECT * FROM probe_errors
            WHERE file_id=?
            ORDER BY error_id DESC LIMIT 1
            """,
            (item["file_id"],),
        ).fetchone()
        item["latest_error"] = row_to_dict(err) if err else None

    return {
        "source": row_to_dict(source_row),
        "latest_scan": row_to_dict(latest_scan) if latest_scan else None,
        "completed_scan": row_to_dict(completed_scan) if completed_scan else None,
        "active": active,
        "successful": successful,
        "failed": failed,
        "missing": missing,
    }
