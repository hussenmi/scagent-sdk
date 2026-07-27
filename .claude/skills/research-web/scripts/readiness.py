"""Report whether the Tavily credential needed for web search is present.

`web_search` cannot work without `TAVILY_API_KEY`, and a missing credential is a fact the model
should state rather than discover by watching a tool fail. Page fetching does not need the key.
Standard library only: this runs in the control plane at session assembly.
"""

from __future__ import annotations

import os
from typing import Any

KEY_VARIABLE = "TAVILY_API_KEY"


def probe(environment: dict[str, str] | None = None) -> dict[str, Any]:
    resolved = environment or {}
    key = (resolved.get(KEY_VARIABLE) or os.environ.get(KEY_VARIABLE) or "").strip()
    if not key:
        return {
            "status": "partial",
            "summary": f"{KEY_VARIABLE} is not set; web search is unavailable",
            "details": [
                "fetch_web_page still works on public URLs.",
                "Do not retry web_search hoping for a different result; say the key is missing.",
            ],
        }
    return {
        "status": "ready",
        "summary": f"{KEY_VARIABLE} is configured; search and page fetch are both available",
    }
