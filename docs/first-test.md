# First end-to-end test on Iris

## 1. Start from the project

```bash
cd /home/ibrahih3/projects/scagent-sdk
source setup_gpu.sh
```

Optional preflight:

```bash
scagent-sdk capability validate
scagent-sdk doctor environment
```

The environment doctor should report six healthy logical profiles. `gpu-singlecell`, `scvi`,
`scimilarity`, and `cellbender` require GPUs; `celltypist` and `diffxpy` do not. Shared RAPIDS
profiles should have the same physical runtime fingerprint.

## 2. Start an analysis

For the workshop PBMC input:

```bash
scagent start \
  --data /data1/peerd/sharmar1/workshop_2025_files/workshop_data/pbmc_data/pbmc_10k_v3_filtered_feature_bc_matrix.h5 \
  --title "PBMC 10k trial" \
  --prompt "Perform a careful end-to-end PBMC analysis. Inspect the input, choose and explain QC parameters, preprocess and cluster, evaluate cluster QC, investigate whether batch correction is applicable, obtain marker plus independent reference annotation evidence, adjudicate conservative labels, and publish the final dataset and report. Do not bypass a floor; show caveats and artifact paths."
```

Expect a Rich welcome panel, a reasoning spinner, clearly rendered model text, and durable tool
activity lines such as:

```text
▶ Preparing single-cell data...
✓ Preparing single-cell data done
```

The agent may stop for scientific clarification or replan after warnings; that is expected. It
should not claim completion until finalization floors pass.

## 3. Inspect and resume

Within the terminal:

```text
/state
/skills
/session
/exit
```

Resume the most recent session:

```bash
source setup_gpu.sh
scagent start --resume
```

Or copy the session ID from the welcome panel:

```bash
scagent start --resume <session-id>
```

Session state, events, model transcripts, code, logs, and artifacts live under
`sessions/<session-id>/`. Exiting preserves them. LiteLLM is stopped only when this process started
it; a pre-existing gateway is left running.

## 4. Current test boundary

This release is ready for the complete first vertical slice: inspection, standard Scanpy
preprocessing/clustering, cluster QC, batch decision, optional scVI, marker evidence, CellTypist,
SCimilarity, gated label finalization, custom Python, and resume.

It is not yet full legacy `scagent` parity. The CellBender runtime is installed and brokered, but
the CellBender scientific skill and explicit doublet workflow are not implemented yet.
Harmony/BBKNN/Scanorama, sample-aware DEG/pseudobulk, pathways, and dedicated downstream reporting
skills also remain future capability packages. The agent should report those as unavailable rather
than inventing results.
