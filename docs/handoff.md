# scagent-sdk continuation handoff

Status: authoritative continuation brief as of 2026-07-21

This document is the durable handoff from the initial architecture and platform implementation
phase to the scientific-capability migration phase. Read it before making changes. Verify facts
against the code when they matter; this file explains intent, boundaries, completed work, and the
recommended order of future work.

## 1. Product intent

Build an independent, resumable, skill-driven single-cell RNA-seq analysis agent that preserves
the scientific rigor and detailed reasoning of the legacy `scagent` while avoiding its giant
`prompts.py`, giant `tools.py`, and increasingly DAG-like orchestration.

The desired system has the following shape:

- The model reasons about the biological question, chooses among valid branches, and explains
  material decisions.
- A concise orchestration skill describes the normal analysis loop and decision points rather
  than prescribing a fixed tool order.
- Focused skills own scientific guidance, deterministic scripts, references, schemas, assets,
  tests, and evals for one concern at a time.
- Deterministic code handles brittle computation, validation, artifact production, and
  provenance. Model prose does not directly mutate scientific state.
- Hooks enforce a small number of state-based scientific floors. Floors constrain unsafe or
  unsupported conclusions without selecting the workflow.
- Sessions are resumable like Claude Code or Codex. Durable scientific facts and artifacts are
  authoritative; model conversation history is a replaceable convenience.
- The agent can use local multimodal models, images, PDFs, web research, bounded filesystem and
  shell inspection, and restricted custom code.
- Specialized Python/CUDA environments are brokered by capability. They are never ambient shell
  state and never silently fall back to an incompatible environment.
- The new project must remain usable when the legacy repository and package are absent.

The motivating principle from Anthropic's biology-agent work is that consistency comes from
putting the right portions of execution behind deterministic, testable interfaces. The intent is
not to turn the entire analysis into a deterministic pipeline. Scientific choice remains flexible;
identity, validation, execution, and provenance are deterministic.

## 2. Source of truth and reference boundaries

### Active implementation

- Iris project: `/home/ibrahih3/projects/scagent-sdk`
- Normal activation: `source setup_gpu.sh`
- Normal terminal: `scagent start`
- Durable sessions: `/home/ibrahih3/projects/scagent-sdk/sessions`

This project is the only active implementation for this effort. It is not currently a Git
repository. Do not initialize Git, commit, push, or modify another repository unless the user asks.

### Scientific and UX references only

- Legacy implementation: `/data1/peerd/ibrahih3/cs_agent/scagent`
- Earlier disposable prototype: `/home/ibrahih3/projects/sdk-floor-proto`
- BioNeMo skill work: `/home/ibrahih3/projects/bionemo-lab`
- BioNeMo contributed skills: `/home/ibrahih3/projects/bionemo-lab/contrib-skills`

The legacy and prototype trees may be read to recover scientific requirements, failure modes,
terminal behavior, environment knowledge, and test cases. Do not import, subclass, vendor, or
shell into their runtime code from `scagent-sdk`. Reimplement deliberate capability contracts
using standard libraries inside focused skill packages.

Useful legacy reference areas include:

- `scagent/agent/prompts.py`, `tools.py`, `agent.py`, and `world_state.py` for workflow rules,
  annotation guards, run-loop behavior, and failure handling;
- `scagent/core/{io,qc,normalization,dimred,clustering,genes,gpu}.py` for deterministic core
  behavior and edge cases;
- `scagent/analysis/{batch_diagnostic,batch_gene_investigation,cluster_confidence,deg,
  interpretation,literature}.py` for scientific diagnostics and interpretation requirements;
- `scagent/annotation/{celltypist,scimilarity,cytopus_markers}.py` for annotation evidence;
- `scagent/batch/{scvi,harmony,scanorama,bbknn,scib,diffxpy,entropy}.py` for integration and batch
  evaluation;
