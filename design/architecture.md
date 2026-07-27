# Architecture proposal

Status: accepted architecture; Phases 0-2 implemented

## 1. System boundary

`scagent-sdk` is a new implementation. The existing `cs_agent/scagent` repository is a
reference for scientific requirements and known failure modes, not a runtime dependency.
Code may be deliberately reimplemented or extracted conceptually, but the new project must
be installable and runnable when `scagent` is absent.

## 2. Separation of responsibilities

### Agent runtime

The runtime creates the Claude Agent SDK session, exposes tools, enables skills, installs
hooks, streams messages, and manages session lifecycle. Its base prompt should be short:
identity, tool usage, evidence standards, and how to consult the orchestration skill.

### Skill packages

An orchestration skill describes the normal single-cell workflow and decision points. It
does not run a fixed end-to-end pipeline. Focused skills implement inspection, preprocessing,
cluster QC, batch investigation, integration, reference annotation, annotation adjudication,
DEG, pathway analysis, visualization, and reporting.

A capability skill may contain:

- `SKILL.md`: when to use it, reasoning guidance, caveats, and interpretation rules;
- `capability.yaml`: machine-readable tool declarations, schemas, environment needs, and
  state effects;
- `scripts/`: deterministic entrypoints;
- `schemas/`: structured input/output contracts;
- `references/`: detailed APIs and scientific background;
- `tests/` and `evals/`: deterministic checks and agent-behavior cases;
- `assets/`: templates or static resources when needed.

The runtime discovers capability manifests and creates typed MCP tools dynamically. This
avoids a central tool registry and still gives the model specific, well-described tools.

A skill therefore reaches the model through two distinct channels, and both must be wired:
`capability.yaml` becomes callable MCP tools, and `SKILL.md` becomes standing context. Instruction
delivery is deterministic, not a model decision — every skill's instructions are injected into the
system prompt at assembly. Progressive disclosure is deliberately rejected at this scale: with one
scientific domain, ~20 skills, and a very large context window, deferring the read saves a few
percent of context and reintroduces the failure it was meant to prevent, a model that never
consults the guidance. The `Skill` tool — the single built-in tool this runtime grants — remains for
`references/` and re-reading, with skills published as a per-session local plugin root rather than
through project setting sources, which would also import the repository's own `CLAUDE.md`/`AGENTS.md`
development instructions into a scientific session.

A skill that depends on a host asset it may never download — a cached reference model — declares an
optional `readiness` probe in `capability.yaml`. The probe is standard-library skill code, run in
the control plane at assembly time with the declared capability environment's variables, so the
runtime learns nothing about the science and the skill keeps ownership of what "available" means.
Verdicts are rendered into the model's system prompt as local prerequisites and recorded as a
`capability.readiness_probed` event. Capability self-knowledge is therefore declared and injected,
never inferred by the model from filesystem searches; a probe that fails degrades to an explicit
unknown rather than blocking assembly.

### Deterministic execution layer

Skill scripts operate on explicit input artifacts and emit a standard result envelope. They
use standard scientific libraries directly and do not import `scagent`.

The result envelope should include status, summary, produced artifacts, evidence, warnings,
provenance, verification checks, and a proposed state delta. The harness validates the
envelope before committing state.

### State and provenance

State records facts and evidence about the current artifacts rather than a pipeline stage.
Important identities include dataset revision, cell-set fingerprint, representation,
clustering identity, QC attestation, integration decision, reference-model runs, annotation
evidence, and finalized outputs.

Each session stores an append-only event log plus a materialized `state.json`, artifacts,
saved custom code, shell commands, decisions, transcript, and environment/model metadata.
Material changes create new identities, making old evidence stale automatically. For example,
reclustering creates a new clustering identity, so QC evidence tied to the previous identity
no longer satisfies the QC floor.

### Resumability

The durable scientific session and the model conversation are separate identities. The
scientific session owns facts, artifacts, decisions, and evidence. It may bind to a Claude
Agent SDK session ID for exact conversational continuation, but it never depends on that
history for scientific recovery.

When the runtime and model profile match a recorded binding, resume the SDK conversation and
also provide the current scientific checkpoint. Otherwise start a new model conversation and
reconstruct its context from the durable session. This supports provider changes, lost CLI
history, moving between hosts, and intentional forks without losing analysis continuity.

Every tool result is appended as an event before the materialized state is atomically updated.
If execution stops between those writes, opening the session replays unapplied event patches.
Corrupt or out-of-order history is surfaced explicitly rather than silently ignored.

### Floors

Floors are independent state predicates evaluated by hooks. They gate consequential actions
without selecting the analysis path. Examples include:

