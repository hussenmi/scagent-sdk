"""Replay-only switches for the Figure 4 ablation. Never set in normal use.

SCAGENT_REPLAY_OFF is a comma-separated list of protections to turn off:
  ancestor_check     accept an older recorded version of the data as input
  evidence_clearing  keep annotation evidence when the data it describes changes
  evidence_filter    let the annotation review use evidence about a different clustering
Recording is unaffected: every execution is still staged, committed and logged.
"""
import os

KNOWN = {"ancestor_check", "evidence_clearing", "evidence_filter"}


def off(name: str) -> bool:
    assert name in KNOWN, name
    raw = os.environ.get("SCAGENT_REPLAY_OFF", "")
    return name in {item.strip() for item in raw.split(",") if item.strip()}
