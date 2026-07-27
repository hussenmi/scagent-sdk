# ADR 0006: Provider-neutral terminal events and environment-routed skills

Status: accepted and implemented

## Decision

The model runtime emits provider-neutral message and tool-activity events. A Rich observer owns
terminal presentation, including reasoning, Markdown/LaTeX rendering, spinners, persistent tool
start/completion lines, summaries, and errors. Scientific skills do not print terminal UI.

Executable skills name logical environments. The environment broker maps those names to a
host-specific Python executable and environment variables, performs real import/GPU checks,
executes the handler across a JSON process boundary, and records an environment fingerprint.

The main agent runtime therefore remains small and independently installable. Scanpy, scVI,
CellTypist, SCimilarity, and future specialized stacks do not need to coexist in the agent venv.

## Consequences

- Other runtime backends can reuse the terminal without imitating Claude SDK message classes.
- A capability cannot silently fall back to the main CPU environment.
- External-worker import paths must not expose project module names as top-level modules; workers
  are bootstrapped through `runpy` from `python -c` to avoid standard-library name collisions.
- Host portability is configuration work rather than a rewrite of scientific skills.
