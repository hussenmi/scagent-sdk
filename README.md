# scagent-sdk

A skill-driven single-cell RNA-seq analysis agent built on the Claude Agent SDK.

The model reasons about the analysis and chooses the path. Focused skills carry the scientific
workflow knowledge and the deterministic programs that do the actual computation. Hooks enforce a
small number of state-based scientific floors rather than a fixed pipeline. Every run is
reproducible from durable events and immutable artifacts.

## Setup

On Iris, from the project root:

```bash
source setup_gpu.sh
scagent start
```

`setup_gpu.sh` syncs and activates the locked uv control plane (Python 3.12), ensures the locked
Pixi compute runtimes (RAPIDS, CellBender, diffxpy) exist, and starts the configured LiteLLM
gateway. It is safe to source repeatedly; the first run may take a while to build environments.

Start with a dataset, or resume:

```bash
scagent start --data /absolute/path/to/matrix.h5 --prompt "Analyze this PBMC dataset."
scagent start --resume            # latest session
scagent start --resume <session-id>
```

Inside the terminal, `/state`, `/session`, `/skills`, `/help`, and `/exit` are local commands.

Optional: put `TAVILY_API_KEY` in an untracked `.env` (see `.env.example`) to enable web research.

## Repository layout

```text
src/scagent_sdk/    runtime: sessions, state, events, capability execution, floors, CLI, terminal
.claude/skills/     focused skills — SKILL.md, capability.yaml, scripts/, tests
configs/            model profiles and environment definitions
docs/               status, catalogs, and guides
design/             architecture
tests/              unit and integration tests
sessions/           per-session working directories (generated)
```

Each session is self-contained:

```text
sessions/<session-id>/
├── session.json, state.json, events.jsonl    durable scientific state
├── outputs.md, outputs.json                  readable + machine index
├── code/ figures/ reports/ tables/ data/     human-facing view (symlinks)
└── artifacts/capabilities/<execution-id>/    immutable canonical artifacts
```

Artifacts under `artifacts/capabilities/` are authoritative; the browsing folders are disposable
and rebuilt from `state.json` and `events.jsonl`.

## Checks

```bash
python -m pytest
ruff check src tests scripts .claude/skills/*/scripts
mypy src/scagent_sdk
scagent-sdk capability validate
scagent-sdk doctor environment
```

`scagent-sdk` is the command name; `scagent` is an equivalent alias installed in this project's
venv.

## Documentation

- [`docs/current-state.md`](docs/current-state.md) — authoritative status, verified behavior, and
  known defects.
- [`design/architecture.md`](design/architecture.md) — session, runtime, capability, and floor
  architecture.
- [`docs/skill-catalog.md`](docs/skill-catalog.md) — skill, tool, environment, and floor inventory.
- [`docs/environments.md`](docs/environments.md) — the uv/Pixi split and runtime routing.
- [`docs/capability-authoring.md`](docs/capability-authoring.md) — how to add a focused skill.
- [`AGENTS.md`](AGENTS.md) — instructions for coding agents working in this repository.
