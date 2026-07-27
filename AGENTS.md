# AGENTS.md

Operational instructions for coding agents working in this repository.

## Environment

For normal Iris work, activate from the repository root before any Python, tests, or CLI command:

```bash
source setup_gpu.sh
```

This synchronizes the locked uv control plane and locked Pixi compute runtimes, then activates only
the uv environment. It is safe to source repeatedly. For agent-only work after the environment has
already been bootstrapped, `source .venv/bin/activate` is sufficient.

The locks are authoritative:

- `uv.lock` and `.python-version` own the Python 3.12 control plane;
- `pixi.toml` and `pixi.lock` own RAPIDS, CellBender, and diffxpy compute runtimes.

Use uv, never bare pip, for control-plane changes. Use Pixi manifest/lock changes, never manual
pip/Conda mutation, for compute dependencies. Scientific packages execute through
`configs/environments/iris.toml`, not inside the agent venv. Read `docs/environments.md` before
changing environment behavior.

## Checks

```bash
python -m pytest
ruff check src tests scripts .claude/skills/*/scripts
mypy src/scagent_sdk
scagent-sdk capability validate
scagent-sdk doctor environment
```

Run the checks relevant to the change; run the complete set before finishing a scientific or
environment behavior change.

## Architecture constraints

- This project must run without `/data1/peerd/ibrahih3/cs_agent` and without the legacy
  `scagent` package installed.
- Do not import, subclass, or shell into `scagent`. Reimplement intentional scientific
  capabilities inside focused skills using standard libraries.
- The scientific session is authoritative. Model-runtime sessions are replaceable bindings.
- Persist state changes as events first; `state.json` is a replayable materialized view.
- Floors are state predicates, not tool-order pipelines.
- Scientific skill code belongs with the skill; do not grow a central biology or tools
  monolith in the runtime package.
- Executable skills require matching directory, SKILL.md, and `capability.yaml` IDs. Entrypoints
  write only beneath their supplied staging directory and return the standard result envelope.
- The model's capabilities are declared to it, not discovered by it. A skill needing a host asset
  it cannot download declares a `readiness` probe; never make the model search the filesystem to
  learn what is installed or cached.
- Never let a skill edit session state or event files directly; PostToolUse commits validated
  patches through the capability executor.
- Add deterministic tests for scripts/contracts and separate evals for model behavior.
- Read `docs/current-state.md` first for authoritative completed work, verified behavior, observed
  defects, and immediate priorities. Use `docs/handoff.md` for detailed historical context and
  `docs/skill-catalog.md` for the current capability taxonomy.

## Git

Do not commit or push unless the user asks.
