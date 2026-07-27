"""Append-only scientific-session event contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ._json import (
    ensure_jsonable,
    require_mapping,
    require_nonempty_string,
    require_nonnegative_int,
)

EVENT_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SessionEvent:
    """One durable fact in a scientific session's append-only history."""

    session_id: str
    sequence: int
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    state_patch: dict[str, Any] = field(default_factory=dict)
    actor: str = "harness"
    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=utc_now)
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_nonempty_string(self.session_id, name="session_id")
        require_nonnegative_int(self.sequence, name="sequence")
        if self.sequence == 0:
            raise ValueError("sequence must start at 1")
        require_nonempty_string(self.kind, name="kind")
        require_nonempty_string(self.actor, name="actor")
        require_nonempty_string(self.event_id, name="event_id")
        require_nonempty_string(self.timestamp, name="timestamp")
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported event schema version: {self.schema_version}")
        ensure_jsonable(self.payload, name="payload")
        ensure_jsonable(self.state_patch, name="state_patch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "kind": self.kind,
            "actor": self.actor,
            "payload": self.payload,
            "state_patch": self.state_patch,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SessionEvent:
        data = require_mapping(value, name="event")
        try:
            return cls(
                schema_version=data["schema_version"],
                event_id=data["event_id"],
                session_id=data["session_id"],
                sequence=data["sequence"],
                timestamp=data["timestamp"],
                kind=data["kind"],
                actor=data["actor"],
                payload=require_mapping(data.get("payload", {}), name="event.payload"),
                state_patch=require_mapping(data.get("state_patch", {}), name="event.state_patch"),
            )
        except KeyError as exc:
            raise ValueError(f"event is missing required field: {exc.args[0]}") from exc