- final annotation requires QC evidence for the current cell set and clustering;
- annotation evidence must cover the proposed label and current cluster identity;
- integration requires a recorded batch investigation and explicit decision;
- destructive filtering requires a preview and authorization policy;
- final save/report cannot claim completion while required evidence is stale or missing;
- artifacts must be registered with provenance and pass schema/integrity checks.

`PreToolUse` gates an action, `PostToolUse` validates and records results,
`PostToolUseFailure` adds recovery context, and `Stop` prevents premature completion. Hooks
must be independent because the SDK may dispatch multiple matchers concurrently.

## 3. Model transport

The scientific system should depend on a small model-profile contract, not on LiteLLM,
vLLM, NIM, or a particular model name.

The first runtime can use the Claude Agent SDK. For local models, a separately managed
LiteLLM gateway exposes an Anthropic Messages-compatible endpoint and translates to an
OpenAI-compatible local server such as vLLM or NIM:

```
Claude Agent SDK / bundled CLI
             |
       Anthropic Messages
             |
       LiteLLM gateway
             |
 OpenAI-compatible vLLM or NIM
             |
         local model
```

LiteLLM is a transport adapter, not part of a skill and not the owner of agent state. The
agent should health-check it but should not silently start or mutate shared model services in
normal operation. Development commands may launch a configured local stack explicitly.

Model profiles are versioned and fingerprinted from non-secret configuration. Exact native
resume requires a compatible fingerprint; changing model, endpoint semantics, prompt, skills,
or runtime settings reconstructs from scientific state instead of blindly continuing an
incompatible conversation.

The Claude SDK's own transcripts are mirrored beneath the scientific session through its
session-store adapter. This makes exact resume portable with the project session directory.
Concise provider-neutral turn events are recorded separately for auditing and reconstruction.

Because arbitrary local models are not the Claude Agent SDK's primary documented target,
every model profile needs a compatibility suite covering tool calls, long tool results,
reasoning plus tool use, retries, context limits, hooks, skills, and multi-turn recovery.
Model-specific template or reasoning fixes belong in the model profile/gateway config.

Keep a runtime boundary so a future direct LiteLLM/native tool-loop implementation can be
added without changing skills, state, floors, or deterministic executors.

## 4. Environment execution

Skills declare capabilities such as `cpu-singlecell`, `gpu-singlecell`, `scvi`,
`cellbender`, `scimilarity`, or `diffxpy`; they do not hardcode host paths or activation
commands. An environment broker maps those capabilities to host-specific profiles.

Profiles separate physical runtimes from logical capabilities. A physical runtime owns an
interpreter, prefix, allowlisted activation environment, health probe, and provenance files. A
logical capability owns required modules, GPU/memory requirements, timeouts, and focused variables.
Several capabilities may share one physical runtime without being collapsed into one scientific
tool. Iris and Spark can therefore use different mechanics while presenting the same skill
contract.

The agent control plane is independently locked with uv. Scientific compute is locked with Pixi
into separate RAPIDS, CellBender, and diffxpy runtimes. The broker invokes those workers directly;
the user's shell never activates a scientific environment, and ambient Conda or virtualenv state
is scrubbed. Runtime and capability fingerprints are recorded separately.

General `run_python` and `run_shell` escape hatches remain available, but execute inside a
declared profile and session workspace. Custom work becomes eligible to satisfy a floor only
after it is published through a validated evidence/artifact contract.

## 5. Suggested project shape

```
scagent-sdk/
  .claude/skills/          orchestration and focused capability skills
  configs/
    models/                LiteLLM/model profiles and compatibility expectations
    environments/          Iris/Spark execution profiles
  src/scagent_sdk/
    runtime/               Claude SDK adapter and session loop
    capabilities/          skill discovery, dynamic tools, staging, and commit
    contracts/             results, evidence, artifacts, events, and state types
    state/                 event store and reducers
    floors/                hook predicates and remediation messages
    execution/             environment broker, sandbox, code/shell runners
    artifacts/             provenance and integrity handling
  tests/
    unit/                  deterministic code and reducers
    contracts/             every capability's input/output behavior
    floors/                deny/allow/staleness/re-fire tests
    integration/           SDK, gateway, environments, and representative data
    evals/                 repeated agent runs and scientific-quality assertions
  sessions/                ignored runtime output
```

## 6. First vertical slice

Build the platform before the catalog:

1. session/event/artifact contracts and a minimal CLI;
2. Claude SDK runtime with one local-model profile and a `doctor` command;
3. capability discovery with one deterministic `inspect-dataset` skill; **implemented**
4. preprocessing/clustering and cluster-QC skills;
5. a QC floor tied to clustering identity, including re-fire after reclustering;
6. one reference annotation skill and one evidence/finalization skill;
7. repeated PBMC evaluation comparing invariants, artifacts, and scientific conclusions
   across runs and models.

Only after this slice works should more capabilities be ported. This prevents rebuilding a
large monolith before the modular contracts and floors have proved themselves.
