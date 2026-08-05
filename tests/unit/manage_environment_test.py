from __future__ import annotations

from pathlib import Path
from typing import Any

from scagent_sdk.capabilities.registry import CapabilityRegistry


def _package() -> Any:
    skills_root = Path(__file__).parents[2] / ".claude" / "skills"
    return next(
        package
        for package in CapabilityRegistry(skills_root).discover()
        if package.manifest.skill_id == "manage-environment"
    )


def _g(name: str) -> Any:
    package = _package()
    tool = next(t for t in package.manifest.tools if t.name == "propose_package_install")
    return package.load_handler(tool).__globals__[name]


def _report(*names_versions: tuple[str, str]) -> dict[str, Any]:
    return {
        "install": [
            {"metadata": {"name": name, "version": version}}
            for name, version in names_versions
        ]
    }


# --- manifest ----------------------------------------------------------------


def test_manifest_declares_four_tools() -> None:
    names = {tool.name for tool in _package().manifest.tools}
    assert names == {
        "propose_package_install",
        "install_proposed_package",
        "list_installed_packages",
        "remove_installed_package",
    }
    for tool in _package().manifest.tools:
        assert tool.environment == "overlay-admin"


# --- classification ----------------------------------------------------------


def test_classify_additive_when_all_new() -> None:
    classify = _g("classify_report")
    result = classify(_report(("gseapy", "1.1.3"), ("typing-extensions", "4.9.0")), {})
    assert result["status"] == "additive"
    assert {item["name"] for item in result["additive"]} == {"gseapy", "typing-extensions"}
    assert result["conflicts"] == []


def test_classify_conflict_when_version_would_change_base() -> None:
    classify = _g("classify_report")
    result = classify(_report(("numpy", "2.1.0")), {"numpy": "1.26.4"})
    assert result["status"] == "conflict"
    assert result["conflicts"][0] == {
        "name": "numpy",
        "base_version": "1.26.4",
        "proposed_version": "2.1.0",
    }
    assert result["additive"] == []


def test_classify_same_version_is_not_a_conflict_and_not_additive() -> None:
    classify = _g("classify_report")
    result = classify(_report(("numpy", "1.26.4")), {"numpy": "1.26.4"})
    assert result["status"] == "already_satisfied"


def test_classify_normalizes_names_before_comparing() -> None:
    classify = _g("classify_report")
    # underscore/case variant of an installed dist must still register as a conflict
    result = classify(_report(("Typing_Extensions", "9.9.9")), {"typing-extensions": "4.9.0"})
    assert result["status"] == "conflict"


def test_classify_mixed_reports_conflict() -> None:
    classify = _g("classify_report")
    result = classify(
        _report(("gseapy", "1.1.3"), ("numpy", "2.1.0")), {"numpy": "1.26.4"}
    )
    assert result["status"] == "conflict"
    # the additive part is still surfaced for transparency
    assert {item["name"] for item in result["additive"]} == {"gseapy"}


# --- uv plan parsing ---------------------------------------------------------

# Real uv --dry-run output goes to stderr as "+ name==ver" / "- name==ver (from ...)" lines.
_UV_ADDITIVE = """\
Using Python 3.14.6 environment at: .pixi/envs/rapids
Resolved 21 packages in 13ms
Would download 1 package
Would install 1 package
 + gseapy==1.3.1
"""

_UV_CONFLICT = """\
Using Python 3.14.6 environment at: .pixi/envs/rapids
Resolved 5 packages in 68ms
Would uninstall 1 package
Would install 1 package
 - requests==2.34.2 (from file:///home/conda/feedstock_root/build_artifacts/work)
 + requests==2.31.0
"""


def test_parse_uv_plan_additive() -> None:
    parse = _g("parse_uv_plan")
    result = parse(_UV_ADDITIVE)
    assert result["status"] == "additive"
    assert result["additive"] == [{"name": "gseapy", "version": "1.3.1"}]
    assert result["conflicts"] == []


def test_parse_uv_plan_conflict_pairs_uninstall_with_install() -> None:
    parse = _g("parse_uv_plan")
    result = parse(_UV_CONFLICT)
    assert result["status"] == "conflict"
    assert result["conflicts"] == [
        {"name": "requests", "base_version": "2.34.2", "proposed_version": "2.31.0"}
    ]
    assert result["additive"] == []


def test_parse_uv_plan_strips_conda_provenance_suffix() -> None:
    parse = _g("parse_uv_plan")
    # the "(from file://...)" suffix on a removal line must not leak into the version
    result = parse(_UV_CONFLICT)
    assert result["conflicts"][0]["base_version"] == "2.34.2"


def test_parse_uv_plan_empty_is_already_satisfied() -> None:
    parse = _g("parse_uv_plan")
    result = parse("Resolved 3 packages in 5ms\nWould make no changes\n")
    assert result["status"] == "already_satisfied"


# --- consultation gate -------------------------------------------------------


def _turn(seq: int, prompt: str, turn_id: str | None = None) -> dict[str, Any]:
    return {"sequence": seq, "turn_id": turn_id or f"t{seq}", "user_prompt": prompt}


