"""Typed and source-backed official game specification."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from battlelab.official.models import GameSpec, RuleItem, RuleVerificationState
from battlelab.official.sources import load_source_bundle_manifest

SUPPORTED_SCHEMA_VERSIONS = {"1.0.0"}

REQUIRED_RULE_SECTIONS: list[str] = [
    "victory_loss_draw_tiebreak",
    "turn_and_phase_ordering",
    "observation_and_visibility",
    "legal_action_space",
    "units_entities_structures",
    "resources_and_economy",
    "movement_and_collision",
    "combat_and_damage",
    "maps_coordinates_symmetry",
    "randomness_and_seeds",
    "communication_shared_memory",
    "compute_bytecode_time_memory_limits",
    "supported_languages_runtimes",
    "bot_source_layout_entrypoint",
    "build_package_process",
    "local_engine_command_structure",
    "local_engine_output_exit_codes",
    "replay_discovery_format",
    "outcome_score_mapping",
    "official_starter_bot",
    "remote_test_mechanism",
    "submission_mechanism",
    "match_replay_retrieval",
]


def init_game_spec(
    source_bundle_hash: str,
    output_path: Path,
    bundles_dir: Path | None = None,
) -> GameSpec:
    """Initialize a typed game specification from an ingested source bundle."""
    # Verify bundle exists
    manifest = load_source_bundle_manifest(source_bundle_hash, bundles_dir=bundles_dir)
    doc_hashes = [f.sha256 for f in manifest.files]

    now_iso = datetime.now(timezone.utc).isoformat()
    rules: dict[str, RuleItem] = {}
    for section in REQUIRED_RULE_SECTIONS:
        rules[section] = RuleItem(
            meaning=f"Rule specification for {section.replace('_', ' ')}",
            source_refs=[],
            verification_state=RuleVerificationState.MISSING.value,
            implementation_impacts=[],
            test_coverage=[],
            notes="",
        )

    spec = GameSpec(
        schema_version="1.0.0",
        competition_name="Battlecode",
        competition_season="Autumn 2026",
        spec_version="1.0.0",
        source_bundle_hash=source_bundle_hash,
        official_document_hashes=doc_hashes,
        sdk_version="",
        generated_at=now_iso,
        updated_at=now_iso,
        rules=rules,
    )

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(spec.to_dict(), f, sort_keys=False)

    return spec


def validate_game_spec(
    spec_data: dict[str, Any],
) -> tuple[bool, list[str], GameSpec | None, bool]:
    """Validate a game specification dictionary.

    Returns:
        (is_structurally_valid, errors, spec_obj, is_activation_ready)
    """
    errors: list[str] = []

    # 1. Schema version
    schema_ver = spec_data.get("schema_version")
    if not isinstance(schema_ver, str) or schema_ver not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"schema_version: unsupported or missing schema version '{schema_ver}'. "
            f"Supported versions: {sorted(list(SUPPORTED_SCHEMA_VERSIONS))}"
        )

    # 2. Required string metadata
    for field_name in [
        "competition_name",
        "competition_season",
        "spec_version",
        "source_bundle_hash",
    ]:
        val = spec_data.get(field_name)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{field_name}: must be a non-empty string")

    # 3. Document hashes
    doc_hashes = spec_data.get("official_document_hashes")
    if not isinstance(doc_hashes, list) or not all(isinstance(x, str) for x in doc_hashes):
        errors.append("official_document_hashes: must be a list of strings")

    # 4. Rules dictionary
    rules_dict = spec_data.get("rules")
    if not isinstance(rules_dict, dict):
        errors.append("rules: must be a dictionary of rule sections")
        return False, errors, None, False

    # Check for missing required sections
    for req_sec in REQUIRED_RULE_SECTIONS:
        if req_sec not in rules_dict:
            errors.append(f"rules.{req_sec}: missing required rule section")

    allowed_states = {s.value for s in RuleVerificationState}
    all_sections_activation_ready = True

    parsed_rules: dict[str, RuleItem] = {}
    for section_name, item_raw in rules_dict.items():
        if not isinstance(item_raw, dict):
            errors.append(f"rules.{section_name}: must be a dictionary")
            all_sections_activation_ready = False
            continue

        state = item_raw.get("verification_state")
        if state not in allowed_states:
            errors.append(
                f"rules.{section_name}.verification_state: invalid state '{state}'. "
                f"Allowed states: {sorted(list(allowed_states))}"
            )
            all_sections_activation_ready = False

        source_refs = item_raw.get("source_refs", [])
        if not isinstance(source_refs, list) or not all(isinstance(s, str) for s in source_refs):
            errors.append(f"rules.{section_name}.source_refs: must be a list of strings")
            source_refs = []

        test_cov = item_raw.get("test_coverage", [])
        if not isinstance(test_cov, list) or not all(isinstance(t, str) for t in test_cov):
            errors.append(f"rules.{section_name}.test_coverage: must be a list of strings")
            test_cov = []

        impacts = item_raw.get("implementation_impacts", [])
        if not isinstance(impacts, list) or not all(isinstance(i, str) for i in impacts):
            errors.append(f"rules.{section_name}.implementation_impacts: must be a list of strings")
            impacts = []

        # Verification state rules
        if state == RuleVerificationState.DOCUMENTED.value:
            if not source_refs or not any(s.strip() for s in source_refs):
                errors.append(
                    f"rules.{section_name}.source_refs: cannot be empty when verification_state is DOCUMENTED"
                )
        elif state == RuleVerificationState.TEST_VERIFIED.value:
            if not source_refs or not any(s.strip() for s in source_refs):
                errors.append(
                    f"rules.{section_name}.source_refs: cannot be empty when verification_state is TEST_VERIFIED"
                )
            if not test_cov or not any(t.strip() for t in test_cov):
                errors.append(
                    f"rules.{section_name}.test_coverage: cannot be empty when verification_state is TEST_VERIFIED"
                )

        if state != RuleVerificationState.TEST_VERIFIED.value:
            all_sections_activation_ready = False

        parsed_rules[section_name] = RuleItem(
            meaning=str(item_raw.get("meaning", "")),
            source_refs=source_refs,
            verification_state=str(state),
            implementation_impacts=impacts,
            test_coverage=test_cov,
            notes=str(item_raw.get("notes", "")),
        )

    is_valid = len(errors) == 0
    spec_obj = None
    if is_valid:
        spec_obj = GameSpec(
            schema_version=str(schema_ver),
            competition_name=str(spec_data["competition_name"]),
            competition_season=str(spec_data["competition_season"]),
            spec_version=str(spec_data["spec_version"]),
            source_bundle_hash=str(spec_data["source_bundle_hash"]),
            official_document_hashes=[str(x) for x in (doc_hashes or [])],
            sdk_version=str(spec_data.get("sdk_version", "")),
            generated_at=str(spec_data.get("generated_at", "")),
            updated_at=str(spec_data.get("updated_at", "")),
            rules=parsed_rules,
        )

    is_activation_ready = is_valid and all_sections_activation_ready
    return is_valid, errors, spec_obj, is_activation_ready


def load_and_validate_spec(
    spec_path: Path,
) -> tuple[bool, list[str], GameSpec | None, bool]:
    """Load spec YAML file and validate it."""
    p = Path(spec_path)
    if not p.is_file():
        return False, [f"Spec file not found: {p}"], None, False
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return False, ["Root YAML document must be a dictionary"], None, False
        return validate_game_spec(data)
    except Exception as e:
        return False, [f"YAML parsing error: {e}"], None, False
