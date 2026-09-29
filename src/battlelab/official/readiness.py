"""Readiness evaluation and capability assessment for official competition integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.models import ReadinessCheckItem, ReadinessReport
from battlelab.official.sources import load_source_bundle_manifest
from battlelab.official.spec import load_and_validate_spec
from battlelab.storage.paths import get_official_source_bundles_dir, get_project_root


class OfficialReadinessChecker:
    """Evaluates readiness checklist and derives official adapter capabilities."""

    def __init__(
        self,
        bridge: OfficialEngineBridge | None = None,
        spec_path: Path | None = None,
        source_bundle_hash: str | None = None,
    ) -> None:
        self.bridge = bridge if bridge is not None else UnconfiguredOfficialBridge()
        self.spec_path = (
            spec_path
            if spec_path is not None
            else get_project_root() / "configs" / "game_spec.yaml"
        )
        self.source_bundle_hash = source_bundle_hash

    def evaluate(self) -> ReadinessReport:
        """Run all readiness checks and compute derived capabilities."""
        checks: list[ReadinessCheckItem] = []
        blockers: list[str] = []

        # 1. Source bundle verification
        bundle_hash_found: str | None = self.source_bundle_hash
        bundle_ok = False
        bundle_detail = "No source bundle specified or discovered."
        if bundle_hash_found:
            try:
                manifest = load_source_bundle_manifest(bundle_hash_found)
                bundle_ok = True
                bundle_detail = (
                    f"Verified bundle {bundle_hash_found[:12]} ({manifest.file_count} files)"
                )
            except Exception as e:
                bundle_detail = f"Bundle verification failed: {e}"
        else:
            # Check if any bundle directory exists
            bundles_dir = get_official_source_bundles_dir()
            if bundles_dir.is_dir():
                subdirs = [d for d in bundles_dir.iterdir() if d.is_dir()]
                if subdirs:
                    bundle_hash_found = subdirs[0].name
                    try:
                        manifest = load_source_bundle_manifest(bundle_hash_found)
                        bundle_ok = True
                        bundle_detail = f"Discovered bundle {bundle_hash_found[:12]} ({manifest.file_count} files)"
                    except Exception as e:
                        bundle_detail = f"Discovered bundle invalid: {e}"

        checks.append(
            ReadinessCheckItem(
                name="source_bundle_exists",
                description="Authoritative source bundle exists and verifies",
                passed=bundle_ok,
                blocker=True,
                details=bundle_detail,
            )
        )
        if not bundle_ok:
            blockers.append("Authoritative rulebook/documentation bundle not ingested")

        # 2. Spec schema and documentation verification
        spec_hash: str | None = None
        spec_valid = False
        spec_documented = False
        spec_sources_ref = False
        spec_obj = None

        if self.spec_path.is_file():
            is_valid, errors, spec_obj, is_ready = load_and_validate_spec(self.spec_path)
            spec_valid = is_valid
            if is_valid and spec_obj:
                spec_hash = spec_obj.canonical_hash()
                # Check if all required sections documented
                missing_items = [
                    k for k, v in spec_obj.rules.items() if v.verification_state == "MISSING"
                ]
                spec_documented = len(missing_items) == 0
                # Check if all documented items have source refs
                unreferenced = [
                    k
                    for k, v in spec_obj.rules.items()
                    if v.verification_state in ("DOCUMENTED", "TEST_VERIFIED") and not v.source_refs
                ]
                spec_sources_ref = len(unreferenced) == 0
            spec_err_str = f"Spec errors: {len(errors)}" if errors else "Valid"
        else:
            spec_err_str = f"Spec file not found at {self.spec_path}"

        checks.append(
            ReadinessCheckItem(
                name="spec_schema_valid",
                description="Game specification schema is valid",
                passed=spec_valid,
                blocker=True,
                details=spec_err_str,
            )
        )
        if not spec_valid:
            blockers.append("Game specification schema invalid or missing")

        checks.append(
            ReadinessCheckItem(
                name="spec_items_documented",
                description="All critical rule sections documented from official sources",
                passed=spec_documented,
                blocker=True,
                details=f"Documented rules: {'All' if spec_documented else 'Incomplete'}",
            )
        )
        if not spec_documented:
            blockers.append("Game specification contains unpopulated MISSING rules")

        checks.append(
            ReadinessCheckItem(
                name="spec_sources_referenced",
                description="Documented rule sections reference authoritative sources",
                passed=spec_sources_ref,
                blocker=True,
                details=f"Source references: {'Verified' if spec_sources_ref else 'Missing'}",
            )
        )
        if not spec_sources_ref:
            blockers.append("Rule sections lack source document references")

        # 3. SDK Probing & Location
        sdk_version: str | None = None
        sdk_configured = False
        sdk_exists = False
        sdk_probed = False
        sdk_version_matches = False
        maps_discovered = False
        build_succeeds = False
        local_match_succeeds = False
        replay_parsing_succeeds = False
        deterministic_repeat_succeeds = False

        if not isinstance(self.bridge, UnconfiguredOfficialBridge):
            try:
                probe_res = self.bridge.probe_sdk()
                sdk_configured = True
                sdk_exists = bool(probe_res.get("executable_exists", True))
                sdk_version = probe_res.get("sdk_version")
                sdk_probed = bool(sdk_version)

                configured_ver = spec_obj.sdk_version if spec_obj and spec_obj.sdk_version else None
                sdk_version_matches = not configured_ver or configured_ver == sdk_version

                maps = self.bridge.discover_maps()
                maps_discovered = len(maps) > 0

                build_res = probe_res.get("build_verified", False)
                build_succeeds = bool(build_res)

                match_res = probe_res.get("match_verified", False)
                local_match_succeeds = bool(match_res)

                replay_res = probe_res.get("replay_verified", False)
                replay_parsing_succeeds = bool(replay_res)

                det_res = probe_res.get("determinism_verified", False)
                deterministic_repeat_succeeds = bool(det_res)
            except Exception as e:
                sdk_configured = False
                blockers.append(f"SDK probing failed: {e}")
        else:
            blockers.append("Official competition SDK unreleased and unconfigured")

        checks.append(
            ReadinessCheckItem(
                name="sdk_configured",
                description="Official SDK path/executable configured",
                passed=sdk_configured,
                blocker=True,
                details=f"SDK configured: {sdk_configured}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="sdk_executable_exists",
                description="Official SDK executable exists and is runnable",
                passed=sdk_exists,
                blocker=True,
                details=f"Executable exists: {sdk_exists}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="sdk_version_probed",
                description="SDK version successfully queried from engine executable",
                passed=sdk_probed,
                blocker=True,
                details=f"Detected SDK version: {sdk_version}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="sdk_version_matches",
                description="Detected SDK version matches game spec",
                passed=sdk_version_matches,
                blocker=True,
                details=f"Version match: {sdk_version_matches}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="map_discovery_succeeds",
                description="Engine discovers available competition maps",
                passed=maps_discovered,
                blocker=True,
                details=f"Maps discovered: {maps_discovered}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="build_command_succeeds",
                description="Engine build/package step verifies against bot artifact",
                passed=build_succeeds,
                blocker=True,
                details=f"Build verified: {build_succeeds}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="local_match_succeeds",
                description="Engine local match execution contract succeeds",
                passed=local_match_succeeds,
                blocker=True,
                details=f"Local match verified: {local_match_succeeds}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="replay_parsing_succeeds",
                description="Replay output parsed into normalized replay contract",
                passed=replay_parsing_succeeds,
                blocker=True,
                details=f"Replay parsing verified: {replay_parsing_succeeds}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="deterministic_repeat_succeeds",
                description="Repeated identical match reproduces bit-exact gameplay",
                passed=deterministic_repeat_succeeds,
                blocker=True,
                details=f"Determinism verified: {deterministic_repeat_succeeds}",
            )
        )

        # 4. Minimal bot
        min_bot_path = get_project_root() / "bots" / "baselines" / "official_minimal"
        min_bot_exists = min_bot_path.is_dir() and any(min_bot_path.iterdir())
        checks.append(
            ReadinessCheckItem(
                name="minimal_legal_bot_exists",
                description="Minimal legal official starter bot baseline exists",
                passed=min_bot_exists,
                blocker=True,
                details=f"Path: {min_bot_path} ({'Exists' if min_bot_exists else 'Missing'})",
            )
        )
        if not min_bot_exists:
            blockers.append("Minimal legal official bot baseline not yet created")

        # 5. Remote and submission safety
        checks.append(
            ReadinessCheckItem(
                name="remote_configured",
                description="Remote competition ladder/API configured",
                passed=False,
                blocker=False,
                details="Remote testing unconfigured pending competition portal release",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="submission_verified",
                description="Authoritative submission workflow verified and dry-run tested",
                passed=False,
                blocker=False,
                details="Submissions disabled pending official submission portal validation",
            )
        )

        # Compute derived capabilities
        can_run_local = (
            sdk_probed
            and build_succeeds
            and local_match_succeeds
            and replay_parsing_succeeds
            and deterministic_repeat_succeeds
        )
        can_submit = False  # Strictly False until release
        all_blockers_cleared = len(blockers) == 0
        ready = all_blockers_cleared and can_run_local

        return ReadinessReport(
            ready=ready,
            can_run_local=can_run_local,
            can_submit=can_submit,
            checks=checks,
            source_bundle_hash=bundle_hash_found,
            spec_hash=spec_hash,
            sdk_version=sdk_version,
            blockers=blockers,
        )

    def dry_run_activation(self) -> dict[str, Any]:
        """Perform dry-run activation check without making changes."""
        report = self.evaluate()
        return {
            "action": "activate --dry-run",
            "ready": report.ready,
            "blockers_count": len(report.blockers),
            "blockers": report.blockers,
            "source_bundle_hash": report.source_bundle_hash,
            "spec_hash": report.spec_hash,
            "sdk_version": report.sdk_version,
            "capabilities_to_activate": {
                "can_run_local": report.can_run_local,
                "can_submit": False,
            },
            "status": "READY_FOR_ACTIVATION" if report.ready else "BLOCKED",
            "message": (
                "Official activation is possible."
                if report.ready
                else f"Official activation blocked by {len(report.blockers)} unsatisfied requirements."
            ),
        }
