# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import fcntl
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

from .logger import RunLogger
from .platform_api import (
    AccessPathResult,
    SourceDescriptor,
    SourceExportResult,
    StorageDescriptor,
    StorageMatch,
    StorageMatchResult,
)


_NETWORK_FILESYSTEMS = {
    "cifs",
    "smb3",
    "nfs",
    "nfs4",
}

_AUTOFS_FILESYSTEMS = {
    "autofs",
}


@dataclass(frozen=True)
class _MountInfo:
    mount_id: int
    parent_id: int
    major_minor: str
    root: str
    mount_point: str
    filesystem: str
    source: str
    mount_options: str
    super_options: str


class _LinuxInstanceLock:
    def __init__(self) -> None:
        uid = getattr(os, "getuid", lambda: 0)()
        self.path = Path(tempfile.gettempdir()) / f"MediaCatalog_{uid}.lock"
        self.handle: Optional[Any] = None

    def acquire(self) -> bool:
        try:
            handle = open(self.path, "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            self.handle = handle
            return True
        except BlockingIOError:
            try:
                handle.close()
            except Exception:
                pass
            return False
        except Exception:
            try:
                handle.close()
            except Exception:
                pass
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self.handle.close()
        except Exception:
            pass
        self.handle = None


def _strip_surrounding_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1].strip()
    return value


def _unescape_mountinfo(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 8))
        except Exception:
            return match.group(0)

    return re.sub(r"\\([0-7]{3})", repl, value)


def _read_mountinfo() -> list[_MountInfo]:
    mounts: list[_MountInfo] = []
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return mounts

    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 6 or len(right_fields) < 3:
                continue
            mounts.append(
                _MountInfo(
                    mount_id=int(left_fields[0]),
                    parent_id=int(left_fields[1]),
                    major_minor=left_fields[2],
                    root=_unescape_mountinfo(left_fields[3]),
                    mount_point=_unescape_mountinfo(left_fields[4]),
                    mount_options=left_fields[5],
                    filesystem=right_fields[0],
                    source=_unescape_mountinfo(right_fields[1]),
                    super_options=" ".join(right_fields[2:]),
                )
            )
        except Exception:
            continue
    return mounts


def _path_key(value: str) -> str:
    return posixpath.normpath(value)


def _relative_key(value: str) -> str:
    value = value.replace("\\", "/").strip("/")
    if not value:
        return ""
    normalized = posixpath.normpath(value)
    return "" if normalized == "." else normalized


def _path_parts(value: str) -> tuple[str, ...]:
    value = value.replace("\\", "/").strip("/")
    if not value:
        return ()
    return tuple(part for part in PurePosixPath(value).parts if part not in ("/", ""))


def _is_parts_ancestor(parent: str, child: str) -> bool:
    p = _path_parts(parent)
    c = _path_parts(child)
    return len(p) < len(c) and c[: len(p)] == p


def _join_posix(root: str, relative: str) -> str:
    if not relative:
        return posixpath.normpath(root)
    return posixpath.normpath(root.rstrip("/") + "/" + relative.lstrip("/"))


def _is_path_within_mount(path: str, mount_point: str) -> bool:
    try:
        path_norm = posixpath.normpath(path)
        mount_norm = posixpath.normpath(mount_point)
        return path_norm == mount_norm or posixpath.commonpath([path_norm, mount_norm]) == mount_norm
    except Exception:
        return False


def _find_mount_for_path(path: str, mounts: Optional[Sequence[_MountInfo]] = None) -> Optional[_MountInfo]:
    candidates = mounts if mounts is not None else _read_mountinfo()
    matching = [mount for mount in candidates if _is_path_within_mount(path, mount.mount_point)]
    if not matching:
        return None

    # systemd automounts can expose two entries for the same target:
    # an autofs trigger plus the real backing filesystem.  The backing
    # filesystem must win when both have the same mount-point depth.
    return max(
        matching,
        key=lambda item: (
            len(posixpath.normpath(item.mount_point)),
            item.filesystem.casefold() not in _AUTOFS_FILESYSTEMS,
            item.mount_id,
        ),
    )


