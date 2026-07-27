"""Stage deterministic tool results and commit them after PostToolUse."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from scagent_sdk.capabilities.manifest import CapabilityTool
from scagent_sdk.capabilities.registry import SkillPackage
from scagent_sdk.capabilities.results import (
    INLINE_RESULT_LIMIT_BYTES,
    MODEL_MEDIA_LIMIT_BYTES,
    MODEL_MEDIA_TOTAL_BYTES,
    CapabilityContext,
    CapabilityResult,
)
from scagent_sdk.errors import CapabilityExecutionError, CapabilityInterrupted
from scagent_sdk.execution.broker import EnvironmentBroker
from scagent_sdk.floors import FloorEvaluator
from scagent_sdk.session import AnalysisSession

_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning):\s+(.*)$")
# How long a forced stop waits for a signalled worker to unwind before giving up on it.
_WORKER_STOP_SECONDS = 15.0
_PATH_ARGUMENT_NAMES = frozenset({"cwd", "path"})
_PATH_ARGUMENT_SUFFIXES = ("_dir", "_directory", "_file", "_path")


def _concise_capability_error(message: str) -> str:
    """Reduce a capability failure (often a wrapped subprocess traceback) to one clean line.

    The full message is still handed to the model; this is only what the terminal shows the user.
    """

    lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
    if not lines:
        return "capability failed"
    last = lines[-1]
    match = _EXCEPTION_LINE.match(last)
    concise = match.group(1) if match else last
    return concise if len(concise) <= 300 else concise[:297] + "..."


def _resolve_session_paths(
    value: Any,
    session_dir: Path,
    *,
    argument_name: str | None = None,
) -> Any:
    """Resolve existing session-relative path arguments before capability dispatch.

    Capability artifacts are stored below the scientific session. A model may pass either the
    canonical absolute path returned by the executor or the portable session-relative path kept
    in provenance. Resolve the latter centrally so every skill, current-runtime utility, and
    brokered worker observes the same filesystem contract.
    """

    if isinstance(value, dict):
        return {
            key: _resolve_session_paths(item, session_dir, argument_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_session_paths(item, session_dir, argument_name=argument_name)
            for item in value
        ]
    is_path_argument = argument_name is not None and (
        argument_name in _PATH_ARGUMENT_NAMES
        or argument_name.endswith(_PATH_ARGUMENT_SUFFIXES)
    )
    if not is_path_argument or not isinstance(value, str) or not value.strip():
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    session_candidate = (session_dir / candidate).resolve()
    return str(session_candidate) if session_candidate.exists() else value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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
    finally:
        temporary_path.unlink(missing_ok=True)


def _fsync_tree(path: Path) -> None:
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        with child.open("rb") as handle:
            os.fsync(handle.fileno())
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _find_execution_id(value: Any) -> str | None:
    if isinstance(value, dict):
        direct = value.get("scagent_execution_id")
        if isinstance(direct, str):
            return direct
        for item in value.values():
            found = _find_execution_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_execution_id(item)
            if found:
                return found
    elif isinstance(value, str):
        try:
            return _find_execution_id(json.loads(value))
        except (json.JSONDecodeError, RecursionError):
            return None
    return None


class CapabilityExecutor:
    def __init__(
        self, session: AnalysisSession, *, environment_broker: EnvironmentBroker | None = None
    ):
        self.session = session
        self.environment_broker = environment_broker
        self.pending_root = session.directory / "runtime" / "capabilities" / "pending"
        self.artifact_root = session.directory / "artifacts" / "capabilities"
        self.pending_root.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    async def execute(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        execution_id = str(uuid4())
        staging_dir = self.pending_root / execution_id
        staging_dir.mkdir()
        context = CapabilityContext(
            scientific_session_id=self.session.session_id,
            session_dir=self.session.directory,
            staging_dir=staging_dir,
            skill_id=package.manifest.skill_id,
            tool_name=tool.name,
            execution_id=execution_id,
            state_revision=self.session.store.state.revision,
            state_facts=deepcopy(self.session.store.state.facts),
        )
        resolved_arguments = _resolve_session_paths(arguments, self.session.directory)
        try:
            failures = FloorEvaluator().failures(self.session.store.state, tool.floors)
            if failures:
                reason = " ".join(
                    f"[{failure.floor}] {failure.reason} {failure.remediation}"
                    for failure in failures
                )
                raise CapabilityExecutionError(f"scientific floor denied execution: {reason}")
            if tool.environment == "current":
                handler = package.load_handler(tool)
                value = handler(resolved_arguments, context)
                if inspect.isawaitable(value):
                    value = await value
                environment = {"name": "current", "python": sys.executable}
            elif self.environment_broker is not None:
                execution = await self._execute_in_environment(
                    package, tool, resolved_arguments, context
                )
                value = execution.value
                environment = execution.provenance
            else:
                raise CapabilityExecutionError(
                    f"environment {tool.environment!r} requires the environment broker"
                )
            result = CapabilityResult.from_value(value)
            model_content = self._model_content(context, result)
            envelope = self._stage_result(
                package,
                tool,
                context,
                result,
                environment,
                arguments=arguments,
            )
        except asyncio.CancelledError:
            # A forced stop unwinds the whole turn; record why this execution has no result
            # before the cancellation continues. Nothing is committed, so state stays sound.
            self._record_interrupted(package, tool, execution_id, forced=True)
            raise
        except CapabilityInterrupted as exc:
            self._record_interrupted(package, tool, execution_id, forced=False)
            summary = f"{tool.name} was stopped before it finished"
            return {
                "content": [{"type": "text", "text": f"{summary}: {exc}"}],
                "is_error": True,
                "error_summary": summary,
            }
        except Exception as exc:
            self.session.store.record(
                "capability.execution_failed",
                payload={
                    "execution_id": execution_id,
                    "skill_id": package.manifest.skill_id,
                    "tool_name": tool.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            concise = _concise_capability_error(str(exc))
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{tool.name} failed: {concise}\n\nFull detail:\n{exc}",
                    }
                ],
                "is_error": True,
                "error_summary": f"{tool.name} failed: {concise}",
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(envelope, sort_keys=True, allow_nan=False),
                }
            ]
            + model_content,
            "structuredContent": envelope,
        }

    async def _execute_in_environment(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        arguments: dict[str, Any],
        context: CapabilityContext,
    ) -> Any:
        """Run a compute capability off the event loop so the turn stays interruptible.

        The broker blocks on a worker that can run for minutes; calling it directly would
        freeze the event loop, and neither a keyboard interrupt nor the model runtime's own
        control channel could be serviced while science is running. The worker future is
        shielded so a cancellation deliberately stops the worker and waits for it to unwind,
        instead of orphaning a GPU process.
        """

        broker = self.environment_broker
        assert broker is not None
        worker = asyncio.ensure_future(
            asyncio.to_thread(broker.execute, package, tool, arguments, context)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            broker.cancel(context.execution_id)
            with suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(worker), timeout=_WORKER_STOP_SECONDS)
            worker.cancel()
            raise

    def _record_interrupted(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        execution_id: str,
        *,
        forced: bool,
    ) -> None:
        with suppress(Exception):
            self.session.store.record(
                "capability.execution_interrupted",
                payload={
                    "execution_id": execution_id,
                    "skill_id": package.manifest.skill_id,
                    "tool_name": tool.name,
                    "forced": forced,
                },
                actor="user",
            )

    @staticmethod
    def _model_content(
        context: CapabilityContext, result: CapabilityResult
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        declared = {artifact.relative_path for artifact in result.artifacts}
        total = 0
        for media in result.model_media:
            if media.relative_path not in declared:
                raise CapabilityExecutionError(
                    f"model_media must also be declared as an artifact: {media.relative_path}"
                )
            source = (context.staging_dir / media.relative_path).resolve()
            try:
                source.relative_to(context.staging_dir.resolve())
            except ValueError as exc:
                raise CapabilityExecutionError(
                    f"model_media escapes staging directory: {media.relative_path}"
                ) from exc
            if not source.is_file():
                raise CapabilityExecutionError(f"declared model_media does not exist: {source}")
            size = source.stat().st_size
            if size > MODEL_MEDIA_LIMIT_BYTES:
                raise CapabilityExecutionError(
                    f"model_media exceeds {MODEL_MEDIA_LIMIT_BYTES} bytes: {media.relative_path}"
                )
            total += size
            if total > MODEL_MEDIA_TOTAL_BYTES:
                raise CapabilityExecutionError(
                    f"model_media totals more than {MODEL_MEDIA_TOTAL_BYTES} bytes; attach fewer "
                    "or smaller figures"
                )
            content.append(
                {
                    "type": "image",
                    "data": base64.b64encode(source.read_bytes()).decode("ascii"),
                    "mimeType": media.media_type,
                }
            )
        return content

    def _stage_result(
        self,
        package: SkillPackage,
        tool: CapabilityTool,
        context: CapabilityContext,
        result: CapabilityResult,
        environment: dict[str, Any],
        *,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        details_bytes = json.dumps(
            result.details, sort_keys=True, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        files: list[dict[str, Any]] = []
        inline_details: Any = result.details
        if len(details_bytes) > INLINE_RESULT_LIMIT_BYTES:
            details_path = context.staging_dir / "details.json"
            details_path.write_bytes(details_bytes + b"\n")
            inline_details = None
            files.append(
                {
                    "name": "details",
                    "relative_path": "details.json",
                    "media_type": "application/json",
                    "size_bytes": details_path.stat().st_size,
                }
            )
        for produced in result.artifacts:
            source = (context.staging_dir / produced.relative_path).resolve()
            try:
                source.relative_to(context.staging_dir.resolve())
            except ValueError as exc:
                raise CapabilityExecutionError(
                    f"artifact escapes staging directory: {produced.relative_path}"
                ) from exc
            if not source.is_file():
                raise CapabilityExecutionError(f"declared artifact does not exist: {source}")
            files.append(
                {
                    "name": produced.name,
                    "relative_path": produced.relative_path,
                    "media_type": produced.media_type,
                    "size_bytes": source.stat().st_size,
                }
            )
        persisted = {
            "schema_version": result.schema_version,
            "execution_id": context.execution_id,
            "skill_id": package.manifest.skill_id,
            "skill_version": package.manifest.version,
            "skill_fingerprint": package.fingerprint,
            "tool_name": tool.name,
            "environment": environment,
            "input_state_revision": context.state_revision,
            "arguments": arguments,
            "summary": result.summary,
            "details": inline_details,
            "facts_patch": result.facts_patch,
            "decisions_patch": result.decisions_patch,
            "files": files,
            "model_media": [
                {
                    "name": media.name,
                    "relative_path": media.relative_path,
                    "media_type": media.media_type,
                }
                for media in result.model_media
            ],
        }
        _atomic_json(context.staging_dir / "result.json", persisted)
        _fsync_tree(context.staging_dir)
        self.session.store.record(
            "capability.result_staged",
            payload={
                "execution_id": context.execution_id,
                "skill_id": package.manifest.skill_id,
                "skill_version": package.manifest.version,
                "skill_fingerprint": package.fingerprint,
                "tool_name": tool.name,
                "environment": environment,
                "input_state_revision": context.state_revision,
                "arguments": arguments,
                "files": files,
            },
        )
        artifact_relative_path = f"artifacts/capabilities/{context.execution_id}"
        artifact_path = (self.session.directory / artifact_relative_path).resolve()
        model_files = [
            {
                **item,
                "session_relative_path": f"{artifact_relative_path}/{item['relative_path']}",
                "path": str(artifact_path / item["relative_path"]),
            }
            for item in files
        ]
        return {
            "schema_version": result.schema_version,
            "scagent_execution_id": context.execution_id,
            "status": "validated",
            "state_commit": "PostToolUse",
            "summary": result.summary,
            "details": inline_details,
            "artifact_path": str(artifact_path) if files else None,
            "artifact_relative_path": artifact_relative_path if files else None,
            "files": model_files,
            "model_media": persisted["model_media"],
        }

    def commit(self, execution_id: str) -> bool:
        pending = self.pending_root / execution_id
        final = self.artifact_root / execution_id
        if execution_id in self.session.store.state.artifacts:
            return False
        source = pending if pending.is_dir() else final
        result_path = source / "result.json"
        if not result_path.is_file():
            raise CapabilityExecutionError(f"pending capability result not found: {execution_id}")
        data = json.loads(result_path.read_text(encoding="utf-8"))
        if pending.is_dir():
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pending, final)
        artifact_record = {
            "kind": "capability-result",
            "skill_id": data["skill_id"],
            "skill_version": data["skill_version"],
            "skill_fingerprint": data["skill_fingerprint"],
            "tool_name": data["tool_name"],
            "environment": data["environment"],
            "input_state_revision": data.get("input_state_revision"),
            "arguments": data.get("arguments", {}),
            "summary": data["summary"],
            "path": str(final.relative_to(self.session.directory)),
            "files": data["files"],
            "model_media": data.get("model_media", []),
        }
        self.session.store.record(
            "capability.result_committed",
            payload={"execution_id": execution_id, **artifact_record},
            state_patch={
                "facts": data["facts_patch"],
                "decisions": data["decisions_patch"],
                "artifacts": {execution_id: artifact_record},
            },
        )
        self.session.refresh_outputs_best_effort()
        return True

    def commit_from_hook(self, input_data: dict[str, Any]) -> bool:
        execution_id = _find_execution_id(input_data.get("tool_response", input_data))
        if execution_id is None:
            raise CapabilityExecutionError("PostToolUse response has no execution ID")
        return self.commit(execution_id)

    def _staged_sequence(self) -> dict[str, int]:
        """Map execution ID to the sequence of the event that staged it.

        ``_stage_result`` records ``capability.result_staged`` for every execution it stages, so
        this is the authoritative order in which pending work was produced.
        """

        order: dict[str, int] = {}
        try:
            events = self.session.store.events()
        except Exception:
            return order
        for event in events:
            if event.kind != "capability.result_staged":
                continue
            execution_id = event.payload.get("execution_id")
            if isinstance(execution_id, str):
                order.setdefault(execution_id, event.sequence)
        return order

    def recover_pending(self) -> list[str]:
        """Commit orphaned staged results in the order they were produced.

        Directory names are UUID4s, so sorting them replays crash recovery in an arbitrary order.
        That matters because commits apply state patches in sequence: recovering two executions
        backwards can leave the later patch overwritten by the earlier one. Order by the staging
        event instead. A directory with no staging event -- ``result.json`` is written just before
        the event is recorded, so a crash between the two is possible -- has no defensible position
        in that order, and is committed after everything sequenced, by name for determinism.
        """

        recovered: list[str] = []
        candidates = list(self.pending_root.iterdir() if self.pending_root.exists() else [])
        candidates.extend(
            path
            for path in (self.artifact_root.iterdir() if self.artifact_root.exists() else [])
            if path.name not in self.session.store.state.artifacts
        )
        order = self._staged_sequence()
        # Sequences are global event numbers, not a dense 0..n range, so the sentinel has to clear
        # the largest one actually present rather than the count of entries.
        unsequenced = max(order.values(), default=0) + 1
        for path in sorted(
            candidates, key=lambda item: (order.get(item.name, unsequenced), item.name)
        ):
            if path.is_dir() and (path / "result.json").is_file():
                if self.commit(path.name):
                    recovered.append(path.name)
        return recovered
