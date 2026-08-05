# ADR 0010: On-request package installs use a per-user overlay, never the locked base

Status: accepted; P0 implemented and live-validated on the rapids-main runtime on branch
`feat/on-request-package-installs` (uv resolver, additive/conflict/unavailable classification,
overlay install/import/remove all exercised against the real conda env)

## Context

Users sometimes need a Python package mid-analysis that the base environment does not ship
(the recurring legacy examples were `gseapy`, `mygene`, `biomart`). The analysis must not stall
on an operator round-trip for every such case.

The legacy `scagent` had a first-class answer: an LLM-callable `install_package` tool, human-gated
with a deny-by-default prompt, that ran `uv pip install --python <sys.executable>` — i.e. straight
into the agent interpreter's own `site-packages`. That design carried a latent contradiction with
how the tool was actually deployed. Lab members ran a shared, read-only Lmod module whose compute
prefix was `chmod 555`; installing into that prefix would either fail outright or, if writable,
silently mutate the environment every other user shared and invalidate reproducibility. The feature
only worked in a developer's writable env.

This version's architecture makes a direct port doubly wrong:

- Locks are authoritative. `pixi.toml`/`pixi.lock` install `--locked`; `AGENTS.md` forbids `pip`
  or Conda into a locked compute prefix and forbids installing scientific packages into `.venv`.
- Provenance is fingerprinted. Every capability result records the physical runtime fingerprint,
  which includes the `pixi.toml`/`pixi.lock` hashes. An ad-hoc install into that prefix invisibly
  invalidates every downstream fingerprint.
- Deployment is again a shared read-only base (ADR 0008; the legacy module model). We cannot, and
  must not, install into the shared runtime at a user's request.

Custom scientific code — the overwhelming reason a user wants a new package — runs through
`run_analysis_code`, which declares `environment: gpu-singlecell` and routes to the `rapids-main`
runtime in a subprocess. `environment: current` tools run in-process in the control-plane `.venv`
(`executor.py` builds `{"name": "current", "python": sys.executable}` for those). So the relevant
install target is a compute runtime, defaulting to `rapids-main`, not the control plane.

## Decision

On-request installs go into a **persistent, per-user, per-runtime overlay** layered on top of the
immutable base runtime. The shared locked base is never mutated. Every install is gated by a
**dependency dry-run** that only admits additive (non-overlapping) packages, so the composed import
environment can never shadow the base stack.

### Model-facing tools

A `manage-environment` skill declares the install door (the model still may not shell
`pip`/`uv`/`conda`, matching "capabilities are declared, not shell-discovered"). Following the
investigate→decide split, install is two tools plus list/remove:

- `propose_package_install(package, reason)` — resolves and classifies, records a proposal, installs
  nothing.
- `install_proposed_package(install_request_id, question_asked)` — installs an additive proposal
  after verifying user approval.
- `list_installed_packages()` / `remove_installed_package(package)` — inspect and prune the overlay.

The tools declare `environment: overlay-admin`, a logical capability that routes to the
`rapids-main` runtime with `gpu_required = false` and no required modules. So the entrypoints run
*inside* the runtime where custom analysis code runs: `sys.executable` is that interpreter, pip
resolves against the real base, and the broker hands the overlay directory to the tool via the
`SCAGENT_OVERLAY_DIR` environment variable (declared, not discovered). No executor special-casing
is needed — install is a normal environment-routed capability whose runtime is a no-GPU view of
rapids-main.

### Human approval

Reuse the version-native consultation pattern, not an inline terminal yes/no. `propose_...` stores
a `consultation_anchor_sequence` (the newest user-turn sequence at proposal time).
`install_proposed_package` verifies approval against the event log through
`CapabilityContext.user_turns` (`capabilities/results.py`): the approving turn must be the first
real user turn *after* the anchor, so the original request can never be replayed as authorization.
The model supplies only `question_asked`; the runtime resolves the verbatim answer. No approving
turn ⇒ the install is refused.

### The dry-run safety gate

The base prefix cannot be corrupted on disk (installs target a separate overlay dir, and in
deployment the prefix is read-only). The real risk is the *composed* import environment: an overlay
package whose dependency closure pulls a different `numpy`/`pandas`/`scipy`/CUDA build than the
RAPIDS stack pins, shadowing it on `PYTHONPATH` and breaking `cudf`/`cupy`/`scanpy` imports.