def _directory_is_enumerable(path: str) -> bool:
    """Return True only when the directory can actually be opened.

    os.path.isdir() only proves that the inode is a directory.  It can still
    return True for an unreadable directory and for an empty automount trigger.
    Opening the directory catches permission failures and also provides the
    filesystem access that systemd autofs needs in order to activate a backing
    mount.
    """
    try:
        with os.scandir(path):
            return True
    except OSError:
        return False


def _resolve_backing_mount_for_path(path: str, *, trigger_automount: bool) -> Optional[_MountInfo]:
    mounts = _read_mountinfo()
    mount = _find_mount_for_path(path, mounts)
    if mount is None:
        return None

    if mount.filesystem.casefold() not in _AUTOFS_FILESYSTEMS:
        return mount

    if trigger_automount:
        # Do not trust the autofs layer as storage identity.  Touch the target
        # directory to give systemd a chance to activate the real filesystem,
        # then re-read mountinfo because the table may have changed.
        _directory_is_enumerable(path)
        mounts = _read_mountinfo()
        mount = _find_mount_for_path(path, mounts)
        if mount is not None and mount.filesystem.casefold() not in _AUTOFS_FILESYSTEMS:
            return mount

    # autofs/systemd-1 is only a trigger layer, never a storage identity.
    return None


def _path_accessible_on_expected_storage(path: str, identity_key: str) -> tuple[bool, Optional[str]]:
    if not os.path.isdir(path):
        return False, None

    resolved = posixpath.normpath(os.path.realpath(path))
    mount = _resolve_backing_mount_for_path(resolved, trigger_automount=True)
    if mount is None:
        return False, None

    try:
        _, current_key, _, _ = _identity_for_mount(mount)
    except Exception:
        return False, None

    if current_key != identity_key:
        return False, None

    if not _directory_is_enumerable(resolved):
        return False, None

    return True, resolved


def _device_identifier(device_source: str, directory: str) -> Optional[str]:
    if not device_source.startswith("/dev/"):
        return None
    identifier_root = Path(directory)
    try:
        target = os.path.realpath(device_source)
        if not identifier_root.is_dir():
            return None
        for entry in identifier_root.iterdir():
            try:
                if os.path.realpath(str(entry)) == target:
                    return entry.name
            except Exception:
                continue
    except Exception:
        return None
    return None


def _device_uuid(device_source: str) -> Optional[str]:
    return _device_identifier(device_source, "/dev/disk/by-uuid")


def _device_partuuid(device_source: str) -> Optional[str]:
    return _device_identifier(device_source, "/dev/disk/by-partuuid")


def _normalized_network_source(filesystem: str, source: str) -> str:
    fs = filesystem.casefold()
    source = source.strip()
    if fs in {"cifs", "smb3"}:
        source = source.replace("\\", "/")
        while source.startswith("////"):
            source = source[2:]
        return source.casefold()
    return source


def _identity_for_mount(mount: _MountInfo) -> tuple[str, str, Optional[str], Optional[str]]:
    filesystem = mount.filesystem.casefold()
    mount_root = posixpath.normpath(mount.root or "/")

    if filesystem in {"cifs", "smb3"}:
        source = _normalized_network_source(filesystem, mount.source)
        return (
            "LINUX_CIFS",
            f"LINUX_CIFS:{source}|ROOT:{mount_root}",
            None,
            None,
        )

    if filesystem in {"nfs", "nfs4"}:
        source = _normalized_network_source(filesystem, mount.source)
        return (
            "LINUX_NFS",
            f"LINUX_NFS:{source}|ROOT:{mount_root}",
            None,
            None,
        )

    volume_uuid = _device_uuid(mount.source)
    if volume_uuid:
        return (
            "LINUX_LOCAL",
            f"LINUX_LOCAL:UUID:{volume_uuid.casefold()}|ROOT:{mount_root}",
            volume_uuid,
            mount.major_minor,
        )

    source_key = mount.source if mount.source else mount.major_minor
    return (
        "LINUX_LOCAL",
        f"LINUX_LOCAL:DEV:{source_key}|MM:{mount.major_minor}|ROOT:{mount_root}",
        None,
        mount.major_minor,
    )