- `scagent/agent/{sandbox,sandbox_runner,vision_sidecar,run_manager,tracing}.py` and
  `scagent/terminal.py` for code isolation, multimodal handling, provenance, and terminal UX.

The BioNeMo contributed-skill references currently include:

- `contrib-skills/library-skills/rapids-singlecell/SKILL.md`;
- `contrib-skills/open-models-skills/scimilarity/SKILL.md`;
- `contrib-skills/workflows/single-cell/SKILL.md`.

Use these to learn good skill packaging and science communication. A useful skill is more than a
Markdown prompt: it may need `scripts/`, `references/`, schemas, assets, deterministic tests, and
model-behavior evals.

## 3. Architecture already implemented

### 3.1 Durable scientific sessions

Each analysis has a project-owned scientific session ID and directory. The session contains:

- versioned `session.json` metadata;
- versioned, materialized `state.json`;
- append-only, ordered, fsynced `events.jsonl`;
- committed artifacts and staged executions;
- project-mirrored Claude Agent SDK transcripts;
- runtime/model bindings, saved custom code, logs, and other provenance.

State changes are appended as events before the materialized checkpoint is replaced atomically.
Startup replays unapplied events and recovers the narrow staged/finalized capability crash windows.
Corrupt or out-of-order history fails explicitly.

Resume has two modes:

1. Exact resume reuses the Claude SDK session when the runtime and model-profile fingerprint are
   compatible.
2. Reconstructed resume starts a new model conversation from durable facts, artifacts, decisions,
   and events when exact history is absent or incompatible.

Commands:

```bash
scagent start --resume
scagent start --resume <scientific-session-id>
```

Inside the terminal, `/state`, `/session`, `/skills`, `/help`, and `/exit` are local commands.

### 3.2 Model and agent runtime

The first backend uses the Claude Agent SDK behind an internal provider-neutral runtime protocol.
The current Iris profile routes the local `Qwen3.6-27B` vLLM service through a project-owned
LiteLLM Anthropic Messages gateway. Profiles and their non-secret fingerprints live under
`configs/models/` and `configs/litellm/`.

The backend is deny-by-default. It does not inherit ambient Claude tools or settings. Validated
skill packages contribute an exact MCP tool allowlist and independent hooks.

The terminal preserves the useful legacy interaction style through provider-neutral events:

- Rich Markdown and LaTeX-aware output;
- dimmed reasoning when the gateway exposes a distinguishable reasoning stream;
- clear final model text;
- spinners while the model is working;
- durable activity lines such as `▶ Running code...` and `✓ Running code done`;
- errors, retries, hook denials, and result-limit events.

The gateway may merge reasoning into ordinary text for the current local model. This is recorded as
a compatibility warning, not confused with absent reasoning capability.

### 3.3 Skill and capability system

Skills live under `.claude/skills/<skill-id>/`. `SKILL.md` carries model-facing guidance. An
optional `capability.yaml` declares executable tools, JSON schemas, logical environments, activity
labels, and floors. Deterministic code stays inside that skill's `scripts/` directory. Detailed
science and API guidance belongs in `references/`; templates and static material belong in
`assets/` when needed.

The registry validates matching directory/SKILL/manifest IDs, safe relative entrypoints, schemas,
and exact package fingerprints. The capability executor:

- evaluates declared floors before execution;
- provides a narrow `CapabilityContext`;
- restricts artifact writes to an execution staging directory;
- validates a versioned finite-JSON result envelope;
- spills details over 48 KiB to a registered artifact;
- supports up to eight bounded model-media images;
- stages results before returning them to the model;
- commits facts, decisions, and artifacts through PostToolUse;
- records skill and environment provenance.

Skill code must never edit `state.json`, `events.jsonl`, or committed artifact directories.

### 3.4 Scientific floors

The currently implemented independent floors are:

- `dataset_identity`: a durable input fingerprint must exist;
- `cellbender_input_suitable`: raw-droplet suitability must match the active input path and
  fingerprint;
