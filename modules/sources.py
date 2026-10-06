# MediaCatalog
# Author: GlacialPigeon
# GitHub: https://github.com/GlacialPigeon

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from .config import ConfigManager
from .database import Database
from .datetime_helpers import utc_now_iso
from .logger import RunLogger, format_elapsed
from .platform_api import PlatformAdapter, SourceDescriptor, StorageDescriptor


class SourceError(Exception):
    def __init__(self, key: str, **values: Any) -> None:
        super().__init__(key)
        self.key = key
        self.values = values


@dataclass(frozen=True)
class SourceImportResult:
    removed: int
    requested: int
    added: int
    skipped: int
    invalid: int
    elapsed_seconds: float


@dataclass(frozen=True)
class SourceResolution:
    input_path: str
    resolved_path: str
    storage_root: str
    storage_type: str
    changed: bool


@dataclass(frozen=True)
class SourceBatchIssue:
    input_path: str
    detail: str


@dataclass(frozen=True)
class SourceReplacement:
    new_path: str
    replaced_paths: tuple[str, ...]


@dataclass
class SourceBatchPlan:
    mode: str
    requested: list[str]
    previous_sources: list[str]
    resolutions: list[SourceResolution]
    same_storage_groups: list[tuple[str, tuple[SourceResolution, ...]]]
    unavailable: list[SourceBatchIssue]
    invalid: list[SourceBatchIssue]
    duplicate_inputs: list[tuple[str, str]]
    selected_covered: list[tuple[str, str]]
    already_configured: list[str]
    already_covered: list[tuple[str, str]]
    replacements: list[SourceReplacement]
    final_sources: list[str]

    @property
    def has_resolution_summary(self) -> bool:
        return any(item.changed for item in self.resolutions) or bool(self.same_storage_groups)

    @property
    def has_overlap_summary(self) -> bool:
        return bool(
            self.replacements
            or self.already_covered
            or self.already_configured
            or self.duplicate_inputs
            or self.selected_covered
            or self.unavailable
            or self.invalid
        )

    @property
    def valid_count(self) -> int:
        return len(self.resolutions) - len(self.duplicate_inputs) - len(self.selected_covered)

    @property
    def configuration_changed(self) -> bool:
        return self.final_sources != self.previous_sources


@dataclass(frozen=True)
class SourceSyncResult:
    configured: int
    matched: int
    created: int
    reactivated: int
    unavailable: int
    invalid: int


@dataclass(frozen=True)
class StartupSourceNormalizationResult:
    before: int
    after: int
    resolved: int
    canonicalized: int
    duplicates_removed: int
    covered_removed: int
    unresolved: int
    changed: bool


def strip_surrounding_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1].strip()
    return value


