# ADR 0003: Mirror Claude SDK transcripts into scientific sessions

Status: accepted for the initial runtime

## Context

Recording only a Claude SDK session ID still leaves exact resume dependent on Claude Code's
host-local transcript directory. Recent Claude Agent SDK releases provide a session-store
adapter that mirrors transcript entries and can materialize them again for resume.

## Decision

Install a scientific-session-scoped SDK transcript store under:

```text
sessions/<scientific-id>/runtime/claude-agent-sdk/<sdk-id>/transcript.jsonl
```

Mirror main and subagent transcripts. Treat entry UUIDs as idempotency keys, fsync appended
batches, validate all path components, and serialize writes with file locks. Pass this store
to every Claude SDK execution, including exact resume and SDK forks.

Also record concise `runtime.turn_started`, `runtime.bound`, `runtime.turn_completed`, and
`runtime.turn_failed` events in the scientific event log. The mirrored SDK transcript gives
exact conversational continuity; the scientific events give provider-neutral auditability.

## Consequences

- Exact resume can survive movement of the project session directory to another host.
- Full model transcripts and scientific events have separate purposes and retention needs.
- The Claude SDK remains free to keep its own temporary/local transcript; our mirror is the
  durable project-controlled copy.

