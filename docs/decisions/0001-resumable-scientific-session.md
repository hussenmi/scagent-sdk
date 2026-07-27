# ADR 0001: Scientific sessions are independent of model conversations

Status: accepted for the initial implementation

## Context

Claude Agent SDK and similar runtimes can resume their own conversation history, but a
single-cell analysis has durable state outside that conversation: datasets, transformations,
cluster identities, evidence, decisions, figures, and reports. Provider history may be
deleted, incompatible with a new model, unavailable on another host, or intentionally forked.

## Decision

Give every analysis a stable **scientific session ID** owned by `scagent-sdk`. Persist its
facts and provenance locally in a versioned, append-only session directory.

Record model-runtime session IDs as replaceable bindings inside the scientific session.

Resume has two modes:

1. **Exact resume** — use the runtime's native resume ID when the runtime and model profile
   are compatible.
2. **Reconstructed resume** — start a new model conversation and inject a compact rendering
   of durable facts, artifacts, and decisions. Completed work is not repeated automatically.

The scientific session is authoritative in both modes. Model transcripts are helpful context,
not the source of truth for what happened to the data.

## Consequences

- Analyses survive provider changes and missing conversation history.
- The same analysis can be resumed with a stronger or different local model.
- State requires schema versions, validation, atomic writes, and migrations.
- Exact conversational continuity is best-effort; scientific continuity is guaranteed by the
  project-controlled state and artifacts.

