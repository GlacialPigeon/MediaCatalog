# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path, PureWindowsPath
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

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime_dwLowDateTime", ctypes.c_uint32),
        ("ftCreationTime_dwHighDateTime", ctypes.c_uint32),
        ("ftLastAccessTime_dwLowDateTime", ctypes.c_uint32),
        ("ftLastAccessTime_dwHighDateTime", ctypes.c_uint32),
        ("ftLastWriteTime_dwLowDateTime", ctypes.c_uint32),
        ("ftLastWriteTime_dwHighDateTime", ctypes.c_uint32),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _GlobalInstanceLock:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        self.handle: Any = None

    def acquire(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        name = "Global\\MediaCatalog_GlobalInstance"
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return False
        error = ctypes.get_last_error()
        if error == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None


def _strip_surrounding_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1].strip()
    return value


def _path_key(value: str) -> str:
    value = value.replace("/", "\\")
    value = ntpath.normpath(value)
    return value.casefold()


def _relative_key(value: str) -> str:
    value = value.replace("/", "\\").strip("\\")
    if not value:
        return ""
    return ntpath.normpath(value).casefold()


def _path_parts(value: str) -> tuple[str, ...]:
    value = value.replace("/", "\\").strip("\\")
    if not value:
        return ()
    return tuple(part.casefold() for part in PureWindowsPath(value).parts if part not in ("\\", "/"))


def _is_parts_ancestor(parent: str, child: str) -> bool:
    p = _path_parts(parent)
    c = _path_parts(child)
    return len(p) < len(c) and c[: len(p)] == p


def _join_windows(root: str, relative: str) -> str:
    if not relative:
        return ntpath.normpath(root)
    return ntpath.normpath(root.rstrip("\\/") + "\\" + relative.lstrip("\\/"))


def _split_unc(path: str) -> tuple[str, str]:
    normalized = path.replace("/", "\\")
    if not normalized.startswith("\\\\"):
        raise ValueError(f"Not a UNC path: {path}")
    parts = normalized[2:].split("\\")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid UNC path: {path}")
    root = f"\\\\{parts[0]}\\{parts[1]}"
    rel = "\\".join(parts[2:]).strip("\\")
    return root, rel


def _resolve_mapped_drive(path: str) -> Optional[str]:
    match = re.match(r"^([A-Za-z]:)(?:\\|/|$)", path)
    if not match:
        return None
    drive = match.group(1)
    try:
        mpr = ctypes.WinDLL("mpr", use_last_error=True)
        WNetGetConnectionW = mpr.WNetGetConnectionW
        WNetGetConnectionW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        WNetGetConnectionW.restype = ctypes.c_uint32
        size = ctypes.c_uint32(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        result = WNetGetConnectionW(drive, buf, ctypes.byref(size))
        if result != 0:
            return None
        tail = path[len(drive):].lstrip("\\/")
        unc_root = buf.value.rstrip("\\/")
        return unc_root if not tail else unc_root + "\\" + tail.replace("/", "\\")
    except Exception:
        return None


def _guid_from_string(value: str) -> _GUID:
    guid = _GUID()
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    fn = ole32.CLSIDFromString
    fn.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_GUID)]
    fn.restype = ctypes.c_long
    hr = fn(value, ctypes.byref(guid))
    if hr < 0:
        raise OSError(f"CLSIDFromString failed: 0x{hr & 0xFFFFFFFF:08X}")
    return guid