def test_consultation_requires_a_turn_after_the_anchor() -> None:
    validate = _g("validate_consultation")
    # only turns at or before the anchor exist
    result = validate(
        "Install gseapy?", anchor_sequence=5, user_turns=[_turn(3, "start"), _turn(5, "go")]
    )
    assert result["ok"] is False
    assert result["consultation"] is None


def test_consultation_takes_first_turn_after_anchor_verbatim() -> None:
    validate = _g("validate_consultation")
    result = validate(
        "Install gseapy?",
        anchor_sequence=5,
        user_turns=[
            _turn(5, "proposal shown"),
            _turn(6, "yes go ahead"),
            _turn(7, "and then plot"),
        ],
    )
    assert result["ok"] is True
    assert result["consultation"]["answer"] == "yes go ahead"
    assert result["consultation"]["turn_id"] == "t6"
    assert result["consultation"]["turns_after_proposal"] == 2


def test_consultation_requires_question_asked() -> None:
    validate = _g("validate_consultation")
    result = validate("   ", anchor_sequence=0, user_turns=[_turn(1, "yes")])
    assert result["ok"] is False


def test_latest_user_turn_sequence_ignores_non_dicts_and_bools() -> None:
    latest = _g("latest_user_turn_sequence")
    assert latest([_turn(2, "a"), "junk", {"sequence": True}, _turn(4, "b")]) == 4
    assert latest([]) == 0


# --- fact shaping ------------------------------------------------------------


def test_question_identity_is_stable_and_scoped() -> None:
    question_identity = _g("question_identity")
    assert question_identity("gseapy", "rapids-main") == question_identity(
        "GSEApy", "rapids-main"
    )
    assert question_identity("gseapy", "rapids-main") != question_identity("gseapy", "cellbender")


def test_merge_installed_replaces_by_canonical_name_and_sorts() -> None:
    merge = _g("merge_installed")
    existing = [{"name": "abc", "version": "1"}, {"name": "gseapy", "version": "1.0.0"}]
    merged = merge(existing, {"name": "GSEApy", "version": "1.1.3"})
    versions = {item["name"]: item["version"] for item in merged}
    assert versions == {"abc": "1", "GSEApy": "1.1.3"}
    assert [item["name"] for item in merged] == ["abc", "GSEApy"]


def test_overlay_fact_patch_writes_runtime_scoped_installed_and_proposal() -> None:
    build = _g("overlay_fact_patch")
    patch = build(
        {},
        "rapids-main",
        installed=[{"name": "gseapy", "version": "1.1.3"}],
        proposal=("qid", None),
    )
    overlay = patch["environment_overlay"]
    assert overlay["runtimes"]["rapids-main"]["installed"][0]["name"] == "gseapy"
    assert overlay["proposals"]["qid"] is None  # null clears via merge-patch


def test_existing_installed_and_proposal_read_from_state_facts() -> None:
    existing_installed = _g("existing_installed")
    existing_proposal = _g("existing_proposal")
    facts = {
        "environment_overlay": {
            "runtimes": {"rapids-main": {"installed": [{"name": "gseapy", "version": "1.1.3"}]}},
            "proposals": {"qid": {"package": "gseapy"}},
        }
    }
    assert existing_installed(facts, "rapids-main")[0]["name"] == "gseapy"
    assert existing_installed(facts, "cellbender") == []
    assert existing_proposal(facts, "qid") == {"package": "gseapy"}
    assert existing_proposal(facts, "missing") is None


def test_primary_version_matches_requested_package_ignoring_pin() -> None:
    primary = _g("_primary_version")
    additive = [{"name": "gseapy", "version": "1.1.3"}, {"name": "matplotlib", "version": "3.8"}]
    assert primary("gseapy==1.1.3", additive) == "1.1.3"
    assert primary("gseapy", additive) == "1.1.3"


# --- overlay rebuild-on-remove -----------------------------------------------


def test_remaining_records_drops_target_by_canonical_name() -> None:
    remaining = _g("remaining_records")
    manifest = [{"name": "gseapy", "version": "1.3.1"}, {"name": "mygene", "version": "3.2"}]
    kept = remaining(manifest, "GSEApy")
    assert [item["name"] for item in kept] == ["mygene"]


def test_specs_from_records_flattens_added_distributions_and_dedups() -> None:
    specs_from = _g("specs_from_records")
    records = [
        {
            "name": "gseapy",
            "version": "1.3.1",
            "added_distributions": [{"name": "gseapy", "version": "1.3.1"}],
        },
        {"name": "mygene", "version": "3.2"},  # no added_distributions -> use the record itself
        {"name": "gseapy", "version": "1.3.1"},  # duplicate -> deduped
    ]
    specs = specs_from(records)
    assert specs == [
        {"name": "gseapy", "version": "1.3.1"},
        {"name": "mygene", "version": "3.2"},
    ]


def test_wipe_overlay_empties_dir_but_keeps_it(tmp_path: Path) -> None:
    wipe = _g("_wipe_overlay")
    overlay = tmp_path / "ov"
    (overlay / "pkg").mkdir(parents=True)
    (overlay / "pkg" / "__init__.py").write_text("x", encoding="utf-8")
    (overlay / "mod.py").write_text("y", encoding="utf-8")
    wipe(overlay)
    assert overlay.is_dir()
    assert list(overlay.iterdir()) == []
