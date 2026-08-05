---
name: query-analysis-state
description: Answer questions about what this analysis has done, which parameters a step used, and what was decided — read from the durable session record rather than from memory. Use whenever a user asks whether something was done, what value was used, or what was decided, and before you assert any such fact yourself.
---

# Query analysis state

The durable session is the authoritative account of the analysis: `state.json` holds the current
facts, the committed capability records with their exact arguments, and the recorded decisions; the
event log owns their order; the lineage forest owns which version is active. These tools read that
record directly and deterministically. Reach for them instead of answering from conversation memory
— memory drifts over a long session, and a step you recall may have run on an alternative version
that is no longer active.

Prefer these over shelling into `state.json`/`events.jsonl` with `analysis-workspace`. A raw text
search is blind to lineage: it can return a parameter from a superseded sibling branch and present
it as current. These tools resolve the active line of descent and label every result against it.

## Where the analysis stands

`summarize_analysis_state` reports the dataset identity and working shape, the active version and
how many alternatives exist, and every recorded fact area flattened to its scalar values. Use it to
re-ground yourself on a resumed or long-running session before answering "what have we done so far".

## Whether a step ran, and with what parameters

`find_analysis_steps` searches the committed steps by tool name or free text and returns them
newest-first with their exact recorded `arguments`, summary, sequence, timestamp, and artifact path.
Each step is annotated:

- `is_version: true` with `on_active_line: true/false` — the step transformed the dataset, so its
  currency is precise. `on_active_line: false` means it belongs to an alternative branch, not the
  line the analysis is currently on.
- `is_version: false` with `on_active_line: null` — a read-only step (a QC measurement, a plot, an
  evaluation) that created no version, so no currency claim is made for it.

An empty result means the step was not committed in this session — treat that as "not done", not as
"done but unrecorded". Answer parameter questions from the returned `arguments`, not from recall.

## What was decided

`list_recorded_decisions` lists the explicit decisions — batch keep/integrate/separate, QC and
cluster-QC reviews, version switches — each with its rationale and, when the decision cites a user
turn, that turn's prompt text. Use it to answer "what did we decide about X and why", and to check
that a decision you are about to rely on was actually recorded rather than merely discussed.

## Scope

These tools are read-only and never change the analysis. They report what is recorded; they do not
evaluate floors (the runtime already states outstanding floors) and they do not decide anything. If
a fact or step is absent, say "not recorded" rather than filling the gap from narrative memory.
