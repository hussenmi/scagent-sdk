# Implementation roadmap

Status: living plan

## Principles

- Prove the modular contracts with a narrow vertical slice before expanding the catalog.
- Keep the scientific session durable and independent of the model conversation.
- Port requirements and lessons, not dependencies on `cs_agent/scagent`.
- Test deterministic behavior separately from model behavior.
- Treat repeated-run consistency as a product requirement.

## Phase 0 — architecture and session spine

Deliverables:

- versioned scientific-session metadata and state;
- append-only, ordered, fsynced event log;
- atomic materialized-state checkpoints;
- replay after an event/state checkpoint interruption;
- exact SDK resume when a compatible runtime session exists;
- reconstructed resume from durable facts and artifacts otherwise;
- CLI create/list/show/resume operations;
- corruption and identity checks.

Acceptance criteria:

- no model SDK is required to create or recover a scientific session;
- a simulated interruption between event append and state write recovers automatically;
- corrupt history is reported, never silently discarded;
- changing model profile cannot erase or invalidate scientific state.

## Phase 1 — runtime and model compatibility

Implemented and verified:

- Claude Agent SDK adapter with a short system prompt;
- explicit, fingerprinted model profiles and LiteLLM gateway configuration;
- read-only profile, health, and Anthropic Messages diagnostics;
- automatic binding of returned SDK session IDs;
- exact resume, reconstructed resume, and intentional SDK fork semantics;
- SDK transcript mirroring inside the scientific session;
- normalized turn, usage, success, and failure events.
- explicit, deny-by-default runtime extensions for in-process MCP tools and hooks;
- live behavioral probes for text, tools, hooks, reasoning+tools, retries, and result size;
- interactive multi-turn `agent chat` with exact or reconstructed resume;
- verified Iris LiteLLM/vLLM deployment profile for `Qwen3.6-27B`;
- 48 KiB inline-result ceiling and artifact-spill requirement above it.
- validated MCP image-result transport with bounded artifact-backed media attachments;

Phase 1 acceptance was verified on Iris. Reasoning is merged into text by the gateway/model, which
is reported as a compatibility warning rather than treated as missing reasoning. The direct SDK
tool-result boundary lies between 49,203 and 51,251 total characters; Phase 2 must enforce the
conservative 48 KiB limit in the capability result envelope.

## Phase 2 — capability registry

Implemented:

- versioned `capability.yaml` schema and strict package validation;
- skill discovery under `.claude/skills` with exact content fingerprints;
- safe relative Python entrypoint loading;
- dynamic, typed in-process MCP tool construction and exact allowlists;
- standard result envelope and finite-JSON validation;
- atomic artifact spill for results above the 48 KiB inline ceiling;
- two-phase staged/PostToolUse state and artifact commit;
- recovery of pending and finalized-but-uncommitted executions;
- CLI listing and validation;
- executable `inspect-dataset` proof skill with script, reference, and artifact.

Environment requirements are recorded in manifests and provenance. Non-current requirements are
resolved by the Phase 4 broker and fail closed rather than silently using the wrong environment.

The complete path was verified live on Iris with `Qwen3.6-27B`: the model discovered and invoked
`inspect_dataset` on the PBMC 10k H5 input, PostToolUse committed the result, and the session stored
the sampled dataset fingerprint, HDF5 format evidence, exact skill fingerprint, and inspection
artifact.

## Phase 3 — first scientific vertical slice

Implemented and verified on Iris:

- data inspection plus preprocessing and clustering skills;
- stable cell-set, representation, and clustering identities embedded in AnnData and state;
- cluster-QC tables, figures, report, and current-clustering attestation;
- independent dataset, current-QC, batch-decision, and current-annotation floors;
- reclustering/integration invalidation and tested QC-floor re-fire;
- per-library GPU Scrublet evidence, explicit review, and identity-safe predicted-call filtering;
- marker, CellTypist, and SCimilarity evidence;
- gated final label coverage validation, annotated H5AD, tables, UMAP, and report;
- real PBMC 10k end-to-end deterministic execution.

