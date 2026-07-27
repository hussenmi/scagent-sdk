# ADR 0008: uv owns the control plane and Pixi owns compute runtimes

Status: accepted and implemented on Iris

## Context

The agent runtime needs a small, predictable Python environment, while single-cell analysis needs
large and sometimes mutually incompatible stacks. The legacy deployment proved that one RAPIDS
environment can efficiently serve Scanpy, scVI, CellTypist, and SCimilarity, but CellBender and
diffxpy need different Python/CUDA/dependency constraints. Activating `scagent_rapids` around the
whole agent leaked Conda and loader state into model, terminal, and session code and made the new
project depend operationally on legacy environments.

## Decision

- Use a uv-managed, locked Python 3.12 `.venv` for the agent control plane.
- Use one committed Pixi workspace and lockfile for independent `rapids`, `cellbender`, and
  `diffxpy` physical compute runtimes.
- Map logical skill capabilities onto physical runtimes in host-specific configuration.
- Execute every non-current capability in an isolated subprocess with an allowlisted environment.
- Fingerprint the physical runtime and logical contract separately and persist both.
- Make `source setup_gpu.sh` bootstrap both planes from locks, validate changed compute locks, and
  remain a sub-second no-op when stamps and interpreters are current.
- Retain no runtime dependency on the legacy `scagent` package or its Conda environments.

## Consequences

The user keeps the simple `source setup_gpu.sh; scagent start` workflow. Skills select capabilities
rather than package managers or absolute activation commands. Large scientific stacks remain
isolated, reproducible, and replaceable per host. First-time setup is heavier because it creates
three locked environments, while repeat setup is fast. Host storage and GPU details remain an
explicit deployment concern rather than leaking into scientific skills.