Before any install and before the approval gate resolves, resolve the full closure without
installing and classify it:

1. Load the target runtime's **base inventory** — `pip list --format=json` for that runtime, cached
   and keyed by the runtime's `pixi.lock` hash (so an operator base redeploy regenerates it and
   makes every overlay's recorded `base_lock` visibly stale).
2. `uv pip install <pkg> --dry-run --report - --target <tmp> -c <base-constraints>` (pip fallback).
3. Classify:
   - **Additive** — closure is absent from base or matches base versions exactly → install to overlay.
   - **Near-miss** — one retry with base versions pinned as constraints; if it then resolves clean,
     treat as additive.
   - **Conda-only** — no PyPI wheel/sdist exists → refuse, route to the operator/pixi path.
   - **Incompatible** — still conflicts after pinning → refuse; needs an isolated runtime (see below).

Only additive packages ever enter the overlay, so nothing on `PYTHONPATH` overlaps the base and
ordering can never shadow the RAPIDS stack. This is the invariant that makes the overlay safe.

The gate runs on the target runtime, so the resolver is environment-aware: its plan is exactly the
set it would add or change relative to the base. The additive set is then materialized into the
overlay with `--target <overlay> --no-deps <name>==<version> …` — the exact resolved pins, so no
base distribution is duplicated and no re-resolution can drift.

**uv is the default resolver, pip the fallback** (`resolve_package` / `materialize` pick uv when a
uv binary is reachable via `SCAGENT_SDK_UV`, else pip). uv is pixi's own PyPI resolver, so it is
the native tool here: measured against the real rapids conda env it reads conda-installed
distributions correctly (it even reports their conda-forge origin), classifies identically to pip,
and is ~5–15× faster. uv's `--dry-run` plan (stderr) is `+ name==ver` / `- name==ver` lines: a `+`
with no matching `-` is additive; a `-` (a base distribution uv would uninstall) paired with a `+`
is a version change — a conflict. `parse_uv_plan` maps that onto the additive/conflict buckets with
no separate inventory call. The pip fallback uses `pip install --dry-run --report` and classifies
its `install` list against a `pip list` inventory (`classify_report`); both paths return the same
shape, so uv and pip stay interchangeable and the classified set always equals the installed set.

Two things live testing caught and fixed: (1) an early `--ignore-installed` on the pip dry-run made
it re-resolve the entire base closure from PyPI — wrong for the gate and pathologically slow on a
bleeding-edge Python; removed. (2) removal cannot delete a `--target` package's files by RECORD,
because an import leaves a `__pycache__` the RECORD does not list, so a directory package survives
as an importable namespace remnant; removal instead **rebuilds the overlay from the remaining pins**
(wipe + reinstall), which is deterministic and fast with uv.

### Storage — persistent across sessions

```
/usersoftware/peerd/$USER/scagent-overlay/<runtime>/<base-lock-hash>/site-packages...
```

Keyed by `(user, runtime, base-lock)`, written with `uv pip install --target`. It is **not**
session-scoped, so a package installed once survives session exit and is present whether the user
resumes the same session or starts a new one — no reinstall. Scoping the directory by base-lock
hash keeps packages resolved against different bases from mixing.

### Injection seam

`ExecutionRuntime.build_environment` (`execution/profile.py`) assembles the allowlisted worker env
and currently unconditionally pops `PYTHONPATH`. The overlay hooks in exactly there: when a runtime
runs, prepend its overlay directory to the worker `PYTHONPATH`. The executor's `current`-vs-runtime
split is untouched.

### Provenance

A new `environment_overlay` fact records, per runtime, each package's name, resolved version,
`reason`, install time, and the `base_lock` it was resolved against. Every capability result's
environment fingerprint additionally records the **active overlay set for its runtime**, so a
result stays fully explainable ("base lock X + gseapy 1.1.3") and resume can detect an overlay that
changed between sessions. Storage is persistent; provenance is per-result — that separation gives
install-once convenience without losing per-result reproducibility.

