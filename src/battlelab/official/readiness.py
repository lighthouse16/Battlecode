"""Readiness evaluation and capability assessment for official competition integration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.models import ReadinessCheckItem, ReadinessReport
from battlelab.official.sources import load_source_bundle_manifest
from battlelab.official.spec import load_and_validate_spec
from battlelab.storage.paths import get_project_root


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

        # 1. Spec schema and documentation verification
        spec_hash: str | None = None
        spec_valid = False
        spec_obj = None
        rules_test_verified = False

        if self.spec_path.is_file():
            is_valid, errors, spec_obj, is_ready = load_and_validate_spec(self.spec_path)
            spec_valid = is_valid
            if is_valid and spec_obj:
                spec_hash = spec_obj.canonical_hash()
                unverified_rules = [
                    k
                    for k, v in spec_obj.rules.items()
                    if v.verification_state != "TEST_VERIFIED"
                    or not v.source_refs
                    or not v.test_coverage
                ]
                rules_test_verified = len(unverified_rules) == 0 and len(spec_obj.rules) == 23
            spec_err_str = f"Spec errors: {len(errors)}" if errors else "Valid"
        else:
            spec_err_str = f"Spec file not found at {self.spec_path}"

        checks.append(
            ReadinessCheckItem(
                name="spec_schema_valid",
                description="Game specification schema is valid and bound to source bundle",
                passed=spec_valid,
                blocker=True,
                details=spec_err_str,
            )
        )

        checks.append(
            ReadinessCheckItem(
                name="mandatory_rules_test_verified",
                description="All 23 mandatory rules are TEST_VERIFIED with authoritative citations and test coverage",
                passed=rules_test_verified,
                blocker=True,
                details=(
                    "All 23 mandatory rules TEST_VERIFIED"
                    if rules_test_verified
                    else "Mandatory rules not fully TEST_VERIFIED"
                ),
            )
        )

        # 2. Source bundle verification (strictly from spec or explicit hash, never iterdir)
        bundle_hash_found: str | None = self.source_bundle_hash
        if not bundle_hash_found and spec_obj and spec_obj.source_bundle_hash:
            bundle_hash_found = spec_obj.source_bundle_hash

        bundle_ok = False
        bundle_detail = "No source bundle hash specified or referenced by game spec."
        if bundle_hash_found:
            try:
                manifest = load_source_bundle_manifest(bundle_hash_found)
                bundle_ok = True
                bundle_detail = (
                    f"Verified bundle {bundle_hash_found[:12]} ({manifest.file_count} files)"
                )
            except Exception as e:
                bundle_detail = f"Bundle verification failed: {e}"

        checks.append(
            ReadinessCheckItem(
                name="source_bundle_exists",
                description="Authoritative source bundle exists and verifies",
                passed=bundle_ok,
                blocker=True,
                details=bundle_detail,
            )
        )

        # 3. SDK Probing & Location
        sdk_version: str | None = None
        sdk_configured = False
        sdk_exists = False
        sdk_runnable = False
        sdk_probed = False
        sdk_version_matches = False
        maps_discovered = False
        discovered_maps: list[str] = []

        if not isinstance(self.bridge, UnconfiguredOfficialBridge):
            try:
                probe_res = self.bridge.probe_sdk()
                sdk_configured = True
                sdk_exists = bool(probe_res.get("executable_exists", False))
                if hasattr(self.bridge, "sdk_path"):
                    sdk_exists = sdk_exists and Path(getattr(self.bridge, "sdk_path")).exists()
                sdk_runnable = bool(probe_res.get("executable_runnable", False))
                ver = probe_res.get("sdk_version")
                if (
                    ver
                    and isinstance(ver, str)
                    and ver.strip()
                    and ver.strip().lower()
                    not in ("unknown", "missing", "unspecified", "unreleased")
                ):
                    sdk_probed = True
                    sdk_version = ver.strip()

                configured_ver = spec_obj.sdk_version if spec_obj and spec_obj.sdk_version else None
                sdk_version_matches = bool(
                    sdk_probed and (not configured_ver or configured_ver == sdk_version)
                )

                discovered_maps = self.bridge.discover_maps()
                maps_discovered = len(discovered_maps) > 0
            except Exception:
                sdk_configured = False

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
                description="Official SDK executable exists",
                passed=sdk_exists,
                blocker=True,
                details=f"Executable exists: {sdk_exists}",
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="sdk_executable_runnable",
                description="Official SDK executable is runnable",
                passed=sdk_runnable,
                blocker=True,
                details=f"Executable runnable: {sdk_runnable}",
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
                details=f"Maps discovered: {len(discovered_maps)}",
            )
        )

        # 4. Minimal bot check
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

        # 5. Verification Execution: DO NOT trust bridge-returned booleans!
        # Actually execute build, match, replay parsing, and repeated determinism checks
        build_succeeds = False
        local_match_succeeds = False
        replay_parsing_succeeds = False
        deterministic_repeat_succeeds = False
        build_detail = "Prerequisites not met; build check not executed"
        match_detail = "Prerequisites not met; match check not executed"
        replay_detail = "Prerequisites not met; replay check not executed"
        det_detail = "Prerequisites not met; determinism check not executed"

        if (
            sdk_configured
            and sdk_exists
            and sdk_runnable
            and sdk_probed
            and maps_discovered
            and min_bot_exists
        ):
            import tempfile

            from battlelab.core.models import BotArtifact, MatchOutcome, MatchSpec
            from battlelab.official.adapter import OfficialAdapter
            from battlelab.official.models import compare_replay_determinism

            with tempfile.TemporaryDirectory() as tmp_str:
                tmp_dir = Path(tmp_str)
                # A. Execute actual build
                try:
                    build_out_dir = tmp_dir / "build"
                    b_res = self.bridge.build_or_prepare_artifact(min_bot_path, build_out_dir)
                    if isinstance(b_res, dict) and b_res.get("status") in ("SUCCESS", "OK", True):
                        build_succeeds = True
                        build_detail = "Executed artifact build successfully"
                    else:
                        build_detail = f"Build returned unexpected response: {b_res}"
                except Exception as e:
                    build_detail = f"Build execution failed: {e}"

                # B. Execute actual match
                if build_succeeds:
                    try:
                        bot_art = BotArtifact(
                            artifact_id="official_min",
                            display_name="OfficialMinimal",
                            source_location=str(min_bot_path),
                            language="python",
                            git_commit=None,
                            dirty_worktree=False,
                            source_hash="min_hash",
                        )
                        map_name = discovered_maps[0]
                        spec1 = MatchSpec(
                            match_id="readiness_check_match_1",
                            adapter_name="official",
                            adapter_version="1.0.0",
                            bot_a_id="official_min",
                            bot_b_id="official_min",
                            map_name=map_name,
                            seed=1337,
                            time_limit_ms=5000,
                        )
                        adapter = OfficialAdapter(bridge=self.bridge)
                        res1 = adapter.run_local_match(spec1, bot_art, bot_art, tmp_dir / "match1")
                        if (
                            res1.outcome
                            in (MatchOutcome.WIN_A, MatchOutcome.WIN_B, MatchOutcome.DRAW)
                            and res1.replay_path is not None
                        ):
                            local_match_succeeds = True
                            match_detail = (
                                f"Executed match successfully: outcome={res1.outcome.value}"
                            )

                            # C. Replay parsing
                            try:
                                rep1 = self.bridge.parse_replay(Path(res1.replay_path))
                                if rep1 and rep1.turn_count >= 0:
                                    replay_parsing_succeeds = True
                                    replay_detail = (
                                        "Parsed match replay into NormalizedReplay successfully"
                                    )
                            except Exception as e:
                                replay_detail = f"Failed to parse replay: {e}"

                            # D. Deterministic repeated execution
                            if replay_parsing_succeeds:
                                try:
                                    spec2 = MatchSpec(
                                        match_id="readiness_check_match_2",
                                        adapter_name="official",
                                        adapter_version="1.0.0",
                                        bot_a_id="official_min",
                                        bot_b_id="official_min",
                                        map_name=map_name,
                                        seed=1337,
                                        time_limit_ms=5000,
                                    )
                                    res2 = adapter.run_local_match(
                                        spec2, bot_art, bot_art, tmp_dir / "match2"
                                    )
                                    if res2.replay_path is not None:
                                        rep2 = self.bridge.parse_replay(Path(res2.replay_path))
                                        det_cmp = compare_replay_determinism(rep1, rep2)
                                        if det_cmp.gameplay_equal:
                                            deterministic_repeat_succeeds = True
                                            det_detail = (
                                                "Bit-exact repeated gameplay determinism confirmed"
                                            )
                                        else:
                                            det_detail = (
                                                f"Determinism mismatch: {det_cmp.differences}"
                                            )
                                except Exception as e:
                                    det_detail = f"Determinism run failed: {e}"
                        else:
                            match_detail = f"Match failed: outcome={res1.outcome.value}"
                    except Exception as e:
                        match_detail = f"Match execution failed: {e}"

        checks.append(
            ReadinessCheckItem(
                name="build_command_succeeds",
                description="Engine build/package step executes and verifies against bot artifact",
                passed=build_succeeds,
                blocker=True,
                details=build_detail,
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="local_match_succeeds",
                description="Engine local match executes and completes successfully",
                passed=local_match_succeeds,
                blocker=True,
                details=match_detail,
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="replay_parsing_succeeds",
                description="Replay output parsed into normalized replay contract",
                passed=replay_parsing_succeeds,
                blocker=True,
                details=replay_detail,
            )
        )
        checks.append(
            ReadinessCheckItem(
                name="deterministic_repeat_succeeds",
                description="Repeated identical match reproduces bit-exact gameplay",
                passed=deterministic_repeat_succeeds,
                blocker=True,
                details=det_detail,
            )
        )

        # 6. Remote and submission safety
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

        # Derive blockers mechanically from every failed check where blocker=True
        blockers = [c.name for c in checks if c.blocker and not c.passed]

        # can_run_local requires all prerequisites AND verified execution
        can_run_local = (
            bundle_ok
            and spec_valid
            and rules_test_verified
            and sdk_configured
            and sdk_exists
            and sdk_runnable
            and sdk_probed
            and sdk_version_matches
            and maps_discovered
            and min_bot_exists
            and build_succeeds
            and local_match_succeeds
            and replay_parsing_succeeds
            and deterministic_repeat_succeeds
        )

        can_submit = False  # Strictly False until release
        ready = (len(blockers) == 0) and can_run_local

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
