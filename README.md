# scagent-sdk

An independent, skill-driven single-cell analysis agent built on the Claude Agent SDK.

This project starts cleanly. It may reuse scientific lessons, algorithms, and validation
ideas from `cs_agent`, but it must not import, subclass, shell into, or require the existing
`scagent` package. The new system owns its runtime, state contracts, skills, deterministic
executors, environment profiles, floors, tests, and evaluation harness.

## Design intent

- The model reasons about the analysis and chooses the path.
- Skills carry scientific workflow knowledge and executable, testable capabilities.
- Deterministic programs handle brittle computation, validation, and data access.
- Hooks enforce a small number of state-based scientific floors, not a tool-order DAG.
- Capability tools are discovered from skill packages rather than declared in one giant file.
- Local-model transport is replaceable and does not leak into scientific skills.
- Every run is reproducible, inspectable, and replayable from structured events and artifacts.

The initial architecture proposal is in [`design/architecture.md`](design/architecture.md).

## Session files and data lifetime

Each scientific session is self-contained:

```text
sessions/<session-id>/
├── session.json
├── state.json
├── events.jsonl
├── outputs.md
├── outputs.json
├── code/
├── figures/
├── reports/
├── tables/
├── data/
│   └── intermediates/
├── artifacts/capabilities/<execution-id>/
├── logs/
└── runtime/
```

Capability results return a canonical absolute `artifact_path` and an absolute `path` for every
file, plus session-relative paths for portable provenance. Pass the absolute file path to a later
tool. Session-relative artifact paths are also resolved against the current scientific session.

The UUID-keyed capability directories are the immutable provenance layer, not the primary browsing
interface. After every commit and resume, the SDK rebuilds a human-facing view in `code/`,
`figures/`, `reports/`, `tables/`, and `data/`. These are relative symlinks to committed artifacts,
so even multi-gigabyte H5AD files consume no additional storage. Descriptive names retain a short
execution-ID suffix for unambiguous provenance. `outputs.md` is the readable index, `outputs.json`
is its machine-readable counterpart, and finalization outputs also receive stable
`data/final-annotated.h5ad`, `reports/final-analysis-report.md`, `tables/final-labels.csv`,
and `code/analysis-recipe.py`
aliases when present. The view is disposable and reconstructible from `state.json` and
`events.jsonl`; authoritative paths always remain under `artifacts/capabilities/`.
The human `reports/` view intentionally contains readable Markdown/PDF/HTML deliverables only.
Machine JSON/YAML remains in canonical capability artifacts and in `outputs.json`, preserving
complete provenance without overwhelming the report folder.
Nested artifact groups such as cluster-structure panels retain descriptive subdirectories. If view
materialization encounters a filesystem error, the committed result remains valid and the failure
is recorded as `session.output_view_refresh_failed`.

AnnData is artifact-backed rather than process-global. A scientific capability loads its input
H5AD, computes, writes a new immutable artifact when needed, and exits; the Python object is
released between capability calls and turns. Durable state records which artifact and identities
are current. This allows compute runtimes to remain isolated and sessions to resume after process
or host changes.

## Quick start on Iris

From the project directory:

```bash
source setup_gpu.sh
scagent start
```

That command synchronizes and activates the locked uv agent runtime, ensures the locked Pixi
RAPIDS/CellBender/diffxpy compute runtimes exist, checks/starts the configured LiteLLM gateway for
the session, and opens the Rich terminal. The first setup may take time to build compute
environments; subsequent activation is normally sub-second. No separate model-server command is
needed when the configured vLLM service is available.

Start with a dataset and an initial goal:

```bash
scagent start \
  --data /absolute/path/to/matrix.h5 \
  --prompt "Perform a careful PBMC analysis and explain each material decision."
```

Resume the latest session or a specific one:

```bash
scagent start --resume
scagent start --resume <session-id>
```

The terminal then asks how to continue:

- **Automatic** (recommended) uses exact model history while it fits and reconstructs from the
  durable scientific checkpoint when it does not;
- **Exact** requires the compatible model conversation and never silently falls back;
- **Reconstructed** starts a fresh model conversation from persisted facts, decisions, artifact
  references, events, and a bounded recent-turn handoff.

Unless `--profile` is supplied explicitly, resume uses the model profile recorded in the session
so compatible exact history is not invalidated merely because the current project default changed.

The context ceiling is resolved per model/deployment. For LiteLLM aliases, the resolver follows
`/model/info` to the concrete upstream model and reads the vLLM `/v1/models` advertisement; hosted
models use provider metadata when available, and the Claude Agent SDK's `rawMaxTokens` is an
in-turn fallback. The Iris Qwen service currently advertises 262,144 tokens.

Inside the terminal, `/state`, `/session`, `/skills`, `/help`, and `/exit` are local commands.
Model reasoning is dimmed when the gateway exposes it; final Markdown is rendered clearly; and
capabilities report persistent progress such as `▶ Running code...` and `✓ Running code done`.
After every completed turn, the terminal's bottom-right corner shows live SDK context usage, for
example `▓▓▓░░░░░░░ 38% · 100K/262K`. The bar is owned by prompt_toolkit's bottom toolbar, so
prompt redraws cannot erase it; the default full-row reverse-video style is explicitly cancelled,
so only the indicator glyphs are visible. A native compaction or automatic conversation rollover
replaces the old high-water value with the new epoch's lower usage rather than leaving a stale
estimate.

Ask about a local image or PDF by giving its absolute path. The agent calls the media skill and
receives real image pixels or rendered PDF pages, not a path-only placeholder. To enable current
web research, place `TAVILY_API_KEY` in the project's untracked `.env` (see `.env.example`) or
export it before `scagent start`. Set `SCAGENT_SDK_ENV_FILE` to use another dotenv file explicitly.

