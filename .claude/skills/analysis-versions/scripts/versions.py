"""Describe recorded dataset versions and validate a request to switch between them.

These entrypoints are deliberately read-only. ``switch_analysis_version`` resolves and checks its
target and reports what the switch means; the executor performs the lineage mutation, because a
skill must never write session state. The manifest declares ``lineage_operation: checkout`` and the
executor applies the head move and the fact swap in one event.
"""

from __future__ import annotations

import json
from typing import Any


def _forest(context: Any) -> tuple[dict[str, Any], str | None]:
    lineage = getattr(context, "state_lineage", None) or {}
    nodes = lineage.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    active = lineage.get("active_execution_id")
    return nodes, active if isinstance(active, str) else None


def _ancestry(nodes: dict[str, Any], start: str | None) -> list[str]:
    chain: list[str] = []
    seen: set[str] = set()
    current = start
    while isinstance(current, str) and current not in seen and current in nodes:
        seen.add(current)
        chain.append(current)
        current = nodes[current].get("parent_execution_id")
    return chain


def _label(node: dict[str, Any]) -> str:
    created = node.get("created_by")
    if isinstance(created, dict):
        tool = created.get("tool_name")
        if isinstance(tool, str) and tool:
            return tool
    return "unknown step"


def _resolve(nodes: dict[str, Any], version_id: str) -> str:
    if version_id in nodes:
        return version_id
    matches = sorted(key for key in nodes if key.startswith(version_id))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"no recorded version matches {version_id!r}; call list_analysis_versions for the "
            "available versions"
        )
    raise ValueError(
        f"{version_id!r} matches several versions ({', '.join(short[:8] for short in matches)}); "
        "supply more characters"
    )


def _describe(nodes: dict[str, Any], active: str | None) -> list[dict[str, Any]]:
    main_line = set(_ancestry(nodes, active))
    rows: list[dict[str, Any]] = []
    for execution_id, node in sorted(nodes.items()):
        if not isinstance(node, dict):
            continue
        rows.append(
            {
                "version_id": execution_id,
                "short_id": execution_id[:8],
                "produced_by": _label(node),
                "derived_from": (
                    str(node["parent_execution_id"])[:8]
                    if isinstance(node.get("parent_execution_id"), str)
                    else None
                ),
                "artifact": node.get("head_path"),
                "active": execution_id == active,
                # An alternative is any version the active line of descent does not pass through.
                "on_active_line": execution_id in main_line,
                "created_as_alternative": bool(node.get("branch_intent")),
            }
        )
    return rows


def list_versions(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    nodes, active = _forest(context)
    rows = _describe(nodes, active)
    (context.staging_dir / "analysis-versions.json").write_text(
        json.dumps({"active_version_id": active, "versions": rows}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    alternatives = [row for row in rows if not row["on_active_line"]]
    if not rows:
        summary = "No dataset versions recorded yet; no step has transformed the dataset."
    else:
        current = next((row for row in rows if row["active"]), None)
        summary = (
            f"{len(rows)} recorded version(s). Active: "
            f"{current['short_id'] + ' from ' + current['produced_by'] if current else 'none'}. "
            f"{len(alternatives)} off the active line."
        )
    return {
        "summary": summary,
        "details": {"active_version_id": active, "versions": rows},
        "artifacts": [
            {
                "name": "analysis-versions",
                "relative_path": "analysis-versions.json",
                "media_type": "application/json",
            }
        ],
    }


def switch_version(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    version_id = str(arguments.get("version_id", "")).strip()
    rationale = str(arguments.get("rationale", "")).strip()
    if not version_id:
        raise ValueError("version_id must not be empty")
    if not rationale:
        raise ValueError(
            "rationale must not be empty: switching changes what the analysis is about"
        )

    nodes, active = _forest(context)
    if not nodes:
        raise ValueError("no dataset versions are recorded yet, so there is nothing to switch to")
    target = _resolve(nodes, version_id)
    if target == active:
        raise ValueError(
            f"version {target[:8]} is already active; no switch is needed"
        )

    node = nodes[target]
    report = {
        # The executor reads this to perform the checkout; a skill never mutates lineage itself.
        "target_execution_id": target,
        "previous_execution_id": active,
        "produced_by": _label(node),
        "artifact": node.get("head_path"),
        "rationale": rationale,
    }
    (context.staging_dir / "version-switch.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "summary": (
            f"Switched the analysis to version {target[:8]} from {_label(node)}. Later steps "
            "continue from it and its evidence is now current."
        ),
        "details": report,
        "artifacts": [
            {
                "name": "version-switch",
                "relative_path": "version-switch.json",
                "media_type": "application/json",
            }
        ],
    }
