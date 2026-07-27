"""Live Iris acceptance for artifact-centric tools and SCimilarity handoff."""

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

DEFAULT_INPUT = (
    "/data1/peerd/ibrahih3/cs_agent/test_data/crc_benchmark/CRC_KG136P_counts.h5ad"
)


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


def _file_path(envelope: dict[str, Any], name: str, key: str = "path") -> str:
    return str(next(item for item in envelope["files"] if item["name"] == name)[key])


async def _execute(
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
    return envelope


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
    )
    source = Path(args.input).expanduser().resolve()

    counts_session = AnalysisSession.create(
        args.sessions_root.resolve(), title="Standalone count materialization"
    )
    counts_executor = CapabilityExecutor(counts_session, environment_broker=broker)
    counts = await _execute(
        counts_executor,
        _package(packages, "single-cell-counts"),
        "materialize_count_matrix",
        {"path": str(source), "counts_source": args.counts_source},
    )

    scimilarity_session = AnalysisSession.create(
        args.sessions_root.resolve(), title="Standalone SCimilarity"
    )
    scimilarity_executor = CapabilityExecutor(
        scimilarity_session, environment_broker=broker
    )
    prediction = await _execute(
        scimilarity_executor,
        _package(packages, "scimilarity-annotation"),
        "run_scimilarity_annotation",
        {
            "path": str(source),
            "organism": args.organism,
            "counts_layer": None,
            "model_path": args.model_path,
            "min_gene_overlap": args.min_gene_overlap,
        },
    )
    prediction_path = (
        scimilarity_session.directory
        / str(prediction["artifact_path"])
        / "scimilarity-annotated.h5ad"
    )
    description = await _execute(
        scimilarity_executor,
        _package(packages, "inspect-dataset"),
        "describe_dataset",
        {"path": str(prediction_path)},
    )

    handoff_session = AnalysisSession.create(
        args.sessions_root.resolve(), title="Session-relative artifact handoff"
    )
    handoff_executor = CapabilityExecutor(handoff_session, environment_broker=broker)
    conversion = await _execute(
        handoff_executor,
        _package(packages, "inspect-dataset"),
        "convert_gene_ids",
        {
            "path": str(source),
            "organism": args.organism,
            "normalize_case": True,
        },
    )
    portable_converted_path = _file_path(
        conversion, "gene-symbols", "session_relative_path"
    )
    handoff_prediction = await _execute(
        handoff_executor,
        _package(packages, "scimilarity-annotation"),
        "run_scimilarity_annotation",
        {
            "path": portable_converted_path,
            "organism": args.organism,
            "counts_layer": "X",
            "model_path": args.model_path,
            "min_gene_overlap": args.min_gene_overlap,
        },
    )
    events = [
        json.loads(line)
        for line in (scimilarity_session.directory / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    denials = [
        event
        for event in events
        if event.get("event_type") == "capability.execution_failed"
        and "floor" in str(event.get("payload", {}).get("error", "")).lower()
    ]
    return {
        "input": str(source),
        "count_materialization": {
            "session": str(counts_session.directory),
            "summary": counts["summary"],
            "initial_state_was_blank": True,
        },
        "scimilarity": {
            "session": str(scimilarity_session.directory),
            "summary": prediction["summary"],
            "details": prediction["details"],
            "artifact": str(prediction_path),
            "description": description["details"],
            "initial_state_was_blank": True,
            "floor_denials": len(denials),
        },
        "artifact_handoff": {
            "session": str(handoff_session.directory),
            "portable_input_path": portable_converted_path,
            "canonical_input_path": _file_path(conversion, "gene-symbols"),
            "summary": handoff_prediction["summary"],
            "counts_layer": "X",
            "artifact": _file_path(
                handoff_prediction, "scimilarity-annotated-anndata"
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--organism", choices=("human", "mouse"), default="human")
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--counts-source", choices=("auto", "X", "raw", "layer"), default="auto")
    parser.add_argument("--min-gene-overlap", type=int, default=5000)
    return parser


def main() -> int:
    print(json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
