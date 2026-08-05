"""Deterministic tests for the read-only query-analysis-state capability.

The query logic is pure standard-library Python over plain dictionaries, so it is exercised here
from the control plane with a fabricated session directory and a minimal context stand-in -- no
compute runtime, no live session. These tests pin the behaviour that makes a structured query worth
more than a raw grep: lineage-aware currency labelling, event-ordered step history, and decision
enrichment from the user-turn index.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_MODULE_PATH = (
    Path(__file__).parents[2]
    / ".claude"
    / "skills"
    / "query-analysis-state"
    / "scripts"
    / "query.py"
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("query_analysis_state_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


Q = _load()


# --------------------------------------------------------------------------------------------------
# Fixtures: a small but realistic session on disk
# --------------------------------------------------------------------------------------------------


def _artifact(
    execution_id: str, tool: str, skill: str, *, arguments: dict[str, Any], summary: str = ""
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "skill_id": skill,
        "skill_version": "0.1.0",
        "tool_name": tool,
        "arguments": arguments,
        "summary": summary,
        "path": f"artifacts/capabilities/{tool}--{execution_id}",
        "files": [],
    }


def _write_session(
    tmp_path: Path,
    *,
    facts: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    decisions: dict[str, Any] | None = None,
    lineage: dict[str, Any] | None = None,
    commit_order: list[tuple[str, int, str]] | None = None,
) -> Path:
    session_dir = tmp_path / "session"
    (session_dir / "artifacts").mkdir(parents=True)
    state = {
        "schema_version": 3,
        "session_id": "run_test",
        "created_at": "2026-01-01T00:00:00.000000+00:00",
        "updated_at": "2026-01-01T00:00:00.000000+00:00",
        "revision": 1,
        "last_event_sequence": 1,
        "facts": facts or {},
        "artifacts": artifacts or {},
        "decisions": decisions or {},
        "runtime": {"active": None, "history": []},
        "lineage": lineage or {"active_execution_id": None, "nodes": {}},
        "retention": {"schema_version": 1, "branches": {}, "artifact_roles": {}, "operations": {}},
    }
    (session_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    lines = []
    for execution_id, sequence, timestamp in commit_order or []:
        lines.append(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_id": f"evt-{sequence}",
                    "session_id": "run_test",
                    "sequence": sequence,
                    "timestamp": timestamp,
                    "kind": "capability.result_committed",
                    "actor": "harness",
                    "payload": {"execution_id": execution_id},
                    "state_patch": {},
                }
            )
        )
    (session_dir / "events.jsonl").write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )
    return session_dir


def _context(
    session_dir: Path,
    tmp_path: Path,
    *,
    facts: dict[str, Any],
    lineage: dict[str, Any],
    user_turns: tuple[dict[str, Any], ...] = (),
) -> Any:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    return SimpleNamespace(
        session_dir=session_dir,
        staging_dir=staging,
        state_facts=facts,
        state_lineage=lineage,
        user_turns=user_turns,
    )


# --------------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------------


def test_flatten_bounds_and_marks_truncation() -> None:
    flat = Q._flatten({"a": {"b": 1, "c": [2, 3]}, "d": "x"})
    assert flat == {"a.b": 1, "a.c[0]": 2, "a.c[1]": 3, "d": "x"}

    big = {str(i): i for i in range(Q._MAX_FLATTENED_LEAVES + 50)}
    bounded = Q._flatten(big)
    assert bounded.get("__truncated__") is True
    assert len(bounded) <= Q._MAX_FLATTENED_LEAVES + 1


def test_ancestry_follows_parents_and_survives_a_cycle() -> None:
    nodes = {
        "c": {"parent_execution_id": "b"},
        "b": {"parent_execution_id": "a"},
        "a": {"parent_execution_id": None},
        "loop1": {"parent_execution_id": "loop2"},
        "loop2": {"parent_execution_id": "loop1"},
    }
    assert Q._ancestry(nodes, "c") == ["c", "b", "a"]
    # A cyclic chain terminates rather than hanging.
    assert set(Q._ancestry(nodes, "loop1")) == {"loop1", "loop2"}


def test_build_history_orders_by_commit_sequence() -> None:
    artifacts = {
        "late": _artifact("late", "cluster_cells", "single-cell-clustering", arguments={}),
        "early": _artifact("early", "materialize_count_matrix", "single-cell-counts", arguments={}),
    }
    index = {
        "late": {"sequence": 9, "timestamp": "t9"},
        "early": {"sequence": 2, "timestamp": "t2"},
    }
    history = Q.build_history(artifacts, index)
    assert [item["execution_id"] for item in history] == ["early", "late"]
    assert history[0]["timestamp"] == "t2"


# --------------------------------------------------------------------------------------------------
# find_steps: the lineage-aware currency labelling that beats grep
# --------------------------------------------------------------------------------------------------


def test_find_steps_labels_active_line_and_sibling_branch() -> None:
    # A -> B is the active line; A -> S is an alternative (sibling) branch. A read-only plot P
    # created no version.
    lineage = {
        "active_execution_id": "B",
        "nodes": {
            "A": {
                "parent_execution_id": None,
                "created_by": {"tool_name": "materialize_count_matrix"},
            },
            "B": {"parent_execution_id": "A", "created_by": {"tool_name": "cluster_cells"}},
            "S": {"parent_execution_id": "A", "created_by": {"tool_name": "cluster_cells"}},
        },
    }
    history = [
        {
            "execution_id": "A",
            "sequence": 1,
            "tool_name": "materialize_count_matrix",
            "arguments": {},
            "summary": "",
        },
        {
            "execution_id": "B",
            "sequence": 2,
            "tool_name": "cluster_cells",
            "arguments": {"resolution": 1.0},
            "summary": "",
        },
        {
            "execution_id": "S",
            "sequence": 3,
            "tool_name": "cluster_cells",
            "arguments": {"resolution": 2.0},
            "summary": "",
        },
        {
            "execution_id": "P",
            "sequence": 4,
            "tool_name": "plot_embedding",
            "arguments": {},
            "summary": "",
        },
    ]
    result = Q.find_steps(history, lineage)
    placement = {step["execution_id"]: step for step in result["steps"]}
    assert placement["B"]["is_version"] is True and placement["B"]["on_active_line"] is True
    assert placement["S"]["is_version"] is True and placement["S"]["on_active_line"] is False
    assert placement["A"]["on_active_line"] is True
    # A read-only step creates no version, so no currency is claimed for it.
    assert placement["P"]["is_version"] is False and placement["P"]["on_active_line"] is None
    # Most-recent first.
    assert [step["execution_id"] for step in result["steps"]] == ["P", "S", "B", "A"]


def test_find_steps_filters_by_tool_and_query_and_limit() -> None:
    lineage = {"active_execution_id": None, "nodes": {}}
    history = [
        {
            "execution_id": "1",
            "sequence": 1,
            "tool_name": "cluster_cells",
            "arguments": {"resolution": 1.0},
            "summary": "leiden",
        },
        {
            "execution_id": "2",
            "sequence": 2,
            "tool_name": "cluster_cells",
            "arguments": {"resolution": 2.0},
            "summary": "leiden",
        },
        {
            "execution_id": "3",
            "sequence": 3,
            "tool_name": "calculate_cell_qc",
            "arguments": {},
            "summary": "mito",
        },
    ]
    by_tool = Q.find_steps(history, lineage, tool_name="cluster")
    assert by_tool["total_matched"] == 2
    by_query = Q.find_steps(history, lineage, query="mito")
    assert by_query["total_matched"] == 1 and by_query["steps"][0]["execution_id"] == "3"
    # A query matches inside recorded arguments, not just the summary.
    by_arg = Q.find_steps(history, lineage, query="2.0")
    assert [step["execution_id"] for step in by_arg["steps"]] == ["2"]
    limited = Q.find_steps(history, lineage, tool_name="cluster", limit=1)
    assert limited["total_matched"] == 2 and limited["returned"] == 1


# --------------------------------------------------------------------------------------------------
# summarize
# --------------------------------------------------------------------------------------------------


def test_summarize_reports_shape_active_version_and_areas() -> None:
    facts = {
        "analysis": {
            "dataset_revision": {"n_cells": 1000, "n_genes": 2000},
            "clustering": {"n_clusters": 12, "resolution": 1.0},
        },
        "dataset": {"path": "/data/example.h5ad"},
        "finalization": {"status": "in_progress"},
    }
    lineage = {
        "active_execution_id": "B",
        "nodes": {
            "A": {
                "parent_execution_id": None,
                "created_by": {"tool_name": "materialize_count_matrix"},
            },
            "B": {"parent_execution_id": "A", "created_by": {"tool_name": "cluster_cells"}},
            "S": {"parent_execution_id": "A", "created_by": {"tool_name": "cluster_cells"}},
        },
    }
    out = Q.summarize(facts, lineage, history=[{"execution_id": "A"}, {"execution_id": "B"}])
    assert out["dataset"]["working_shape"] == {"n_cells": 1000, "n_genes": 2000}
    assert out["dataset"]["source_path"] == "/data/example.h5ad"
    assert out["active_version"]["short_id"] == "B"
    assert out["active_version"]["produced_by"] == "cluster_cells"
    assert out["versions"] == {"total": 3, "on_active_line": 2, "alternatives": 1}
    assert out["highlights"] == {"n_clusters": 12, "finalization_status": "in_progress"}
    assert "analysis.clustering.resolution" in out["facts"]
    assert out["recorded_fact_areas"] == ["analysis", "dataset", "finalization"]


# --------------------------------------------------------------------------------------------------
# list_decisions
# --------------------------------------------------------------------------------------------------


def test_list_decisions_surfaces_rationale_and_resolves_authorizing_turn() -> None:
    decisions = {
        "batch_handling": {
            "choice": "integrate",
            "rationale": "recurring sample-associated structure across donors",
            "user_consultation": {
                "turn_id": "turn-7",
                "question_asked": "integrate donors?",
                "user_answer": "yes integrate",
            },
        }
    }
    user_turns = (
        {"turn_id": "turn-1", "sequence": 1, "user_prompt": "analyze this"},
        {"turn_id": "turn-7", "sequence": 7, "user_prompt": "yes integrate"},
    )
    out = Q.list_decisions(decisions, user_turns)
    assert out["decision_count"] == 1
    record = out["decisions"][0]
    assert record["decision"] == "batch_handling"
    assert record["rationale"].startswith("recurring")
    assert record["recorded_answer"] == "integrate"
    assert record["authorizing_turn"] == {"turn_id": "turn-7", "user_prompt": "yes integrate"}


def test_list_decisions_handles_unknown_turn_reference() -> None:
    decisions = {"x": {"turn_id": "missing"}}
    out = Q.list_decisions(decisions, ())
    assert out["decisions"][0]["authorizing_turn"] == {"turn_id": "missing", "user_prompt": None}


# --------------------------------------------------------------------------------------------------
# Entrypoints end-to-end against a session on disk
# --------------------------------------------------------------------------------------------------


def test_entrypoints_read_disk_and_write_artifacts(tmp_path: Path) -> None:
    facts = {"analysis": {"clustering": {"n_clusters": 5}}, "dataset": {"path": "/d.h5ad"}}
    lineage = {
        "active_execution_id": "b",
        "nodes": {
            "a": {
                "parent_execution_id": None,
                "created_by": {"tool_name": "materialize_count_matrix"},
            },
            "b": {"parent_execution_id": "a", "created_by": {"tool_name": "cluster_cells"}},
        },
    }
    artifacts = {
        "a": _artifact(
            "a", "materialize_count_matrix", "single-cell-counts", arguments={"layer": "counts"}
        ),
        "b": _artifact(
            "b",
            "cluster_cells",
            "single-cell-clustering",
            arguments={"resolution": 1.0},
            summary="12 clusters",
        ),
    }
    decisions = {"batch_handling": {"choice": "keep_uncorrected", "rationale": "single sample"}}
    session_dir = _write_session(
        tmp_path,
        facts=facts,
        artifacts=artifacts,
        decisions=decisions,
        lineage=lineage,
        commit_order=[("a", 3, "t3"), ("b", 6, "t6")],
    )
    context = _context(session_dir, tmp_path, facts=facts, lineage=lineage)

    summary = Q.summarize_analysis_state({}, context)
    assert "5" in json.dumps(summary["details"]["highlights"])
    assert (context.staging_dir / "analysis-state-summary.json").is_file()

    steps = Q.find_analysis_steps({"tool_name": "cluster"}, context)
    assert steps["details"]["total_matched"] == 1
    step = steps["details"]["steps"][0]
    assert step["execution_id"] == "b" and step["arguments"] == {"resolution": 1.0}
    assert step["on_active_line"] is True

    # Ordering comes from the event log, so the newest committed step sorts first.
    all_steps = Q.find_analysis_steps({}, context)
    assert [item["execution_id"] for item in all_steps["details"]["steps"]] == ["b", "a"]

    decisions_out = Q.list_recorded_decisions({}, context)
    assert decisions_out["details"]["decision_count"] == 1
    assert decisions_out["details"]["decisions"][0]["rationale"] == "single sample"
    assert (context.staging_dir / "recorded-decisions.json").is_file()


def test_find_steps_empty_when_nothing_committed(tmp_path: Path) -> None:
    session_dir = _write_session(tmp_path)
    context = _context(
        session_dir, tmp_path, facts={}, lineage={"active_execution_id": None, "nodes": {}}
    )
    result = Q.find_analysis_steps({"tool_name": "doublet"}, context)
    assert result["details"]["total_matched"] == 0
    assert "not run in this session" in result["summary"]
