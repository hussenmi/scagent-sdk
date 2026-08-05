"""Query the durable session as the authoritative record of the analysis.

These entrypoints answer three questions about an in-progress analysis directly from committed
provenance, so the model never has to reconstruct them from conversation memory:

- ``summarize_analysis_state`` -- where the analysis stands right now: dataset identity and working
  shape, the active version, and every recorded fact area with its scalar leaves.
- ``find_analysis_steps`` -- whether a step ran and with exactly which arguments, ordered by the
  event log, each annotated with where it sits relative to the active line of descent.
- ``list_recorded_decisions`` -- the explicit scientific/human decisions and their rationale, with
  the authorizing user turn resolved from the event index when one is cited.

All three are strictly read-only. They read the same authoritative sources every other part of the
runtime does -- ``state.json`` (facts, artifacts, decisions), the ``events.jsonl`` commit log for
ordering, and the executor-owned lineage forest -- and never mutate session state.

The logic here is pure standard-library Python operating on plain dictionaries, so it runs in the
control plane with no compute runtime and is unit-testable without a live session. Lineage awareness
is the reason a structured query beats a raw ``grep`` of ``state.json``: a bare text search is blind
to which version is active, so it can return a value from a superseded sibling branch and present it
as current. These tools resolve the active line of descent and label every step and version against
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# A summary or a decision record flattens the recorded facts to dotted scalar leaves. The cap stops
# a pathological state (thousands of per-cell entries wrongly written to facts) from producing an
# unbounded result; the honest thing when it trips is to say so, not to silently truncate in a way
# the reader cannot see.
_MAX_FLATTENED_LEAVES = 400
_DEFAULT_STEP_LIMIT = 25
_MAX_STEP_LIMIT = 200

# Leaf names that carry the human meaning of a decision, surfaced explicitly rather than left buried
# among the flattened leaves. Matched on the final dotted segment, case-folded.
_RATIONALE_LEAVES = frozenset({"rationale", "reason", "justification"})
_ANSWER_LEAVES = frozenset({"user_answer", "answer", "choice", "decision"})
_QUESTION_LEAVES = frozenset({"question_asked", "question"})
_TURN_LEAVES = frozenset({"turn_id"})


# --------------------------------------------------------------------------------------------------
# Shared readers and pure helpers
# --------------------------------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _flatten(
    value: Any, *, prefix: str = "", budget: int = _MAX_FLATTENED_LEAVES
) -> dict[str, Any]:
    """Flatten a nested fact/decision tree to dotted keys over scalar leaves, bounded.

    Lists are indexed (``key[0]``). A ``__truncated__`` sentinel is added when the leaf budget is
    reached, so a reader can never mistake a cut-short view for the whole recorded state.
    """

    out: dict[str, Any] = {}

    def walk(node: Any, path: str) -> None:
        if len(out) >= budget:
            out.setdefault("__truncated__", True)
            return
        if isinstance(node, dict):
            if not node:
                out[path] = {}
                return
            for key in sorted(node, key=str):
                walk(node[key], f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            if not node:
                out[path] = []
                return
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        else:
            out[path] = node

    walk(value, prefix)
    return out


def _ancestry(nodes: dict[str, Any], start: str | None) -> list[str]:
    """The active line of descent, active head first. Guards against a cyclic parent chain."""

    chain: list[str] = []
    seen: set[str] = set()
    current = start
    while isinstance(current, str) and current not in seen and current in nodes:
        seen.add(current)
        chain.append(current)
        node = nodes[current]
        current = node.get("parent_execution_id") if isinstance(node, dict) else None
    return chain


def _forest(lineage: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    nodes = lineage.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    active = lineage.get("active_execution_id")
    return nodes, active if isinstance(active, str) else None


def _step_label(node: dict[str, Any]) -> str:
    created = node.get("created_by")
    if isinstance(created, dict):
        tool = created.get("tool_name")
        if isinstance(tool, str) and tool:
            return tool
    return "unknown step"


def _nested_int(facts: dict[str, Any], *keys: str) -> int | None:
    node: Any = facts
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return int(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def _working_shape(facts: dict[str, Any]) -> dict[str, int] | None:
    """The shape of the working object, preferring the revision facts over the original input."""

    cells = _nested_int(facts, "analysis", "dataset_revision", "n_cells")
    genes = _nested_int(facts, "analysis", "dataset_revision", "n_genes")
    if cells is None or genes is None:
        cells = _nested_int(facts, "dataset_contents", "shape", "n_obs")
        genes = _nested_int(facts, "dataset_contents", "shape", "n_vars")
    if cells is None or genes is None:
        return None
    return {"n_cells": cells, "n_genes": genes}


def _dataset_path(facts: dict[str, Any], history: list[dict[str, Any]]) -> str | None:
    dataset = facts.get("dataset")
    if isinstance(dataset, dict) and isinstance(dataset.get("path"), str):
        return str(dataset["path"])
    revision = facts.get("analysis")
    if isinstance(revision, dict):
        revision = revision.get("dataset_revision")
        if isinstance(revision, dict) and isinstance(revision.get("source_path"), str):
            return str(revision["source_path"])
    for item in history:
        arguments = item.get("arguments")
        if isinstance(arguments, dict) and isinstance(arguments.get("path"), str):
            return str(arguments["path"])
    return None


def _event_index(session_dir: Path) -> dict[str, dict[str, Any]]:
    """Map each committed execution to its commit event's ordering.

    ``state.json`` holds the artifact records but not their order; the event log owns sequence and
    timestamp. Only turn-start-free commit lines are parsed, mirroring ``analysis-notebook``.
    """

    index: dict[str, dict[str, Any]] = {}
    events_path = session_dir / "events.jsonl"
    if not events_path.is_file():
        return index
    try:
        handle = events_path.open(encoding="utf-8")
    except OSError:
        return index
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("kind") != "capability.result_committed":
                continue
            payload = event.get("payload")
            execution_id = payload.get("execution_id") if isinstance(payload, dict) else None
            if isinstance(execution_id, str):
                index[execution_id] = {
                    "sequence": int(event.get("sequence", 0) or 0),
                    "timestamp": event.get("timestamp"),
                }
    return index


def build_history(
    artifacts: dict[str, Any], event_index: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Committed capability executions, ordered by their commit event (oldest first)."""

    history: list[dict[str, Any]] = []
    for execution_id, raw in artifacts.items():
        if not isinstance(raw, dict):
            continue
        order = event_index.get(str(execution_id), {})
        history.append(
            {
                "sequence": int(order.get("sequence", 0) or 0),
                "timestamp": order.get("timestamp"),
                "execution_id": str(execution_id),
                "skill_id": raw.get("skill_id"),
                "skill_version": raw.get("skill_version"),
                "tool_name": raw.get("tool_name"),
                "arguments": raw.get("arguments", {}),
                "summary": raw.get("summary", ""),
                "artifact_path": raw.get("path", f"artifacts/capabilities/{execution_id}"),
                "files": raw.get("files", []),
            }
        )
    return sorted(history, key=lambda item: (item["sequence"], item["execution_id"]))


