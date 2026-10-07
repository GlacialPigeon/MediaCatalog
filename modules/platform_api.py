# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from .logger import RunLogger


class InstanceLock(Protocol):
    def acquire(self) -> bool: ...
    def release(self) -> None: ...


@dataclass
class StorageDescriptor:
    identity_type: str
    identity_key: str
    canonical_root: str
    volume_guid: Optional[str] = None
    volume_serial: Optional[str] = None
    filesystem: Optional[str] = None
    volume_label: Optional[str] = None
    remote_volume_serial: Optional[int] = None
    remote_file_id: Optional[str] = None


@dataclass
class SourceDescriptor:
    user_path: str
    canonical_path: str
    display_path: str
    storage: StorageDescriptor
    source_root_relative_path: str
    source_root_key: str
    aliases: list[tuple[str, str, int]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StorageMatch:
    storage_id: int
    method: str
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorageMatchResult:
    match: Optional[StorageMatch] = None
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessPathResult:
    path: Optional[str]
    canonical_path: Optional[str] = None


@dataclass(frozen=True)
class SourceExportResult:
    path: str
    canonical_path: Optional[str] = None


class PlatformAdapter(Protocol):
    def configure_console(self) -> None: ...
    def clear_screen(self) -> None: ...
    def create_instance_lock(self) -> InstanceLock: ...

    def ffprobe_path(self, app_root: Path) -> Path: ...
    def subprocess_creation_kwargs(self) -> dict[str, Any]: ...

    def default_excluded_directories(self) -> list[str]: ...
    def default_excluded_extensions(self) -> list[str]: ...

    def path_key(self, value: str) -> str: ...
    def relative_key(self, value: str) -> str: ...
    def paths_equal(self, left: str, right: str) -> bool: ...
    def is_path_ancestor(self, parent: str, child: str) -> bool: ...
    def join_path(self, root: str, relative: str) -> str: ...
    def storage_relative_path(self, source_root_relative_path: str, rel_from_source: str) -> str: ...
    def normalize_path(self, value: str) -> str: ...
    def resolve_user_path(self, value: str) -> str: ...

    def resolve_source(self, raw_path: str, logger: RunLogger) -> SourceDescriptor: ...
    def match_storage(
        self,
        descriptor: StorageDescriptor,
        source_relative_path: str,
        existing: Sequence[Mapping[str, Any]],
        logger: RunLogger,
    ) -> StorageMatchResult: ...
    def canonicalize_source(self, descriptor: SourceDescriptor, storage: Mapping[str, Any]) -> SourceDescriptor: ...
    def resolve_source_access_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> AccessPathResult: ...
    def source_export_path(
        self,
        source: Mapping[str, Any],
        aliases: Sequence[Mapping[str, Any]],
    ) -> SourceExportResult: ...

    def is_directory_link(self, path: str) -> bool: ...
    def sanitize_filename_from_path(self, path: str) -> str: ...