class SourceManager:
    def __init__(
        self,
        db: Database,
        logger: RunLogger,
        platform: PlatformAdapter,
        config: Optional[ConfigManager] = None,
        *,
        notice_callback: Optional[Callable[[str], None]] = None,
        overlap_confirm_callback: Optional[Callable[[SourceDescriptor, list[sqlite3.Row]], bool]] = None,
    ) -> None:
        self.db = db
        self.logger = logger
        self.platform = platform
        self.config = config
        self.notice_callback = notice_callback
        self.overlap_confirm_callback = overlap_confirm_callback

    def set_ui_callbacks(
        self,
        *,
        notice_callback: Optional[Callable[[str], None]] = None,
        overlap_confirm_callback: Optional[Callable[[SourceDescriptor, list[sqlite3.Row]], bool]] = None,
    ) -> None:
        self.notice_callback = notice_callback
        self.overlap_confirm_callback = overlap_confirm_callback

    @property
    def conn(self) -> sqlite3.Connection:
        assert self.db.conn is not None
        return self.db.conn

    def _emit_notice(self, code: str) -> None:
        if self.notice_callback is not None:
            self.notice_callback(code)

    def resolve_descriptor(self, raw_path: str) -> SourceDescriptor:
        original = strip_surrounding_quotes(raw_path)
        if not original:
            raise SourceError("source.add.path_empty")
        try:
            descriptor = self.platform.resolve_source(original, self.logger)
        except ValueError as exc:
            if str(exc) == "EMPTY_SOURCE_PATH":
                raise SourceError("source.add.path_empty") from exc
            raise
        except FileNotFoundError as exc:
            missing = str(exc.args[0]) if exc.args else original
            raise SourceError("source.add.folder_not_found", path=missing) from exc
        for notice in descriptor.notices:
            self._emit_notice(notice)
        return descriptor

    def _canonicalize_against_existing_storage(self, descriptor: SourceDescriptor) -> SourceDescriptor:
        existing = list(self.conn.execute("SELECT * FROM storages ORDER BY storage_id").fetchall())
        if not existing:
            return descriptor
        match_result = self.platform.match_storage(
            descriptor.storage,
            descriptor.source_root_relative_path,
            existing,
            self.logger,
        )
        for notice in match_result.notices:
            self._emit_notice(notice)
        if match_result.match is None:
            return descriptor
        matched = next(
            (row for row in existing if int(row["storage_id"]) == match_result.match.storage_id),
            None,
        )
        if matched is None:
            return descriptor
        return self._canonicalize_against_storage(descriptor, matched)

    def _paths_equal(self, left: str, right: str) -> bool:
        try:
            return self.platform.paths_equal(left, right)
        except Exception:
            return self.platform.path_key(left) == self.platform.path_key(right)

    def _is_ancestor(self, parent: str, child: str) -> bool:
        if self._paths_equal(parent, child):
            return False
        try:
            return self.platform.is_path_ancestor(parent, child)
        except Exception:
            return False

    def plan_batch(self, values: Iterable[str], *, mode: str = "ADD") -> SourceBatchPlan:
        mode = str(mode).upper()
        if mode not in {"ADD", "REPLACE"}:
            raise ValueError(f"Unsupported source batch mode: {mode}")

        requested = [strip_surrounding_quotes(value) for value in values if strip_surrounding_quotes(value)]
        previous = list(self.config.sources if self.config is not None else [])
        resolutions: list[SourceResolution] = []
        resolved_descriptors: list[tuple[int, str, SourceDescriptor]] = []
        unavailable: list[SourceBatchIssue] = []
        invalid: list[SourceBatchIssue] = []

        self.logger.info("SOURCE", f"Batch planning started mode={mode} inputs={len(requested)}")
        for index, raw in enumerate(requested):
            try:
                descriptor = self.resolve_descriptor(raw)
                descriptor = self._canonicalize_against_existing_storage(descriptor)
                resolved = descriptor.canonical_path
                changed = not self._paths_equal(raw, resolved)
                resolutions.append(SourceResolution(
                    input_path=raw,
                    resolved_path=resolved,
                    storage_root=descriptor.storage.canonical_root,
                    storage_type=descriptor.storage.identity_type,
                    changed=changed,
                ))
                resolved_descriptors.append((index, raw, descriptor))
                self.logger.debug(
                    "SOURCE",
                    f"Batch resolved input={raw!r} canonical={resolved!r} "
                    f"storage_type={descriptor.storage.identity_type} storage_root={descriptor.storage.canonical_root!r}",
                )
            except SourceError as exc:
                detail = exc.key
                if exc.values:
                    detail += f" {exc.values!r}"
                if exc.key == "source.add.folder_not_found":
                    unavailable.append(SourceBatchIssue(raw, detail))
                else:
                    invalid.append(SourceBatchIssue(raw, detail))
            except FileNotFoundError as exc:
                unavailable.append(SourceBatchIssue(raw, str(exc)))
            except Exception as exc:
                invalid.append(SourceBatchIssue(raw, str(exc)))
                self.logger.warning("SOURCE", f"Batch source resolution failed input={raw!r}: {exc}")

        # Exact duplicates after canonical resolution: first input wins.
        unique: list[tuple[int, str, SourceDescriptor]] = []
        duplicate_inputs: list[tuple[str, str]] = []
        for item in resolved_descriptors:
            _, raw, descriptor = item
            existing = next((entry for entry in unique if self._paths_equal(entry[2].canonical_path, descriptor.canonical_path)), None)
            if existing is not None:
                duplicate_inputs.append((raw, existing[2].canonical_path))
                continue
            unique.append(item)

        # If selected sources overlap each other, retain only the broadest required roots.
        selected_covered: list[tuple[str, str]] = []
        final_selected: list[tuple[int, str, SourceDescriptor]] = []
        rank_by_path: dict[str, int] = {}
        for item in unique:
            index, _, descriptor = item
            parents = [
                other for other in unique
                if other is not item and self._is_ancestor(other[2].canonical_path, descriptor.canonical_path)
            ]
            if parents:
                # Point every covered selection at the broadest selected ancestor that
                # will actually survive in the final configuration. This keeps both
                # the overlap explanation and the retained ordering deterministic.
                parent = min(parents, key=lambda entry: len(entry[2].canonical_path))
                selected_covered.append((descriptor.canonical_path, parent[2].canonical_path))
                parent_key = self.platform.path_key(parent[2].canonical_path)
                rank_by_path[parent_key] = min(rank_by_path.get(parent_key, parent[0]), index)
                continue
            final_selected.append(item)
            rank_by_path.setdefault(self.platform.path_key(descriptor.canonical_path), index)
        final_selected.sort(key=lambda item: rank_by_path[self.platform.path_key(item[2].canonical_path)])

        # Group network inputs that resolved to the same canonical UNC storage root.
        group_map: dict[str, list[SourceResolution]] = {}
        root_display: dict[str, str] = {}
        for item in resolutions:
            if item.storage_type != "UNC":
                continue
            key = self.platform.path_key(item.storage_root)
            group_map.setdefault(key, []).append(item)
            root_display[key] = item.storage_root
        same_storage_groups = [
            (root_display[key], tuple(items))
            for key, items in group_map.items()
            if len(items) > 1
        ]

        already_configured: list[str] = []
        already_covered: list[tuple[str, str]] = []
        replacements: list[SourceReplacement] = []
        actionable: list[tuple[int, str]] = []

        if mode == "ADD":
            for index, _, descriptor in final_selected:
                path = descriptor.canonical_path
                exact = next((old for old in previous if self._paths_equal(old, path)), None)
                if exact is not None:
                    already_configured.append(path)
                    continue
                parent = next((old for old in previous if self._is_ancestor(old, path)), None)
                if parent is not None:
                    already_covered.append((path, parent))
                    continue
                children = tuple(old for old in previous if self._is_ancestor(path, old))
                if children:
                    replacements.append(SourceReplacement(path, children))
                actionable.append((index, path))

            final_sources = list(previous)
            for _, path in actionable:
                replacement = next((item for item in replacements if self._paths_equal(item.new_path, path)), None)
                if replacement is not None:
                    indexes = [i for i, old in enumerate(final_sources) if any(self._paths_equal(old, child) for child in replacement.replaced_paths)]
                    insert_at = min(indexes) if indexes else len(final_sources)
                    final_sources = [
                        old for old in final_sources
                        if not any(self._paths_equal(old, child) for child in replacement.replaced_paths)
                    ]
                    if not any(self._paths_equal(old, path) for old in final_sources):
                        final_sources.insert(min(insert_at, len(final_sources)), path)
                elif not any(self._paths_equal(old, path) for old in final_sources):
                    final_sources.append(path)
        else:
            # Import/replace only replaces the configured list when at least one
            # source resolved successfully. An empty file or a batch containing
            # only invalid/unavailable inputs must never wipe the configuration.
            final_sources = (
                [descriptor.canonical_path for _, _, descriptor in final_selected]
                if final_selected
                else list(previous)
            )

        plan = SourceBatchPlan(
            mode=mode,
            requested=requested,
            previous_sources=previous,
            resolutions=resolutions,
            same_storage_groups=same_storage_groups,
            unavailable=unavailable,
            invalid=invalid,
            duplicate_inputs=duplicate_inputs,
            selected_covered=selected_covered,
            already_configured=already_configured,
            already_covered=already_covered,
            replacements=replacements,
            final_sources=final_sources,
        )
        canonical_storage_count = len({
            (item.storage_type, self.platform.path_key(item.storage_root))
            for item in resolutions
        })
        self.logger.info(
            "SOURCE",
            f"Batch planning finished mode={mode} inputs={len(requested)} resolved={len(resolutions)} "
            f"canonical_storages={canonical_storage_count} final={len(final_sources)} "
            f"replacements={len(replacements)} selected_covered={len(selected_covered)} "
            f"already_configured={len(already_configured)} already_covered={len(already_covered)} "
            f"duplicates={len(duplicate_inputs)} unavailable={len(unavailable)} invalid={len(invalid)}",
        )
        return plan

    def apply_batch_plan(self, plan: SourceBatchPlan) -> SourceSyncResult:
        if self.config is None:
            raise RuntimeError("Source configuration manager is unavailable")
        self.config.set_sources(plan.final_sources)
        return self.sync_config_sources()

    def normalize_config_sources_for_startup(self) -> StartupSourceNormalizationResult:
        """Normalize configured sources before the authoritative config-to-DB sync.

        Successfully resolved paths are canonicalized, duplicates are removed, and
        resolved child paths are collapsed beneath the broadest resolved parent.
        Unavailable/unresolvable paths are retained exactly as configured (apart from
        surrounding whitespace already permitted by schema validation) so temporary
        offline state can never erase configuration.
        """
        if self.config is None:
            return StartupSourceNormalizationResult(0, 0, 0, 0, 0, 0, 0, False)

        original = list(self.config.sources)
        entries: list[dict[str, Any]] = []
        resolved_count = 0
        canonicalized_count = 0
        unresolved_count = 0

        self.logger.info("SOURCE", f"Startup source normalization started configured={len(original)}")
        for index, configured_path in enumerate(original):
            retained = configured_path.strip()
            resolved = False
            try:
                descriptor = self.resolve_descriptor(configured_path)
                descriptor = self._canonicalize_against_existing_storage(descriptor)
                retained = descriptor.canonical_path
                resolved = True
                resolved_count += 1
                if retained != configured_path:
                    canonicalized_count += 1
                    self.logger.debug(
                        "SOURCE",
                        f"Startup normalization canonicalized input={configured_path!r} canonical={retained!r}",
                    )
                else:
                    self.logger.debug("SOURCE", f"Startup normalization resolved path={retained!r}")
            except Exception as exc:
                unresolved_count += 1
                self.logger.debug(
                    "SOURCE",
                    f"Startup normalization unresolved; retained path={configured_path!r} error={exc}",
                )
            entries.append({
                "index": index,
                "input": configured_path,
                "path": retained,
                "resolved": resolved,
            })

        # Exact aliases/duplicates collapse deterministically: the first retained
        # occurrence wins. This is safe even for offline sources because equality
        # is based on path spelling semantics, not filesystem availability.
        unique: list[dict[str, Any]] = []
        duplicates_removed = 0
        for entry in entries:
            duplicate = next((old for old in unique if self._paths_equal(old["path"], entry["path"])), None)
            if duplicate is not None:
                duplicates_removed += 1
                self.logger.debug(
                    "SOURCE",
                    f"Startup normalization duplicate removed path={entry['path']!r} kept={duplicate['path']!r}",
                )
                continue
            unique.append(entry)

        # Only resolved paths participate in hierarchy collapse. An unresolved
        # source is never deleted merely because it is currently offline.
        rank_by_id: dict[int, int] = {id(entry): int(entry["index"]) for entry in unique}
        covered_ids: set[int] = set()
        covered_removed = 0
        for entry in unique:
            if not entry["resolved"]:
                continue
            parents = [
                candidate for candidate in unique
                if candidate is not entry
                and candidate["resolved"]
                and self._is_ancestor(str(candidate["path"]), str(entry["path"]))
            ]
            if not parents:
                continue
            parent = min(parents, key=lambda item: len(str(item["path"])))
            covered_ids.add(id(entry))
            covered_removed += 1
            rank_by_id[id(parent)] = min(rank_by_id[id(parent)], int(entry["index"]))
            self.logger.debug(
                "SOURCE",
                f"Startup normalization covered source removed path={entry['path']!r} parent={parent['path']!r}",
            )

        survivors = [entry for entry in unique if id(entry) not in covered_ids]
        survivors.sort(key=lambda entry: rank_by_id[id(entry)])
        normalized = [str(entry["path"]) for entry in survivors]
        changed = normalized != original
        if changed:
            self.config.set_sources(normalized)
            self.logger.info(
                "SOURCE",
                f"Startup source normalization saved configured={len(original)} final={len(normalized)}",
            )

        result = StartupSourceNormalizationResult(
            before=len(original),
            after=len(normalized),
            resolved=resolved_count,
            canonicalized=canonicalized_count,
            duplicates_removed=duplicates_removed,
            covered_removed=covered_removed,
            unresolved=unresolved_count,
            changed=changed,
        )
        self.logger.info(
            "SOURCE",
            f"Startup source normalization finished before={result.before} after={result.after} "
            f"resolved={result.resolved} canonicalized={result.canonicalized} "
            f"duplicates_removed={result.duplicates_removed} covered_removed={result.covered_removed} "
            f"unresolved={result.unresolved} changed={result.changed}",
        )
        return result

    def sync_config_sources(self) -> SourceSyncResult:
        if self.config is None:
            return SourceSyncResult(0, 0, 0, 0, 0, 0)

        desired = list(self.config.sources)
        self.logger.info("SOURCE", f"Config sync started configured_paths={len(desired)}")

        # A successfully resolved network source is always persisted in config as
        # canonical UNC. Local source spelling is left as configured. Unavailable
        # paths stay untouched so an offline source is never silently removed.
        normalized_desired: list[str] = []
        config_changed = False
        for configured_path in desired:
            normalized_path = configured_path
            try:
                descriptor = self.resolve_descriptor(configured_path)
                descriptor = self._canonicalize_against_existing_storage(descriptor)
                if descriptor.storage.identity_type == "UNC":
                    normalized_path = descriptor.canonical_path
            except Exception:
                pass
            if not self._paths_equal(configured_path, normalized_path) or configured_path != normalized_path:
                self.logger.info(
                    "SOURCE",
                    f"Config network path normalized input={configured_path!r} canonical={normalized_path!r}",
                )
                config_changed = True
            if not any(self._paths_equal(normalized_path, existing) for existing in normalized_desired):
                normalized_desired.append(normalized_path)
            else:
                config_changed = True
                self.logger.info("SOURCE", f"Config duplicate source removed path={normalized_path!r}")

        if config_changed or normalized_desired != desired:
            self.config.set_sources(normalized_desired)
        desired = normalized_desired
        active = self.db.list_sources(active_only=True)

        # Config is authoritative. Active DB sources absent from config become inactive,
        # but all catalog records are retained for future reconciliation/reactivation.
        for row in active:
            path = str(row["canonical_path"])
            if not any(self._paths_equal(path, wanted) for wanted in desired):
                with self.conn:
                    self.conn.execute(
                        "UPDATE sources SET status='REMOVED', superseded_by_source_id=NULL WHERE source_id=?",
                        (int(row["source_id"]),),
                    )
                self.logger.info(
                    "SOURCE",
                    f"Config sync deactivated source_id={int(row['source_id'])} path={path!r}",
                )

        matched = 0
        created = 0
        reactivated = 0
        unavailable = 0
        invalid = 0
        for path in desired:
            try:
                _, result_key, _ = self.add_source(path, interactive=False)
                if result_key == "source.add.success":
                    created += 1
                elif result_key == "source.add.reactivated":
                    reactivated += 1
                else:
                    matched += 1
            except SourceError as exc:
                if exc.key == "source.add.folder_not_found":
                    unavailable += 1
                    self.logger.warning("SOURCE", f"Configured source unavailable path={path!r}")
                else:
                    invalid += 1
                    self.logger.warning("SOURCE", f"Configured source invalid path={path!r} key={exc.key}")
            except Exception as exc:
                invalid += 1
                self.logger.warning("SOURCE", f"Configured source sync failed path={path!r}: {exc}")

        result = SourceSyncResult(len(desired), matched, created, reactivated, unavailable, invalid)
        self.logger.info(
            "SOURCE",
            f"Config sync finished configured={result.configured} matched={result.matched} created={result.created} "
            f"reactivated={result.reactivated} unavailable={result.unavailable} invalid={result.invalid}",
        )
        return result

    def configured_source_rows(self) -> list[sqlite3.Row]:
        if self.config is None:
            return self.db.list_sources(active_only=True)
        rows = self.db.list_sources(active_only=True)
        ordered: list[sqlite3.Row] = []
        for configured in self.config.sources:
            row = next((candidate for candidate in rows if self._paths_equal(str(candidate["canonical_path"]), configured)), None)
            if row is not None:
                ordered.append(row)
        return ordered

    def _find_or_create_storage(self, desc: StorageDescriptor, source_relative_path: str = "") -> sqlite3.Row:
        existing = list(self.conn.execute("SELECT * FROM storages ORDER BY storage_id").fetchall())
        match_result = self.platform.match_storage(
            desc, source_relative_path, existing, self.logger
        )
        for notice in match_result.notices:
            self._emit_notice(notice)
        if match_result.match is not None:
            matched = next(
                (row for row in existing if int(row["storage_id"]) == match_result.match.storage_id),
                None,
            )
            if matched is not None:
                return matched

        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO storages(identity_type,identity_key,canonical_root,volume_guid,volume_serial,filesystem,volume_label,
                                     remote_volume_serial,remote_file_id,created_at_utc)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    desc.identity_type, desc.identity_key, desc.canonical_root, desc.volume_guid,
                    desc.volume_serial, desc.filesystem, desc.volume_label,
                    desc.remote_volume_serial, desc.remote_file_id, now,
                ),
            )
        created = self.conn.execute("SELECT * FROM storages WHERE storage_id=?", (cur.lastrowid,)).fetchone()
        assert created is not None
        self.logger.info(
            "SOURCE",
            f"Source identity created method={desc.identity_type} storage_id={int(created['storage_id'])} "
            f"canonical_root={created['canonical_root']!r}",
        )
        return created

    def _canonicalize_against_storage(self, descriptor: SourceDescriptor, storage_row: sqlite3.Row) -> SourceDescriptor:
        return self.platform.canonicalize_source(descriptor, storage_row)

    def _add_alias(self, source_id: int, alias_path: str, alias_type: str, verified: int) -> None:
        alias_path = self.platform.normalize_path(strip_surrounding_quotes(alias_path))
        alias_key = self.platform.path_key(alias_path)
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO source_aliases(source_id,alias_path,alias_key,alias_type,verified,created_at_utc)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(alias_key) DO NOTHING
                    """,
                    (source_id, alias_path, alias_key, alias_type, int(bool(verified)), utc_now_iso()),
                )
        except Exception as exc:
            self.logger.warning("SOURCE", f"Could not store source alias {alias_path}: {exc}")

    def _confirm_covering_children(
        self,
        descriptor: SourceDescriptor,
        covered_children: list[sqlite3.Row],
        interactive: bool,
    ) -> bool:
        if not covered_children:
            return True
        if not interactive or self.overlap_confirm_callback is None:
            return False
        return bool(self.overlap_confirm_callback(descriptor, covered_children))

    def _reassign_files_under_root(self, source_id: int, storage_id: int, source_root_relative_path: str) -> int:
        rows = list(self.conn.execute(
            "SELECT file_id,storage_relative_path FROM media_files WHERE storage_id=?",
            (storage_id,),
        ).fetchall())
        ids: list[int] = []
        root_key = self.platform.relative_key(source_root_relative_path)
        for row in rows:
            child = str(row["storage_relative_path"] or "")
            if self.platform.relative_key(child) == root_key or self.platform.is_path_ancestor(source_root_relative_path, child):
                ids.append(int(row["file_id"]))
        if not ids:
            return 0
        with self.conn:
            self.conn.executemany("UPDATE media_files SET source_id=? WHERE file_id=?", [(source_id, fid) for fid in ids])
        return len(ids)

    def _add_source_impl(self, raw_path: str, *, interactive: bool = True) -> tuple[bool, str, dict[str, Any]]:
        descriptor = self.resolve_descriptor(raw_path)
        storage_row = self._find_or_create_storage(descriptor.storage, descriptor.source_root_relative_path)
        descriptor = self._canonicalize_against_storage(descriptor, storage_row)
        storage_id = int(storage_row["storage_id"])

        same = self.conn.execute(
            "SELECT * FROM sources WHERE storage_id=? AND source_root_key=?",
            (storage_id, descriptor.source_root_key),
        ).fetchone()

        active_same_storage = list(self.conn.execute(
            "SELECT * FROM sources WHERE storage_id=? AND status='ACTIVE' ORDER BY source_id",
            (storage_id,),
        ).fetchall())
        active_others = [row for row in active_same_storage if same is None or row["source_id"] != same["source_id"]]

        covering_parent = next(
            (row for row in active_others if self.platform.is_path_ancestor(row["source_root_relative_path"], descriptor.source_root_relative_path)),
            None,
        )
        if covering_parent:
            return False, "source.add.already_covered", {"path": covering_parent["display_path"]}

        covered_children = [
            row for row in active_others
            if self.platform.is_path_ancestor(descriptor.source_root_relative_path, row["source_root_relative_path"])
        ]

        for row in self.conn.execute("SELECT * FROM sources WHERE status='ACTIVE' ORDER BY source_id"):
            if same is not None and row["source_id"] == same["source_id"]:
                continue
            if int(row["storage_id"]) == storage_id:
                continue
            existing_path = str(row["canonical_path"])
            if self.platform.paths_equal(descriptor.canonical_path, existing_path):
                return False, "source.add.already_configured_as", {"path": row["display_path"]}
            if self.platform.is_path_ancestor(existing_path, descriptor.canonical_path):
                return False, "source.add.already_covered", {"path": row["display_path"]}
            if self.platform.is_path_ancestor(descriptor.canonical_path, existing_path):
                return False, "source.add.overlap_unverified", {"path": row["display_path"]}

        if covered_children and not self._confirm_covering_children(descriptor, covered_children, interactive):
            return False, "source.add.cancelled", {}

        if same:
            source_id = int(same["source_id"])
            if same["status"] != "ACTIVE":
                with self.conn:
                    self.conn.execute(
                        """
                        UPDATE sources SET status='ACTIVE', superseded_by_source_id=NULL,
                                           canonical_path=?, user_path=?, display_path=?
                        WHERE source_id=?
                        """,
                        (descriptor.canonical_path, descriptor.user_path, descriptor.display_path, source_id),
                    )
                    for child in covered_children:
                        self.conn.execute(
                            "UPDATE sources SET status='SUPERSEDED', superseded_by_source_id=? WHERE source_id=?",
                            (source_id, child["source_id"]),
                        )
                self._reassign_files_under_root(source_id, storage_id, descriptor.source_root_relative_path)
                result_key = "source.add.reactivated"
                result_values = {"path": descriptor.display_path}
            else:
                result_key = "source.add.already_configured"
                result_values = {"path": same["display_path"]}
            self._add_alias(source_id, descriptor.user_path, "USER_INPUT", 0)
            self._add_alias(source_id, descriptor.canonical_path, "CANONICAL", 1)
            for alias_path, alias_type, verified in descriptor.aliases:
                self._add_alias(source_id, alias_path, alias_type, verified)
            return True, result_key, result_values

        now = utc_now_iso()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO sources(storage_id,source_root_relative_path,source_root_key,canonical_path,user_path,display_path,status,created_at_utc)
                VALUES(?,?,?,?,?,?,'ACTIVE',?)
                """,
                (
                    storage_id, descriptor.source_root_relative_path, descriptor.source_root_key,
                    descriptor.canonical_path, descriptor.user_path, descriptor.display_path, now,
                ),
            )
            source_id = int(cur.lastrowid)
            for child in covered_children:
                self.conn.execute(
                    "UPDATE sources SET status='SUPERSEDED', superseded_by_source_id=? WHERE source_id=?",
                    (source_id, child["source_id"]),
                )
                self.conn.execute(
                    "UPDATE media_files SET source_id=? WHERE source_id=?",
                    (source_id, child["source_id"]),
                )

        self._add_alias(source_id, descriptor.user_path, "USER_INPUT", 0)
        self._add_alias(source_id, descriptor.canonical_path, "CANONICAL", 1)
        for alias_path, alias_type, verified in descriptor.aliases:
            self._add_alias(source_id, alias_path, alias_type, verified)
        self.logger.info("SOURCE", f"Added source ID={source_id}: {descriptor.canonical_path}")
        return True, "source.add.success", {"path": descriptor.display_path}

    def add_source(self, raw_path: str, *, interactive: bool = True) -> tuple[bool, str, dict[str, Any]]:
        started = time.perf_counter()
        try:
            return self._add_source_impl(raw_path, interactive=interactive)
        finally:
            self.logger.info(
                "SOURCE",
                f"Add Source finished path={strip_surrounding_quotes(raw_path)!r} elapsed={format_elapsed(time.perf_counter() - started)}",
            )

    def remove_config_path(self, path: str) -> bool:
        if self.config is None:
            return False
        current = list(self.config.sources)
        kept = [value for value in current if not self._paths_equal(value, path)]
        if len(kept) == len(current):
            return False
        self.config.set_sources(kept)
        rows = self.db.list_sources(active_only=True)
        for row in rows:
            if self._paths_equal(str(row["canonical_path"]), path):
                with self.conn:
                    self.conn.execute(
                        "UPDATE sources SET status='REMOVED', superseded_by_source_id=NULL WHERE source_id=?",
                        (int(row["source_id"]),),
                    )
                self.logger.info(
                    "SOURCE",
                    f"Removed source detail source_id={int(row['source_id'])} path={path!r}",
                )
        self.logger.info("SOURCE", f"Removed configured source path={path!r}")
        return True

    def remove_source(self, source_id: int) -> None:
        started = time.perf_counter()
        row = self.db.get_source(source_id)
        path = str(row["canonical_path"]) if row is not None else ""
        with self.conn:
            self.conn.execute(
                "UPDATE sources SET status='REMOVED', superseded_by_source_id=NULL WHERE source_id=?",
                (source_id,),
            )
        if self.config is not None and path:
            self.config.set_sources([value for value in self.config.sources if not self._paths_equal(value, path)])
        self.logger.info(
            "SOURCE",
            f"Removed source from active configuration: source_id={source_id} path={path!r} "
            f"elapsed={format_elapsed(time.perf_counter() - started)}",
        )

    def remove_all_sources(self) -> int:
        started = time.perf_counter()
        active = self.db.list_sources(active_only=True)
        with self.conn:
            cur = self.conn.execute("UPDATE sources SET status='REMOVED', superseded_by_source_id=NULL WHERE status='ACTIVE'")
        if self.config is not None:
            self.config.set_sources([])
        for row in active:
            self.logger.info(
                "SOURCE",
                f"Removed source detail source_id={int(row['source_id'])} path={str(row['canonical_path'])!r}",
            )
        self.logger.info(
            "SOURCE",
            f"Removed {cur.rowcount} source(s) from active configuration elapsed={format_elapsed(time.perf_counter() - started)}",
        )
        return int(cur.rowcount)

    def _persist_canonical_path_update(self, source: sqlite3.Row, canonical_path: str) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE sources SET canonical_path=?, display_path=? WHERE source_id=?",
                    (canonical_path, canonical_path, source["source_id"]),
                )
            self.logger.info(
                "SOURCE",
                f"Updated current mount path for source_id={source['source_id']}: {canonical_path}",
            )
        except Exception as exc:
            self.logger.warning(
                "SOURCE",
                f"Could not update current mount path for source_id={source['source_id']}: {exc}",
            )

    def resolve_access_path(self, source: sqlite3.Row) -> Optional[str]:
        aliases = self.db.get_source_aliases(int(source["source_id"]), verified_only=True)
        result = self.platform.resolve_source_access_path(source, aliases)
        if result.path is None:
            return None
        if result.canonical_path is not None:
            self._persist_canonical_path_update(source, result.canonical_path)
        return result.path

    def normalize_import_values(self, lines: Iterable[str]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for line in lines:
            value = strip_surrounding_quotes(line)
            if not value:
                continue
            key = self.platform.path_key(value)
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        return values

    def import_replace(self, values: Iterable[str], *, interactive: bool = True) -> SourceImportResult:
        requested_values = list(values)
        started = time.perf_counter()
        removed = self.remove_all_sources()
        added = 0
        skipped = 0
        invalid = 0
        for value in requested_values:
            try:
                ok, result_key, result_values = self.add_source(value, interactive=interactive)
                if ok:
                    if result_key == "source.add.already_configured":
                        skipped += 1
                    else:
                        added += 1
                else:
                    skipped += 1
                self.logger.info("SOURCE", f"Import source result key={result_key} values={result_values!r}")
            except Exception as exc:
                invalid += 1
                self.logger.warning("SOURCE", f"Import source failed {value!r}: {exc}")
        elapsed = time.perf_counter() - started
        self.logger.info(
            "SOURCE",
            f"Import Sources finished mode=REPLACE removed_active={removed} requested={len(requested_values)} "
            f"imported_or_reactivated={added} skipped={skipped} invalid={invalid} elapsed={format_elapsed(elapsed)}",
        )
        return SourceImportResult(removed, len(requested_values), added, skipped, invalid, elapsed)

    def export_paths(self) -> list[str]:
        lines: list[str] = []
        for source in self.db.list_sources(active_only=True):
            aliases = self.db.get_source_aliases(int(source["source_id"]), verified_only=True)
            result = self.platform.source_export_path(source, aliases)
            if result.canonical_path is not None:
                self._persist_canonical_path_update(source, result.canonical_path)
            lines.append(result.path)
        return lines
