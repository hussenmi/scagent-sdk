"""Crash-tolerant, resumable scientific-session storage."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scagent_sdk.contracts.events import SessionEvent, utc_now
from scagent_sdk.contracts.state import (
    SESSION_STATE_SCHEMA_VERSION,
    SessionMetadata,
    SessionState,
)
from scagent_sdk.errors import (
    EventLogCorruptionError,
    SessionFormatError,
    SessionIdentityError,
    SessionNotFoundError,
)
from scagent_sdk.state.lineage import rebuild_forest

# A session id is used verbatim as a filesystem directory name and as the resume
# key, so it must be a single safe token: start with an alphanumeric, then only
# alphanumerics, dash, underscore, or dot, and never a ".." traversal sequence.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def new_session_id() -> str:
    """Return a time-ordered, collision-safe session id.

    The ``run_<UTC-timestamp>_<random>`` form sorts chronologically in a plain
    directory listing and reads as a wall-clock time, while the random suffix
    keeps it globally unique even for sessions started within the same second.
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{secrets.token_hex(3)}"


def validate_session_id(value: str) -> str:
    """Return ``value`` if it is a safe session token, else raise."""

    if not _SAFE_SESSION_ID.fullmatch(value) or ".." in value:
        raise SessionIdentityError(f"invalid session id: {value!r}")
    return value


