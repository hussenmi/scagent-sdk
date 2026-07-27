# Environment architecture

The project deliberately separates the agent control plane from scientific compute.

## Control plane: uv

The terminal, Claude Agent SDK, LiteLLM client, session store, skill discovery, hooks, media,
web tools, and environment broker run in `.venv`. `uv.lock` is authoritative and
`.python-version` selects managed CPython 3.12. No Scanpy, RAPIDS, scVI, CellBender, or diffxpy
stack is installed in this environment.

`source setup_gpu.sh` deactivates ambient virtualenv/Conda state, creates or synchronizes the
locked uv environment, and activates it. It is safe to source repeatedly. The script never
activates a compute environment in the user's shell and does not set `SCAGENT_GPU` globally.

## Compute plane: Pixi

`pixi.toml` and `pixi.lock` own three physical runtimes:

| Runtime | Purpose | Key stack |
|---|---|---|
| `rapids` | standard and GPU single-cell work | RAPIDS 26.04, CUDA 13.1, Scanpy, scVI, CellTypist, SCimilarity |
| `cellbender` | ambient-RNA removal | CellBender 0.3.2 and CUDA PyTorch |
| `diffxpy` | legacy-compatible differential testing | Python 3.9, diffxpy 0.7.4, TensorFlow 2.10.1 |

On Iris, `/home` is capacity-constrained, so project-local Pixi configuration places the actual
environments and cache under `/usersoftware/peerd/$USER`. `.pixi/envs` is a Pixi-managed symlink;
code and profiles can use its stable project-relative paths without depending on Pixi's hashed
detached directory name.

`scripts/bootstrap_compute.sh` configures this storage, installs all environments from the lock,
and runs a deterministic import/GPU smoke check the first time the lock changes. A content stamp
makes later `source setup_gpu.sh` calls fast. Override the storage base with
`SCAGENT_SDK_COMPUTE_STORAGE` and the Pixi executable with `SCAGENT_SDK_PIXI` when needed.

The RAPIDS feature uses cross-channel solving because the working CUDA 13 RAPIDS distribution has
overlapping package names across `conda-forge`, `rapidsai`, and `nvidia`, while Pixi has no Conda
`flexible` priority mode. This is scoped only to that feature. The committed lock then fixes exact
artifacts and hashes; installation always uses `--locked`.

## Physical runtimes and logical capabilities

`configs/environments/iris.toml` is a versioned host-routing table:

- `[runtimes.*]` describes a physical interpreter, prefix, loader variables, health probe, and
  provenance files.
- `[capabilities.*]` describes what a skill needs: required modules, GPU count/memory, timeout,
  and capability-specific variables.

Several logical capabilities can share one physical runtime. On Iris, `gpu-singlecell`, `scvi`,
`celltypist`, and `scimilarity` all route to `rapids-main`; the broker probes that interpreter once
and evaluates each logical contract separately. CellBender and diffxpy route to isolated runtimes
because their Python and dependency constraints differ.

Workers receive an allowlisted environment, with the selected prefix first on `PATH` and ambient
`VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`, and unrelated Conda state removed. Secrets are not
inherited unless a runtime explicitly names them. A missing module, GPU, memory requirement,
interpreter, lock/provenance file, or timeout fails closed—there is no silent CPU fallback.

Every capability result records both logical and physical fingerprints, interpreter, resolved
prefix, module versions, and CUDA information. The fingerprints include `pixi.toml` and
`pixi.lock`, so a lock change produces new provenance even if an executable path stays constant.

## Operator commands

Normal use:

```bash
source setup_gpu.sh
scagent start
```

Health and provenance:

```bash
scagent-sdk doctor environment
```

Explicit compute maintenance:

```bash
scripts/bootstrap_compute.sh
"$SCAGENT_SDK_PIXI" run --locked -e rapids check
"$SCAGENT_SDK_PIXI" run --locked -e cellbender check
"$SCAGENT_SDK_PIXI" run --locked -e diffxpy check
```

To change a compute dependency, edit `pixi.toml`, regenerate `pixi.lock`, and run all three checks
before using the new lock. Never install scientific packages into `.venv`, and never change a
locked compute prefix manually with `pip` or Conda.
