# ADR 0005: Executable capabilities live inside validated skill packages

Status: accepted

## Context

The model needs concise procedural guidance while brittle computation and state mutation need
deterministic code. A central tools module would recreate the legacy monolith. Loading arbitrary
entrypoints without validation would make provenance, filesystem boundaries, and recovery weak.

## Decision

An executable skill is a standard `.claude/skills/<skill-id>/` package containing `SKILL.md` and
an optional `capability.yaml`. Prompt-only skills omit the manifest. Executable manifests declare
a version, typed tools, relative Python entrypoints, and logical environment requirements.

The registry requires the directory name, SKILL.md name, and manifest skill ID to match. It rejects
absolute or escaping entrypoints, validates JSON object schemas, loads only declared functions,
and fingerprints all meaningful package bytes. The fingerprint is persisted with every execution.

Entrypoints receive arguments and a narrow `CapabilityContext`. They return a versioned result
envelope containing a summary, optional details, fact/decision patches, and artifacts created only
inside the provided staging directory.

Execution is two-phase:

1. validate and stage the result, then append `capability.result_staged`;
2. after the SDK emits PostToolUse, atomically finalize the artifact directory and append
   `capability.result_committed` with the state patch.

Startup recovers staged results and the narrow crash window where artifacts were renamed but the
commit event was not appended. Details above 48 KiB spill to a registered JSON artifact before the
model receives a compact envelope.

## Consequences

- adding a scientific capability does not edit the runtime adapter;
- exact script/reference bytes are auditable per result;
- model prose cannot directly mutate scientific state;
- tool execution can survive a model or process interruption;
- logical environments are declared now and resolved by the Phase 4 broker later;
- Python entrypoints currently run in the harness environment and therefore must use only available
  dependencies until external environment execution is implemented.
