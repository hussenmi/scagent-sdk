from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from scagent_sdk.runtime.claude_store import ScientificSessionTranscriptStore


def test_transcript_store_round_trips_and_deduplicates_uuid_entries(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = ScientificSessionTranscriptStore(tmp_path)
        key = {"project_key": "project", "session_id": "sdk-1"}
        first = {"type": "assistant", "uuid": "u1", "message": {"text": "a"}}
        without_uuid = {"type": "mode", "value": "default"}
        await store.append(key, [first, without_uuid])
        await store.append(key, [first, without_uuid])

        loaded = await store.load(key)
        assert loaded == [first, without_uuid, without_uuid]
        assert (await store.list_sessions("project"))[0]["session_id"] == "sdk-1"

    asyncio.run(exercise())


def test_transcript_store_supports_subagent_subpaths(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = ScientificSessionTranscriptStore(tmp_path)
        key = {
            "project_key": "project",
            "session_id": "sdk-1",
            "subpath": "subagents/agent-1",
        }
        await store.append(key, [{"type": "assistant", "uuid": "sub-1"}])

        assert await store.load(key) == [{"type": "assistant", "uuid": "sub-1"}]
        assert await store.list_subkeys({"project_key": "project", "session_id": "sdk-1"}) == [
            "subagents/agent-1"
        ]

    asyncio.run(exercise())


def test_transcript_store_rejects_path_traversal(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = ScientificSessionTranscriptStore(tmp_path)
        with pytest.raises(ValueError, match="subpath"):
            await store.append(
                {
                    "project_key": "project",
                    "session_id": "sdk-1",
                    "subpath": "../escape",
                },
                [{"type": "assistant"}],
            )

    asyncio.run(exercise())
