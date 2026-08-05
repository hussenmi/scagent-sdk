"""Assemble validated skill capabilities into isolated Claude SDK extensions."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.executor import CapabilityExecutor, execution_id_from_hook
from scagent_sdk.capabilities.instructions import instruction_sources, render_skill_instructions
from scagent_sdk.capabilities.readiness import (
    probe_environments,
    probe_packages,
    render_readiness_block,
)
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.capabilities.skill_plugin import materialize_skill_plugin
from scagent_sdk.execution.broker import EnvironmentBroker
from scagent_sdk.floors import FloorEvaluator, render_floor_change, render_outstanding_block
from scagent_sdk.runtime.claude import ClaudeRuntimeExtensions
from scagent_sdk.runtime.observer import (
    NullRuntimeObserver,
    RuntimeObserver,
    ToolActivity,
    ToolOutput,
)
from scagent_sdk.session import AnalysisSession


class CapabilityAssembler:
    def __init__(
        self,
        registry: CapabilityRegistry,
        session: AnalysisSession,
        *,
        sdk_module: Any | None = None,
        observer: RuntimeObserver | None = None,
        environment_broker: EnvironmentBroker | None = None,
    ):
        self.registry = registry
        self.session = session
        self.environment_broker = environment_broker
        self.executor = CapabilityExecutor(session, environment_broker=environment_broker)
        self._sdk_module = sdk_module
        self.observer = observer or NullRuntimeObserver()
        self.floors = FloorEvaluator()
        # Every floor any registered tool declares, and the ones unmet as of the last committed
        # capability. Kept so a step that makes evidence stale says so when it happens instead of
        # when something is finally blocked by it.
        self._known_floors: tuple[str, ...] = ()
        self._unmet_floors: frozenset[str] = frozenset()

    def _sdk(self) -> Any:
        return self._sdk_module or importlib.import_module("claude_agent_sdk")

    def assemble(self) -> ClaudeRuntimeExtensions:
        sdk = self._sdk()
        packages = self.registry.discover()
        discovered_skills = self.registry.skills()
        self.executor.recover_pending()
        mcp_servers: dict[str, Any] = {}
        allowed_tools: list[str] = []
        post_hooks: list[Any] = []
        pre_hooks: list[Any] = []
        for package in packages:
            server_name = package.manifest.skill_id.replace("-", "_")
            sdk_tools: list[Any] = []
            for tool in package.manifest.tools:
                handler = self._handler(package, tool)
                sdk_tool = sdk.tool(tool.name, tool.description, tool.input_schema)(handler)
                sdk_tools.append(sdk_tool)
                full_name = f"mcp__{server_name}__{tool.name}"
                allowed_tools.append(full_name)
                post_hooks.append(
                    sdk.HookMatcher(
                        matcher=full_name,
                        hooks=[self._post_tool_hook(package, tool)],
                    )
                )
                if tool.floors:
                    pre_hooks.append(
                        sdk.HookMatcher(
                            matcher=full_name,
                            hooks=[self._pre_tool_hook(tool.floors)],
                        )
                    )
            mcp_servers[server_name] = sdk.create_sdk_mcp_server(
                server_name, version=package.manifest.version, tools=sdk_tools
            )
        hooks: dict[str, list[Any]] = {}
        if pre_hooks:
            hooks["PreToolUse"] = pre_hooks
        if post_hooks:
            hooks["PostToolUse"] = post_hooks
        self._known_floors = tuple(
            dict.fromkeys(
                floor
                for package in packages
                for tool in package.manifest.tools
                for floor in tool.floors
            )
        )
        outstanding = self.floors.failures(self.session.store.state, self._known_floors)
        self._unmet_floors = frozenset(failure.floor for failure in outstanding)
        readiness = probe_packages(packages, broker=self.environment_broker)
        environments = probe_environments(packages, broker=self.environment_broker)
        if readiness or environments:
            self.session.store.record(
                "capability.readiness_probed",
                payload={
                    "reports": [report.to_dict() for report in readiness],
                    "environments": [report.to_dict() for report in environments],
                },
            )
        self.session.store.record(
            "capability.registry_assembled",
            payload={
                "skills_root": str(self.registry.skills_root),
                "skills": [
                    {
                        "id": package.manifest.skill_id,
                        "version": package.manifest.version,
                        "fingerprint": package.fingerprint,
                        "tools": [tool.name for tool in package.manifest.tools],
                    }
                    for package in packages
                ],
                "allowed_tools": allowed_tools,
                "enabled_skills": [
                    {
                        "name": skill.name,
                        "fingerprint": skill.fingerprint,
                        "executable": skill.executable,
                    }
                    for skill in discovered_skills
                ],
            },
        )
        plugin_root = materialize_skill_plugin(
            self.registry.skills_root, self.session.directory / "runtime" / "skills-plugin"
        )
        instructions = render_skill_instructions(discovered_skills)
        self.session.store.record(
            "capability.instructions_injected",
            payload={
                "bytes": len(instructions),
                "skills": instruction_sources(discovered_skills),
            },
        )
        suffix = "\n\n".join(
            block
            for block in (
                render_readiness_block(readiness, environments),
                render_outstanding_block(outstanding),
                instructions,
            )
            if block
        )
        return ClaudeRuntimeExtensions(
            mcp_servers=mcp_servers,
            allowed_tools=tuple(allowed_tools),
            hooks=hooks,
            skills=tuple(skill.name for skill in discovered_skills),
            # `Skill` is the one built-in tool this runtime grants: without it the skill
            # instructions beside every capability never reach the model, leaving it to work from
            # tool schemas alone. Deny-by-default still holds for every other built-in tool.
            tools=("Skill",),
            plugins=({"type": "local", "path": str(plugin_root)},),
            include_hook_events=True,
            system_prompt_suffix=suffix,
        )

    def _handler(self, package: SkillPackage, tool: Any) -> Any:
        async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            activity = ToolActivity(
                tool_name=tool.name,
                label=tool.activity_label or tool.name.replace("_", " ").title(),
                skill_id=package.manifest.skill_id,
            )
            self.observer.on_tool_started(activity)
            response = await self.executor.execute(
                package,
                tool,
                arguments,
                progress_callback=lambda progress: self.observer.on_tool_progress(
                    activity, progress
                ),
            )
            if response.get("is_error"):
                # The full detail stays in the tool result the model receives; the terminal only
                # needs the concise one-line summary.
                summary = response.get("error_summary")
                if not summary:
                    content = response.get("content") or []
                    summary = (
                        str(content[0].get("text", "capability failed"))
                        if content
                        else "capability failed"
                    )
                self.observer.on_tool_failed(activity, str(summary))
            else:
                structured = response.get("structuredContent") or {}
                self.observer.on_tool_finished(activity, structured.get("summary"))
            return response

        return handler

    def _pre_tool_hook(self, floors: tuple[str, ...]) -> Any:
        async def hook(
            _input_data: dict[str, Any],
            _tool_use_id: str | None,
            _context: dict[str, Any],
        ) -> dict[str, Any]:
            failures = self.floors.failures(self.session.store.state, floors)
            if not failures:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            reason = " ".join(
                f"[{failure.floor}] {failure.reason} {failure.remediation}" for failure in failures
            )
            self.session.store.record(
                "floor.denied",
                payload={
                    "floors": [failure.floor for failure in failures],
                    "reason": reason,
                },
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        return hook

    @staticmethod
    def _artifact_description(name: str, media_type: str, title: str = "") -> str:
        # A title the skill declared beats anything inferred from the filename here: the skill knows
        # which embedding, clustering, or sample the file shows, and the name deliberately does not.
        if title.strip():
            return title.strip()
        normalized = name.replace("_", "-").casefold()
        explicit = {
            "batch-composition": "Sample composition across the compared cell groups",
            "final-annotation-umap": "Final cell-type labels on the analysis embedding",
            "final-label-counts": "Number of cells assigned to each final cell type",
            "analysis-report": "Final scientific findings, decisions, evidence, and caveats",
            "analysis-recipe": "Exact ordered capability calls for replaying the analysis",
            "final-annotated-anndata": "Final annotated single-cell dataset",
            "final-labels": "Final cluster labels and supporting evidence",
        }
        if normalized in explicit:
            return explicit[normalized]
        if normalized.startswith("embedding-grid-"):
            group = normalized.removeprefix("embedding-grid-").replace("-", " ")
            return f"Embedding split into panels by {group}"
        if normalized.startswith("embedding"):
            return "Two-dimensional embedding colored by the requested cell metadata"
        if "pca-variance" in normalized:
            return "Variance explained by successive principal components"
        if normalized.startswith("qc-"):
            return "Cell-level quality-control diagnostic"
        if normalized.startswith("cluster-qc"):
            return "Cluster-level quality-control diagnostic"
        if media_type.startswith("image/"):
            return name.replace("-", " ").replace("_", " ").strip().capitalize()
        return name.replace("-", " ").replace("_", " ").strip().capitalize()

    def _post_tool_hook(self, package: SkillPackage, tool: Any) -> Any:
        async def hook(
            input_data: dict[str, Any],
            _tool_use_id: str | None,
            _context: dict[str, Any],
        ) -> dict[str, Any]:
            self.executor.commit_from_hook(input_data)
            execution_id = execution_id_from_hook(input_data)
            activity = ToolActivity(
                tool_name=tool.name,
                label=tool.activity_label or tool.name.replace("_", " ").title(),
                skill_id=package.manifest.skill_id,
            )
            artifact = (
                self.session.store.state.artifacts.get(execution_id)
                if execution_id is not None
                else None
            )
            if isinstance(artifact, dict):
                root = self.session.directory / str(artifact.get("path", ""))
                outputs = tuple(
                    ToolOutput(
                        name=str(item.get("name", Path(str(item.get("relative_path", ""))).stem)),
                        path=str((root / str(item.get("relative_path", ""))).resolve()),
                        media_type=str(item.get("media_type", "application/octet-stream")),
                        description=self._artifact_description(
                            str(
                                item.get(
                                    "name",
                                    Path(str(item.get("relative_path", ""))).stem,
                                )
                            ),
                            str(item.get("media_type", "application/octet-stream")),
                            str(item.get("title", "")),
                        ),
                    )
                    for item in artifact.get("files", [])
                    if isinstance(item, dict) and item.get("relative_path")
                )
                if outputs:
                    self.observer.on_tool_outputs(
                        activity,
                        outputs,
                        index_path=str((self.session.directory / "outputs.md").resolve()),
                    )
            note = self._floor_change_note()
            if not note:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": note,
                }
            }

        return hook

    def _floor_change_note(self) -> str:
        """Report what this committed step made outstanding, or stopped being outstanding.

        Evaluated after the commit, so it reflects the state the next tool call will be judged
        against. Silent when nothing changed: a per-call recital of every requirement would read as
        a pipeline to work through, which is the opposite of the intent.
        """

        if not self._known_floors:
            return ""
        outstanding = self.floors.failures(self.session.store.state, self._known_floors)
        note = render_floor_change(self._unmet_floors, outstanding)
        self._unmet_floors = frozenset(failure.floor for failure in outstanding)
        return note
