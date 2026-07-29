"""Executor-owned artifact lineage: identity signatures, fact scope, and forest topology.

Session facts merge by identity and always converge. The H5AD chain merges only by whichever path
the model passed forward, so a capability that adds columns can derive from a sibling artifact and
silently drop another capability's contribution from the delivered file. Identity cannot detect that
-- both branches carry the same identities -- so topology has to be recorded separately.

This module holds the pure, side-effect-free half of that design: what an identity signature is,
which fact roots belong to a node rather than the session, and how a committed execution is
classified against the current forest. The executor owns the mutations; nothing here touches disk.

Specification: ``docs/artifact-lineage-and-head-spec.md`` (v2.2), decisions D1, D2, D10.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

# --- identity signature (D1) ---------------------------------------------------------------

# Every ``facts.analysis`` node that currently carries an id. Signatures no longer define topology,
# so over-inclusion is harmless -- it only makes staleness and overlay-eligibility queries finer,
# while a short list would collide two genuinely different matrices onto one signature.
IDENTITY_AXES_V1: tuple[str, ...] = (
    "cell_set",
    "clustering",
    "count_representation",
    "dataset_revision",
    "expression",
    "hvg",
    "representation",
    "umap",
)
IDENTITY_SIGNATURE_VERSION = 1

# --- fact scope registry (D1) --------------------------------------------------------------

Scope = Literal["node", "session"]

# Scope is semantic and cannot be inferred from JSON shape: ``reference_runs`` nests a
# ``cell_set_id`` under each execution, so a "contains an identity id" rule would classify it
# node-scoped, when it is a reuse cache that is already self-describing and is meant to be shared
# across branches. Hence an explicit table.
FACT_ROOT_SCOPES: dict[str, Scope] = {
    # Node-scoped: describes one matrix and must travel with it across a checkout.
    "analysis": "node",
    "annotation": "node",
    "batch": "node",
    "cell_qc": "node",
    "cluster_qc": "node",
    "doublets": "node",
    "finalization": "node",
    # Session-scoped: properties of the input, or caches keyed independently of the active branch.
    "custom_analysis": "session",
    "dataset": "session",
    "dataset_contents": "session",
    "gene_conversion": "session",
    "reference_runs": "session",
}


class LineageContractError(ValueError):
    """An identity axis or fact root outside the registered vocabulary.

    Raised rather than defaulted: silently guessing a scope decides whether a fact survives a
    branch checkout, and silently ignoring an axis lets two different matrices share a signature.
    """


def identity_signature(
    facts: Mapping[str, Any], *, axes: tuple[str, ...] = IDENTITY_AXES_V1
) -> str:
    """Hash the identity axes present in ``facts.analysis`` into one comparable signature.

    Absent axes are omitted rather than encoded as null, so a session that has not clustered yet
    does not collide with one whose clustering was explicitly cleared. An ``analysis`` child that
    carries an ``id`` but is not a registered axis raises.
    """

    analysis = facts.get("analysis")
    if not isinstance(analysis, Mapping):
        analysis = {}
    unknown = sorted(
        name
        for name, value in analysis.items()
        if isinstance(value, Mapping) and "id" in value and name not in axes
    )
    if unknown:
        raise LineageContractError(
            "unregistered identity axis in facts.analysis: "
            + ", ".join(unknown)
            + ". Add it to IDENTITY_AXES_V1 deliberately, or the signature silently ignores it."
        )
    present: dict[str, str] = {}
    for axis in axes:
        node = analysis.get(axis)
        if isinstance(node, Mapping) and isinstance(node.get("id"), str):
            present[axis] = str(node["id"])
    encoded = json.dumps(present, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return f"identity:v{IDENTITY_SIGNATURE_VERSION}:sha256:{digest}"


def fact_scope(root: str) -> Scope:
    """Return the registered scope for a top-level fact root, or raise."""

    try:
        return FACT_ROOT_SCOPES[root]
    except KeyError:
        raise LineageContractError(
            f"unregistered fact root: {root!r}. Register it in FACT_ROOT_SCOPES as 'node' "
            "(travels with one matrix) or 'session' (independent of the active branch)."
        ) from None


def partition_facts_patch(
    patch: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a facts patch into its node-scoped and session-scoped halves.

    Node-scoped keys land on a lineage node's snapshot and reach global facts only when that node
    is active; session-scoped keys always merge globally. Raises on an unregistered root.
    """

    node: dict[str, Any] = {}
    session: dict[str, Any] = {}
    for root, value in patch.items():
        target = node if fact_scope(str(root)) == "node" else session
        target[str(root)] = value
    return node, session


# --- forest topology (D2) ------------------------------------------------------------------


