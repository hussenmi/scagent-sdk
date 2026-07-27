# ADR 0009: Context exhaustion rolls the model conversation, not the scientific session

Status: accepted

## Context

A provider-native transcript can outgrow its model's context window even though the analysis is
still scientifically active. Loading the same complete transcript on resume recreates the same
failure. A fixed project-wide token ceiling is also incorrect: local vLLM deployments and hosted
models advertise different limits, and a deployment may change the model behind a profile alias.

The scientific session already has stronger persistence than a transcript: versioned facts,
decisions, artifact provenance, identities/lineage, append-only events, atomic materialized state,
and project-mirrored runtime history.

## Decision

Treat context compaction as a **model-conversation epoch rollover** within one scientific session.

1. Discover the context limit for the selected model/deployment. Prefer live LiteLLM/provider/
   upstream model metadata, then model-specific hosted-provider metadata, then an explicit profile
   fallback. During Claude Agent SDK execution, use `rawMaxTokens` when earlier discovery has no
   value.
2. Before an exact resumed query, read runtime context usage. Reserve maximum output plus a safety
   margin. If the next turn cannot fit, do not submit it against the old transcript.
3. In automatic mode, append a `runtime.context_rolled_over` event and retry once with a fresh
   runtime conversation reconstructed from the same scientific state. Recognize an actual
   provider context-window error as a second trigger in case preflight metadata was unavailable.
4. Preserve the old transcript and all scientific files. Never edit an SDK transcript JSONL or a
   prior scientific checkpoint to make a request fit.
5. Bound the reconstructed prompt, not the authoritative state. Include structured facts and
   decisions, compact artifact provenance/paths, state/event file pointers, and recent turn
   handoff. If state is abnormally large, include deterministic hashes and require selective reads
   from `state.json`.
6. On `scagent start --resume`, ask the user for Automatic, Exact, or Reconstructed policy. A
   forced Exact choice fails clearly instead of silently rebuilding.

## Consequences

- A context-full session can continue without losing scientific provenance or changing its
  scientific session ID.
- Exact conversational continuity remains available while compatible history fits.
- Resume after process exit and automatic rollover use the same durable reconstruction contract.
- Model narrative that was never committed is best-effort recent handoff and is labeled
  non-authoritative; committed facts, decisions, artifacts, and events remain authoritative.
- The runtime can support differently sized local and cloud models without model-specific
  constants in scientific code.
