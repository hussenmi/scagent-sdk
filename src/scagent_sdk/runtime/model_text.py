"""Normalize provider text that embeds reasoning or tool protocol as plain text.

Local OpenAI-compatible models occasionally serialize protocol channels into a normal text
block. Those strings are transport artifacts: reasoning must stay on the reasoning channel and
a printed tool call must never be mistaken for an executed tool call.
"""

from __future__ import annotations

import re

_REASONING_PATTERNS = (
    re.compile(r"<think>(.*?)</think>\s*", re.DOTALL | re.IGNORECASE),
    re.compile(
        r"<\|channel\|>analysis\s*(.*?)<\|channel\|>final\s*",
        re.DOTALL | re.IGNORECASE,
    ),
)
_TEXTUAL_TOOL_PATTERNS = (
    re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>\s*", re.DOTALL | re.IGNORECASE),
    re.compile(r"<tools>\s*(.*?)\s*</tools>\s*", re.DOTALL | re.IGNORECASE),
)


def split_model_text(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return visible narration, reasoning fragments, and textual tool-call fragments.

    Complete textual tool wrappers are removed from narration. An unterminated wrapper is also
    quarantined from its opening tag onward: it cannot be executed and displaying it would expose
    broken protocol markup as if it were a user-facing answer.
    """

    visible = text
    reasoning: list[str] = []
    textual_tools: list[str] = []
    for pattern in _REASONING_PATTERNS:
        reasoning.extend(match.strip() for match in pattern.findall(visible) if match.strip())
        visible = pattern.sub("", visible)
    for pattern in _TEXTUAL_TOOL_PATTERNS:
        textual_tools.extend(match.strip() for match in pattern.findall(visible) if match.strip())
        visible = pattern.sub("", visible)

    # Fail closed on a truncated tool wrapper. The bounded recovery path can ask the model to
    # reissue it natively; neither the terminal nor the durable final response should print it.
    lowered = visible.casefold()
    starts = [
        index
        for tag in ("<tool_call>", "<tools>")
        if (index := lowered.find(tag)) >= 0
    ]
    if starts:
        start = min(starts)
        fragment = visible[start:].strip()
        if fragment:
            textual_tools.append(fragment)
        visible = visible[:start]

    visible = re.sub(r"^<\|[^|]+\|>\s*$", "", visible, flags=re.MULTILINE)
    return visible.strip(), tuple(reasoning), tuple(textual_tools)
