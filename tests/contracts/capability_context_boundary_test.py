"""Every capability-context field must cross the isolated-environment boundary.

A compute capability does not receive the ``CapabilityContext`` object: the broker serializes it to
JSON and the worker rebuilds it as a namespace on the far side. Adding a field to the dataclass and
to only one side of that boundary is silent — the handler simply sees the dataclass default, so a
consultation check reads an empty session, a lineage check reads no versions, and nothing raises.
This pins both halves to the dataclass instead.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

from scagent_sdk.capabilities.results import CapabilityContext

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "scagent_sdk"


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _context_field_names() -> set[str]:
    return {field.name for field in fields(CapabilityContext)}


def test_broker_serializes_every_context_field() -> None:
    written = _string_constants(_SOURCE_ROOT / "execution" / "broker.py")
    missing = sorted(_context_field_names() - written)
    assert not missing, (
        f"execution/broker.py does not serialize context field(s) {missing}; a capability running "
        "in an isolated environment would silently receive the dataclass default"
    )


def test_worker_rebuilds_every_context_field() -> None:
    read = _string_constants(_SOURCE_ROOT / "execution" / "worker.py")
    missing = sorted(_context_field_names() - read)
    assert not missing, (
        f"execution/worker.py does not rebuild context field(s) {missing}; the broker sends them "
        "but the handler never sees them"
    )