### Incompatible packages

A genuinely incompatible package is not force-fit and is never resolved by cloning the RAPIDS stack
(~17 GiB, and a separate clone cannot co-import the existing data anyway). It escalates to an
**isolated sidecar runtime** used via cross-process handoff — the exact pattern `cellbender` and
`diffxpy` already use because their pins cannot coexist with `rapids-main`. This is
operator-mediated and out of the initial scope.

## Consequences

- The shared locked base is never written at a user's request, on disk or in provenance. The legacy
  read-only contradiction is designed out: overlays live under per-user storage this version
  already owns.
- Users install once and keep the package across exit, resume, and new sessions.
- Results remain reproducible and explainable; drift between a persistent overlay and a redeployed
  base is a visible signal (`doctor environment`), not silent breakage.
- A `list_overlay_packages` / `remove_overlay_package` surface is included so a persistent overlay
  stays inspectable and prunable.
- Broadly useful packages graduate: the operator promotes one into `pixi.toml`, re-locks, and it
  leaves everyone's overlay for the shared base.

### Build order

- **P0 (landed and live-validated on this branch)** — `manage-environment` skill with the
  propose/install/list/remove tools, the `overlay-admin` capability, the **uv-first** (pip-fallback)
  additive/conflict gate, consultation approval, `--target --no-deps` overlay install, rebuild-based
  remove, broker `PYTHONPATH` injection + `SCAGENT_OVERLAY_DIR`, the `environment_overlay` session
  fact, and overlay provenance on every runtime result. Conflicts, conda-only, and unavailable
  packages are refused with a clear reason. Exercised end-to-end against the real rapids conda env.
- **P1** — constrained-retry rescue for near-misses; `doctor environment` surfaces overlay-vs-base
  drift and graduation hints.
- **P2** — isolated sidecar runtime for incompatible packages (operator-mediated, reusing the
  cellbender/diffxpy pattern) and graduation tooling into `pixi.toml`.

### Governance (decided: open)

The install policy is **open**: any PyPI package may be requested, gated only by the additive
dependency check and explicit user approval. No allowlist, version pinning, or private index for
now. The rationale is that this is a small, trusted lab tool, and the intended posture is that the
**base environment carries the common single-cell stack**, so on-request installs are the exception
(an occasional `gseapy`/`mygene`), not the normal path. The overlay is the escape hatch, not the
primary way packages arrive.

Consequence to keep in view: the additive gate ensures compatibility, not trust. Revisit this if the
deployment widens beyond a trusted group, runs a smaller/local model whose judgment is weaker, or
begins touching sensitive credentials — at which point an operator-curated allowlist (checked before
the additive gate) is the cheapest structural defense, and the graduation path (promote a common
overlay package into `pixi.toml`) doubles as the "add it to the allowlist" workflow.

### Base-environment curation (preferred path)

Because installs are open and unversioned, the shared base should carry the packages a single-cell
analysis normally needs so users rarely reach for the overlay. An audit of the `rapids` feature
(2026-08-04) found the base already well-provisioned; three genuinely-useful packages were added to
`pixi.toml` and the lock: **`mygene==3.2.2`** (enables `inspect-dataset`'s opt-in online gene-id
conversion, previously inert), **`harmonypy==0.0.10`** (Harmony integration, wired into
`scanpy.external.pp.harmony_integrate`), and **`gseapy==1.3.1`** (GSEA enrichment). `scrublet` was
deliberately *not* added — `doublet-evidence` uses scanpy's built-in `sc.pp.scrublet`. `decoupler`,
`scanorama`, `bbknn`, `scib-metrics` (pulls JAX), and others were left to on-request overlay
installs.

Lesson worth keeping: **additive resolution is not a guarantee the package builds.** `harmonypy`
resolved additive at its latest `2.0.0`, but that is a C++/armadillo rewrite with no py3.14 wheel
that fails to build from source; the pure-Python `0.0.x` line scanpy actually uses installs cleanly.
The same gap exists in the overlay gate — a dry-run can classify a package additive that then fails
at build time; the install step surfaces the build error and installs nothing, but the propose step
cannot promise a build will succeed. Validate installability (build + import), not just resolution,
before pinning anything into the base.