## Phase 4 — environment broker

Implemented for Iris:

- uv-managed, locked Python 3.12 agent control plane;
- locked Pixi `rapids`, `cellbender`, and `diffxpy` physical compute runtimes;
- logical `gpu-singlecell`, `scvi`, `celltypist`, `scimilarity`, `cellbender`, and `diffxpy`
  profiles mapped independently onto those runtimes;
- isolated subprocess execution with JSON input/output contracts;
- allowlisted environment construction with ambient venv/Conda/Python state removal;
- real module-import, CUDA-device, GPU-memory, timeout, and provenance-file probes;
- separate persisted logical and physical fingerprints per capability result;
- one-command locked bootstrap with deterministic changed-lock validation;
- verified two-GPU RAPIDS-singlecell, Scanpy, CellTypist, SCimilarity, scVI, and CellBender
  execution, plus isolated diffxpy/TensorFlow execution.

Remaining: portable host discovery, Spark profiles, scheduler/resource allocation beyond
per-process timeout/GPU-memory policy, and multi-node execution.

## Phase 5 — capability expansion

Implemented: strengthened batch investigation and explicit decisions, GPU Scrublet evidence and
review, scVI, CellTypist, SCimilarity, auditable custom Python/read-only shell diagnostics, and the
focused CellBender ambient-background slice.

The CellBender slice includes fingerprint-bound raw-droplet suitability/refusal, strict parameters,
GPU-only routing, command/environment/seed provenance, full artifact registration, caught-failure
and checkpoint semantics, corrected-count and barcode-set lineage, and downstream invalidation.
Iris validation used a genuine 396,297-barcode raw 10x matrix and its 8,201-barcode filtered pair.
The filtered input was refused; raw GPU inference reached sustained training before an intentional
operator interruption; and a separate controlled timeout committed failure evidence while leaving
the raw dataset active. A full 150-epoch output was not required for this capability-confirmation
phase and therefore no biological-improvement claim is made.

Also implemented: image inspection, PDF text-plus-page review, Tavily web research, bounded public
page fetching, and automatic model review of figures emitted by preprocessing, cluster QC, batch
investigation, scVI, and finalization skills.

The doublet slice validates current raw-count/cell-set identities, normally executes Scrublet per
biological library, preserves its predicted Boolean calls, registers score/call evidence, requires
an explicit review decision, and issues a new cell-set/count lineage only for confirmed filtering.
A live PBMC run predicted 565/11,043 cells (5.12%) and retained enriched clusters for review rather
than removing them. This verified execution and state behavior; it did not prove every call.

Immediate corrective pass before broad expansion:

- resolve H5AD raw-count sources rather than assuming `X` is counts;
- bind batch decisions/integration authorization to current cell/count/representation/clustering
  identities and test staleness;
- make multi-batch figures readable for high-cardinality sample keys;
- strengthen pDC-versus-plasma and Scrublet-uncertainty guidance/evals;
- add direct finalization contract, non-overwrite, and state/artifact tests.

Remaining:

- Harmony, Scanorama, and BBKNN;
- flag-first cell QC and preview/authorize filtering separated from preparation;
- DEG, pseudobulk, pathways, visualization, and reporting;
- promotion of custom-code outputs into validated scientific-floor evidence.

## Phase 6 — scientific and agent evaluations

- deterministic unit and contract tests;
- hook allow/deny/staleness/re-fire tests;
- trace replay without a model;
- repeated PBMC runs across supported local models;
- evaluation of invariant compliance, artifact integrity, label evidence, and run-to-run
  variability;
- explicit separation of deterministic-contract, live-compute, model-behavior, and biological
  generality claims;
- comparison with `scagent` as a scientific baseline, not a dependency.