def apply_merge_patch(target: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an RFC 7396-style merge patch without mutating the caller's value."""

    result = deepcopy(target)
    for key, value in patch.items():
        name = str(key)
        if value is None:
            result.pop(name, None)
        elif isinstance(value, Mapping):
            existing = result.get(name)
            base = existing if isinstance(existing, dict) else {}
            result[name] = apply_merge_patch(base, value)
        else:
            result[name] = deepcopy(value)
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SessionFormatError(f"required session file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SessionFormatError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SessionFormatError(f"expected a JSON object in {path}")
    return value


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


class SessionStore:
    """Persistence boundary for one scientific session.

    The event log is the source of truth. ``state.json`` is a materialized view. If a
    process stops after appending an event but before checkpointing state, ``open`` replays
    the unapplied event patches.
    """

    def __init__(self, session_dir: Path, metadata: SessionMetadata, state: SessionState):
        self.session_dir = session_dir
        self.metadata = metadata
        self.state = state

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def metadata_path(self) -> Path:
        return self.session_dir / "session.json"

    @property
    def state_path(self) -> Path:
        return self.session_dir / "state.json"

    @property
    def events_path(self) -> Path:
        return self.session_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.session_dir / ".session.lock"

    @classmethod
    def create(
        cls,
        sessions_root: str | Path,
        *,
        title: str,
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> SessionStore:
        root = Path(sessions_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        resolved_id = validate_session_id(session_id) if session_id else new_session_id()
        session_dir = root / resolved_id
        try:
            session_dir.mkdir()
        except FileExistsError as exc:
            raise SessionFormatError(f"session already exists: {resolved_id}") from exc

        for child in ("artifacts", "code", "figures", "reports", "tables", "data", "shell", "logs"):
            (session_dir / child).mkdir()

        now = utc_now()
        metadata = SessionMetadata(
            session_id=resolved_id,
            title=title,
            parent_session_id=parent_session_id,
            created_at=now,
            updated_at=now,
        )
        state = SessionState(session_id=resolved_id, created_at=now, updated_at=now)
        store = cls(session_dir, metadata, state)
        _atomic_write_json(store.metadata_path, metadata.to_dict())
        _atomic_write_json(store.state_path, state.to_dict())
        store.events_path.touch(exist_ok=False)
        _fsync_directory(session_dir)
        store.record(
            "session.created",
            payload={"title": title, "parent_session_id": parent_session_id},
        )
        return store

    @classmethod
    def open(cls, sessions_root: str | Path, session_id: str) -> SessionStore:
        validate_session_id(session_id)
        session_dir = Path(sessions_root).expanduser().resolve() / session_id
        if not session_dir.is_dir():
            raise SessionNotFoundError(f"scientific session not found: {session_id}")
        try:
            metadata = SessionMetadata.from_dict(_read_json(session_dir / "session.json"))
            state = SessionState.from_dict(_read_json(session_dir / "state.json"))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from exc
        store = cls(session_dir, metadata, state)
        store._validate_identity()
        with store._exclusive_lock():
            store._reload_under_lock()
            store._replay_unapplied_events()
            warnings = store._migrate_state_schema()
        # Recorded after the lock is released: ``record`` takes the same exclusive lock on its own
        # descriptor, and flock does not re-enter, so appending an event while holding it deadlocks.
        if warnings:
            store.record(
                "session.state_migrated",
                payload={
                    "from_schema_version": 1,
                    "to_schema_version": SESSION_STATE_SCHEMA_VERSION,
                    "lineage_nodes": len(store.state.lineage.get("nodes", {})),
                    "warnings": warnings,
                },
            )
        return store

    @classmethod
    def list_sessions(cls, sessions_root: str | Path) -> list[SessionMetadata]:
        root = Path(sessions_root).expanduser().resolve()
        if not root.exists():
            return []
        items: list[SessionMetadata] = []
        for child in root.iterdir():
            if not child.is_dir() or not (child / "session.json").is_file():
                continue
            try:
                items.append(SessionMetadata.from_dict(_read_json(child / "session.json")))
            except (SessionFormatError, ValueError):
                continue
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _validate_identity(self) -> None:
        directory_id = self.session_dir.name
        ids = {directory_id, self.metadata.session_id, self.state.session_id}
        if len(ids) != 1:
            raise SessionIdentityError(
                "session directory, metadata, and state IDs disagree: " + ", ".join(sorted(ids))
            )

    def _reload_under_lock(self) -> None:
        try:
            self.metadata = SessionMetadata.from_dict(_read_json(self.metadata_path))
            self.state = SessionState.from_dict(_read_json(self.state_path))
        except ValueError as exc:
            raise SessionFormatError(str(exc)) from exc
        self._validate_identity()

    def events(self) -> list[SessionEvent]:
        if not self.events_path.is_file():
            raise SessionFormatError(f"required session file is missing: {self.events_path}")
        parsed: list[SessionEvent] = []
        expected = 1
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise EventLogCorruptionError(
                        f"blank event-log line at {self.events_path}:{line_number}"
                    )
                try:
                    value = json.loads(line)
                    event = SessionEvent.from_dict(value)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise EventLogCorruptionError(
                        f"invalid event at {self.events_path}:{line_number}: {exc}"
                    ) from exc
                if event.session_id != self.session_id:
                    raise EventLogCorruptionError(
                        f"event {event.event_id} belongs to {event.session_id}, "
                        f"expected {self.session_id}"
                    )
                if event.sequence != expected:
                    raise EventLogCorruptionError(
                        f"event sequence gap: expected {expected}, found {event.sequence}"
                    )
                parsed.append(event)
                expected += 1
        return parsed

    def _replay_unapplied_events(self) -> None:
        events = self.events()
        if self.state.last_event_sequence > len(events):
            raise EventLogCorruptionError("state checkpoint is ahead of the append-only event log")
        changed = False
        for event in events[self.state.last_event_sequence :]:
            self._apply_event(event)
            changed = True
        if changed:
            _atomic_write_json(self.state_path, self.state.to_dict())

    def _migrate_state_schema(self) -> list[str]:
        """Bring a v1 state checkpoint up to v2 by reconstructing the artifact lineage.

        A schema default cannot substitute for this: historical events carry no lineage patch, so
        replaying them leaves the forest empty and every recorded artifact unreachable. The forest
        is derived rather than new information, so it is materialized into the checkpoint without
        recording an event -- consistent with ``state.json`` being a replayable view of the log.

        Returns any reconstruction warnings for the caller to record once the lock is released. They
        are worth an event: a fact root that could not be placed changes what a checkout restores.
        Must be called with the exclusive lock held and must not append an event itself.
        """

        if self.state.schema_version >= SESSION_STATE_SCHEMA_VERSION:
            return []
        committed = [
            (str(event.payload["execution_id"]), event.payload, event.state_patch)
            for event in self.events()
            if event.kind == "capability.result_committed"
            and isinstance(event.payload.get("execution_id"), str)
        ]
        forest, warnings = rebuild_forest(committed, merge=apply_merge_patch)
        self.state.lineage = forest
        self.state.schema_version = SESSION_STATE_SCHEMA_VERSION
        _atomic_write_json(self.state_path, self.state.to_dict())
        return warnings

    def _apply_event(self, event: SessionEvent) -> None:
        if event.state_patch:
            current = self.state.to_dict()
            protected = {
                "schema_version": current["schema_version"],
                "session_id": current["session_id"],
                "created_at": current["created_at"],
            }
            updated = apply_merge_patch(current, event.state_patch)
            for key, value in protected.items():
                if updated.get(key) != value:
                    raise EventLogCorruptionError(
                        f"event {event.event_id} attempted to change protected state field {key}"
                    )
            updated["revision"] = self.state.revision + 1
            updated["updated_at"] = event.timestamp
            updated["last_event_sequence"] = event.sequence
            try:
                self.state = SessionState.from_dict(updated)
            except ValueError as exc:
                raise EventLogCorruptionError(
                    f"event {event.event_id} produced invalid state: {exc}"
                ) from exc
        else:
            self.state.updated_at = event.timestamp
            self.state.last_event_sequence = event.sequence

    def _append_event(self, event: SessionEvent) -> None:
        encoded = json.dumps(event.to_dict(), separators=(",", ":"), allow_nan=False) + "\n"
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def record(
        self,
        kind: str,
        *,
        payload: Mapping[str, Any] | None = None,
        state_patch: Mapping[str, Any] | None = None,
        actor: str = "harness",
    ) -> SessionEvent:
        with self._exclusive_lock():
            self._reload_under_lock()
            self._replay_unapplied_events()
            event = SessionEvent(
                session_id=self.session_id,
                sequence=self.state.last_event_sequence + 1,
                kind=kind,
                payload=dict(payload or {}),
                state_patch=dict(state_patch or {}),
                actor=actor,
            )
            self._append_event(event)
            self._apply_event(event)
            self.metadata.updated_at = event.timestamp
            _atomic_write_json(self.state_path, self.state.to_dict())
            _atomic_write_json(self.metadata_path, self.metadata.to_dict())
            return event