- `current_doublet_evidence`: complete evidence must match the current cell-set and count identities;
- `current_cluster_qc`: QC must be attested for the current clustering identity;
- `batch_decision`: an explicit batch-handling decision must exist; current implementation still
  needs P0 identity binding;
- `integration_authorized`: the recorded batch decision must explicitly authorize integration;
  current implementation still needs the same P0 identity binding;
- `current_annotation_evidence`: current-clustering marker evidence plus at least one independent
  reference method must be complete.

Unknown floors fail closed. Representation or clustering changes issue new identities, making old
QC and annotation evidence stale without requiring a hardcoded tool sequence.

### 3.5 Media, PDF, and web capabilities

The multimodal path is real, not path-only narration:

- `inspect_image` validates and normalizes raster input, saves an artifact, and attaches pixels to
  the model.
- `inspect_pdf` extracts bounded text, renders selected pages, saves artifacts, and attaches page
  images.
- Scientific plotting skills can return the same validated `model_media` contract for automatic
  review.
- `web_search` uses Tavily and records normalized source-linked evidence.
- `fetch_web_page` fetches bounded public HTTP(S) content or saves a public PDF while blocking
  private-network targets.

The untracked `.env` contains the Iris Tavily credential and is mode `0600`. Never print, copy into
documentation, commit, or persist secret values in session facts. `.env.example` documents names.

### 3.6 Environment architecture

The control and compute planes are separate:

- uv owns the lightweight Python 3.12 `.venv`, `uv.lock`, terminal, model SDK, state, registry,
  hooks, media, web, and broker.
- Pixi owns locked `rapids`, `cellbender`, and `diffxpy` scientific runtimes through `pixi.toml`
  and `pixi.lock`.

`source setup_gpu.sh` deactivates ambient virtualenv and Conda state, synchronizes both lockfiles,
activates only the uv control plane, and exports explicit uv/Pixi paths. Scientific environments
are never activated in the user's shell. Repeated setup is approximately 0.7 seconds once stamps
are current.

Iris `/home` was full during implementation. Pixi therefore stores detached environments and
caches beneath `/usersoftware/peerd/$USER`, with `.pixi/envs` as a stable project-local symlink.
Do not move large compute environments back into `/home`.

Physical runtimes in `configs/environments/iris.toml`:

- `rapids-main`: Python 3.14.6, RAPIDS 26.04, CUDA 13.1, CuPy 14, Scanpy 1.12.1,
  RAPIDS-singlecell 0.15.2, scVI 1.4.3, CellTypist 1.7.1, SCimilarity 0.4.1,
  PyTorch 2.12.1+cu130;
- `cellbender`: Python 3.10.20, CellBender 0.3.2, PyTorch 2.13.0+cu130;
- `diffxpy`: Python 3.9.23, diffxpy 0.7.4, TensorFlow 2.10.1, NumPy 1.23.5,
  pandas 1.5.3, SciPy 1.9.3, AnnData 0.8.0.

Logical capabilities map onto physical runtimes:

- `gpu-singlecell`, `scvi`, `celltypist`, and `scimilarity` share `rapids-main`;
- `cellbender` uses the isolated CellBender runtime;
- `diffxpy` uses the isolated legacy-compatible differential-expression runtime.

Workers receive an allowlisted environment with the selected prefix first on `PATH`. Ambient
`VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH`, and unrelated Conda state are removed. Required modules,
GPU count, free memory, timeout, and provenance files fail closed. Results record separate logical
and physical fingerprints, resolved interpreter/prefix, module versions, and CUDA information.

The RAPIDS feature alone uses cross-channel solving because the validated CUDA 13 RAPIDS stack has
overlapping artifacts across `conda-forge`, `rapidsai`, and `nvidia`, and Pixi has no equivalent of
Conda's flexible priority. The committed lock fixes exact artifacts. Never install packages
manually into locked prefixes.

