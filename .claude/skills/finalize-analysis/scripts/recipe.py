"""Render the committed capability-call list as an importable Python module.

Kept beside finalization because the recipe is part of the finalized provenance record. The
human reading surface is a separate requested capability (``analysis-notebook``), so this file
only has to be exact and importable, not pleasant to read end to end.
"""

from __future__ import annotations

import math
import pprint
from typing import Any


class _Literal:
    """Carries a pre-rendered Python expression through ``pprint``, which calls ``repr``."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text


def _replace_non_finite(value: Any) -> Any:
    """Make non-finite floats survive as source.

    ``json.loads`` accepts ``Infinity`` and ``NaN``, so an argument can hold one, and ``pprint``
    renders those as the bare names ``inf``/``nan`` -- which raise ``NameError`` on import. The
    recipe has to stay an importable module, so they become explicit ``float(...)`` calls.
    """

    if isinstance(value, float) and not math.isfinite(value):
        return _Literal(f"float({str(value)!r})")
    if isinstance(value, dict):
        return {key: _replace_non_finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_non_finite(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_non_finite(item) for item in value)
    return value


def render_capability_recipe(recipe_calls: list[dict[str, Any]]) -> str:
    """Render the machine replay list as readable Python: one commented call per entry.

    ``pprint`` rather than ``json.dumps``: this file has to stay an importable ``.py``, and JSON
    renders ``true``/``false``/``null``, which Python cannot parse.
    """

    lines = [
        '"""Exact capability-call recipe generated from committed session provenance.',
        "",
        "This records the ordered calls and parameters. Replay them through the scagent-sdk",
        "capability runtime so scientific floors and artifact commits are preserved. Ask for the",
        "`analysis-notebook` capability for a readable walkthrough of the same steps.",
        '"""',
        "",
        "CAPABILITY_CALLS = [",
    ]
    for number, call in enumerate(recipe_calls, start=1):
        tool = call.get("tool", "capability")
        skill = call.get("skill", "?")
        version = call.get("skill_version", "?")
        lines.append(f"    # {number}. {tool} ({skill} {version})")
        body = pprint.pformat(_replace_non_finite(call), indent=1, width=94, sort_dicts=True)
        lines.extend("    " + line for line in body.splitlines())
        lines[-1] = lines[-1] + ","
    lines.extend(["]", ""])
    return "\n".join(lines)
