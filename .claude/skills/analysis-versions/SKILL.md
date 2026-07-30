---
name: analysis-versions
description: List the dataset versions this analysis has produced and switch which one is active. Use when comparing alternatives created with branch_from, when a tool reports that an artifact has been superseded, or when the user asks what versions exist or wants to go back to an earlier one.
---

# Analysis versions

Every step that transforms the dataset records a version. Steps normally continue from the active
version, so this skill is only needed when more than one line of work exists.

## Listing

`list_analysis_versions` describes each version: which step produced it, what it derived from,
whether it is the active one, and which alternatives branch away from the main line. Use it to
decide what to compare or switch to — not to find a path to pass to a tool, since tools resolve the
active version themselves.

## Switching

`switch_analysis_version` makes a recorded version active. Everything afterwards continues from it,
and the evidence bound to it becomes the current evidence.

Switching is a scientific decision, not bookkeeping. It changes which cells, clustering, and
evidence the analysis is about, so say what the alternatives were and why this one is chosen.

## Comparing alternatives

To compare rather than replace, pass `branch_from` to the transforming tool with the version to fork
from. That records an alternative without moving the active version, so several can exist at once —
for example clustering the same embedding at more than one resolution.

Evidence produced on an alternative belongs to that alternative and is not the session's current
evidence until it is switched to. Compare alternatives from what each step returned and from their
artifacts; do not expect a branch's evidence to appear in session facts while another version is
active. Finalization runs against the active version, so switch to the chosen one first.
