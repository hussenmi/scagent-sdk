"""Launch the synthetic cluster-cleanup acceptance under the gpu-singlecell compute runtime.

Runs in the agent venv: resolves the gpu-singlecell environment profile (interpreter + scrubbed
environment) exactly as the broker would, then subprocesses the compute-env acceptance script so it
can import anndata/scanpy/scipy with the correct CUDA loader environment.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Import capabilities.executor first to resolve the execution/capabilities import cycle,
# mirroring the working live-validation scripts.
from scagent_sdk.capabilities.executor import CapabilityExecutor  # noqa: F401
from scagent_sdk.execution import EnvironmentRegistry


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    registry = EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
    profile = registry.resolve("gpu-singlecell")
    environment = profile.build_environment()
    script = root / "scripts" / "validate_cluster_cleanup_synthetic.py"
    completed = subprocess.run(
        [str(profile.python), str(script)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    sys.stdout.write(completed.stdout)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr[-4000:])
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