# --------------------------------------------------------------------------------------------------
# Pure query logic
# --------------------------------------------------------------------------------------------------


def summarize(
    facts: dict[str, Any], lineage: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    """Structured, lineage-aware overview of where the analysis stands."""

    nodes, active = _forest(lineage)
    active_line = _ancestry(nodes, active)
    active_line_set = set(active_line)

    active_version: dict[str, Any] | None = None
    if active is not None and active in nodes and isinstance(nodes[active], dict):
        active_version = {
            "version_id": active,
            "short_id": active[:8],
            "produced_by": _step_label(nodes[active]),
        }

    versions = {
        "total": len(nodes),
        "on_active_line": len(active_line),
        "alternatives": sum(
            1 for key in nodes if isinstance(nodes.get(key), dict) and key not in active_line_set
        ),
    }

    highlights: dict[str, Any] = {}
    n_clusters = _nested_int(facts, "analysis", "clustering", "n_clusters")
    if n_clusters is not None:
        highlights["n_clusters"] = n_clusters
    finalization = facts.get("finalization")
    if isinstance(finalization, dict) and finalization.get("status") is not None:
        highlights["finalization_status"] = finalization.get("status")

    flattened = _flatten(facts)
    truncated = bool(flattened.pop("__truncated__", False))

    return {
        "dataset": {
            "source_path": _dataset_path(facts, history),
            "working_shape": _working_shape(facts),
        },
        "active_version": active_version,
        "versions": versions,
        "recorded_fact_areas": sorted(str(key) for key in facts),
        "step_count": len(history),
        "highlights": highlights,
        "facts": flattened,
        "facts_truncated": truncated,
    }


def _matches(step: dict[str, Any], query: str) -> bool:
    haystack = " ".join(
        str(part)
        for part in (
            step.get("tool_name"),
            step.get("skill_id"),
            step.get("summary"),
            json.dumps(step.get("arguments", {}), sort_keys=True, default=str),
        )
        if part is not None
    )
    return query in haystack.casefold()


def _lineage_placement(
    execution_id: str, nodes: dict[str, Any], active_line: set[str]
) -> dict[str, Any]:
    """Where a step sits relative to the active line.

    A transforming step is a lineage node, so ``on_active_line`` is precise. A read-only step (QC
    measurement, plotting, evaluation) creates no version; ``on_active_line`` is ``null`` for it
    rather than a fabricated currency claim.
    """

    if execution_id in nodes:
        return {"is_version": True, "on_active_line": execution_id in active_line}
    return {"is_version": False, "on_active_line": None}


def find_steps(
    history: list[dict[str, Any]],
    lineage: dict[str, Any],
    *,
    query: str | None = None,
    tool_name: str | None = None,
    limit: int = _DEFAULT_STEP_LIMIT,
) -> dict[str, Any]:
    """Committed steps matching the filters, most recent first, annotated with lineage placement."""

    nodes, active = _forest(lineage)
    active_line = set(_ancestry(nodes, active))

    normalized_query = (
        query.casefold().strip() if isinstance(query, str) and query.strip() else None
    )
    normalized_tool = (
        tool_name.casefold().strip() if isinstance(tool_name, str) and tool_name.strip() else None
    )
    bounded_limit = max(1, min(int(limit), _MAX_STEP_LIMIT))

    matched: list[dict[str, Any]] = []
    for step in history:
        if normalized_tool is not None:
            candidate = str(step.get("tool_name") or "").casefold()
            if normalized_tool not in candidate:
                continue
        if normalized_query is not None and not _matches(step, normalized_query):
            continue
        placement = _lineage_placement(str(step.get("execution_id")), nodes, active_line)
        matched.append({**step, **placement})

    matched.sort(
        key=lambda item: (item.get("sequence", 0), item.get("execution_id", "")), reverse=True
    )
    returned = matched[:bounded_limit]
    return {
        "total_matched": len(matched),
        "returned": len(returned),
        "limit": bounded_limit,
        "filters": {"query": normalized_query, "tool_name": normalized_tool},
        "steps": returned,
    }


def _leaf_name(dotted: str) -> str:
    tail = dotted.rsplit(".", 1)[-1]
    return tail.split("[", 1)[0].casefold()


def list_decisions(
    decisions: dict[str, Any], user_turns: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    """Explicit recorded decisions, each with rationale and authorizing turn when cited."""

    turn_prompts: dict[str, str] = {}
    for turn in user_turns:
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("turn_id")
        prompt = turn.get("user_prompt")
        if isinstance(turn_id, str) and isinstance(prompt, str):
            turn_prompts[turn_id] = prompt

    records: list[dict[str, Any]] = []
    for key in sorted(decisions, key=str):
        value = decisions[key]
        flattened = _flatten(value)
        flattened.pop("__truncated__", None)

        rationale: Any = None
        answer: Any = None
        question: Any = None
        authorizing_turn: dict[str, Any] | None = None
        for dotted, leaf in flattened.items():
            name = _leaf_name(dotted)
            if rationale is None and name in _RATIONALE_LEAVES:
                rationale = leaf
            elif answer is None and name in _ANSWER_LEAVES:
                answer = leaf
            elif question is None and name in _QUESTION_LEAVES:
                question = leaf
            elif authorizing_turn is None and name in _TURN_LEAVES and isinstance(leaf, str):
                authorizing_turn = {
                    "turn_id": leaf,
                    "user_prompt": turn_prompts.get(leaf),
                }

        records.append(
            {
                "decision": str(key),
                "rationale": rationale,
                "recorded_answer": answer,
                "question_asked": question,
                "authorizing_turn": authorizing_turn,
                "detail": flattened,
            }
        )

    return {"decision_count": len(records), "decisions": records}


# --------------------------------------------------------------------------------------------------
# Entrypoints
# --------------------------------------------------------------------------------------------------


def _facts(context: Any) -> dict[str, Any]:
    facts = getattr(context, "state_facts", None)
    return facts if isinstance(facts, dict) else {}


def _lineage(context: Any) -> dict[str, Any]:
    lineage = getattr(context, "state_lineage", None)
    return lineage if isinstance(lineage, dict) else {}


def _history(context: Any) -> list[dict[str, Any]]:
    session_dir = Path(context.session_dir)
    state = _load_json(session_dir / "state.json")
    artifacts = state.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    return build_history(artifacts, _event_index(session_dir))


def _write_artifact(context: Any, filename: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    (context.staging_dir / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return [
        {
            "name": filename.removesuffix(".json"),
            "relative_path": filename,
            "media_type": "application/json",
            "include_in_output_view": False,
        }
    ]


def summarize_analysis_state(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    result = summarize(_facts(context), _lineage(context), _history(context))
    dataset = result["dataset"]
    shape = dataset.get("working_shape")
    shape_text = (
        f"{shape['n_cells']:,} cells x {shape['n_genes']:,} genes"
        if shape
        else "shape not recorded"
    )
    active = result["active_version"]
    active_text = (
        f"active version {active['short_id']} from {active['produced_by']}"
        if active
        else "no dataset version recorded yet"
    )
    areas = ", ".join(result["recorded_fact_areas"]) or "none"
    summary = (
        f"Analysis state: {shape_text}; {active_text}; {result['step_count']} committed step(s). "
        f"Recorded fact areas: {areas}."
    )
    artifacts = _write_artifact(context, "analysis-state-summary.json", result)
    return {"summary": summary, "details": result, "artifacts": artifacts}


def find_analysis_steps(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    query = arguments.get("query")
    tool_name = arguments.get("tool_name")
    limit = arguments.get("limit", _DEFAULT_STEP_LIMIT)
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = _DEFAULT_STEP_LIMIT
    result = find_steps(
        _history(context),
        _lineage(context),
        query=query if isinstance(query, str) else None,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        limit=limit_value,
    )
    descriptor_parts = []
    if result["filters"]["tool_name"]:
        descriptor_parts.append(f"tool ~ {result['filters']['tool_name']!r}")
    if result["filters"]["query"]:
        descriptor_parts.append(f"query ~ {result['filters']['query']!r}")
    descriptor = f" matching {', '.join(descriptor_parts)}" if descriptor_parts else ""
    if result["total_matched"] == 0:
        summary = (
            f"No committed step{descriptor} is recorded. If you expected one, it was not run in "
            "this session (or ran on an alternative version not on the active line)."
        )
    else:
        summary = (
            f"{result['total_matched']} committed step(s){descriptor}; showing the "
            f"{result['returned']} most recent. Each is annotated with whether it sits on the "
            "active line of descent."
        )
    artifacts = _write_artifact(context, "matched-analysis-steps.json", result)
    return {"summary": summary, "details": result, "artifacts": artifacts}


def list_recorded_decisions(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    session_dir = Path(context.session_dir)
    state = _load_json(session_dir / "state.json")
    recorded = state.get("decisions")
    recorded = recorded if isinstance(recorded, dict) else {}
    user_turns = getattr(context, "user_turns", ()) or ()
    result = list_decisions(recorded, tuple(user_turns))
    if result["decision_count"] == 0:
        summary = "No explicit decisions are recorded yet in this analysis."
    else:
        names = ", ".join(record["decision"] for record in result["decisions"])
        summary = (
            f"{result['decision_count']} recorded decision(s): {names}. Each carries its rationale "
            "and, when a decision cites one, the authorizing user turn."
        )
    artifacts = _write_artifact(context, "recorded-decisions.json", result)
    return {"summary": summary, "details": result, "artifacts": artifacts}
