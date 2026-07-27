"""Live Iris acceptance for SCimilarity organism verification and reference atlas queries.

Three things are checked against real assets, in increasing cost:

1. a declared organism that the input genes contradict is refused *before* any model loads;
2. the refusal is overridable, deliberately, and the override is recorded; and
3. a real reference atlas query returns per-population reference composition, distances, and
   coherence, with small groups reported as skipped rather than silently queried.

Step 3 loads the organism's cell-search index into memory (46.9 GiB for human on this host),
so it runs once and queries every group in one call. Pass --skip-query to run only 1 and 2.

The ground-truth labels shipped with the CRC benchmark are used as the query grouping so the
returned reference composition can be judged biologically, not just structurally.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.executor import CapabilityExecutor
from scagent_sdk.capabilities.registry import CapabilityRegistry, SkillPackage
from scagent_sdk.execution import EnvironmentBroker, EnvironmentRegistry
from scagent_sdk.session import AnalysisSession

DEFAULT_INPUT = (
    "/data1/peerd/ibrahih3/cs_agent/test_data/crc_benchmark/CRC_KG136P_counts.h5ad"
)
DEFAULT_TRUTH = (
    "/data1/peerd/ibrahih3/cs_agent/test_data/crc_benchmark/CRC_KG136P_truth.csv"
)
GROUP_KEY = "truth_cell_type"

# Exact labels of the human annotation model (698 of them), covering colorectal epithelium.
COLORECTAL_SAFELIST = [
    "enterocyte",
    "colonocyte",
    "BEST4+ enterocyte",
    "goblet cell",
    "colon goblet cell",
    "brush cell",
    "tuft cell of colon",
    "paneth cell of colon",
    "intestinal crypt stem cell of colon",
    "transit amplifying cell of colon",
    "enteroendocrine cell",
]

_FIXTURE_SOURCE = '''
import json, sys
import pandas as pd
import scanpy as sc

source, truth, destination, group_key = sys.argv[1:5]
adata = sc.read_h5ad(source)
labels = pd.read_csv(truth, index_col=0)
adata.obs[group_key] = labels.loc[adata.obs_names, "cell_type"].astype(str).to_numpy()
adata.write_h5ad(destination, compression="gzip")
print(json.dumps({
    "cells": int(adata.n_obs),
    "genes": int(adata.n_vars),
    "groups": {str(k): int(v) for k, v in adata.obs[group_key].value_counts().items()},
}))
'''


def _package(packages: tuple[SkillPackage, ...], skill_id: str) -> SkillPackage:
    return next(package for package in packages if package.manifest.skill_id == skill_id)


def _tool(package: SkillPackage, name: str) -> Any:
    return next(tool for tool in package.manifest.tools if tool.name == name)


async def _execute(
    executor: CapabilityExecutor,
    package: SkillPackage,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, Any]:
    """Run one tool, returning (ok, envelope-or-error-text) instead of raising."""

    response = await executor.execute(package, _tool(package, tool_name), arguments)
    if response.get("is_error"):
        return False, str(response["content"][0]["text"])
    envelope = response["structuredContent"]
    executor.commit(str(envelope["scagent_execution_id"]))
    return True, envelope


def _build_fixture(
    broker: EnvironmentBroker, source: Path, truth: Path, destination: Path
) -> dict[str, Any]:
    """Attach the benchmark's ground-truth labels, using the project's own compute runtime."""

    profile = broker.registry.resolve("scimilarity")
    completed = subprocess.run(
        [
            str(profile.python),
            "-c",
            textwrap.dedent(_FIXTURE_SOURCE),
            str(source),
            str(truth),
            str(destination),
            GROUP_KEY,
        ],
        capture_output=True,
        text=True,
        env=profile.build_environment(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"fixture preparation failed: {completed.stderr[-2000:]}")
    return dict(json.loads(completed.stdout.strip().splitlines()[-1]))


def _truth_group_sizes(truth: Path) -> dict[str, int]:
    with truth.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    sizes: dict[str, int] = {}
    for row in rows:
        sizes[row["cell_type"]] = sizes.get(row["cell_type"], 0) + 1
    return sizes


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    packages = CapabilityRegistry(root / ".claude" / "skills").discover()
    scimilarity = _package(packages, "scimilarity-annotation")
    broker = EnvironmentBroker(
        EnvironmentRegistry.from_path(root / "configs" / "environments" / "iris.toml")
    )
    source = Path(args.input).expanduser().resolve()
    truth = Path(args.truth).expanduser().resolve()
    fixture = Path(args.fixture).expanduser().resolve()
    fixture.parent.mkdir(parents=True, exist_ok=True)
    prepared = _build_fixture(broker, source, truth, fixture)

    results: dict[str, Any] = {
        "input": str(source),
        "fixture": {"path": str(fixture), **prepared},
        "group_sizes": _truth_group_sizes(truth),
    }

    if args.skip_organism_checks:
        results["contradicted_organism_refused"] = {"skipped": True}
        results["override_runs_and_is_recorded"] = {"skipped": True}
    else:
        await _organism_checks(args, broker, scimilarity, results, fixture)

    if not args.skip_annotation:
        await _annotation_checks(args, broker, scimilarity, results, fixture)
    else:
        results["constrained_annotation"] = {"skipped": True}

    if args.skip_query:
        results["reference_query"] = {"skipped": True}
        return results
    return await _query_check(args, broker, scimilarity, results, fixture)


async def _annotation_checks(
    args: argparse.Namespace,
    broker: EnvironmentBroker,
    scimilarity: SkillPackage,
    results: dict[str, Any],
    fixture: Path,
) -> None:
    """Constrained annotation, its safelist validation, and the per-cell vote margins."""

    session = AnalysisSession.create(
        args.sessions_root.resolve(), title="SCimilarity constrained annotation"
    )
    executor = CapabilityExecutor(session, environment_broker=broker)

    ok, refusal = await _execute(
        executor,
        scimilarity,
        "run_scimilarity_annotation",
        {
            "path": str(fixture),
            "organism": "human",
            "counts_layer": "X",
            "target_celltypes": ["enterocyte", "goblet cel"],
        },
    )
    results["safelist_typo_refused"] = {
        "refused": not ok,
        "message": (refusal.splitlines()[0] if not ok else None),
    }

    ok, constrained = await _execute(
        executor,
        scimilarity,
        "run_scimilarity_annotation",
        {
            "path": str(fixture),
            "organism": "human",
            "counts_layer": "X",
            "knn_k": args.knn_k,
            "weighting": args.weighting,
            "target_celltypes": COLORECTAL_SAFELIST,
        },
    )
    if not ok:
        results["constrained_annotation"] = {"error": constrained}
        return
    details = constrained["details"]
    results["constrained_annotation"] = {
        "session": str(session.directory),
        "summary": constrained["summary"],
        "knn_k": details["knn_k"],
        "distance_weighted_voting": details["distance_weighted_voting"],
        "unconstrained_label_count": details["prediction_count"],
        "vote_confidence": details["vote_confidence"],
        "constrained": details["constrained_annotation"],
        "artifacts": [item["name"] for item in constrained["files"]],
    }


async def _organism_checks(
    args: argparse.Namespace,
    broker: EnvironmentBroker,
    scimilarity: SkillPackage,
    results: dict[str, Any],
    fixture: Path,
) -> None:
    # 1. Contradicted organism: real human data declared as mouse must be refused, and must be
    #    refused cheaply — before the encoder, reference labels, or any index is touched.
    session = AnalysisSession.create(
        args.sessions_root.resolve(), title="SCimilarity organism verification"
    )
    executor = CapabilityExecutor(session, environment_broker=broker)
    ok, refusal = await _execute(
        executor,
        scimilarity,
        "run_scimilarity_annotation",
        {"path": str(fixture), "organism": "mouse", "counts_layer": "X"},
    )
    results["contradicted_organism_refused"] = {
        "session": str(session.directory),
        "refused": not ok,
        "message": refusal if not ok else refusal.get("summary"),
    }

    # 2. The same call with the documented override must proceed past the species gate.
    ok, overridden = await _execute(
        executor,
        scimilarity,
        "run_scimilarity_annotation",
        {
            "path": str(fixture),
            "organism": "mouse",
            "counts_layer": "X",
            "allow_species_mismatch": True,
            "min_gene_overlap": args.min_gene_overlap,
        },
    )
    results["override_runs_and_is_recorded"] = {
        "ran": ok,
        "species_check": overridden.get("details", {}).get("species_check") if ok else None,
        "message": None if ok else overridden,
    }


async def _query_check(
    args: argparse.Namespace,
    broker: EnvironmentBroker,
    scimilarity: SkillPackage,
    results: dict[str, Any],
    fixture: Path,
) -> dict[str, Any]:
    # 3. The real atlas query, one index load for every ground-truth population.
    query_session = AnalysisSession.create(
        args.sessions_root.resolve(), title="SCimilarity reference atlas query"
    )
    query_executor = CapabilityExecutor(query_session, environment_broker=broker)
    ok, query = await _execute(
        query_executor,
        scimilarity,
        "query_reference_cells",
        {
            "path": str(fixture),
            "organism": "human",
            "counts_layer": "X",
            "query_mode": args.query_mode,
            "group_key": None if args.query_cell_ids else GROUP_KEY,
            "group_values": args.group_values.split(",") if args.group_values else None,
            "cell_ids": args.query_cell_ids.split(",") if args.query_cell_ids else None,
            "exclude_studies": (
                args.exclude_studies.split(",") if args.exclude_studies else None
            ),
            "k": args.k,
            "min_query_cells": args.min_query_cells,
            "measure_coherence": not args.no_coherence,
        },
    )
    if not ok:
        results["reference_query"] = {"session": str(query_session.directory), "error": query}
        return results
    details = query["details"]
    results["reference_query"] = {
        "session": str(query_session.directory),
        "summary": query["summary"],
        "reference_cells_indexed": details["reference_cells_indexed"],
        "index_load_seconds": details["index_load_seconds"],
        "search_seconds": details["search_seconds"],
        "query_mode": details["query_mode"],
        "excluded_studies": details["excluded_studies"],
        "background_seconds": details["background_seconds"],
        "skipped_groups": details["selection"]["skipped_groups"],
        "species_check": details["species_check"],
        "composition_columns_absent": details["composition_columns_absent"],
        "artifacts": [item["name"] for item in query["files"]],
        "per_group": [
            {
                "query": item["query"],
                "n_query_cells": item["n_query_cells"],
                "coherence": item["coherence"]["value"],
                "median_distance": (item["neighbor_distance"] or {}).get("median"),
                "top_reference_types": [
                    f"{entry['value']} ({entry['fraction']:.0%})"
                    for entry in item["composition"].get("celltype_name", [])[:3]
                ],
                "top_tissues": [
                    entry["value"]
                    for entry in item["composition"].get("tissue_general", [])[:2]
                ],
                "study_exclusion": item.get("study_exclusion"),
                "top_samples": (item.get("top_reference_samples") or [])[:2],
                "reference_background": item.get("reference_background"),
            }
            for item in details["queries"]
        ],
    }
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--truth", default=DEFAULT_TRUTH)
    parser.add_argument(
        "--fixture", default=str(Path.cwd() / "sessions" / "crc_truth_labeled.h5ad")
    )
    parser.add_argument("--sessions-root", type=Path, default=Path.cwd() / "sessions")
    parser.add_argument("--k", type=int, default=100)
    parser.add_argument("--min-query-cells", type=int, default=10)
    parser.add_argument("--min-gene-overlap", type=int, default=5000)
    parser.add_argument("--skip-query", action="store_true")
    parser.add_argument("--skip-organism-checks", action="store_true")
    parser.add_argument("--skip-annotation", action="store_true")
    parser.add_argument("--knn-k", type=int, default=50)
    parser.add_argument("--weighting", action="store_true")
    parser.add_argument("--query-mode", choices=("centroid", "cells"), default="centroid")
    parser.add_argument("--query-cell-ids", default=None, help="Comma-separated obs_names.")
    parser.add_argument("--exclude-studies", default=None, help="Comma-separated study ids.")
    parser.add_argument("--group-values", default=None, help="Comma-separated subset to query.")
    parser.add_argument(
        "--no-coherence",
        action="store_true",
        help="Skip the coherence QC, which costs about ten extra atlas searches per query.",
    )
    return parser


def main() -> int:
    print(json.dumps(asyncio.run(_run(_parser().parse_args())), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