Verify the installation and every configured scientific environment:

```bash
scagent-sdk capability validate
scagent-sdk doctor environment
```

`scagent-sdk` is the explicit command name; `scagent` is an equivalent convenience alias installed
only in this project's isolated venv.

## Current implementation

The project now provides a complete first scientific vertical slice:

- versioned `session.json` and `state.json`;
- append-only, ordered, fsynced `events.jsonl`;
- atomic state checkpoints and replay of unapplied events after interruption;
- model-runtime bindings kept separate from scientific state;
- exact resume for a compatible bound runtime session;
- reconstructed resume from durable facts/artifacts otherwise;
- per-model context-limit discovery and preflight model-conversation rollover without changing
  the scientific session;
- an interactive automatic/exact/reconstructed resume chooser for `scagent start --resume`;
- CLI create, list, show, bind-runtime, and resume operations.
- fingerprinted TOML model profiles with secret-free configuration;
- a provider-neutral runtime protocol and Claude Agent SDK backend;
- LiteLLM health and Anthropic Messages diagnostics;
- exact resume, reconstructed resume, and intentional scientific/SDK forks;
- project-controlled mirroring of Claude SDK transcripts;
- durable turn prompts, normalized responses, usage, and failures.
- deny-by-default, composable MCP tool and hook extensions;
- an interactive, resumable chat loop;
- live runtime diagnostics for tool use, hooks, retries, reasoning, and result limits;
- a verified Iris LiteLLM profile for the local `Qwen3.6-27B` vLLM service.
- validated executable skill discovery under `.claude/skills`;
- dynamic MCP construction with exact tool allowlists;
- two-phase PostToolUse commits and crash recovery;
- content-fingerprinted skills and 48 KiB artifact spill;
- a deterministic `inspect-dataset` skill package;
- provider-neutral Rich runtime observation and a resumable interactive terminal;
- per-session automatic LiteLLM startup and owned-process teardown;
- a host-configured environment broker with real imports, GPU checks, timeouts, and fingerprints;
- a uv-managed Python 3.12 control plane plus locked, independently validated Pixi RAPIDS,
  CellBender, and diffxpy physical runtimes;
- bounded file inspection, read-only shell diagnostics, and saved restricted custom Python;
- multimodal image inspection and PDF text-plus-page review through bounded reusable skills;
- source-linked Tavily search and public-page retrieval with private-network blocking;
- preprocessing/clustering, cluster QC, and explicit batch-investigation skills;
- strict raw-droplet validation and GPU-only CellBender ambient-background removal with corrected
  count lineage, failure artifacts, and downstream invalidation;
- GPU Scrublet evidence on verified raw counts, normally per library, plus an explicit
  review-before-filtering transition with cell-set lineage and downstream invalidation;
- marker, CellTypist, and SCimilarity annotation-evidence skills;
- an optional scVI integration skill that issues new representation/clustering identities;
- state-based PreToolUse floors and gated final annotated H5AD/report publication.

The first vertical slice was exercised on the PBMC 10k 10x H5 input on Iris: inspection,
preprocessing, 17-cluster QC, batch decision, marker evidence, CellTypist, SCimilarity, final
publication, exact terminal resume, and an independent two-epoch GPU scVI API smoke run.
CellBender was additionally validated on a genuine 396,297-barcode raw 10x matrix: the paired
filtered matrix was refused, GPU inference trained normally, and a controlled timeout committed
logs and provenance without replacing the active dataset.

The latest PBMC doublet run produced 565 Scrublet predicted calls among 11,043 cells (5.12%) and
retained enriched clusters for biological/QC review rather than deleting them. A 26-sample batch
case correctly recorded `request_guidance` when strong sample structure could not be separated
from study design. These are representative live checks, not claims of general biological
validity.

Developer setup and verification:

```bash
source setup_gpu.sh
python -m pytest
ruff check src tests
mypy src/scagent_sdk
```

Create and inspect a session:

```bash
.venv/bin/scagent-sdk session new --title "PBMC analysis"
.venv/bin/scagent-sdk session list
.venv/bin/scagent-sdk session show <session-id>
.venv/bin/scagent-sdk session resume <session-id> --model-profile local-default
.venv/bin/scagent-sdk session fork <session-id> --title "Alternative analysis"
.venv/bin/scagent-sdk model show local-default
.venv/bin/scagent-sdk doctor model --profile local-default --no-network
.venv/bin/scagent-sdk capability validate
.venv/bin/scagent-sdk capability list
```

For model/gateway compatibility details, follow
[`docs/runtime-compatibility.md`](docs/runtime-compatibility.md).
For the uv/Pixi split, runtime routing, and maintenance commands, follow
[`docs/environments.md`](docs/environments.md).

Run one model turn when the configured gateway is available:

```bash
.venv/bin/scagent-sdk agent run <session-id> \
  --profile local-default \
  --prompt "Inspect the current analysis state and continue."
```

Or continue multiple turns interactively:

```bash
.venv/bin/scagent-sdk agent chat <session-id> --profile iris-qwen36
```

See [`docs/roadmap.md`](docs/roadmap.md) for completed and future capability phases.
See [`docs/current-state.md`](docs/current-state.md) for the authoritative dated status, verified
claims, known defects, and immediate priorities.
See [`docs/skill-catalog.md`](docs/skill-catalog.md) for the categorized skill/tool/environment/floor
inventory.
See [`docs/first-test.md`](docs/first-test.md) for the exact Iris end-to-end trial.
See [`docs/capability-authoring.md`](docs/capability-authoring.md) to add a focused executable skill.