def _com_method(instance: ctypes.c_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
    vtable = ctypes.cast(instance, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = vtable[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(address)


def _resolve_lnk_native(link_path: Path, logger: RunLogger) -> Optional[str]:
    """Resolve a Windows .lnk through ShellLink COM without PowerShell/pywin32."""
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    co_initialize = ole32.CoInitializeEx
    co_initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    co_initialize.restype = ctypes.c_long
    co_uninitialize = ole32.CoUninitialize
    co_uninitialize.argtypes = []
    co_uninitialize.restype = None
    co_create = ole32.CoCreateInstance
    co_create.argtypes = [
        ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    co_create.restype = ctypes.c_long

    clsid_shell_link = _guid_from_string("{00021401-0000-0000-C000-000000000046}")
    iid_shell_link_w = _guid_from_string("{000214F9-0000-0000-C000-000000000046}")
    iid_persist_file = _guid_from_string("{0000010B-0000-0000-C000-000000000046}")

    hr_init = co_initialize(None, 0x2)
    rpc_e_changed_mode = -2147417850
    if hr_init < 0 and hr_init != rpc_e_changed_mode:
        logger.warning("SOURCE", f"COM initialization failed for .lnk resolution: 0x{hr_init & 0xFFFFFFFF:08X}")
        return None
    should_uninitialize = hr_init in (0, 1)
    shell_link = ctypes.c_void_p()
    persist_file = ctypes.c_void_p()
    try:
        hr = co_create(
            ctypes.byref(clsid_shell_link), None, 0x1,
            ctypes.byref(iid_shell_link_w), ctypes.byref(shell_link),
        )
        if hr < 0 or not shell_link:
            return None

        query_interface = _com_method(
            shell_link, 0, ctypes.c_long,
            ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        )
        hr = query_interface(shell_link, ctypes.byref(iid_persist_file), ctypes.byref(persist_file))
        if hr < 0 or not persist_file:
            return None

        load = _com_method(persist_file, 5, ctypes.c_long, ctypes.c_wchar_p, ctypes.c_uint32)
        hr = load(persist_file, str(link_path), 0)
        if hr < 0:
            return None

        get_path = _com_method(
            shell_link, 3, ctypes.c_long,
            ctypes.POINTER(ctypes.c_wchar), ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        )
        for flags in (0, 0x4):
            buffer = ctypes.create_unicode_buffer(32768)
            hr = get_path(shell_link, buffer, len(buffer), None, flags)
            target = buffer.value.strip()
            if hr >= 0 and target:
                return target
        return None
    except Exception as exc:
        logger.warning("SOURCE", f"Native .lnk resolution failed for {link_path}: {exc}")
        return None
    finally:
        for instance in (persist_file, shell_link):
            if instance:
                try:
                    release = _com_method(instance, 2, ctypes.c_ulong)
                    release(instance)
                except Exception:
                    pass
        if should_uninitialize:
            try:
                co_uninitialize()
            except Exception:
                pass


def _resolve_network_shortcut(path: str, logger: RunLogger) -> tuple[str, list[tuple[str, str, int]], bool]:
    aliases: list[tuple[str, str, int]] = []
    p = Path(path)
    shortcut_link: Optional[Path] = None
    try:
        if p.is_file() and p.suffix.casefold() == ".lnk":
            shortcut_link = p
        elif p.is_dir():
            target_lnk = p / "target.lnk"
            if target_lnk.exists():
                shortcut_link = target_lnk
    except Exception:
        shortcut_link = None

    if shortcut_link is None:
        return path, aliases, False

    target = _resolve_lnk_native(shortcut_link, logger)
    if target:
        aliases.append((path, "NETWORK_SHORTCUT", 1))
        return target, aliases, False

    aliases.append((path, "NETWORK_SHORTCUT", 0))
    return path, aliases, True


def _get_volume_descriptor(path: str) -> Optional[dict[str, Any]]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GetVolumePathNameW = kernel32.GetVolumePathNameW
    GetVolumePathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    GetVolumePathNameW.restype = ctypes.c_bool
    mount = ctypes.create_unicode_buffer(32768)
    if not GetVolumePathNameW(path, mount, len(mount)):
        return None
    mount_path = mount.value

    GetVolumeNameForVolumeMountPointW = kernel32.GetVolumeNameForVolumeMountPointW
    GetVolumeNameForVolumeMountPointW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    GetVolumeNameForVolumeMountPointW.restype = ctypes.c_bool
    volbuf = ctypes.create_unicode_buffer(32768)
    if not GetVolumeNameForVolumeMountPointW(mount_path, volbuf, len(volbuf)):
        return None
    volume_guid = volbuf.value

    volume_name = ctypes.create_unicode_buffer(1024)
    filesystem = ctypes.create_unicode_buffer(1024)
    serial = ctypes.c_uint32()
    max_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    GetVolumeInformationW = kernel32.GetVolumeInformationW
    GetVolumeInformationW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p, ctypes.c_uint32,
    ]
    GetVolumeInformationW.restype = ctypes.c_bool
    ok = GetVolumeInformationW(
        mount_path,
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    )
    return {
        "mount_path": mount_path,
        "volume_guid": volume_guid,
        "volume_serial": f"{serial.value:08X}" if ok else None,
        "filesystem": filesystem.value if ok else None,
        "volume_label": volume_name.value if ok else None,
    }


def _get_volume_mount_paths(volume_guid: str) -> list[str]:
    if not volume_guid:
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel32.GetVolumePathNamesForVolumeNameW
    fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    fn.restype = ctypes.c_bool
    needed = ctypes.c_uint32(0)
    fn(volume_guid, None, 0, ctypes.byref(needed))
    if needed.value <= 1:
        return []
    buf = ctypes.create_unicode_buffer(needed.value)
    if not fn(volume_guid, buf, needed.value, ctypes.byref(needed)):
        return []
    raw = buf[: needed.value]
    joined = "".join(raw)
    return [item for item in joined.split("\x00") if item]


def _get_directory_file_identity(path: str) -> Optional[tuple[int, str]]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    CreateFileW.restype = ctypes.c_void_p
    handle = CreateFileW(
        path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE or not handle:
        return None
    try:
        info = _BY_HANDLE_FILE_INFORMATION()
        GetFileInformationByHandle = kernel32.GetFileInformationByHandle
        GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
        GetFileInformationByHandle.restype = ctypes.c_bool
        if not GetFileInformationByHandle(handle, ctypes.byref(info)):
            return None
        file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        if file_index == 0:
            return None
        return int(info.dwVolumeSerialNumber), f"{file_index:016X}"
    finally:
        kernel32.CloseHandle(handle)


class WindowsPlatform:
    def configure_console(self) -> None:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            STD_OUTPUT_HANDLE = -11
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if handle not in (0, -1) and kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        except Exception:
            pass

        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            try:
                if stream is not None and hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def clear_screen(self) -> None:
        try:
            os.system("cls")
        except Exception:
            print("\n" * 3)

    def create_instance_lock(self) -> _GlobalInstanceLock:
        return _GlobalInstanceLock()

    def ffprobe_path(self, app_root: Path) -> Path:
        return app_root / "ffmpeg" / "bin" / "ffprobe.exe"

    def subprocess_creation_kwargs(self) -> dict[str, Any]:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

    def default_excluded_directories(self) -> list[str]:
        return ["$RECYCLE.BIN", "System Volume Information"]

    def default_excluded_extensions(self) -> list[str]:
        return [".lnk"]

    def path_key(self, value: str) -> str:
        return _path_key(value)

    def relative_key(self, value: str) -> str:
        return _relative_key(value)

    def paths_equal(self, left: str, right: str) -> bool:
        return _path_key(left) == _path_key(right)

    def is_path_ancestor(self, parent: str, child: str) -> bool:
        return _is_parts_ancestor(parent, child)

    def join_path(self, root: str, relative: str) -> str:
        return _join_windows(root, relative)

    def storage_relative_path(self, source_root_relative_path: str, rel_from_source: str) -> str:
        base = str(source_root_relative_path or "").strip("\\/")
        rel = rel_from_source.replace("/", "\\").strip("\\/")
        if base and rel:
            return ntpath.normpath(base + "\\" + rel)
        return ntpath.normpath(base or rel) if (base or rel) else ""

    def normalize_path(self, value: str) -> str:
        return ntpath.normpath(value)

    def resolve_source(self, raw_path: str, logger: RunLogger) -> SourceDescriptor:
        original = _strip_surrounding_quotes(raw_path)
        if not original:
            raise ValueError("EMPTY_SOURCE_PATH")

        resolved, aliases, unresolved_shortcut = _resolve_network_shortcut(original, logger)
        resolved = _strip_surrounding_quotes(resolved)
        notices: list[str] = []
        shortcut_alias = next((item for item in aliases if item[1] == "NETWORK_SHORTCUT"), None)
        if shortcut_alias is not None and not unresolved_shortcut:
            logger.info("SOURCE", f"Source resolve method=NETWORK_SHORTCUT_LNK input={original!r} target={resolved!r}")
        if unresolved_shortcut:
            warning = (
                "The actual Network Shortcut target could not be resolved. "
                "This source will be added if the folder is accessible, but MediaCatalog cannot verify "
                "whether it duplicates another configured source. Please verify source aliases manually."
            )
            logger.warning("SOURCE", f"{warning} Path={original}")
            notices.append("NETWORK_SHORTCUT_UNRESOLVED")

        mapped = _resolve_mapped_drive(resolved)
        if mapped:
            mapped_input = resolved
            aliases.append((original, "MAPPED_DRIVE", 0))
            resolved = mapped
            logger.info("SOURCE", f"Source resolve method=MAPPED_DRIVE input={mapped_input!r} target={resolved!r}")

        normalized = ntpath.normpath(resolved)
        if not os.path.isdir(normalized):
            raise FileNotFoundError(normalized)

        if normalized.startswith("\\\\"):
            unc_root, rel = _split_unc(normalized)
            if shortcut_alias is None and not mapped:
                logger.info("SOURCE", f"Source resolve method=DIRECT_UNC input={original!r} target={normalized!r}")
            remote_identity = _get_directory_file_identity(unc_root)
            storage = StorageDescriptor(
                identity_type="UNC",
                identity_key="UNC:" + _path_key(unc_root),
                canonical_root=unc_root,
                remote_volume_serial=remote_identity[0] if remote_identity else None,
                remote_file_id=remote_identity[1] if remote_identity else None,
            )
            aliases.append((normalized, "UNC", 1))
            return SourceDescriptor(
                user_path=original,
                canonical_path=normalized,
                display_path=normalized,
                storage=storage,
                source_root_relative_path=rel,
                source_root_key=_relative_key(rel),
                aliases=aliases,
                notices=notices,
            )

        volume = _get_volume_descriptor(normalized)
        if volume and volume.get("volume_guid"):
            mount = ntpath.normpath(volume["mount_path"])
            try:
                rel = ntpath.relpath(normalized, mount)
                if rel == ".":
                    rel = ""
            except Exception:
                rel = normalized
            volume_guid = str(volume["volume_guid"])
            storage = StorageDescriptor(
                identity_type="VOLUME_GUID",
                identity_key="VOLUME:" + volume_guid.casefold(),
                canonical_root=mount,
                volume_guid=volume_guid,
                volume_serial=volume.get("volume_serial"),
                filesystem=volume.get("filesystem"),
                volume_label=volume.get("volume_label"),
            )
            return SourceDescriptor(
                user_path=original,
                canonical_path=normalized,
                display_path=normalized,
                storage=storage,
                source_root_relative_path=rel,
                source_root_key=_relative_key(rel),
                aliases=aliases,
                notices=notices,
            )

        logger.warning("SOURCE", f"Stable storage identity unavailable; using PATH_FALLBACK for {normalized}")
        storage = StorageDescriptor(
            identity_type="PATH_FALLBACK",
            identity_key="PATH:" + _path_key(normalized),
            canonical_root=normalized,
        )
        return SourceDescriptor(
            user_path=original,
            canonical_path=normalized,
            display_path=normalized,
            storage=storage,
            source_root_relative_path="",
            source_root_key="",
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
        mismatched_exact_storage_id: Optional[int] = None

        if exact is not None:
            if descriptor.identity_type != "UNC":
                logger.debug(
                    "SOURCE",
                    f"Source identity matched method={descriptor.identity_type} storage_id={int(exact['storage_id'])} "
                    f"canonical_root={exact['canonical_root']!r}",
                )
                return StorageMatchResult(
                    StorageMatch(int(exact["storage_id"]), descriptor.identity_type),
                    tuple(notices),
                )

            have_current_identity = descriptor.remote_volume_serial is not None and bool(descriptor.remote_file_id)
            have_stored_identity = exact["remote_volume_serial"] is not None and bool(exact["remote_file_id"])
            identity_matches = (
                have_current_identity
                and have_stored_identity
                and exact["remote_volume_serial"] == descriptor.remote_volume_serial
                and exact["remote_file_id"] == descriptor.remote_file_id
            )
            if not (have_current_identity and have_stored_identity) or identity_matches:
                match_method = "CANONICAL_UNC+DIRECTORY_ID" if identity_matches else "CANONICAL_UNC"
                logger.info(
                    "SOURCE",
                    f"Source identity matched method={match_method} storage_id={int(exact['storage_id'])} "
                    f"canonical_root={exact['canonical_root']!r}",
                )
                return StorageMatchResult(
                    StorageMatch(int(exact["storage_id"]), match_method),
                    tuple(notices),
                )

            mismatched_exact_storage_id = int(exact["storage_id"])
            base_key = descriptor.identity_key
            descriptor.identity_key = (
                f"{base_key}|REMOTE:{int(descriptor.remote_volume_serial):08X}:"
                f"{str(descriptor.remote_file_id).casefold()}"
            )
            replacement = next(
                (row for row in existing if row["identity_key"] == descriptor.identity_key),
                None,
            )
            if replacement is not None:
                return StorageMatchResult(
                    StorageMatch(int(replacement["storage_id"]), "REMOTE_IDENTITY_REPLACEMENT"),
                    tuple(notices),
                )
            logger.warning(
                "SOURCE",
                f"UNC path now reports a different directory/storage identity and will be treated as a new storage: {descriptor.canonical_root}",
            )

        if descriptor.identity_type != "UNC":
            return StorageMatchResult(notices=tuple(notices))

        unc_existing = [row for row in existing if row["identity_type"] == "UNC"]
        if mismatched_exact_storage_id is not None:
            unc_existing = [row for row in unc_existing if int(row["storage_id"]) != mismatched_exact_storage_id]

        need_marker_fallback = True
        if descriptor.remote_volume_serial is not None and descriptor.remote_file_id:
            matches = [
                row for row in unc_existing
                if row["remote_volume_serial"] == descriptor.remote_volume_serial
                and row["remote_file_id"] == descriptor.remote_file_id
            ]
            if len(matches) == 1:
                row = matches[0]
                logger.info(
                    "SOURCE",
                    f"Source identity matched method=READ_ONLY_DIRECTORY_ID storage_id={int(row['storage_id'])} "
                    f"input_root={descriptor.canonical_root!r} canonical_root={row['canonical_root']!r}",
                )
                return StorageMatchResult(StorageMatch(int(row["storage_id"]), "READ_ONLY_DIRECTORY_ID"))
            all_existing_have_identity = all(
                row["remote_volume_serial"] is not None and bool(row["remote_file_id"]) for row in unc_existing
            )
            if not matches and all_existing_have_identity:
                need_marker_fallback = False

        if not need_marker_fallback or not unc_existing:
            return StorageMatchResult(notices=tuple(notices))

        marker_name = f".MediaCatalogIdentity_{uuid.uuid4().hex}.tmp"
        token = uuid.uuid4().hex + uuid.uuid4().hex
        candidate_dir = _join_windows(descriptor.canonical_root, source_relative_path) if source_relative_path else descriptor.canonical_root
        candidate_marker = Path(candidate_dir) / marker_name
        try:
            with open(candidate_marker, "x", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        except PermissionError:
            message = (
                "Source is read-only. UNC alias verification by marker file was skipped. "
                "The source remains valid and will be kept separate unless identity can be proven read-only."
            )
            logger.warning("SOURCE", message + f" Path={descriptor.canonical_root}")
            notices.append("READ_ONLY_IDENTITY")
            return StorageMatchResult(notices=tuple(notices))
        except OSError as exc:
            logger.warning("SOURCE", f"UNC marker fallback unavailable for {descriptor.canonical_root}: {exc}")
            return StorageMatchResult(notices=tuple(notices))

        match_row: Optional[Mapping[str, Any]] = None
        try:
            for row in unc_existing:
                other_root = str(row["canonical_root"])
                if _path_key(other_root) == _path_key(descriptor.canonical_root):
                    match_row = row
                    break
                try:
                    other_dir = _join_windows(other_root, source_relative_path) if source_relative_path else other_root
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
        if descriptor.storage.identity_type == "UNC" and _path_key(str(storage["canonical_root"])) != _path_key(descriptor.storage.canonical_root):
            canonical = _join_windows(str(storage["canonical_root"]), descriptor.source_root_relative_path)
            descriptor.canonical_path = canonical
            descriptor.display_path = canonical
        descriptor.storage.identity_key = str(storage["identity_key"])
        descriptor.storage.canonical_root = str(storage["canonical_root"])
        return descriptor

    def resolve_source_access_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> AccessPathResult:
        identity_type = source["identity_type"]
        if identity_type == "VOLUME_GUID":
            mounts = _get_volume_mount_paths(str(source["volume_guid"] or ""))
            for mount in mounts:
                candidate = _join_windows(mount, str(source["source_root_relative_path"] or ""))
                if os.path.isdir(candidate):
                    changed = None
                    if _path_key(candidate) != _path_key(str(source["canonical_path"])):
                        changed = candidate
                    return AccessPathResult(candidate, changed)
            return AccessPathResult(None)

        canonical = str(source["canonical_path"])
        if os.path.isdir(canonical):
            return AccessPathResult(canonical)

        for alias in aliases:
            if alias["alias_type"] not in ("UNC", "CANONICAL"):
                continue
            path = str(alias["alias_path"])
            if os.path.isdir(path):
                return AccessPathResult(path)
        return AccessPathResult(None)

    def source_export_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> SourceExportResult:
        if source["identity_type"] == "VOLUME_GUID":
            access = self.resolve_source_access_path(source, aliases)
            return SourceExportResult(
                access.path or str(source["canonical_path"]),
                access.canonical_path,
            )
        return SourceExportResult(str(source["canonical_path"]))

    def is_directory_link(self, path: str) -> bool:
        try:
            st = os.stat(path, follow_symlinks=False)
            attrs = getattr(st, "st_file_attributes", 0)
            return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
        except Exception:
            return False

    def sanitize_filename_from_path(self, path: str) -> str:
        value = path.replace("/", "\\")
        if value.startswith("\\\\"):
            value = value[2:]
        value = re.sub(r"^[A-Za-z]:", lambda m: m.group(0)[0], value)
        value = value.replace("\\", "_")
        value = re.sub(r'[<>:"/\\|?*]', "_", value)
        value = re.sub(r"_+", "_", value)
        value = value.rstrip(" ._")
        return value or "Source"
