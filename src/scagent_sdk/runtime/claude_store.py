"""Mirror Claude SDK transcripts inside the durable scientific session."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ScientificSessionTranscriptStore:
    """A Claude Agent SDK SessionStore scoped to one scientific session directory."""

    def __init__(self, scientific_session_dir: str | Path):
        self.root = Path(scientific_session_dir).resolve() / "runtime" / "claude-agent-sdk"
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe_component(self, value: str, *, name: str) -> str:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError(f"invalid {name}: {value!r}")
        return value

    def _path(self, key: dict[str, Any]) -> Path:
        session_id = self._safe_component(str(key["session_id"]), name="runtime session ID")
        directory = self.root / session_id
        subpath = key.get("subpath")
        if subpath is not None:
            parts = Path(str(subpath)).parts
            invalid = any(part in {"", ".", ".."} or not _SAFE_ID.fullmatch(part) for part in parts)
            if not parts or invalid:
                raise ValueError(f"invalid transcript subpath: {subpath!r}")
            directory = directory.joinpath(*parts)
        return directory / "transcript.jsonl"

    @staticmethod
    def _read_entries(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid SDK transcript {path}:{line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"SDK transcript entry is not an object: {path}:{line_number}")
                entries.append(value)
        return entries

    def _append_sync(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / ".transcript.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                current = self._read_entries(path)
                known = {
                    str(entry["uuid"]) for entry in current if isinstance(entry.get("uuid"), str)
                }
                with path.open("a", encoding="utf-8") as handle:
                    for entry in entries:
                        item = deepcopy(entry)
                        uuid = item.get("uuid")
                        if isinstance(uuid, str) and uuid in known:
                            continue
                        encoded = json.dumps(item, separators=(",", ":"), allow_nan=False)
                        handle.write(encoded + "\n")
                        if isinstance(uuid, str):
                            known.add(uuid)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._append_sync, key, entries)

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        path = self._path(key)
        entries = await asyncio.to_thread(self._read_entries, path)
        return entries or None

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        del project_key
        items: list[dict[str, Any]] = []
        for child in self.root.iterdir():
            transcript = child / "transcript.jsonl"
            if child.is_dir() and transcript.is_file():
                items.append(
                    {
                        "session_id": child.name,
                        "mtime": int(transcript.stat().st_mtime * 1000),
                    }
                )
        return items

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        session_id = self._safe_component(str(key["session_id"]), name="runtime session ID")
        directory = self.root / session_id
        if not directory.is_dir():
            return []
        return [
            str(path.parent.relative_to(directory))
            for path in directory.rglob("transcript.jsonl")
            if path.parent != directory
        ]