def _mount_relative_path(path: str, mount_point: str) -> str:
    try:
        relative = posixpath.relpath(path, mount_point)
        return "" if relative == "." else relative.strip("/")
    except Exception:
        return ""


def _network_identity_type(identity_type: str) -> bool:
    return identity_type in {"LINUX_CIFS", "LINUX_NFS"}


class LinuxPlatform:
    def configure_console(self) -> None:
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            try:
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def clear_screen(self) -> None:
        try:
            os.system("clear")
        except Exception:
            print("\n" * 3)

    def create_instance_lock(self) -> _LinuxInstanceLock:
        return _LinuxInstanceLock()

    def ffprobe_path(self, app_root: Path) -> Path:
        bundled = app_root / "ffmpeg" / "bin" / "ffprobe"
        if bundled.is_file():
            return bundled
        system_ffprobe = shutil.which("ffprobe")
        if system_ffprobe:
            return Path(system_ffprobe)
        return bundled

    def subprocess_creation_kwargs(self) -> dict[str, Any]:
        return {}

    def default_excluded_directories(self) -> list[str]:
        excluded = ["lost+found", ".Trash"]
        try:
            excluded.append(f".Trash-{os.getuid()}")
        except Exception:
            pass
        return excluded

    def default_excluded_extensions(self) -> list[str]:
        return []

    def path_key(self, value: str) -> str:
        return _path_key(value)

    def relative_key(self, value: str) -> str:
        return _relative_key(value)

    def paths_equal(self, left: str, right: str) -> bool:
        return _path_key(left) == _path_key(right)

    def is_path_ancestor(self, parent: str, child: str) -> bool:
        return _is_parts_ancestor(parent, child)

    def join_path(self, root: str, relative: str) -> str:
        return _join_posix(root, relative)

    def storage_relative_path(self, source_root_relative_path: str, rel_from_source: str) -> str:
        base = str(source_root_relative_path or "").replace("\\", "/").strip("/")
        rel = str(rel_from_source or "").replace("\\", "/").strip("/")
        if base and rel:
            return posixpath.normpath(base + "/" + rel)
        if base or rel:
            normalized = posixpath.normpath(base or rel)
            return "" if normalized == "." else normalized
        return ""

    def normalize_path(self, value: str) -> str:
        return posixpath.normpath(value.replace("\\", "/"))

    def resolve_user_path(self, value: str) -> str:
        stripped = _strip_surrounding_quotes(value)
        expanded = os.path.expanduser(stripped)
        return posixpath.normpath(posixpath.abspath(expanded))

    def resolve_source(self, raw_path: str, logger: RunLogger) -> SourceDescriptor:
        original = _strip_surrounding_quotes(raw_path)
        if not original:
            raise ValueError("EMPTY_SOURCE_PATH")

        normalized_input = self.resolve_user_path(original)
        if not os.path.isdir(normalized_input):
            raise FileNotFoundError(normalized_input)

        resolved = posixpath.normpath(os.path.realpath(normalized_input))
        aliases: list[tuple[str, str, int]] = []
        notices: list[str] = []

        if normalized_input != resolved:
            aliases.append((normalized_input, "SYMLINK", 1))
            logger.info(
                "SOURCE",
                f"Source resolve method=SYMLINK input={normalized_input!r} target={resolved!r}",
            )

        mount = _resolve_backing_mount_for_path(resolved, trigger_automount=True)
        if mount is None:
            logger.warning("SOURCE", f"Stable storage identity unavailable; using PATH_FALLBACK for {resolved}")
            storage = StorageDescriptor(
                identity_type="PATH_FALLBACK",
                identity_key="PATH:" + _path_key(resolved),
                canonical_root=resolved,
            )
            return SourceDescriptor(
                user_path=normalized_input,
                canonical_path=resolved,
                display_path=resolved,
                storage=storage,
                source_root_relative_path="",
                source_root_key="",
                aliases=aliases,
                notices=notices,
            )

        identity_type, identity_key, volume_guid, volume_serial = _identity_for_mount(mount)
        mount_point = posixpath.normpath(mount.mount_point)
        rel = _mount_relative_path(resolved, mount_point)

        storage = StorageDescriptor(
            identity_type=identity_type,
            identity_key=identity_key,
            canonical_root=mount_point,
            volume_guid=volume_guid,
            volume_serial=volume_serial,
            filesystem=mount.filesystem,
            volume_label=None,
        )

        alias_type = "MOUNT"
        if identity_type == "LINUX_CIFS":
            alias_type = "CIFS"
        elif identity_type == "LINUX_NFS":
            alias_type = "NFS"
        aliases.append((resolved, alias_type, 1))

        volume_uuid = _device_uuid(mount.source)
        partuuid = _device_partuuid(mount.source)
        logger.info(
            "SOURCE",
            f"Source resolve method={identity_type} input={normalized_input!r} target={resolved!r} "
            f"mount={mount_point!r} filesystem={mount.filesystem!r} device={mount.source!r} "
            f"uuid={volume_uuid!r} partuuid={partuuid!r} identity_key={identity_key!r}",
        )

        return SourceDescriptor(
            user_path=normalized_input,
            canonical_path=resolved,
            display_path=resolved,
            storage=storage,
            source_root_relative_path=rel,
            source_root_key=_relative_key(rel),
            aliases=aliases,
            notices=notices,
        )

    def match_storage(
        self,
        descriptor: StorageDescriptor,
        source_relative_path: str,
        existing: Sequence[Mapping[str, Any]],
        logger: RunLogger,
    ) -> StorageMatchResult:
        notices: list[str] = []
        exact = next((row for row in existing if row["identity_key"] == descriptor.identity_key), None)
        if exact is not None:
            logger.info(
                "SOURCE",
                f"Source identity matched method={descriptor.identity_type} storage_id={int(exact['storage_id'])} "
                f"canonical_root={exact['canonical_root']!r}",
            )
            return StorageMatchResult(
                StorageMatch(int(exact["storage_id"]), descriptor.identity_type),
                tuple(notices),
            )

        if not _network_identity_type(descriptor.identity_type):
            return StorageMatchResult(notices=tuple(notices))

        network_existing = [
            row for row in existing
            if _network_identity_type(str(row["identity_type"]))
        ]
        if not network_existing:
            return StorageMatchResult(notices=tuple(notices))

        marker_name = f".MediaCatalogIdentity_{uuid.uuid4().hex}.tmp"
        token = uuid.uuid4().hex + uuid.uuid4().hex
        candidate_dir = (
            _join_posix(descriptor.canonical_root, source_relative_path)
            if source_relative_path
            else descriptor.canonical_root
        )
        candidate_marker = Path(candidate_dir) / marker_name

        try:
            with open(candidate_marker, "x", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        except PermissionError:
            message = (
                "Source is read-only. Network alias verification by marker file was skipped. "
                "The source remains valid and will be kept separate unless identity can be proven read-only."
            )
            logger.warning("SOURCE", message + f" Path={descriptor.canonical_root}")
            notices.append("READ_ONLY_IDENTITY")
            return StorageMatchResult(notices=tuple(notices))
        except OSError as exc:
            logger.warning("SOURCE", f"Network marker fallback unavailable for {descriptor.canonical_root}: {exc}")
            return StorageMatchResult(notices=tuple(notices))

        match_row: Optional[Mapping[str, Any]] = None
        try:
            for row in network_existing:
                other_root = str(row["canonical_root"])
                try:
                    other_dir = _join_posix(other_root, source_relative_path) if source_relative_path else other_root
                    other_marker = Path(other_dir) / marker_name
                    if other_marker.exists():
                        data = other_marker.read_text(encoding="utf-8", errors="replace")
                        if data == token:
                            match_row = row
                            break
                except Exception:
                    continue
        finally:
            try:
                candidate_marker.unlink(missing_ok=True)
            except Exception:
                logger.warning("SOURCE", f"Could not remove temporary identity marker: {candidate_marker}")

        if match_row is None:
            return StorageMatchResult(notices=tuple(notices))

        logger.info(
            "SOURCE",
            f"Source identity matched method=TEMP_MARKER storage_id={int(match_row['storage_id'])} "
            f"input_root={descriptor.canonical_root!r} canonical_root={match_row['canonical_root']!r}",
        )
        return StorageMatchResult(
            StorageMatch(int(match_row["storage_id"]), "TEMP_MARKER"),
            tuple(notices),
        )

    def canonicalize_source(self, descriptor: SourceDescriptor, storage: Mapping[str, Any]) -> SourceDescriptor:
        stored_root = str(storage["canonical_root"])
        if _path_key(stored_root) != _path_key(descriptor.storage.canonical_root):
            canonical = _join_posix(stored_root, descriptor.source_root_relative_path)
            descriptor.canonical_path = canonical
            descriptor.display_path = canonical
        descriptor.storage.identity_key = str(storage["identity_key"])
        descriptor.storage.canonical_root = stored_root
        return descriptor

    def resolve_source_access_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> AccessPathResult:
        canonical = str(source["canonical_path"])
        identity_key = str(source["identity_key"])
        source_relative = str(source["source_root_relative_path"] or "")

        accessible, resolved = _path_accessible_on_expected_storage(canonical, identity_key)
        if accessible and resolved is not None:
            changed = None if _path_key(resolved) == _path_key(canonical) else resolved
            return AccessPathResult(resolved, changed)

        # The original mount-point may have moved.  Search currently active
        # backing filesystems for the same stable storage identity.  autofs
        # trigger layers are deliberately ignored here.
        for mount in _read_mountinfo():
            if mount.filesystem.casefold() in _AUTOFS_FILESYSTEMS:
                continue
            try:
                _, candidate_key, _, _ = _identity_for_mount(mount)
            except Exception:
                continue
            if candidate_key != identity_key:
                continue
            candidate = _join_posix(mount.mount_point, source_relative)
            accessible, resolved = _path_accessible_on_expected_storage(candidate, identity_key)
            if accessible and resolved is not None:
                changed = None if _path_key(resolved) == _path_key(canonical) else resolved
                return AccessPathResult(resolved, changed)

        # Aliases may themselves be systemd automount targets or symlinks.
        # Validate both real accessibility and the backing storage identity
        # before returning one as a usable source path.
        for alias in aliases:
            path = str(alias["alias_path"])
            accessible, resolved = _path_accessible_on_expected_storage(path, identity_key)
            if accessible and resolved is not None:
                changed = None if _path_key(resolved) == _path_key(canonical) else resolved
                return AccessPathResult(resolved, changed)

        return AccessPathResult(None)

    def source_export_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> SourceExportResult:
        access = self.resolve_source_access_path(source, aliases)
        return SourceExportResult(
            access.path or str(source["canonical_path"]),
            access.canonical_path,
        )

    def is_directory_link(self, path: str) -> bool:
        try:
            return os.path.islink(path)
        except Exception:
            return False

    def sanitize_filename_from_path(self, path: str) -> str:
        value = path.replace("\\", "/").strip("/")
        value = value.replace("/", "_")
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = re.sub(r"_+", "_", value)
        value = value.rstrip(" ._")
        return value or "Source"
