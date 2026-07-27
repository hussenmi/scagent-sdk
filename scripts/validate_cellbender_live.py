"""Exercise the CellBender capability through the production executor on Iris.

This is an explicit acceptance harness, not a normal test-suite member. It creates a
durable scientific session, commits each staged result through the same two-phase path
used by the agent, and prints a compact machine-readable summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.session import AnalysisSession


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


async def _execute_and_commit(
    executor: CapabilityExecutor,
    package: SkillPackage,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    response = await executor.execute(package, _tool(package, tool_name), arguments)
    if response.get("is_error"):
        raise RuntimeError(str(response["content"][0]["text"]))
    envelope = response["structuredContent"]
    executor.commit(str(envelope["scagent_execution_id"]))
    return {
        "execution_id": envelope["scagent_execution_id"],
        "summary": envelope["summary"],
        "artifact_path": envelope["artifact_path"],
        "files": [item["relative_path"] for item in envelope["files"]],
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    source = args.input.expanduser().resolve()
    sessions_root = args.sessions_root.expanduser().resolve()
    session = AnalysisSession.create(sessions_root, title=args.title)
    packages = CapabilityRegistry(project_root / ".claude" / "skills").discover()
    inspector = _package(packages, "inspect-dataset")
    cellbender = _package(packages, "cellbender-background-removal")
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(project_root / "configs" / "environments" / "iris.toml")
    )
    executor = CapabilityExecutor(session, environment_broker=broker)

    runs = [
        await _execute_and_commit(
            executor,
            inspector,
            "inspect_dataset",
            {"path": str(source), "hash_mode": "full"},
        ),
        await _execute_and_commit(
            executor,
            cellbender,
            "validate_cellbender_input",
            {"path": str(source)},
        ),
    ]
    validation = session.store.state.facts["ambient_background"]["input_validation"]
    if args.run:
        if validation["status"] != "suitable":
            raise RuntimeError(
                "CellBender execution refused because input validation was unsuitable"
            )
        runs.append(
            await _execute_and_commit(
                executor,
                cellbender,
                "remove_ambient_background",
                {
                    "path": str(source),
                    "selected_output": args.selected_output,
                    "timeout_seconds": args.timeout_seconds,
                },
            )
        )

    state = session.store.state
    return {
        "scientific_session_id": session.session_id,
        "session_directory": str(session.directory),
        "input": str(source),
        "validation_status": validation["status"],
        "validation_reasons": validation["reasons"],
        "state_revision": state.revision,
        "active_dataset": state.facts.get("dataset"),
        "ambient_background": state.facts.get("ambient_background"),
        "runs": runs,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--title", default="CellBender live acceptance")
    parser.add_argument("--run", action="store_true", help="Run CellBender after validation")
    parser.add_argument("--selected-output", choices=("filtered", "full"), default="filtered")
    parser.add_argument("--timeout-seconds", type=int, default=21000)
    return parser


def main() -> int:
    result = asyncio.run(_run(_parser().parse_args()))
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
