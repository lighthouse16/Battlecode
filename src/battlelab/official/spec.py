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
    doc_hashes = sorted([f.sha256 for f in manifest.files])

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
        competition_season="",
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
    bundles_dir: Path | None = None,
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
        if not isinstance(val, str):
            errors.append(f"{field_name}: must be a string")
        elif (
            field_name in ("competition_name", "spec_version", "source_bundle_hash")
            and not val.strip()
        ):
            errors.append(f"{field_name}: must be a non-empty string")

    # Validate source_bundle_hash format
    bundle_hash = spec_data.get("source_bundle_hash")
    if isinstance(bundle_hash, str) and (
        len(bundle_hash) != 64 or not all(c in "0123456789abcdef" for c in bundle_hash)
    ):
        errors.append(
            f"source_bundle_hash: must be 64 lowercase hex characters, got {bundle_hash!r}"
        )

    # 3. Document hashes
    doc_hashes = spec_data.get("official_document_hashes")
    if not isinstance(doc_hashes, list) or not all(
        isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdef" for c in x)
        for x in doc_hashes
    ):
        errors.append(
            "official_document_hashes: must be a list of 64-character lowercase hex SHA-256 strings"
        )

    # 4. Bind spec to source bundle and verify document hashes and refs
    valid_bundle_relpaths: set[str] | None = None
    if isinstance(bundle_hash, str) and len(bundle_hash) == 64:
        try:
            bundle_manifest = load_source_bundle_manifest(bundle_hash, bundles_dir=bundles_dir)
            valid_bundle_relpaths = {f.relpath for f in bundle_manifest.files}
            manifest_hashes = sorted([f.sha256 for f in bundle_manifest.files])
            spec_hashes = sorted(doc_hashes) if isinstance(doc_hashes, list) else []
            if spec_hashes != manifest_hashes:
                errors.append(
                    f"official_document_hashes: document hashes do not match bundle entries. "
                    f"Expected {manifest_hashes}, found {spec_hashes}"
                )
        except Exception as e:
            errors.append(f"source_bundle_hash: failed to verify referenced bundle: {e}")

    # 5. Rules dictionary
    rules_dict = spec_data.get("rules")
    if not isinstance(rules_dict, dict):
        errors.append("rules: must be a dictionary of rule sections")
        return False, errors, None, False

    # Check for missing and unsupported sections
    missing_sections = set(REQUIRED_RULE_SECTIONS) - set(rules_dict.keys())
    if missing_sections:
        errors.append(f"rules: missing required rule sections: {sorted(list(missing_sections))}")

    unsupported_sections = set(rules_dict.keys()) - set(REQUIRED_RULE_SECTIONS)
    if unsupported_sections:
        errors.append(f"rules: unsupported rule sections: {sorted(list(unsupported_sections))}")

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

        # Validate source_refs against real bundle relpaths if bundle is available
        if valid_bundle_relpaths is not None:
            for sref in source_refs:
                ref_clean = sref.split("#")[0].split(":")[0].strip()
                if ref_clean and ref_clean not in valid_bundle_relpaths:
                    errors.append(
                        f"rules.{section_name}.source_refs: reference '{sref}' does not match any file in source bundle manifest"
                    )

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
            competition_season=str(spec_data.get("competition_season", "")),
            spec_version=str(spec_data["spec_version"]),
            source_bundle_hash=str(spec_data["source_bundle_hash"]),
            official_document_hashes=sorted([str(x) for x in (doc_hashes or [])]),
            sdk_version=str(spec_data.get("sdk_version", "")),
            generated_at=str(spec_data.get("generated_at", "")),
            updated_at=str(spec_data.get("updated_at", "")),
            rules=parsed_rules,
        )

    is_activation_ready = is_valid and all_sections_activation_ready and len(missing_sections) == 0
    return is_valid, errors, spec_obj, is_activation_ready


def load_and_validate_spec(
    spec_path: Path,
    bundles_dir: Path | None = None,
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
        return validate_game_spec(data, bundles_dir=bundles_dir)
    except Exception as e:
        return False, [f"YAML parsing error: {e}"], None, False
