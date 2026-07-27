@AGENTS.md

# Claude Code project context

This repository is the active implementation of `scagent-sdk`, an independent, skill-driven
single-cell RNA-seq analysis agent. The product goal is flexible model reasoning plus
deterministic scientific execution and identity-aware state floors—not a fixed DAG and not a new
giant prompt or tools module.

## Read before editing

1. `docs/current-state.md` — authoritative dated status, verified behavior, known defects, and
   immediate priorities.
2. `design/architecture.md` — durable-session, runtime, capability, and floor architecture.
3. `docs/skill-catalog.md` — current skill taxonomy, tools, environments, floors, and maturity.
4. `docs/scientific-parity.md` — legacy scientific responsibilities and remaining migration gaps.
5. The complete focused skill package being changed: `SKILL.md`, `capability.yaml`, `scripts/`,
   `references/`, `evals/`, and tests.

Use `docs/handoff.md` as detailed historical context, not as the first or only status source.

## Non-negotiable boundaries

- The active repository is `/home/ibrahih3/projects/scagent-sdk` on Iris.
- Legacy `/data1/peerd/ibrahih3/cs_agent/scagent`, BioNeMo contributed skills, and
  `/home/ibrahih3/projects/sdk-floor-proto` are read-only design/science references. Never import,
  invoke, vendor, or require legacy `scagent` at runtime.
- Put scientific implementation beside its focused skill. Keep base model instructions concise.
- Floors enforce consequential state predicates; they do not prescribe normal tool order.
- Treat deterministic tests, live compute, model behavior, and biological generality as four
  distinct validation levels. Never imply one proves another.
- Preserve durable sessions, exact/reconstructed resume, immutable artifacts, lineage,
  non-overwrite, environment provenance, and the 48 KiB inline-result limit.
- Do not commit or push unless the user asks.

## Normal workflow

```bash
cd /home/ibrahih3/projects/scagent-sdk
source setup_gpu.sh
scagent start
```

Before finishing a scientific or environment change, run the baseline in `AGENTS.md`, inspect the
actual generated artifacts, and update `docs/current-state.md` plus the relevant catalog/parity
entry. Start every new task by comparing the requested work with the explicit P0 corrective items
in `docs/current-state.md`; do not begin a broad rewrite or rerun expensive CellBender/scVI jobs
without a specific scientific reason.