The old Conda-linked agent venv was preserved as `.venv.legacy-conda`. A failed partial Pixi prefix
was moved to `/tmp/scagent-sdk-pixi-failed-envs-20260721`; it is not active and may disappear with
node-local temporary storage.

## 4. Current skill and tool inventory

Capability discovery currently reports 15 skills, 14 executable skills, and 21 tools.

| Skill | Tools | Current scientific role |
|---|---|---|
| `orchestrate-single-cell` | prompt-only | Flexible evidence-driven workflow and resumption guidance |
| `analysis-workspace` | `list_workspace`, `read_text_file`, `run_shell_diagnostic`, `run_analysis_code` | Bounded inspection and restricted auditable escape hatch |
| `inspect-dataset` | `inspect_dataset` | File/container identity and provenance before mutation |
| `cellbender-background-removal` | `validate_cellbender_input`, `remove_ambient_background` | Raw-droplet suitability/refusal and GPU-only ambient-background removal with corrected-count lineage |
| `doublet-evidence` | `evaluate_doublet_evidence`, `review_doublet_evidence` | Per-library GPU Scrublet evidence, explicit conservative review, and confirmed predicted-call filtering with new lineage |
| `prepare-single-cell` | `prepare_single_cell` | H5AD/10x loading, explicit filtering, counts preservation, normalization, HVGs, PCA, neighbors, UMAP, Leiden, markers, identities |
| `cluster-qc` | `evaluate_cluster_qc` | Size, silhouette, marker coherence, QC covariates, current-clustering attestation |
| `batch-investigation` | `investigate_batch` | Batch composition/association evidence and explicit handling decision |
| `scvi-integration` | `integrate_with_scvi` | Count-aware integration, model artifact, new representation/clustering identities |
| `marker-annotation` | `evaluate_marker_evidence` | Cluster DEGs and transparent broad marker programs |
| `celltypist-annotation` | `run_celltypist_annotation` | Cached reference predictions and cluster consensus as non-final evidence |
| `scimilarity-annotation` | `run_scimilarity_annotation` | Local model alignment, embeddings, kNN predictions, non-final evidence |
| `finalize-analysis` | `finalize_analysis` | Gated label adjudication and final H5AD/table/figure/report publication |
| `inspect-media` | `inspect_image`, `inspect_pdf` | Real image pixels and rendered PDF evidence for the model |
| `research-web` | `web_search`, `fetch_web_page` | Current public evidence with source provenance |

These skills implement a credible first vertical slice; they do not represent full legacy
scientific parity. Existing science must still be reviewed rather than assumed perfect merely
because a script executes.

## 5. Verified behavior

The following checks passed on Iris at handoff:

- `70 passed` in the full deterministic Python test suite;
- Ruff clean;
- strict mypy clean;
- capability validation: 15 skills, 14 executable skills, 21 tools;
- shell bootstrap syntax and CLI smoke checks;
- repeated `source setup_gpu.sh` with Conda inactive and managed CPython 3.12.9;
- all six logical environment profiles healthy through `scagent-sdk doctor environment`;
- RAPIDS deterministic GPU check on two Hopper GPUs, including a real CuPy sum;
- CellBender 0.3.2 import/two-GPU check plus a real raw-droplet capability run;
- isolated diffxpy/TensorFlow import check;
- live image, PDF, and Tavily capability tests;
- local-model SDK compatibility for text, tools, hooks, retries, long results, and exact resume;
- a PBMC 10k first vertical slice: inspection, preprocessing, 17-cluster QC, batch decision,
  marker/CellTypist/SCimilarity evidence, gated final publication, terminal resume, and a separate
  two-epoch scVI GPU smoke run.
- CellBender live acceptance on the BC002 Cell Ranger pair: the 8,201-barcode filtered matrix was
  refused for lacking empty-droplet evidence; the 396,297-barcode raw matrix passed with 323,534
  barcodes at no more than 10 UMIs; scientific session
  `2c96ab8a-77fa-4737-8edc-d16aa54e3b2a` reached normal GPU training through epoch 46 before the
  user-requested terminal interruption and therefore retains an uncommitted pending command/log
  directory rather than a false result; and scientific session
  `307a6a52-6147-44f9-9dde-50036d0cd533` separately proved caught-timeout commit behavior with the
  active raw fingerprint unchanged.
