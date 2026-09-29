import hashlib
import json
import os
import platform
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.command_runner import OfficialCommandRunner
from battlelab.official.models import (
    OfficialSDKEvidence,
    ReadinessCheckItem,
    ReadinessReport,
    RuleTestEvidence,
)
from battlelab.official.sources import hash_file, load_source_bundle_manifest
from battlelab.official.spec import load_and_validate_spec
from battlelab.storage.paths import get_project_root


def generate_sdk_evidence(
    bridge: OfficialEngineBridge,
    command_runner: OfficialCommandRunner | None = None,
) -> tuple[bool, str, OfficialSDKEvidence | None]:
    """Cryptographically inspect and probe the bridge's declared SDK executable."""
    if isinstance(bridge, UnconfiguredOfficialBridge):
        return False, "Official competition SDK is unconfigured.", None

    exe_raw = bridge.get_sdk_executable()
    if exe_raw is None or not str(exe_raw).strip():
        return False, "Bridge has no declared SDK executable or launcher.", None

    exe_path = Path(exe_raw)
    if not exe_path.exists():
        return False, f"Declared SDK executable does not exist: {exe_path}", None

    try:
        st = os.lstat(exe_path)
        if stat.S_ISLNK(st.st_mode):
            return False, f"Declared SDK executable cannot be a symlink: {exe_path}", None
        if not stat.S_ISREG(st.st_mode):
            return False, f"Declared SDK executable must be a regular file: {exe_path}", None
    except OSError as e:
        return False, f"Failed to inspect SDK executable with lstat: {e}", None

    launcher_argv = bridge.get_launcher_argv()
    if launcher_argv:
        launcher_bin = Path(launcher_argv[0])
        if not launcher_bin.exists():
            return False, f"Launcher binary does not exist: {launcher_bin}", None
        try:
            l_st = os.lstat(launcher_bin)
            if stat.S_ISLNK(l_st.st_mode) or not stat.S_ISREG(l_st.st_mode):
                return (
                    False,
                    f"Launcher binary must be regular non-symlink file: {launcher_bin}",
                    None,
                )
            if platform.system() != "Windows" and not os.access(launcher_bin, os.X_OK):
                return False, f"Launcher binary is not executable: {launcher_bin}", None
        except OSError as e:
            return False, f"Failed to inspect launcher binary: {e}", None
        probe_argv = list(launcher_argv) + [str(exe_path), "probe"]
    else:
        if platform.system() != "Windows":
            if not os.access(exe_path, os.X_OK):
                return False, f"SDK executable is not executable: {exe_path}", None
        else:
            ext = exe_path.suffix.lower()
            pathext = [
                e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            ]
            if ext not in pathext:
                return (
                    False,
                    f"SDK executable has non-executable extension on Windows: {ext}",
                    None,
                )
        probe_argv = [str(exe_path), "probe"]

    exe_sha256 = hash_file(exe_path)

    runner = command_runner if command_runner is not None else OfficialCommandRunner()
    try:
        res = runner.run(probe_argv, cwd=exe_path.parent, timeout_seconds=10.0)
    except Exception as e:
        return False, f"SDK version probe failed to execute: {e}", None

    if res.timed_out:
        return False, f"SDK version probe timed out after 10s: {probe_argv}", None
    if res.exit_code != 0:
        return (
            False,
            f"SDK version probe failed with exit code {res.exit_code}: {res.stderr}",
            None,
        )

    probe_out_hash = hashlib.sha256(res.stdout.encode("utf-8")).hexdigest()
    probe_err_hash = hashlib.sha256(res.stderr.encode("utf-8")).hexdigest()

    detected_ver = ""
    try:
        probe_data = json.loads(res.stdout)
        if isinstance(probe_data, dict):
            detected_ver = str(probe_data.get("sdk_version", "")).strip()
    except Exception:
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if line_str:
                detected_ver = line_str
                break

    if not detected_ver or detected_ver.lower() in (
        "unknown",
        "missing",
        "unspecified",
        "unreleased",
    ):
        return (
            False,
            f"SDK version probe returned empty or invalid version: {detected_ver!r}",
            None,
        )

    evidence = OfficialSDKEvidence(
        schema_version="1.0.0",
        executable_path=str(exe_path.resolve()),
        file_type="regular_file",
        executable_sha256=exe_sha256,
        launcher_argv=list(launcher_argv),
        probe_argv=probe_argv,
        probe_exit_code=res.exit_code,
        probe_stdout_hash=probe_out_hash,
        probe_stderr_hash=probe_err_hash,
        sdk_version=detected_ver,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return True, f"Verified SDK {detected_ver} at {exe_path.name}", evidence


def probe_engine_executable(
    engine_path: str | None,
    bridge: OfficialEngineBridge | None = None,
    as_json: bool = False,
) -> int:
    """Truthfully probe an engine executable or configured bridge."""
    runner = OfficialCommandRunner()

    if engine_path is not None:
        p = Path(engine_path)
        if not p.exists():
            msg = f"Official SDK executable does not exist: {p}"
            if as_json:
                print(json.dumps({"error": msg, "success": False, "configured": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

        try:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                msg = f"Official SDK executable cannot be a symlink: {p}"
                if as_json:
                    print(
                        json.dumps({"error": msg, "success": False, "configured": False}, indent=2)
                    )
                else:
                    print(f"Error: {msg}", file=sys.stderr)
                return 1
            if not stat.S_ISREG(st.st_mode):
                msg = f"Official SDK executable must be a regular file: {p}"
                if as_json:
                    print(
                        json.dumps({"error": msg, "success": False, "configured": False}, indent=2)
                    )
                else:
                    print(f"Error: {msg}", file=sys.stderr)
                return 1
        except OSError as e:
            msg = f"Cannot access official SDK executable: {e}"
            if as_json:
                print(json.dumps({"error": msg, "success": False, "configured": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

        exe_sha256 = hash_file(p)

        if p.suffix == ".py":
            cmd = [sys.executable, str(p), "probe"]
        else:
            cmd = [str(p), "probe"]

        res = runner.run(cmd, cwd=p.parent, timeout_seconds=10.0)
        if res.exit_code != 0 or res.timed_out:
            msg = f"Official SDK probe command failed with exit code {res.exit_code}: {res.stderr}"
            if as_json:
                print(
                    json.dumps(
                        {
                            "error": msg,
                            "success": False,
                            "exit_code": res.exit_code,
                            "stderr": res.stderr,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

        detected_ver = ""
        try:
            d = json.loads(res.stdout)
            if isinstance(d, dict):
                detected_ver = str(d.get("sdk_version", "")).strip()
        except Exception:
            for line in res.stdout.splitlines():
                if line.strip():
                    detected_ver = line.strip()
                    break

        if not detected_ver or detected_ver.lower() in (
            "unknown",
            "missing",
            "unspecified",
            "unreleased",
        ):
            msg = f"Official SDK version could not be determined from probe output: {res.stdout}"
            if as_json:
                print(json.dumps({"error": msg, "success": False, "stdout": res.stdout}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1

        out_data = {
            "success": True,
            "executable_path": str(p.resolve()),
            "executable_sha256": exe_sha256,
            "sdk_version": detected_ver,
            "command_evidence": {
                "argv": res.argv,
                "exit_code": res.exit_code,
                "stdout_hash": hashlib.sha256(res.stdout.encode("utf-8")).hexdigest(),
                "stderr_hash": hashlib.sha256(res.stderr.encode("utf-8")).hexdigest(),
            },
        }
        if as_json:
            print(json.dumps(out_data, indent=2))
        else:
            print(
                f"Official SDK Probed:\n  Path:    {p}\n  Hash:    {exe_sha256}\n  Version: {detected_ver}"
            )
        return 0

    # engine_path is None: probe configured bridge
    if bridge is None or isinstance(bridge, UnconfiguredOfficialBridge):
        msg = "Official SDK is unconfigured and no engine_path argument was provided."
        if as_json:
            print(json.dumps({"error": msg, "configured": False, "success": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    ok, msg, evidence = generate_sdk_evidence(bridge, runner)
    if not ok or evidence is None:
        if as_json:
            print(json.dumps({"error": msg, "configured": False, "success": False}, indent=2))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    out_data = {
        "success": True,
        "executable_path": evidence.executable_path,
        "executable_sha256": evidence.executable_sha256,
        "sdk_version": evidence.sdk_version,
        "command_evidence": {
            "argv": evidence.probe_argv,
            "exit_code": evidence.probe_exit_code,
            "stdout_hash": evidence.probe_stdout_hash,
            "stderr_hash": evidence.probe_stderr_hash,
        },
    }
    if as_json:
        print(json.dumps(out_data, indent=2))
    else:
        print(
            f"Official SDK Probed:\n  Path:    {evidence.executable_path}\n  Hash:    {evidence.executable_sha256}\n  Version: {evidence.sdk_version}"
        )
    return 0


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

        spec_bundle_hash: str | None = None
        if self.spec_path.is_file():
            is_valid, errors, spec_obj, is_ready = load_and_validate_spec(self.spec_path)
            spec_valid = is_valid
            if is_valid and spec_obj:
                spec_hash = spec_obj.canonical_hash()
                spec_bundle_hash = spec_obj.source_bundle_hash
                unverified_rules = [
                    k
                    for k, v in spec_obj.rules.items()
                    if v.verification_state != "TEST_VERIFIED"
                    or not v.source_refs
                    or not v.test_coverage
                ]
                rules_test_verified = len(unverified_rules) == 0 and len(spec_obj.rules) == 23
            else:
                try:
                    import yaml

                    with open(self.spec_path, "r", encoding="utf-8") as f:
                        raw_data = yaml.safe_load(f)
                    if isinstance(raw_data, dict):
                        sbh = raw_data.get("source_bundle_hash")
                        if isinstance(sbh, str) and sbh.strip():
                            spec_bundle_hash = sbh.strip()
                except Exception:
                    pass
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
        bundle_conflict = False
        if self.source_bundle_hash and spec_bundle_hash:
            if self.source_bundle_hash != spec_bundle_hash:
                bundle_conflict = True

        bundle_hash_found: str | None = self.source_bundle_hash
        if not bundle_hash_found and spec_bundle_hash:
            bundle_hash_found = spec_bundle_hash

        bundle_ok = False
        bundle_detail = "No source bundle hash specified or referenced by game spec."
        if bundle_conflict and spec_bundle_hash:
            bundle_ok = False
            bundle_detail = (
                f"Explicit source bundle hash '{self.source_bundle_hash}' differs from "
                f"game spec source bundle hash '{spec_bundle_hash}'"
            )
        elif bundle_hash_found:
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
        sdk_evidence: OfficialSDKEvidence | None = None

        if not isinstance(self.bridge, UnconfiguredOfficialBridge):
            exe_raw = self.bridge.get_sdk_executable()
            if exe_raw is not None and str(exe_raw).strip():
                sdk_configured = True
                exe_p = Path(exe_raw)
                try:
                    st = os.lstat(exe_p)
                    if not stat.S_ISLNK(st.st_mode) and stat.S_ISREG(st.st_mode):
                        sdk_exists = True
                except OSError:
                    pass

            ok, msg, evidence = generate_sdk_evidence(self.bridge)
            if ok and evidence is not None:
                sdk_evidence = evidence
                sdk_configured = True
                sdk_exists = True
                sdk_runnable = True
                sdk_probed = True
                sdk_version = evidence.sdk_version

                configured_ver = spec_obj.sdk_version if spec_obj and spec_obj.sdk_version else None
                sdk_version_matches = bool(
                    configured_ver is not None and configured_ver == sdk_version
                )

                try:
                    discovered_maps = self.bridge.discover_maps()
                    maps_discovered = len(discovered_maps) > 0
                except Exception:
                    maps_discovered = False

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
                            match_wall_clock_limit_ms=30000,
                            per_turn_limit_ms=5000,
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
                                        match_wall_clock_limit_ms=30000,
                                        per_turn_limit_ms=5000,
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

        # Construct RuleTestEvidence if tests verified and SDK bound
        test_evidence: RuleTestEvidence | None = None
        if rules_test_verified and spec_obj and sdk_evidence and bundle_ok and bundle_hash_found:
            all_node_ids: list[str] = []
            for r in spec_obj.rules.values():
                all_node_ids.extend(r.test_coverage)
            all_node_ids = sorted(set(all_node_ids))

            from battlelab.bots.artifacts import check_git_status

            git_commit, _ = check_git_status(get_project_root())

            test_evidence = RuleTestEvidence(
                schema_version="1.0.0",
                spec_hash=spec_hash or "",
                source_bundle_hash=bundle_hash_found,
                sdk_executable_sha256=sdk_evidence.executable_sha256,
                git_commit=git_commit or "unknown",
                test_node_ids=all_node_ids,
                verified_at=datetime.now(timezone.utc).isoformat(),
            )

        return ReadinessReport(
            ready=ready,
            can_run_local=can_run_local,
            can_submit=can_submit,
            checks=checks,
            source_bundle_hash=bundle_hash_found,
            spec_hash=spec_hash,
            sdk_version=sdk_version,
            blockers=blockers,
            sdk_evidence=sdk_evidence,
            test_evidence=test_evidence,
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
            "sdk_evidence": report.sdk_evidence.to_dict() if report.sdk_evidence else None,
            "test_evidence": report.test_evidence.to_dict() if report.test_evidence else None,
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