@dataclass(frozen=True)
class LineageNode:
    """One H5AD-producing execution.

    Keyed by execution rather than by identity, because two deliberate branches can carry identical
    identities -- the exact case identity-keyed topology cannot represent.
    """

    execution_id: str
    parent_execution_id: str | None
    head_path: str
    identity_signature: str
    requested_input: str | None
    resolved_input_execution_id: str | None
    branch_intent: bool
    skill_id: str
    tool_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_execution_id": self.parent_execution_id,
            "head_path": self.head_path,
            "identity_signature": self.identity_signature,
            "requested_input": self.requested_input,
            "resolved_input_execution_id": self.resolved_input_execution_id,
            "branch_intent": self.branch_intent,
            "created_by": {"skill_id": self.skill_id, "tool_name": self.tool_name},
        }


def empty_forest() -> dict[str, Any]:
    return {"active_execution_id": None, "nodes": {}}


def active_head(lineage: Mapping[str, Any]) -> str | None:
    value = lineage.get("active_execution_id")
    return value if isinstance(value, str) else None


def head_path(lineage: Mapping[str, Any]) -> str | None:
    """Path of the artifact an omitted matrix input should resolve to."""

    node = _node(lineage, active_head(lineage))
    path = node.get("head_path") if node is not None else None
    return path if isinstance(path, str) else None


def _node(lineage: Mapping[str, Any], execution_id: str | None) -> Mapping[str, Any] | None:
    if execution_id is None:
        return None
    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping):
        return None
    node = nodes.get(execution_id)
    return node if isinstance(node, Mapping) else None


def ancestry(lineage: Mapping[str, Any], execution_id: str) -> list[str]:
    """Execution IDs from ``execution_id`` up to its root, nearest first.

    Cycles cannot occur through the normal commit path, where a parent always exists before its
    child, but a corrupted or hand-edited forest must not hang the process.
    """

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = execution_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        node = _node(lineage, current)
        parent = node.get("parent_execution_id") if node is not None else None
        current = parent if isinstance(parent, str) else None
    return chain


PathRelation = Literal["head", "ancestor", "sibling", "untracked"]


def classify_input(lineage: Mapping[str, Any], execution_id: str | None) -> PathRelation:
    """Describe a supplied matrix input relative to the active head.

    ``ancestor`` is called out separately from ``sibling`` because rejecting only siblings is
    exactly what let two annotators diverge: once the first advanced the head, the artifact both
    were handed became an ancestor rather than a sibling.
    """

    if execution_id is None or _node(lineage, execution_id) is None:
        return "untracked"
    head = active_head(lineage)
    if execution_id == head:
        return "head"
    if head is not None and execution_id in ancestry(lineage, head):
        return "ancestor"
    return "sibling"


def node_for_path(lineage: Mapping[str, Any], path: str) -> str | None:
    """Find the execution that produced ``path``, matching on the session-relative suffix.

    Envelopes hand the model absolute paths while records stay session-relative, so a supplied
    path is compared by suffix rather than by string equality.
    """

    nodes = lineage.get("nodes")
    if not isinstance(nodes, Mapping) or not path:
        return None
    normalized = str(path).replace("\\", "/").rstrip("/")
    for execution_id, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        candidate = node.get("head_path")
        if not isinstance(candidate, str) or not candidate:
            continue
        relative = candidate.replace("\\", "/").rstrip("/")
        if normalized == relative or normalized.endswith("/" + relative):
            return str(execution_id)
    return None


def place_node(
    lineage: Mapping[str, Any],
    node: LineageNode,
) -> dict[str, Any]:
    """Return a new forest with ``node`` added.

    The active head advances to the new node unless it was created under branch intent, in which
    case the branch stays reachable but the session keeps working on the previous head. Parentage
    comes from the node itself -- the resolved input's producer -- never from the active head at
    commit time, which is what made three clusterings off one UMAP look like a chain.
    """

    nodes = dict(lineage.get("nodes") or {})
    nodes[node.execution_id] = node.to_dict()
    active = active_head(lineage)
    return {
        "active_execution_id": active if node.branch_intent else node.execution_id,
        "nodes": nodes,
    }


def checkout(lineage: Mapping[str, Any], execution_id: str) -> dict[str, Any]:
    """Make an existing node active. Raises if it is not in the forest."""

    if _node(lineage, execution_id) is None:
        raise LineageContractError(f"cannot check out an unknown lineage node: {execution_id}")
    return {"active_execution_id": execution_id, "nodes": dict(lineage.get("nodes") or {})}


def reachable_from_heads(lineage: Mapping[str, Any], heads: list[str]) -> set[str]:
    """Executions on the ancestry of any supplied head.

    The prerequisite for pruning: an artifact outside this set is not reachable from live work.
    Retention policy is deliberately not decided here -- liveness is a branch-status question, not
    a topology one.
    """

    keep: set[str] = set()
    for head in heads:
        keep.update(ancestry(lineage, head))
    return keep