- Two local-model behavior runs selected inspection then CellBender validation, did not launch
  removal, and stated that suitability does not prove biological improvement. The first exposed a
  low-count-gate/cell-count interpretation error; guidance and an eval case were tightened, and the
  repeated run correctly treated 3,963 as only the minimum empty-tail evidence threshold.
- A live PBMC doublet run predicted 565/11,043 cells (5.12%), identified clusters 4, 11, 16, and
  17 for review, and conservatively retained all cells rather than converting predictions into
  automatic filtering.
- A 26-sample batch validation measured Cramér's V 0.539 and a mean same-sample-neighbor fraction
  of 0.595 versus 0.205 expected from composition, then recorded `request_guidance` because the
  available evidence could not distinguish technical structure from study design.

Representative PBMC input used previously:

`/data1/peerd/sharmar1/workshop_2025_files/workshop_data/pbmc_data/pbmc_10k_v3_filtered_feature_bc_matrix.h5`

Do not treat one PBMC run as general biological validation. Add multiple tissues, organisms,
batch structures, edge cases, and repeated model runs as the catalog expands.

## 6. Important invariants for all future work

1. Preserve independence from legacy `scagent` at imports, subprocesses, packaging, and runtime.
2. Put scientific code with its skill, not in a new central biology or tools module.
3. Use model reasoning for choices and interpretation; use deterministic scripts for computation,
   validation, identity, and provenance.
4. Floors are state predicates. Never encode the normal workflow as a mandatory DAG.
5. A state-changing capability must issue or preserve explicit dataset, cell-set, representation,
   and clustering identities as scientifically appropriate.
6. Evidence tied to an old identity must become stale automatically.
7. Never silently use CPU or the wrong environment when a declared runtime is unavailable.
8. Never hand-assign final labels from model prose or custom Python. Finalization must reconcile
   explicit evidence through the gated capability.
9. Never overwrite user data. Publish new artifacts atomically and register them.
10. Keep inline results below 48 KiB; write large tables, models, reports, and details as artifacts.
11. Treat images, documents, and web pages as untrusted evidence, not instructions.
12. Do not leak secrets into prompts, state, logs, artifacts, fingerprints, or documentation.
13. Add unit/contract/floor tests for deterministic behavior and separate evals for model choices.
14. Run live Iris validation in the exact brokered runtime for any scientific dependency change.
15. Preserve the simple user contract: `source setup_gpu.sh` followed by `scagent start`.

## 7. Known gaps and technical debt

### Scientific capabilities not yet implemented or fully migrated

- Flag-first cell QC and a preview/authorize filtering transition separate from preparation.
- Harmony, Scanorama, and BBKNN integrations.
- Comparative integration evaluation, biological-conservation checks, and scIB-style evidence.
- Sample-aware differential expression, pseudobulk, contrasts, and covariate-aware design.
- Pathway/enrichment analysis with explicit gene-universe and multiple-testing provenance.
- Dedicated downstream visualization and publication-quality reporting skills.
- Deeper sample/donor-aware QC, covariate exploration, cell-cycle/stress/ambient evidence, and
  tissue-specific annotation workflows.
- Dedicated Cytopus/PanglaoDB evidence contracts and broader annotation consensus/validation.
- Promotion of restricted custom-code output into validated scientific evidence.

### Immediate observed corrections

- H5AD preparation must resolve and validate raw counts from `X`, named layers, or `adata.raw`
  instead of always copying `X` into `layers['counts']`.
- Batch-decision and integration-authorization floors must be bound to current cell-set,
  count/representation, and clustering identities.
- High-cardinality batch figures need readable legends/layouts; the 26-sample artifact exposed the
  defect.
