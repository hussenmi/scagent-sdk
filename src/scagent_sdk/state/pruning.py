"""Explicit, recoverable retention policy for scientific-session artifacts.

Nothing is eligible merely because it is old or unreachable. A matrix on an abandoned branch is
eligible only after that branch is explicitly rejected; an artifact inside a live execution is
eligible only after it is explicitly marked superseded. Removal is two phase: an event authorizes
an exact immutable plan, files are atomically renamed into session-local trash, and a second event
records completion. Purge is a separate confirmed operation after a grace period.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from scagent_sdk.contracts.events import utc_now
from scagent_sdk.contracts.state import SessionState
from scagent_sdk.errors import RetentionError
from scagent_sdk.state.lineage import active_head, ancestry
from scagent_sdk.state.retention import ByteAccounting, account, topology_defects
from scagent_sdk.state.store import SessionStore

RETENTION_SCHEMA_VERSION = 1
BRANCH_DISPOSITIONS = frozenset({"retained", "pinned", "rejected"})
ARTIFACT_ROLES = frozenset({"required", "published", "superseded"})
INCOMPLETE_OPERATION_STATUSES = frozenset({"staging", "restoring", "purging"})
UNAVAILABLE_OPERATION_STATUSES = frozenset({"staging", "trashed", "restoring", "purging", "purged"})
OPERATION_STATUSES = UNAVAILABLE_OPERATION_STATUSES | {"restored"}
DEFAULT_GRACE_HOURS = 7 * 24
_MATRIX_SUFFIXES = frozenset({".h5ad", ".h5", ".hdf5", ".loom"})
_MATRIX_MEDIA_TYPES = frozenset(
    {
        "application/x-hdf5",
        "application/x-h5ad",
        "application/x-anndata",
        "application/x-zarr",
    }
)


def empty_retention_state() -> dict[str, Any]:
    return {
        "schema_version": RETENTION_SCHEMA_VERSION,
        "branches": {},
        "artifact_roles": {},
        "operations": {},
    }


def normalized_retention(value: Any) -> dict[str, Any]:
    """Return a detached, complete retention registry or fail on an unknown schema."""

    if not isinstance(value, Mapping) or not value:
        return empty_retention_state()
    version = value.get("schema_version", RETENTION_SCHEMA_VERSION)
    if version != RETENTION_SCHEMA_VERSION:
        raise RetentionError(f"unsupported retention schema version: {version}")
    result = empty_retention_state()
    for key in ("branches", "artifact_roles", "operations"):
        item = value.get(key, {})
        if not isinstance(item, Mapping):
            raise RetentionError(f"state.retention.{key} must be an object")
        result[key] = deepcopy(dict(item))
    for execution_id, record in result["branches"].items():
        if (
            not isinstance(execution_id, str)
            or not isinstance(record, Mapping)
            or record.get("disposition") not in BRANCH_DISPOSITIONS
            or not isinstance(record.get("reason"), str)
            or not isinstance(record.get("recorded_at"), str)
        ):
            raise RetentionError(
                f"state.retention.branches contains an invalid record for {execution_id!r}"
            )
    for execution_id, by_path in result["artifact_roles"].items():
        if not isinstance(execution_id, str) or not isinstance(by_path, Mapping):
            raise RetentionError(
                f"state.retention.artifact_roles contains an invalid execution {execution_id!r}"
            )
        for relative_path, record in by_path.items():
            if (
                not isinstance(relative_path, str)
                or not isinstance(record, Mapping)
                or record.get("role") not in ARTIFACT_ROLES
                or not isinstance(record.get("reason"), str)
                or not isinstance(record.get("recorded_at"), str)
            ):
                raise RetentionError(
                    "state.retention.artifact_roles contains an invalid record for "
                    f"{execution_id!r}/{relative_path!r}"
                )
    for operation_id, operation in result["operations"].items():
        if (
            not isinstance(operation_id, str)
            or not isinstance(operation, Mapping)
            or operation.get("operation_id") != operation_id
            or operation.get("status") not in OPERATION_STATUSES
            or not isinstance(operation.get("entries"), list)
        ):
            raise RetentionError(
                f"state.retention.operations contains an invalid record for {operation_id!r}"
            )
    return result


def _state_value(state: SessionState | Mapping[str, Any], key: str, default: Any) -> Any:
    return getattr(state, key) if isinstance(state, SessionState) else state.get(key, default)


def _nodes(state: SessionState | Mapping[str, Any]) -> Mapping[str, Any]:
    lineage = _state_value(state, "lineage", {})
    nodes = lineage.get("nodes") if isinstance(lineage, Mapping) else {}
    return nodes if isinstance(nodes, Mapping) else {}


def _artifacts(state: SessionState | Mapping[str, Any]) -> Mapping[str, Any]:
    value = _state_value(state, "artifacts", {})
    return value if isinstance(value, Mapping) else {}


def _retention(state: SessionState | Mapping[str, Any]) -> dict[str, Any]:
    return normalized_retention(_state_value(state, "retention", {}))


def _revision(state: SessionState | Mapping[str, Any]) -> int:
    value = _state_value(state, "revision", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _lineage(state: SessionState | Mapping[str, Any]) -> Mapping[str, Any]:
    value = _state_value(state, "lineage", {})
    return value if isinstance(value, Mapping) else {}


def _safe_relative(value: str, *, name: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise RetentionError(f"{name} must be a normalized session-relative path")
    return path


def _session_path(session_dir: Path, relative: str) -> Path:
    part = _safe_relative(relative, name="retention path")
    root = session_dir.resolve()
    path = root / Path(*part.parts)
    current = root
    for component in part.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise RetentionError(f"retention path crosses a symlinked directory: {relative}")
        if current.exists() and not current.is_dir():
            raise RetentionError(f"retention path parent is not a directory: {relative}")
    # Resolve the parent as a second containment check, but deliberately return the lexical final
    # path so callers can detect and refuse a final-component symlink.
    resolved_parent = path.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise RetentionError(f"retention path escapes the session: {relative}") from exc
    return path


def _artifact_relative(
    state: SessionState | Mapping[str, Any],
    execution_id: str,
    relative_path: str,
) -> str:
    if (
        not execution_id
        or execution_id in {".", ".."}
        or "/" in execution_id
        or "\\" in execution_id
    ):
        raise RetentionError(f"unsafe execution id in artifact registry: {execution_id!r}")
    item = _safe_relative(relative_path, name="artifact relative_path")
    record = _artifacts(state).get(execution_id)
    recorded_root = record.get("path") if isinstance(record, Mapping) else None
    if isinstance(recorded_root, str) and recorded_root:
        root = _safe_relative(recorded_root, name="artifact path")
        if root.parts[:2] != ("artifacts", "capabilities"):
            raise RetentionError(
                f"execution {execution_id} has an invalid artifact root: {recorded_root}"
            )
    else:
        root = PurePosixPath("artifacts", "capabilities", execution_id)
    return str(PurePosixPath(*root.parts, *item.parts))


def _recorded_files(
    state: SessionState | Mapping[str, Any], execution_id: str
) -> list[Mapping[str, Any]]:
    raw = _artifacts(state).get(execution_id)
    if not isinstance(raw, Mapping):
        raise RetentionError(f"execution {execution_id} has no committed artifact record")
    files = raw.get("files")
    if not isinstance(files, list):
        raise RetentionError(f"execution {execution_id} has no committed file list")
    return [item for item in files if isinstance(item, Mapping)]


def _recorded_file(
    state: SessionState | Mapping[str, Any], execution_id: str, relative_path: str
) -> Mapping[str, Any]:
    for item in _recorded_files(state, execution_id):
        if item.get("relative_path") == relative_path:
            return item
    raise RetentionError(
        f"{relative_path!r} is not a committed artifact of execution {execution_id}"
    )


def _is_matrix(item: Mapping[str, Any]) -> bool:
    relative = item.get("relative_path")
    if not isinstance(relative, str):
        return False
    media = str(item.get("media_type", "")).lower().split(";", 1)[0].strip()
    return media in _MATRIX_MEDIA_TYPES or Path(relative).suffix.lower() in _MATRIX_SUFFIXES


def _head_relative(state: SessionState | Mapping[str, Any], execution_id: str) -> str | None:
    node = _nodes(state).get(execution_id)
    value = node.get("head_path") if isinstance(node, Mapping) else None
    return value if isinstance(value, str) else None


def _branch_disposition(retention: Mapping[str, Any], execution_id: str) -> str | None:
    branches = retention.get("branches")
    record = branches.get(execution_id) if isinstance(branches, Mapping) else None
    value = record.get("disposition") if isinstance(record, Mapping) else None
    return value if value in BRANCH_DISPOSITIONS else None


def _artifact_role(
    retention: Mapping[str, Any], execution_id: str, relative_path: str
) -> str | None:
    roles = retention.get("artifact_roles")
    by_execution = roles.get(execution_id) if isinstance(roles, Mapping) else None
    record = by_execution.get(relative_path) if isinstance(by_execution, Mapping) else None
    value = record.get("role") if isinstance(record, Mapping) else None
    return value if value in ARTIFACT_ROLES else None


def _leaf_nodes(nodes: Mapping[str, Any]) -> set[str]:
    parents = {
        str(node["parent_execution_id"])
        for node in nodes.values()
        if isinstance(node, Mapping)
        and isinstance(node.get("parent_execution_id"), str)
        and node["parent_execution_id"] in nodes
    }
    return {str(execution_id) for execution_id in nodes} - parents


def protected_nodes(
    state: SessionState | Mapping[str, Any],
    *,
    retention_override: Mapping[str, Any] | None = None,
) -> set[str]:
    """Nodes whose matrix artifacts must remain available.

    Unknown is protected: every non-rejected leaf is a live head until the user says otherwise.
    Retained and pinned nodes are additional live heads, and every live head protects its ancestry.
    """

    nodes = _nodes(state)
    lineage = _lineage(state)
    retention = (
        normalized_retention(retention_override)
        if retention_override is not None
        else _retention(state)
    )
    heads: set[str] = set()
    active = active_head(lineage)
    if active is not None:
        heads.add(active)
    for execution_id in _leaf_nodes(nodes):
        if _branch_disposition(retention, execution_id) != "rejected":
            heads.add(execution_id)
    for execution_id in nodes:
        if _branch_disposition(retention, str(execution_id)) in {"retained", "pinned"}:
            heads.add(str(execution_id))
    protected: set[str] = set()
    for execution_id in heads:
        protected.update(item for item in ancestry(lineage, execution_id) if item in nodes)
    return protected


def execution_unavailable_reason(retention_value: Any, execution_id: str) -> str | None:
    """Why a lineage node cannot currently be checked out or used as an input."""

    retention = normalized_retention(retention_value)
    operations = retention["operations"]
    for operation_id, operation in operations.items():
        if not isinstance(operation, Mapping):
            continue
        status = operation.get("status")
        if status not in UNAVAILABLE_OPERATION_STATUSES:
            continue
        entries = operation.get("entries")
        if not isinstance(entries, list):
            continue
        if any(
            isinstance(entry, Mapping)
            and entry.get("execution_id") == execution_id
            and entry.get("is_lineage_head") is True
            for entry in entries
        ):
            return f"retention operation {str(operation_id)[:8]} is {status}"
    return None


def _covered_artifacts(retention: Mapping[str, Any]) -> set[tuple[str, str]]:
    covered: set[tuple[str, str]] = set()
    for operation in retention["operations"].values():
        if not isinstance(operation, Mapping) or operation.get("status") == "restored":
            continue
        entries = operation.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            execution_id = entry.get("execution_id")
            relative_path = entry.get("relative_path")
            if isinstance(execution_id, str) and isinstance(relative_path, str):
                covered.add((execution_id, relative_path))
    return covered


def _present(path: Path) -> bool:
    """Existence that does not hide a broken symlink."""

    return path.exists() or path.is_symlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _verify_planned_file(path: Path, entry: Mapping[str, Any], *, check_link_count: bool) -> None:
    """Refuse a path replaced after planning or staging.

    Device+inode pins the actual filesystem object; size and mtime catch in-place mutation.
    Link count is rechecked before rename, where it controls the reclaim estimate. Purge does not
    require it because an already-authorized external hard-link change alters freed bytes, not
    which object is being unlinked, and a crash between two hard-linked purge entries legitimately
    changes the remaining entry's link count.
    """

    if path.is_symlink():
        raise RetentionError(f"planned artifact became a symlink: {path}")
    try:
        stat = path.stat()
    except OSError as exc:
        raise RetentionError(f"cannot revalidate planned artifact {path}: {exc}") from exc
    same_inode = entry.get("device") == stat.st_dev and entry.get("inode") == stat.st_ino
    same_metadata = (
        entry.get("size_bytes") == stat.st_size and entry.get("modified_ns") == stat.st_mtime_ns
    )
    same_links = not check_link_count or entry.get("link_count") == stat.st_nlink
    if same_inode and same_metadata and same_links:
        return
    # A complete session restored from backup legitimately has new device/inode values. Accept it
    # only by content identity; this is also the fail-closed fallback for any metadata drift.
    expected_digest = entry.get("content_sha256")
    try:
        actual_digest = _sha256_file(path)
    except OSError as exc:
        raise RetentionError(f"cannot hash planned artifact {path}: {exc}") from exc
    if not isinstance(expected_digest, str) or actual_digest != expected_digest:
        raise RetentionError(
            f"planned artifact changed before filesystem operation (content identity): {path}"
        )
    after = path.stat()
    if (
        after.st_dev != stat.st_dev
        or after.st_ino != stat.st_ino
        or after.st_size != stat.st_size
        or after.st_mtime_ns != stat.st_mtime_ns
    ):
        raise RetentionError(f"planned artifact changed while it was being hashed: {path}")


@dataclass(frozen=True)
class ArtifactTarget:
    execution_id: str
    relative_path: str
    session_relative_path: str
    reason: str
    role: str
    is_lineage_head: bool
    size_bytes: int
    device: int
    inode: int
    link_count: int
    modified_ns: int
    content_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "relative_path": self.relative_path,
            "session_relative_path": self.session_relative_path,
            "reason": self.reason,
            "role": self.role,
            "is_lineage_head": self.is_lineage_head,
            "size_bytes": self.size_bytes,
            "device": self.device,
            "inode": self.inode,
            "link_count": self.link_count,
            "modified_ns": self.modified_ns,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class PrunePlan:
    session_id: str
    state_revision: int
    grace_seconds: int
    plan_id: str | None
    targets: tuple[ArtifactTarget, ...]
    accounting: ByteAccounting
    protected_execution_ids: tuple[str, ...]
    blocking_errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return bool(self.targets) and not self.blocking_errors and self.plan_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state_revision": self.state_revision,
            "grace_seconds": self.grace_seconds,
            "plan_id": self.plan_id,
            "applicable": self.applicable,
            "targets": [item.to_dict() for item in self.targets],
            "accounting": self.accounting.to_dict(),
            "protected_execution_ids": list(self.protected_execution_ids),
            "blocking_errors": list(self.blocking_errors),
            "warnings": list(self.warnings),
        }


def _target(
    session_dir: Path,
    state: SessionState | Mapping[str, Any],
    execution_id: str,
    item: Mapping[str, Any],
    *,
    reason: str,
    role: str,
) -> ArtifactTarget:
    relative_path = item.get("relative_path")
    if not isinstance(relative_path, str):
        raise RetentionError(f"execution {execution_id} has a file without relative_path")
    session_relative = _artifact_relative(state, execution_id, relative_path)
    path = _session_path(session_dir, session_relative)
    if path.is_symlink():
        raise RetentionError(f"refusing to prune symlink artifact: {session_relative}")
    if path.is_dir():
        raise RetentionError(
            f"directory artifact {session_relative} requires a format-specific safe walker; "
            "Zarr pruning is not enabled by the H5AD retention implementation"
        )
    try:
        stat = path.stat()
    except OSError as exc:
        raise RetentionError(
            f"cannot inspect committed artifact {session_relative}: {exc}"
        ) from exc
    expected = item.get("size_bytes")
    if isinstance(expected, int) and not isinstance(expected, bool) and stat.st_size != expected:
        raise RetentionError(
            f"committed artifact {session_relative} changed size "
            f"({expected} recorded, {stat.st_size} on disk)"
        )
    try:
        content_sha256 = _sha256_file(path)
    except OSError as exc:
        raise RetentionError(f"cannot hash committed artifact {session_relative}: {exc}") from exc
    after = path.stat()
    if (
        after.st_dev != stat.st_dev
        or after.st_ino != stat.st_ino
        or after.st_size != stat.st_size
        or after.st_mtime_ns != stat.st_mtime_ns
        or after.st_nlink != stat.st_nlink
    ):
        raise RetentionError(
            f"committed artifact changed while the prune plan was hashing it: {session_relative}"
        )
    return ArtifactTarget(
        execution_id=execution_id,
        relative_path=relative_path,
        session_relative_path=session_relative,
        reason=reason,
        role=role,
        is_lineage_head=_head_relative(state, execution_id) == session_relative,
        size_bytes=stat.st_size,
        device=stat.st_dev,
        inode=stat.st_ino,
        link_count=stat.st_nlink,
        modified_ns=stat.st_mtime_ns,
        content_sha256=content_sha256,
    )


def build_prune_plan(
    session_dir: Path,
    *,
    session_id: str,
    state: SessionState | Mapping[str, Any],
    grace_seconds: int = DEFAULT_GRACE_HOURS * 3600,
) -> PrunePlan:
    """Build an immutable, content-sensitive plan without changing session or files."""

    if grace_seconds < 0:
        raise RetentionError("grace period cannot be negative")
    retention = _retention(state)
    lineage = _lineage(state)
    nodes = _nodes(state)
    defects = topology_defects(lineage)
    errors = list(defects)
    incomplete = sorted(
        str(operation_id)
        for operation_id, operation in retention["operations"].items()
        if isinstance(operation, Mapping)
        and operation.get("status") in INCOMPLETE_OPERATION_STATUSES
    )
    if incomplete:
        errors.append(
            "incomplete retention operation(s) must be recovered before a new plan: "
            + ", ".join(item[:8] for item in incomplete)
        )
    protected = protected_nodes(state)
    covered = _covered_artifacts(retention)
    selected: dict[tuple[str, str], tuple[Mapping[str, Any], str, str]] = {}

    if nodes and defects:
        errors.insert(0, "lineage topology is unreliable; no pruning plan may be applied")
    else:
        for execution_id in sorted(nodes):
            if (
                _branch_disposition(retention, str(execution_id)) != "rejected"
                or execution_id in protected
            ):
                continue
            for item in _recorded_files(state, str(execution_id)):
                relative_path = item.get("relative_path")
                if not isinstance(relative_path, str) or not _is_matrix(item):
                    continue
                role = _artifact_role(retention, str(execution_id), relative_path)
                if role in {"required", "published"}:
                    continue
                selected[(str(execution_id), relative_path)] = (
                    item,
                    "matrix belongs only to an explicitly rejected branch",
                    role or "rejected_branch_matrix",
                )

        for execution_id, by_path in retention["artifact_roles"].items():
            if not isinstance(execution_id, str) or not isinstance(by_path, Mapping):
                errors.append("artifact role registry contains a malformed execution record")
                continue
            for relative_path, role_record in by_path.items():
                role = role_record.get("role") if isinstance(role_record, Mapping) else None
                if not isinstance(relative_path, str) or role != "superseded":
                    continue
                try:
                    item = _recorded_file(state, execution_id, relative_path)
                except RetentionError as exc:
                    errors.append(str(exc))
                    continue
                session_relative = _artifact_relative(state, execution_id, relative_path)
                if (
                    execution_id in protected
                    and _head_relative(state, execution_id) == session_relative
                ):
                    errors.append(
                        f"active or retained lineage head cannot be superseded: {session_relative}"
                    )
                    continue
                selected[(execution_id, relative_path)] = (
                    item,
                    "artifact was explicitly classified as superseded",
                    "superseded",
                )

    targets: list[ArtifactTarget] = []
    for (execution_id, relative_path), (item, reason, role) in sorted(selected.items()):
        if (execution_id, relative_path) in covered:
            continue
        try:
            targets.append(
                _target(
                    session_dir,
                    state,
                    execution_id,
                    item,
                    reason=reason,
                    role=role,
                )
            )
        except RetentionError as exc:
            errors.append(str(exc))

    warnings: list[str] = []
    if not targets and not errors:
        warnings.append(
            "nothing is eligible: reject an abandoned branch or mark an exact artifact superseded"
        )
    accounting = account(
        [_session_path(session_dir, item.session_relative_path) for item in targets]
    )
    payload = {
        "session_id": session_id,
        "state_revision": _revision(state),
        "grace_seconds": grace_seconds,
        "targets": [item.to_dict() for item in targets],
    }
    plan_id = (
        "prune:v1:sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if targets and not errors
        else None
    )
    return PrunePlan(
        session_id=session_id,
        state_revision=_revision(state),
        grace_seconds=grace_seconds,
        plan_id=plan_id,
        targets=tuple(targets),
        accounting=accounting,
        protected_execution_ids=tuple(sorted(protected)),
        blocking_errors=tuple(errors),
        warnings=tuple(warnings),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    _fsync_directory(source.parent)
    _fsync_directory(destination.parent)


def _unlink_path(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def _parse_time(value: Any, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise RetentionError(f"{name} is missing from the retention operation")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RetentionError(f"{name} is not an ISO timestamp") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class RetentionManager:
    """Mutating retention operations over one freshly opened ``SessionStore``."""

    def __init__(self, store: SessionStore):
        self.store = store
        self.session_dir = store.session_dir

    def _updated(self, state: SessionState) -> dict[str, Any]:
        return _retention(state)

    def set_branch_disposition(
        self, execution_id: str, disposition: str | None, *, reason: str
    ) -> dict[str, Any]:
        if disposition is not None and disposition not in BRANCH_DISPOSITIONS:
            raise RetentionError(f"unknown branch disposition: {disposition}")
        if not reason.strip():
            raise RetentionError("a reason is required for every retention classification")
        recorded_at = utc_now()

        def patch(state: SessionState) -> Mapping[str, Any]:
            if execution_id not in _nodes(state):
                raise RetentionError(f"unknown lineage execution: {execution_id}")
            retention = self._updated(state)
            unavailable = execution_unavailable_reason(retention, execution_id)
            if unavailable:
                raise RetentionError(
                    f"cannot reclassify {execution_id}: its matrix is unavailable because "
                    f"{unavailable}"
                )
            current = _branch_disposition(retention, execution_id)
            if current == "pinned" and disposition == "rejected":
                raise RetentionError(
                    "a pinned branch cannot become rejected directly; classify it retained first"
                )
            if disposition is None:
                retention["branches"].pop(execution_id, None)
            else:
                retention["branches"][execution_id] = {
                    "disposition": disposition,
                    "reason": reason.strip(),
                    "recorded_at": recorded_at,
                }
            if disposition == "rejected" and execution_id in protected_nodes(
                state, retention_override=retention
            ):
                raise RetentionError(
                    f"cannot reject protected version {execution_id}; it is active, retained, "
                    "pinned, or an ancestor of a branch not yet rejected"
                )
            return {"retention": retention}

        event = self.store.record(
            "retention.branch_classified",
            payload={
                "execution_id": execution_id,
                "disposition": disposition or "unclassified",
                "reason": reason.strip(),
            },
            state_patch_factory=patch,
            actor="user",
        )
        return event.to_dict()

    def set_artifact_role(
        self,
        execution_id: str,
        relative_path: str,
        role: str | None,
        *,
        reason: str,
    ) -> dict[str, Any]:
        if role is not None and role not in ARTIFACT_ROLES:
            raise RetentionError(f"unknown artifact role: {role}")
        if not reason.strip():
            raise RetentionError("a reason is required for every artifact classification")
        _safe_relative(relative_path, name="artifact relative_path")
        recorded_at = utc_now()

        def patch(state: SessionState) -> Mapping[str, Any]:
            _recorded_file(state, execution_id, relative_path)
            retention = self._updated(state)
            current = _artifact_role(retention, execution_id, relative_path)
            if current == "published" and role in {None, "superseded"}:
                raise RetentionError(
                    "a published artifact cannot be unclassified or superseded directly; "
                    "classify it required first"
                )
            for operation in retention["operations"].values():
                if not isinstance(operation, Mapping) or operation.get("status") == "restored":
                    continue
                entries = operation.get("entries")
                if isinstance(entries, list) and any(
                    isinstance(entry, Mapping)
                    and entry.get("execution_id") == execution_id
                    and entry.get("relative_path") == relative_path
                    for entry in entries
                ):
                    raise RetentionError(
                        "cannot reclassify an artifact covered by an active or completed "
                        "retention operation; restore it first"
                    )
            session_relative = _artifact_relative(state, execution_id, relative_path)
            if (
                role == "superseded"
                and execution_id in protected_nodes(state)
                and _head_relative(state, execution_id) == session_relative
            ):
                raise RetentionError(
                    "the matrix head of an active or retained branch cannot be superseded"
                )
            by_execution = retention["artifact_roles"].setdefault(execution_id, {})
            if not isinstance(by_execution, dict):
                raise RetentionError("artifact role registry is malformed")
            if role is None:
                by_execution.pop(relative_path, None)
                if not by_execution:
                    retention["artifact_roles"].pop(execution_id, None)
            else:
                by_execution[relative_path] = {
                    "role": role,
                    "reason": reason.strip(),
                    "recorded_at": recorded_at,
                }
            return {"retention": retention}

        event = self.store.record(
            "retention.artifact_classified",
            payload={
                "execution_id": execution_id,
                "relative_path": relative_path,
                "role": role or "unclassified",
                "reason": reason.strip(),
            },
            state_patch_factory=patch,
            actor="user",
        )
        return event.to_dict()

    def plan(self, *, grace_seconds: int) -> PrunePlan:
        return build_prune_plan(
            self.session_dir,
            session_id=self.store.session_id,
            state=self.store.state,
            grace_seconds=grace_seconds,
        )

    def apply(self, plan_id: str, *, grace_seconds: int) -> dict[str, Any]:
        plan = self.plan(grace_seconds=grace_seconds)
        if not plan.applicable or plan.plan_id != plan_id:
            raise RetentionError(
                "prune plan is absent, blocked, or stale; run the read-only plan again and "
                "apply its exact plan_id"
            )
        operation_id = str(uuid4())
        created_at = utc_now()
        entries = [
            item.to_dict()
            | {
                "original_path": item.session_relative_path,
                "trash_path": str(
                    PurePosixPath(".trash", "retention", operation_id, item.session_relative_path)
                ),
            }
            for item in plan.targets
        ]

        def patch(state: SessionState) -> Mapping[str, Any]:
            fresh = build_prune_plan(
                self.session_dir,
                session_id=self.store.session_id,
                state=state,
                grace_seconds=grace_seconds,
            )
            if fresh.plan_id != plan_id or not fresh.applicable:
                raise RetentionError(
                    "prune plan changed while applying; no authorization event was recorded"
                )
            retention = self._updated(state)
            retention["operations"][operation_id] = {
                "operation_id": operation_id,
                "plan_id": plan_id,
                "status": "staging",
                "created_at": created_at,
                "grace_seconds": grace_seconds,
                "purge_after": None,
                "entries": entries,
                "accounting": plan.accounting.to_dict(),
            }
            return {"retention": retention}

        self.store.record(
            "retention.prune_staged",
            payload={
                "operation_id": operation_id,
                "plan_id": plan_id,
                "grace_seconds": grace_seconds,
                "entries": entries,
                "accounting": plan.accounting.to_dict(),
            },
            state_patch_factory=patch,
            actor="user",
        )
        return self._complete_trash(operation_id)

    def _operation(self, operation_id: str) -> Mapping[str, Any]:
        operation = _retention(self.store.state)["operations"].get(operation_id)
        if not isinstance(operation, Mapping):
            raise RetentionError(f"unknown retention operation: {operation_id}")
        return operation

    def _complete_trash(self, operation_id: str) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation.get("status") != "staging":
            raise RetentionError(
                f"operation {operation_id} is {operation.get('status')}, not staging"
            )
        entries = operation.get("entries")
        if not isinstance(entries, list):
            raise RetentionError("retention operation entries are malformed")
        try:
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise RetentionError("retention operation entry is malformed")
                source = _session_path(self.session_dir, str(entry["original_path"]))
                destination = _session_path(self.session_dir, str(entry["trash_path"]))
                source_present = _present(source)
                destination_present = _present(destination)
                if source_present and not destination_present:
                    _verify_planned_file(source, entry, check_link_count=True)
                    _move_path(source, destination)
                elif not source_present and destination_present:
                    _verify_planned_file(destination, entry, check_link_count=True)
                    continue
                elif source_present and destination_present:
                    raise RetentionError(
                        f"both original and trash paths exist for {entry['original_path']}"
                    )
                else:
                    raise RetentionError(
                        f"both original and trash paths are missing for {entry['original_path']}"
                    )
        except (OSError, KeyError) as exc:
            raise RetentionError(
                f"trash operation {operation_id} is incomplete and recoverable: {exc}"
            ) from exc

        trashed_at = utc_now()
        grace_seconds = operation.get("grace_seconds")
        if not isinstance(grace_seconds, int):
            raise RetentionError("retention operation grace_seconds is malformed")
        purge_after = (
            datetime.fromisoformat(trashed_at) + timedelta(seconds=grace_seconds)
        ).isoformat(timespec="microseconds")

        def patch(state: SessionState) -> Mapping[str, Any]:
            retention = self._updated(state)
            current = retention["operations"].get(operation_id)
            if not isinstance(current, dict) or current.get("status") != "staging":
                raise RetentionError("trash completion no longer matches staged state")
            current["status"] = "trashed"
            current["trashed_at"] = trashed_at
            current["purge_after"] = purge_after
            return {"retention": retention}

        event = self.store.record(
            "retention.prune_trashed",
            payload={
                "operation_id": operation_id,
                "trashed_at": trashed_at,
                "purge_after": purge_after,
            },
            state_patch_factory=patch,
            actor="harness",
        )
        return {
            "operation": deepcopy(self._operation(operation_id)),
            "event": event.to_dict(),
        }

    def restore(self, operation_id: str, *, confirmation: str) -> dict[str, Any]:
        if confirmation != operation_id:
            raise RetentionError("restore confirmation must exactly match the operation id")

        def patch(state: SessionState) -> Mapping[str, Any]:
            retention = self._updated(state)
            operation = retention["operations"].get(operation_id)
            if not isinstance(operation, dict) or operation.get("status") != "trashed":
                status = operation.get("status") if isinstance(operation, Mapping) else "unknown"
                raise RetentionError(
                    f"operation {operation_id} cannot be restored from status {status}"
                )
            operation["status"] = "restoring"
            operation["restore_started_at"] = utc_now()
            return {"retention": retention}

        self.store.record(
            "retention.restore_staged",
            payload={"operation_id": operation_id},
            state_patch_factory=patch,
            actor="user",
        )
        return self._complete_restore(operation_id)

    def _complete_restore(self, operation_id: str) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation.get("status") != "restoring":
            raise RetentionError(
                f"operation {operation_id} is {operation.get('status')}, not restoring"
            )
        entries = operation.get("entries")
        if not isinstance(entries, list):
            raise RetentionError("retention operation entries are malformed")
        try:
            for entry in reversed(entries):
                if not isinstance(entry, Mapping):
                    raise RetentionError("retention operation entry is malformed")
                source = _session_path(self.session_dir, str(entry["trash_path"]))
                destination = _session_path(self.session_dir, str(entry["original_path"]))
                source_present = _present(source)
                destination_present = _present(destination)
                if source_present and not destination_present:
                    _verify_planned_file(source, entry, check_link_count=False)
                    _move_path(source, destination)
                elif not source_present and destination_present:
                    _verify_planned_file(destination, entry, check_link_count=False)
                    continue
                elif source_present and destination_present:
                    raise RetentionError(
                        f"both original and trash paths exist for {entry['original_path']}"
                    )
                else:
                    raise RetentionError(
                        f"both original and trash paths are missing for {entry['original_path']}"
                    )
        except (OSError, KeyError) as exc:
            raise RetentionError(
                f"restore operation {operation_id} is incomplete and recoverable: {exc}"
            ) from exc
        restored_at = utc_now()

        def patch(state: SessionState) -> Mapping[str, Any]:
            retention = self._updated(state)
            current = retention["operations"].get(operation_id)
            if not isinstance(current, dict) or current.get("status") != "restoring":
                raise RetentionError("restore completion no longer matches staged state")
            current["status"] = "restored"
            current["restored_at"] = restored_at
            return {"retention": retention}

        event = self.store.record(
            "retention.restored",
            payload={"operation_id": operation_id, "restored_at": restored_at},
            state_patch_factory=patch,
            actor="harness",
        )
        return {
            "operation": deepcopy(self._operation(operation_id)),
            "event": event.to_dict(),
        }

    def purge(
        self,
        operation_id: str,
        *,
        confirmation: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if confirmation != operation_id:
            raise RetentionError("purge confirmation must exactly match the operation id")
        checked_at = now or datetime.now(timezone.utc)

        def patch(state: SessionState) -> Mapping[str, Any]:
            retention = self._updated(state)
            operation = retention["operations"].get(operation_id)
            if not isinstance(operation, dict) or operation.get("status") != "trashed":
                status = operation.get("status") if isinstance(operation, Mapping) else "unknown"
                raise RetentionError(
                    f"operation {operation_id} cannot be purged from status {status}"
                )
            purge_after = _parse_time(operation.get("purge_after"), name="purge_after")
            if checked_at < purge_after:
                raise RetentionError(
                    "grace period has not elapsed; purge is allowed after "
                    f"{purge_after.isoformat()}"
                )
            operation["status"] = "purging"
            operation["purge_started_at"] = checked_at.isoformat(timespec="microseconds")
            return {"retention": retention}

        self.store.record(
            "retention.purge_staged",
            payload={
                "operation_id": operation_id,
                "checked_at": checked_at.isoformat(timespec="microseconds"),
            },
            state_patch_factory=patch,
            actor="user",
        )
        return self._complete_purge(operation_id)

    def _complete_purge(self, operation_id: str) -> dict[str, Any]:
        operation = self._operation(operation_id)
        if operation.get("status") != "purging":
            raise RetentionError(
                f"operation {operation_id} is {operation.get('status')}, not purging"
            )
        entries = operation.get("entries")
        if not isinstance(entries, list):
            raise RetentionError("retention operation entries are malformed")
        try:
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise RetentionError("retention operation entry is malformed")
                trash = _session_path(self.session_dir, str(entry["trash_path"]))
                original = _session_path(self.session_dir, str(entry["original_path"]))
                if _present(original):
                    raise RetentionError(
                        f"refusing purge because original path exists: {entry['original_path']}"
                    )
                if trash.is_symlink():
                    raise RetentionError(f"refusing to purge symlink: {entry['trash_path']}")
                if trash.is_dir():
                    raise RetentionError(
                        "directory artifact purge is unsupported until the Zarr retention "
                        "contract is implemented"
                    )
                if _present(trash):
                    _verify_planned_file(trash, entry, check_link_count=False)
                    _unlink_path(trash)
        except (OSError, KeyError) as exc:
            raise RetentionError(
                f"purge operation {operation_id} is incomplete and recoverable: {exc}"
            ) from exc
        purged_at = utc_now()

        def patch(state: SessionState) -> Mapping[str, Any]:
            retention = self._updated(state)
            current = retention["operations"].get(operation_id)
            if not isinstance(current, dict) or current.get("status") != "purging":
                raise RetentionError("purge completion no longer matches staged state")
            current["status"] = "purged"
            current["purged_at"] = purged_at
            return {"retention": retention}

        event = self.store.record(
            "retention.purged",
            payload={"operation_id": operation_id, "purged_at": purged_at},
            state_patch_factory=patch,
            actor="harness",
        )
        return {
            "operation": deepcopy(self._operation(operation_id)),
            "event": event.to_dict(),
        }

    def recover(self) -> list[dict[str, Any]]:
        """Finish every event-authorized incomplete filesystem operation idempotently."""

        recovered: list[dict[str, Any]] = []
        operations = list(_retention(self.store.state)["operations"].items())
        for operation_id, operation in operations:
            if not isinstance(operation, Mapping):
                raise RetentionError(f"retention operation {operation_id} is malformed")
            status = operation.get("status")
            if status == "staging":
                recovered.append(self._complete_trash(str(operation_id)))
            elif status == "restoring":
                recovered.append(self._complete_restore(str(operation_id)))
            elif status == "purging":
                recovered.append(self._complete_purge(str(operation_id)))
        return recovered
