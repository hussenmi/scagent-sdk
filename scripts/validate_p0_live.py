"""Bounded live acceptance for the P0 raw-count and batch-identity corrections.

Runs real brokered compute on Iris to validate, without the model loop:

1. standalone H5AD raw-count source resolution and normalized-input refusal; and
2. batch-decision identity binding recorded by ``investigate_batch`` and honored by the
   ``batch_decision`` / ``integration_authorized`` floors (P0 #2), plus a real render of the
   batch-composition figure (P0 #3).

This is a live-compute check. It does not by itself establish model behavior or biological
generality. It intentionally avoids CellBender/scVI training.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from live_fixture_pipeline import build_standard_fixture

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.floors import FloorEvaluator
from scagent_sdk.session import AnalysisSession

REFUSAL_DEFAULT = "/data1/peerd/ibrahih3/SEACells/data/pbmc3k_processed.h5ad"
COUNTS_DEFAULT = (
    "/data1/peerd/ibrahih3/cs_agent/run_2026_07_14_154445/combined_truly_raw_annotated.h5ad"
)


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


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


async def _try_execute(
    executor: CapabilityExecutor,
    package: SkillPackage,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, str]:
    response = await executor.execute(package, _tool(package, tool_name), arguments)
    text = str(response["content"][0]["text"])
    if response.get("is_error"):
        return False, text
    executor.commit(str(response["structuredContent"]["scagent_execution_id"]))
    return True, text


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    sessions_root = args.sessions_root.resolve()
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    inspector = _package(packages, "inspect-dataset")
    counts = _package(packages, "single-cell-counts")
    batch = _package(packages, "batch-investigation")
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
    )
    report: dict[str, Any] = {}

    # --- P0 #1: refusal on a scaled/normalized X with no integer count source ---------
    refusal_session = AnalysisSession.create(sessions_root, title="P0 raw-count refusal")
    refusal_exec = CapabilityExecutor(refusal_session, environment_broker=broker)
    refusal_source = Path(args.refusal_input).expanduser().resolve()
    await _execute(
        refusal_exec,
        inspector,
        "inspect_dataset",
        {"path": str(refusal_source), "hash_mode": "full"},
    )
    ok, message = await _try_execute(
        refusal_exec,
        counts,
        "materialize_count_matrix",
        {"path": str(refusal_source), "counts_source": "auto"},
    )
    report["refusal"] = {
        "input": str(refusal_source),
        "prepared": ok,
        "message": message,
        "refused_non_count_input": (not ok) and "count source" in message,
    }

    # --- P0 #1 + #2 + #3: layer auto-selection, batch identity binding, real figure ---
    session = AnalysisSession.create(sessions_root, title="P0 raw-count + batch identity")
    executor = CapabilityExecutor(session, environment_broker=broker)
    counts_source = Path(args.counts_input).expanduser().resolve()
    await _execute(
        executor, inspector, "inspect_dataset", {"path": str(counts_source), "hash_mode": "full"}
    )
    prepared, prepared_path = await build_standard_fixture(
        execute=_execute,
        executor=executor,
        packages=packages,
        session_dir=session.directory,
        source=counts_source,
        random_seed=args.random_seed,
    )
    count_repr = session.store.state.facts["analysis"]["count_representation"]
    report["fixture"] = {
        "selected_source": count_repr.get("count_source"),
        "summary": prepared["summary"],
    }

    # A user turn before the investigation, so the recorded anchor is non-zero. This also proves the
    # turn index survives the broker -> worker JSON boundary: the anchor is computed inside the
    # isolated compute environment from the index the broker serialized.
    session.store.record(
        "runtime.turn_started",
        payload={"turn_id": "live-pre-evidence-turn", "user_prompt": "investigate the samples"},
        actor="user",
    )
    batch_env = await _execute(
        executor,
        batch,
        "investigate_batch",
        {
            "path": str(prepared_path),
            "batch_key": args.batch_key,
            # Must be the clustering the fixture just produced. This input also carries a legacy
            # "leiden" column, so the tool's default key would silently investigate the wrong one.
            "cluster_key": "leiden_res_0_8",
        },
    )
    evidence_id = batch_env["details"]["evidence_id"]
    recommendation = str(batch_env["details"]["recommendation"])
    evaluator_pre = FloorEvaluator()
    consultation_required = recommendation in (
        "cannot_determine_technical_vs_biological",
        "integration_optional_for_confirmed_replicates",
    )

    # A substantive decision must be refused while the user has said nothing, so the frictionless
    # path cannot record an unresolved question as resolved.
    unconsulted_ok, unconsulted_message = await _try_execute(
        executor,
        batch,
        "decide_batch_handling",
        {
            "evidence_id": evidence_id,
            "decision": "keep_uncorrected",
            "rationale": "Bounded live acceptance: attempt to keep uncorrected without asking.",
        },
    )

    # request_guidance records the open question but must leave the decision floor failing.
    pending_ok, pending_message = await _try_execute(
        executor,
        batch,
        "decide_batch_handling",
        {
            "evidence_id": evidence_id,
            "decision": "request_guidance",
            "rationale": "Bounded live acceptance: the batch question is with the user.",
        },
    )
    pending_floor = evaluator_pre.evaluate(session.store.state, "batch_decision")

    # The user answers in a real turn; only then does a substantive decision validate.
    session.store.record(
        "runtime.turn_started",
        payload={
            "turn_id": "live-consultation-turn",
            "user_prompt": "they are unrelated donors, keep them uncorrected",
        },
        actor="user",
    )
    consulted_ok, consulted_message = await _try_execute(
        executor,
        batch,
        "decide_batch_handling",
        {
            "evidence_id": evidence_id,
            "decision": "keep_uncorrected",
            "rationale": "Bounded live acceptance: recorded the user's own answer.",
            "user_consultation": {
                "turn_id": "live-consultation-turn",
                "question_asked": "Keep the samples combined uncorrected, integrate, or separate?",
                "user_answer": "they are unrelated donors, keep them uncorrected",
            },
        },
    )
    report["consultation"] = {
        "recommendation": recommendation,
        "consultation_required": consultation_required,
        "anchor_crossed_the_worker_boundary": (
            int(batch_env["details"].get("consultation_anchor_sequence") or 0) > 0
        ),
        "anchor_sequence": batch_env["details"].get("consultation_anchor_sequence"),
        "unconsulted_decision_refused": (not unconsulted_ok) if consultation_required else None,
        "unconsulted_message": unconsulted_message if not unconsulted_ok else None,
        "request_guidance_recorded": pending_ok,
        "request_guidance_message": pending_message if not pending_ok else None,
        "pending_blocks_finalization": pending_floor is not None,
        "pending_floor_reason": pending_floor.reason if pending_floor else None,
        "consulted_decision_accepted": consulted_ok,
        "consulted_message": consulted_message if not consulted_ok else None,
    }
    decision_env = {"summary": consulted_message}
    facts = session.store.state.facts
    analysis = facts["analysis"]
    batch_fact = facts["batch"]
    evaluator = FloorEvaluator()
    report["batch"] = {
        "summary": batch_env["summary"],
        "recorded_cell_set_id": batch_fact["evidence"].get("cell_set_id"),
        "recorded_count_representation_id": batch_fact["evidence"].get(
            "count_representation_id"
        ),
        "recorded_clustering_id": batch_fact["evidence"].get("clustering_id"),
        "identities_match_current": (
            batch_fact["evidence"].get("cell_set_id") == analysis["cell_set"]["id"]
            and batch_fact["evidence"].get("count_representation_id")
            == analysis["count_representation"]["id"]
            and batch_fact["evidence"].get("clustering_id") == analysis["clustering"]["id"]
        ),
        "batch_decision_floor_passes": evaluator.evaluate(session.store.state, "batch_decision")
        is None,
        "artifacts": batch_env["artifact_path"],
        "decision_summary": decision_env["summary"],
    }

    # Prove staleness: forge a new clustering identity and re-check the floor.
    session.store.record(
        "p0.validation.recluster",
        state_patch={"facts": {"analysis": {"clustering": {"id": "forced-new-clustering"}}}},
    )
    report["batch"]["batch_decision_floor_stale_after_reclustering"] = (
        evaluator.evaluate(session.store.state, "batch_decision") is not None
    )
    report["session_directory"] = str(session.directory)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts-input", default=COUNTS_DEFAULT)
    parser.add_argument("--refusal-input", default=REFUSAL_DEFAULT)
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--batch-key", default="sample")
    parser.add_argument("--random-seed", type=int, default=0)
    return parser


def main() -> int:
    print(json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