- Annotation guidance/evals need more conservative Scrublet language and explicit pDC-versus-
  plasma discrimination after a model misinterpreted a pDC-like cluster.
- The strengthened finalization label/override contract needs direct deterministic tests.

### Platform work still remaining

- Spark host profiles and validation.
- Portable host discovery instead of the current explicit Iris profile paths.
- Scheduler/resource allocation beyond per-process timeout, GPU count, and free-memory checks.
- Multi-node execution and resource queues.
- Formal state schema migrations beyond version 1.
- Repeated agent-behavior evaluation harnesses and cross-model scientific benchmarks.
- Git/release/packaging workflow, if and when the user requests it.

### Important distinctions

- A healthy runtime is not an implemented scientific skill. CellBender now has a user-facing
  focused capability; diffxpy can execute but still has no user-facing focused capability.
- A tool that passes unit tests is not proof of scientific generality.
- Current first-slice skills borrow verified requirements from legacy behavior, but the complete
  science in `prompts.py`, `tools.py`, configs, and analysis modules has not yet been systematically
  transferred.

## 8. Recommended scientific migration method

For each scientific area, use the same disciplined sequence:

1. Define the biological question, valid inputs, outputs, optional branches, failure modes, and
   what must remain a model judgment.
2. Read the relevant legacy source, prompts, configs, tests, prior run artifacts, and BioNeMo skill
   material. Extract requirements; do not inherit the implementation.
3. Check current primary package documentation and original methods papers where scientific or API
   details may have changed.
4. Write or update the focused `SKILL.md` and a detailed `references/` document explaining method
   selection, assumptions, warnings, interpretation, and when not to use the method.
5. Define a strict `capability.yaml` input schema, logical environment, activity label, and only the
   consequential floors that are scientifically necessary.
6. Implement deterministic scripts using standard libraries. Produce a compact result envelope and
   complete artifacts with provenance.
7. Design identity and invalidation effects before patching facts. Do not improvise state keys in
   one script without considering future consumers.
8. Add unit tests, manifest/contract tests, floor allow/deny/staleness tests, interruption recovery
   tests when relevant, and scientific edge cases.
9. Run the exact runtime health check and a representative live Iris analysis. Inspect generated
   H5ADs, tables, plots, models, and reports rather than trusting exit status.
10. Run at least one model-driven test to verify that the model selects the skill appropriately,
    interprets its evidence conservatively, and replans after warnings.
11. Update the orchestration reference and roadmap only after the focused capability is verified.

This sequence is how scientific rigor is transferred without recreating the legacy monolith.

## 9. Scientific-capability phase status and next priority

The durable parity audit, focused CellBender slice, and focused doublet-evidence/review slice were
completed in the 2026-07-21/22 phase. Complete the observed correction pass in
`docs/current-state.md` before beginning another broad capability, and continue from the matrix
rather than restarting the audit.

### 9.1 Scientific parity matrix — completed

`docs/scientific-parity.md` now maps legacy responsibilities to:

- current focused skill/tool, if implemented;
- current scientific coverage and live-test evidence;
- missing assumptions, parameters, diagnostics, artifacts, floors, and tests;
- proposed future skill package;
- priority and dependencies.

Audit legacy prompts as requirements, but map each rule to one of: orchestration guidance,
focused-skill guidance, deterministic validation, a state floor, or an evaluation. This is the key
step that prevents another giant prompt while ensuring science is not lost. Keep it current as
each focused capability lands.

### 9.2 First implementation: CellBender ambient-RNA capability — completed

CellBender was selected because its isolated runtime was already locked and a genuine
raw/unfiltered 10x acceptance pair was available. The focused skill now exposes validation and
removal without importing or invoking legacy `scagent`.

The completed slice includes:

- `.claude/skills/cellbender-background-removal/` with `SKILL.md`, `capability.yaml`, `scripts/`,
  `references/`, and `agents/openai.yaml`;
