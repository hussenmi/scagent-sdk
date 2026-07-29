"""Versioned metadata and materialized state for a scientific session."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._json import (
    ensure_jsonable,
    require_mapping,
    require_nonempty_string,
    require_nonnegative_int,
)
from .events import utc_now

# Metadata and state are versioned independently: adding a materialized-view field such as
# ``lineage`` changes state.json only, and must not force an unrelated session.json migration.
SESSION_METADATA_SCHEMA_VERSION = 1
SESSION_STATE_SCHEMA_VERSION = 2
# Retained as the metadata version so existing importers keep working.
SESSION_SCHEMA_VERSION = SESSION_METADATA_SCHEMA_VERSION
# State revisions this process can materialize. v1 sessions predate the lineage forest and open
# with an empty one, so they are readable without a rewrite.
SUPPORTED_STATE_SCHEMA_VERSIONS = frozenset({1, SESSION_STATE_SCHEMA_VERSION})


@dataclass
class SessionMetadata:
    session_id: str
    title: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    status: str = "active"
    parent_session_id: str | None = None
    schema_version: int = SESSION_METADATA_SCHEMA_VERSION

    def validate(self) -> None:
        require_nonempty_string(self.session_id, name="session_id")
        require_nonempty_string(self.title, name="title")
        require_nonempty_string(self.created_at, name="created_at")
        require_nonempty_string(self.updated_at, name="updated_at")
        require_nonempty_string(self.status, name="status")
        if self.parent_session_id is not None:
            require_nonempty_string(self.parent_session_id, name="parent_session_id")
        if self.schema_version != SESSION_METADATA_SCHEMA_VERSION:
            raise ValueError(f"unsupported session schema version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status,
            "parent_session_id": self.parent_session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SessionMetadata:
        data = require_mapping(value, name="session metadata")
        try:
            metadata = cls(
                schema_version=data["schema_version"],
                session_id=data["session_id"],
                title=data["title"],
                status=data["status"],
                parent_session_id=data.get("parent_session_id"),
                created_at=data["created_at"],
                updated_at=data["updated_at"],
            )
        except KeyError as exc:
            raise ValueError(f"session metadata is missing required field: {exc.args[0]}") from exc
        metadata.validate()
        return metadata


@dataclass
class SessionState:
    session_id: str
    created_at: str
    updated_at: str
    revision: int = 0
    last_event_sequence: int = 0
    facts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    decisions: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=lambda: {"active": None, "history": []})
    # Executor-owned artifact lineage. Deliberately outside ``facts``: skills return facts patches,
    # and must never be able to write the topology that decides which matrix they are handed.
    lineage: dict[str, Any] = field(
        default_factory=lambda: {"active_execution_id": None, "nodes": {}}
    )
    schema_version: int = SESSION_STATE_SCHEMA_VERSION

    def validate(self) -> None:
        require_nonempty_string(self.session_id, name="session_id")
        require_nonempty_string(self.created_at, name="created_at")
        require_nonempty_string(self.updated_at, name="updated_at")
        require_nonnegative_int(self.revision, name="revision")
        require_nonnegative_int(self.last_event_sequence, name="last_event_sequence")
        if self.schema_version not in SUPPORTED_STATE_SCHEMA_VERSIONS:
            raise ValueError(f"unsupported state schema version: {self.schema_version}")
        ensure_jsonable(self.facts, name="facts")
        ensure_jsonable(self.artifacts, name="artifacts")
        ensure_jsonable(self.decisions, name="decisions")
        ensure_jsonable(self.runtime, name="runtime")
        ensure_jsonable(self.lineage, name="lineage")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "last_event_sequence": self.last_event_sequence,
            "facts": self.facts,
            "artifacts": self.artifacts,
            "decisions": self.decisions,
            "runtime": self.runtime,
            "lineage": self.lineage,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SessionState:
        data = require_mapping(value, name="session state")
        try:
            state = cls(
                schema_version=data["schema_version"],
                session_id=data["session_id"],
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                revision=data["revision"],
                last_event_sequence=data["last_event_sequence"],
                facts=require_mapping(data.get("facts", {}), name="state.facts"),
                artifacts=require_mapping(data.get("artifacts", {}), name="state.artifacts"),
                decisions=require_mapping(data.get("decisions", {}), name="state.decisions"),
                runtime=require_mapping(data.get("runtime", {}), name="state.runtime"),
                # Absent in v1 sessions; an empty forest is the correct starting point, and the
                # v1 -> v2 reducer rebuilds it by replaying committed events.
                lineage=require_mapping(
                    data.get("lineage") or {"active_execution_id": None, "nodes": {}},
                    name="state.lineage",
                ),
            )
        except KeyError as exc:
            raise ValueError(f"session state is missing required field: {exc.args[0]}") from exc
        state.validate()
        return state