- explicit raw-input suitability checks and clear refusal/remediation for filtered or unsupported
  input;
- deterministic parameter validation, random seed, GPU/resource reporting, command/config
  capture, progress/error handling, and bounded summaries;
- preservation of the original input plus registered CellBender output, posterior/metrics, plots,
  and an interpretation report;
- an explicit state contract for corrected counts and dataset lineage;
- careful identity semantics: ambient correction changes the count representation and may affect
  downstream preprocessing, while barcode/cell-set changes depend on selected output and must be
  represented explicitly;
- floors that require input identity and prevent accidental overwrite, without forcing CellBender
  into analyses where it is inappropriate;
- tests for schemas, input rejection, result/artifact validation, runtime routing, timeouts/failure,
  state effects, staleness of downstream evidence, and recovery;
- a small deterministic or mocked contract test plus a real Iris run on scientifically suitable
  raw data;
- an orchestration update explaining when ambient correction is warranted and when it should be
  skipped.

Live acceptance did not stop at an import: the raw matrix reached real GPU inference and sustained
training; the paired filtered matrix was refused; and a controlled timeout committed honest
failure evidence without changing active data. Per the user's compute guidance, the confirmation
run was interrupted before all 150 epochs, so no corrected-matrix biological claim is recorded.

### 9.3 Status after doublet evidence

1. Correct H5AD raw-count source selection, identity-bound batch floors, high-cardinality plots,
   conservative pDC/plasma and doublet interpretation, and finalization tests.
2. Separate flag-first cell QC and preview/authorize filtering from preparation.
3. Integration alternatives plus comparative batch/biology preservation evidence.
4. diffxpy-backed and pseudobulk differential expression with explicit experimental designs.
5. Pathways/enrichment with reproducible gene universes and databases.
6. Visualization/reporting skills that consume registered evidence rather than recomputing
   untracked analyses.
7. Broader annotation evidence and consensus, including tissue-specific resources.
8. Repeated scientific evals across supported models and datasets.

The user may reprioritize this order. Keep each slice end-to-end and tested.

## 10. Required reading for the next task

Read these before editing:

1. `CLAUDE.md` (which imports `AGENTS.md`)
2. `docs/current-state.md`
3. `design/architecture.md`
4. `docs/skill-catalog.md`
5. `docs/scientific-parity.md`
6. this file for historical detail
7. `docs/environments.md` and `docs/capability-authoring.md` when relevant
8. the complete focused skill package and relevant legacy/BioNeMo requirements

Then run the baseline:

```bash
cd /home/ibrahih3/projects/scagent-sdk
source setup_gpu.sh
python -m pytest
ruff check src tests scripts .claude/skills/*/scripts
mypy src/scagent_sdk
scagent-sdk capability validate
scagent-sdk doctor environment
```

Do not modify anything until the baseline and the selected scientific slice are understood.

## 11. Historical completion record for the CellBender task

The next task is complete only when it has:

- written the scientific parity matrix from actual legacy/BioNeMo inspection;
- selected and justified one focused capability slice;
- implemented guidance, deterministic scripts, manifest/contracts, state effects, and floors;
- added proportionate tests and model-behavior evaluation guidance;
- passed tests, Ruff, mypy, capability validation, and runtime doctor;
- completed an appropriate live Iris validation or explicitly documented why scientifically valid
  live data is unavailable;
- inspected generated artifacts and recorded limitations;
- updated this handoff/roadmap with verified results;
- preserved project independence, resumability, provenance, and the simple terminal workflow.

All items above are satisfied for the CellBender capability-confirmation scope. The subsequently
completed `doublet-evidence` slice added per-library raw-count validation, Scrublet predicted-call
semantics, rate/failure warnings, review-first artifacts, explicit filtering lineage,
CellBender-ordering guidance, identity-aware staleness, deterministic tests, and a bounded live
Iris run. Current status and next corrections are authoritative in `docs/current-state.md`; do not
fold future work into preparation or a central tools module.
