import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from battlelab.core.hashing import hash_bytes
from battlelab.official._win_appexeclink import resolve_executable_for_hash
from battlelab.official.bridge import OfficialEngineBridge, UnconfiguredOfficialBridge
from battlelab.official.command_runner import OfficialCommandRunner
from battlelab.official.models import (
    OfficialCommandPlan,
    OfficialSDKEvidence,
    ReadinessCheckItem,
    ReadinessReport,
    RuleTestEvidence,
)
from battlelab.official.sources import hash_file, load_source_bundle_manifest
from battlelab.official.spec import load_and_validate_spec
from battlelab.storage.paths import get_project_root


@dataclass
class RuleTestRunResult:
    """Result of running pytest collection and execution on rule test node IDs."""

    success: bool
    error_message: str = ""
    collection_command_hash: str = ""
    execution_command_hash: str = ""
    collection_exit_code: int = 0
    execution_exit_code: int = 0
    collection_stdout_hash: str = ""
    collection_stderr_hash: str = ""
    execution_stdout_hash: str = ""
    execution_stderr_hash: str = ""
    junit_xml_hash: str | None = None
    requested_count: int = 0
    collected_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    errored_count: int = 0
    skipped_count: int = 0
    xfailed_count: int = 0
    deselected_count: int = 0
    collected_node_ids: list[str] = field(default_factory=list)


class RuleTestRunner(ABC):
    """Abstract runner for executing rule tests for real with verifiable evidence generation."""

    @abstractmethod
    def run_rule_tests(
        self,
        node_ids: list[str],
        project_root: Path,
        timeout_seconds: float = 60.0,
    ) -> RuleTestRunResult:
        """Run collection and execution for node IDs."""
        ...


class DefaultRuleTestRunner(RuleTestRunner):
    """Concrete RuleTestRunner that runs pytest collection and isolated execution subprocess."""

    def __init__(self, command_runner: OfficialCommandRunner | None = None) -> None:
        self.command_runner = command_runner or OfficialCommandRunner()

    def run_rule_tests(
        self,
        node_ids: list[str],
        project_root: Path,
        timeout_seconds: float = 60.0,
    ) -> RuleTestRunResult:
        if not node_ids:
            return RuleTestRunResult(success=False, error_message="No test node IDs requested")

        requested_set = set(node_ids)
        requested_count = len(node_ids)

        # 1. Run pytest --collect-only -q <node_ids...>
        collect_argv = [sys.executable, "-m", "pytest", "--collect-only", "-q"] + node_ids
        collect_cmd_hash = hash_bytes(" ".join(collect_argv).encode("utf-8"))

        collect_res = self.command_runner.run(
            argv=collect_argv,
            cwd=project_root,
            timeout_seconds=min(30.0, timeout_seconds),
        )
        collect_out_h = hash_bytes(collect_res.stdout.encode("utf-8"))
        collect_err_h = hash_bytes(collect_res.stderr.encode("utf-8"))

        if collect_res.exit_code != 0:
            return RuleTestRunResult(
                success=False,
                error_message=f"pytest collection failed with exit code {collect_res.exit_code}: {collect_res.stderr}",
                collection_command_hash=collect_cmd_hash,
                collection_exit_code=collect_res.exit_code or 1,
                collection_stdout_hash=collect_out_h,
                collection_stderr_hash=collect_err_h,
                requested_count=requested_count,
            )

        # Parse collected node IDs from stdout
        collected_ids: list[str] = []
        for line in collect_res.stdout.splitlines():
            line_str = line.strip()
            if "::" in line_str and not line_str.startswith("<"):
                norm_line = line_str.replace("\\", "/")
                collected_ids.append(norm_line)

        # Confirm every node ID is collected exactly once
        collected_set = set(collected_ids)
        if len(collected_ids) != len(collected_set):
            return RuleTestRunResult(
                success=False,
                error_message=f"Duplicate test items collected: {collected_ids}",
                collection_command_hash=collect_cmd_hash,
                collection_exit_code=collect_res.exit_code or 0,
                collection_stdout_hash=collect_out_h,
                collection_stderr_hash=collect_err_h,
                requested_count=requested_count,
                collected_count=len(collected_ids),
                collected_node_ids=collected_ids,
            )

        missing_from_collected = requested_set - collected_set
        if missing_from_collected:
            return RuleTestRunResult(
                success=False,
                error_message=f"Requested test node IDs not collected: {sorted(missing_from_collected)}",
                collection_command_hash=collect_cmd_hash,
                collection_exit_code=collect_res.exit_code or 0,
                collection_stdout_hash=collect_out_h,
                collection_stderr_hash=collect_err_h,
                requested_count=requested_count,
                collected_count=len(collected_ids),
                collected_node_ids=collected_ids,
            )

        unrequested_collected = collected_set - requested_set
        if unrequested_collected:
            return RuleTestRunResult(
                success=False,
                error_message=f"Unexpected test items collected: {sorted(unrequested_collected)}",
                collection_command_hash=collect_cmd_hash,
                collection_exit_code=collect_res.exit_code or 0,
                collection_stdout_hash=collect_out_h,
                collection_stderr_hash=collect_err_h,
                requested_count=requested_count,
                collected_count=len(collected_ids),
                collected_node_ids=collected_ids,
            )

        # 2. Check repo git status before test execution
        status_before = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

        # 3. Execute exact node IDs in an isolated subprocess with --junitxml
        with tempfile.TemporaryDirectory() as td:
            junit_path = Path(td) / "junit.xml"
            exec_argv = [
                sys.executable,
                "-m",
                "pytest",
                "-v",
                f"--junitxml={junit_path}",
            ] + node_ids
            exec_cmd_hash = hash_bytes(" ".join(exec_argv).encode("utf-8"))

            exec_res = self.command_runner.run(
                argv=exec_argv,
                cwd=project_root,
                timeout_seconds=timeout_seconds,
            )
            exec_out_h = hash_bytes(exec_res.stdout.encode("utf-8"))
            exec_err_h = hash_bytes(exec_res.stderr.encode("utf-8"))

            # 4. Check repo git status after test execution
            status_after = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()

            if status_before != status_after:
                return RuleTestRunResult(
                    success=False,
                    error_message="Test execution mutated repository-tracked files",
                    collection_command_hash=collect_cmd_hash,
                    execution_command_hash=exec_cmd_hash,
                    collection_exit_code=collect_res.exit_code or 0,
                    execution_exit_code=exec_res.exit_code or 1,
                    collection_stdout_hash=collect_out_h,
                    collection_stderr_hash=collect_err_h,
                    execution_stdout_hash=exec_out_h,
                    execution_stderr_hash=exec_err_h,
                    requested_count=requested_count,
                    collected_count=len(collected_ids),
                )

            # 5. Parse JUnit XML
            junit_hash: str | None = None
            passed_c = 0
            failed_c = 0
            errored_c = 0
            skipped_c = 0
            xfailed_c = 0

            if junit_path.exists():
                junit_hash = hash_file(junit_path)
                try:
                    tree = ET.parse(junit_path)
                    root = tree.getroot()
                    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
                    for suite in suites:
                        failed_c += int(suite.attrib.get("failures", 0))
                        errored_c += int(suite.attrib.get("errors", 0))
                        skipped_c += int(suite.attrib.get("skipped", 0))
                        tests_total = int(suite.attrib.get("tests", 0))
                        passed_c += max(0, tests_total - failed_c - errored_c - skipped_c)

                    for tc in root.iter("testcase"):
                        sk_el = tc.find("skipped")
                        if sk_el is not None:
                            msg = sk_el.attrib.get("message", "")
                            if "xfail" in msg.lower():
                                xfailed_c += 1
                except Exception as e:
                    return RuleTestRunResult(
                        success=False,
                        error_message=f"Failed to parse JUnit XML: {e}",
                        collection_command_hash=collect_cmd_hash,
                        execution_command_hash=exec_cmd_hash,
                        collection_exit_code=collect_res.exit_code or 0,
                        execution_exit_code=exec_res.exit_code or 1,
                        collection_stdout_hash=collect_out_h,
                        collection_stderr_hash=collect_err_h,
                        execution_stdout_hash=exec_out_h,
                        execution_stderr_hash=exec_err_h,
                        requested_count=requested_count,
                        collected_count=len(collected_ids),
                    )

            all_passed = (
                exec_res.exit_code == 0
                and passed_c == requested_count
                and failed_c == 0
                and errored_c == 0
                and skipped_c == 0
                and xfailed_c == 0
            )

            err_msg = ""
            if not all_passed:
                err_msg = (
                    f"Test execution failed: exit_code={exec_res.exit_code}, "
                    f"passed={passed_c}/{requested_count}, failed={failed_c}, "
                    f"errored={errored_c}, skipped={skipped_c}, xfailed={xfailed_c}"
                )

            return RuleTestRunResult(
                success=all_passed,
                error_message=err_msg,
                collection_command_hash=collect_cmd_hash,
                execution_command_hash=exec_cmd_hash,
                collection_exit_code=collect_res.exit_code or 0,
                execution_exit_code=exec_res.exit_code or 0,
                collection_stdout_hash=collect_out_h,
                collection_stderr_hash=collect_err_h,
                execution_stdout_hash=exec_out_h,
                execution_stderr_hash=exec_err_h,
                junit_xml_hash=junit_hash,
                requested_count=requested_count,
                collected_count=len(collected_ids),
                passed_count=passed_c,
                failed_count=failed_c,
                errored_count=errored_c,
                skipped_count=skipped_c,
                xfailed_count=xfailed_c,
                deselected_count=0,
                collected_node_ids=collected_ids,
            )


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
            resolved_launcher = launcher_bin.resolve()
            l_st = os.stat(resolved_launcher)
            if not stat.S_ISREG(l_st.st_mode):
                return (
                    False,
                    f"Launcher binary must resolve to a regular file: {launcher_bin}",
                    None,
                )
            if platform.system() != "Windows" and not os.access(resolved_launcher, os.X_OK):
                return False, f"Launcher binary is not executable: {launcher_bin}", None
        except OSError as e:
            return False, f"Failed to inspect launcher binary: {e}", None
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

    exe_sha256 = hash_file(resolve_executable_for_hash(exe_path))
    runner = command_runner if command_runner is not None else OfficialCommandRunner()

    try:
        plan = bridge.build_probe_command()
    except Exception as e:
        return False, f"Failed to build probe command plan: {e}", None

    try:
        res = runner.execute_plan(plan, timeout_seconds=10.0)
    except Exception as e:
        return False, f"SDK version probe failed to execute: {e}", None

    if res.timed_out:
        return False, f"SDK version probe timed out after 10s: {plan.get_argv()}", None
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
        probe_data = bridge.parse_probe_result(res)
        if isinstance(probe_data, dict):
            detected_ver = str(probe_data.get("sdk_version", "")).strip()
    except Exception:
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
        probe_argv=plan.get_argv(),
        probe_exit_code=res.exit_code or 0,
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

        exe_sha256 = hash_file(resolve_executable_for_hash(p))

        if bridge is not None and not isinstance(bridge, UnconfiguredOfficialBridge):
            try:
                plan = bridge.build_probe_command()
            except Exception as e:
                msg = f"Bridge failed to build probe command plan: {e}"
                if as_json:
                    print(json.dumps({"error": msg, "success": False}, indent=2))
                else:
                    print(f"Error: {msg}", file=sys.stderr)
                return 1
        else:
            launcher_argv = [sys.executable] if p.suffix == ".py" else []
            plan = OfficialCommandPlan(
                operation="PROBE",
                launcher_argv=launcher_argv,
                sdk_executable_path=str(p),
                operation_argv=["probe"],
                cwd=str(p.parent),
                sdk_executable_sha256=exe_sha256,
            )

        try:
            res = runner.execute_plan(plan, timeout_seconds=10.0)
        except Exception as e:
            msg = f"Official SDK probe command failed: {e}"
            if as_json:
                print(json.dumps({"error": msg, "success": False}, indent=2))
            else:
                print(f"Error: {msg}", file=sys.stderr)
            return 1
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
        runner: OfficialCommandRunner | None = None,
        rule_test_runner: RuleTestRunner | None = None,
    ) -> None:
        self.bridge = bridge if bridge is not None else UnconfiguredOfficialBridge()
        self.spec_path = (
            spec_path
            if spec_path is not None
            else get_project_root() / "configs" / "game_spec.yaml"
        )
        self.source_bundle_hash = source_bundle_hash
        self.runner = runner if runner is not None else OfficialCommandRunner()
        self.rule_test_runner = (
            rule_test_runner if rule_test_runner is not None else DefaultRuleTestRunner(self.runner)
        )

    def evaluate(self) -> ReadinessReport:
        """Run all readiness checks and compute derived capabilities."""
        checks: list[ReadinessCheckItem] = []

        # 1. Spec schema and documentation verification
        spec_hash: str | None = None
        spec_valid = False
        spec_obj = None
        rules_test_verified = False
        test_run_result: RuleTestRunResult | None = None
        test_file_hashes: dict[str, str] = {}
        all_node_ids: list[str] = []
        rules_verify_detail = "Mandatory rules not fully TEST_VERIFIED"

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
                rules_declared_verified = len(unverified_rules) == 0 and len(spec_obj.rules) == 23
                if rules_declared_verified:
                    for r in spec_obj.rules.values():
                        all_node_ids.extend(r.test_coverage)
                    all_node_ids = sorted(set(all_node_ids))

                    proj_root = get_project_root()
                    test_files = sorted(set(nid.split("::")[0] for nid in all_node_ids))
                    test_files_missing = False
                    for tf in test_files:
                        tf_path = proj_root / tf
                        if not tf_path.is_file():
                            test_files_missing = True
                            rules_verify_detail = f"Rule test file missing: {tf}"
                            break
                        test_file_hashes[tf] = hash_file(tf_path)

                    if not test_files_missing:
                        # Check git status for modified or untracked test/spec files
                        dirty = False
                        try:
                            check_files = [str(self.spec_path.resolve())] + [
                                str((proj_root / tf).resolve()) for tf in test_files
                            ]
                            inside_repo: list[str] = []
                            for p in check_files:
                                try:
                                    p_resolved = Path(p).resolve()
                                    p_resolved.relative_to(proj_root.resolve())
                                    inside_repo.append(str(p_resolved))
                                except ValueError:
                                    pass

                            if inside_repo:
                                res = subprocess.run(
                                    ["git", "status", "--porcelain", "--"] + inside_repo,
                                    cwd=proj_root,
                                    capture_output=True,
                                    text=True,
                                    check=False,
                                )
                                if res.returncode == 0 and res.stdout.strip():
                                    dirty = True
                        except Exception:
                            dirty = True

                        if dirty:
                            rules_verify_detail = "Rule test verification rejected: worktree has uncommitted modifications to test or spec files"
                        else:
                            test_run_result = self.rule_test_runner.run_rule_tests(
                                all_node_ids, proj_root
                            )
                            if test_run_result.success:
                                rules_test_verified = True
                                rules_verify_detail = f"All 23 mandatory rules TEST_VERIFIED ({test_run_result.passed_count} tests executed and passed)"
                            else:
                                rules_verify_detail = (
                                    f"Rule test execution failed: {test_run_result.error_message}"
                                )
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
                details=rules_verify_detail,
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

            ok, msg, evidence = generate_sdk_evidence(self.bridge, self.runner)
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
                    map_plan = self.bridge.build_map_discovery_command()
                    map_res = self.runner.execute_plan(map_plan)
                    if map_res.exit_code == 0:
                        discovered_maps = self.bridge.parse_map_discovery_result(map_res)
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
        min_bot_detail = f"Path: {min_bot_path} ({'Exists' if min_bot_exists else 'Missing'})"
        if min_bot_exists and spec_obj:
            manifest_file = min_bot_path / "manifest.json"
            if manifest_file.is_file():
                try:
                    with open(manifest_file, "r", encoding="utf-8") as mf:
                        m_data = json.load(mf)
                    bot_lang = m_data.get("language", "")
                    if spec_obj.minimal_bot_language and bot_lang != spec_obj.minimal_bot_language:
                        min_bot_exists = False
                        min_bot_detail = f"Minimal bot language '{bot_lang}' does not match spec '{spec_obj.minimal_bot_language}'"
                except Exception as e:
                    min_bot_exists = False
                    min_bot_detail = f"Invalid minimal bot manifest: {e}"
            elif spec_obj.minimal_bot_language:
                lang = spec_obj.minimal_bot_language.lower()
                exts = {
                    "python": [".py"],
                    "java": [".java", ".jar"],
                    "c++": [".cpp", ".cc", ".cxx"],
                    "rust": [".rs"],
                    "go": [".go"],
                }.get(lang, [])
                has_lang_file = any(f.suffix in exts for f in min_bot_path.iterdir() if f.is_file())
                if not has_lang_file:
                    min_bot_exists = False
                    min_bot_detail = f"Minimal bot missing files for declared language '{lang}'"

            if min_bot_exists and spec_obj.minimal_bot_layout:
                req_files = spec_obj.minimal_bot_layout.get("required_files", [])
                for rf in req_files:
                    if not (min_bot_path / rf).exists():
                        min_bot_exists = False
                        min_bot_detail = f"Minimal bot missing required layout file: {rf}"
                        break

        checks.append(
            ReadinessCheckItem(
                name="minimal_legal_bot_exists",
                description="Minimal legal official starter bot baseline exists",
                passed=min_bot_exists,
                blocker=True,
                details=min_bot_detail,
            )
        )

        # 5. Verification Execution: DO NOT trust bridge-returned booleans!
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

            from battlelab.bots.artifacts import compute_artifact_manifest
            from battlelab.core.models import BotArtifact, MatchOutcome, MatchSpec
            from battlelab.official.adapter import OfficialAdapter
            from battlelab.official.models import compare_replay_determinism

            with tempfile.TemporaryDirectory() as tmp_str:
                tmp_dir = Path(tmp_str)
                # A. Execute actual build
                build_out_dir = tmp_dir / "build"
                build_out_dir.mkdir(parents=True, exist_ok=True)
                try:
                    build_plan = self.bridge.build_artifact_command(min_bot_path, build_out_dir)
                    b_res = self.runner.execute_plan(build_plan)
                    if b_res.exit_code == 0:
                        self.bridge.parse_build_result(b_res, build_out_dir)
                        # Verify artifact manifest in build_out_dir
                        manifest_file = build_out_dir / "manifest.json"
                        if not manifest_file.is_file():
                            build_detail = (
                                f"Build artifact missing manifest.json in {build_out_dir}"
                            )
                        else:
                            with open(manifest_file, "r", encoding="utf-8") as mf:
                                m_data = json.load(mf)

                            _, _, expected_bot_hash = compute_artifact_manifest(min_bot_path)
                            expected_sdk_hash = (
                                sdk_evidence.executable_sha256 if sdk_evidence else ""
                            )
                            expected_cmd_hash = hash_bytes(
                                " ".join(build_plan.get_argv()).encode("utf-8")
                            )

                            m_bot_h = m_data.get("source_bot_hash")
                            m_sdk_h = m_data.get("sdk_executable_sha256")
                            m_cmd_h = m_data.get("build_command_hash")

                            if m_bot_h != expected_bot_hash:
                                build_detail = f"Build manifest source_bot_hash mismatch: {m_bot_h} != {expected_bot_hash}"
                            elif m_sdk_h != expected_sdk_hash:
                                build_detail = f"Build manifest sdk_executable_sha256 mismatch: {m_sdk_h} != {expected_sdk_hash}"
                            elif m_cmd_h != expected_cmd_hash:
                                build_detail = f"Build manifest build_command_hash mismatch: {m_cmd_h} != {expected_cmd_hash}"
                            else:
                                build_succeeds = True
                                build_detail = "Executed artifact build and verified cryptographic bindings in manifest"
                    else:
                        build_detail = f"Build execution failed with exit code {b_res.exit_code}: {b_res.stderr}"
                except Exception as e:
                    build_detail = f"Build execution failed: {e}"

                # B. Execute actual match
                if build_succeeds:
                    try:
                        bot_lang = (
                            spec_obj.minimal_bot_language
                            if (spec_obj and spec_obj.minimal_bot_language)
                            else "python"
                        )
                        bot_art = BotArtifact(
                            artifact_id="official_min",
                            display_name="OfficialMinimal",
                            source_location=str(min_bot_path),
                            language=bot_lang,
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
                        adapter = OfficialAdapter(bridge=self.bridge, command_runner=self.runner)
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

        # 6. Resource limits enforcement honesty
        mem_status, mem_reason = self.runner.get_memory_enforcement_status()
        checks.append(
            ReadinessCheckItem(
                name="resource_limits_enforced",
                description="Memory resource limits enforcement status",
                passed=True,
                blocker=False,
                details=f"Memory limit enforcement: {mem_status.value} ({mem_reason})",
            )
        )

        # 7. Remote and submission safety
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
        if (
            rules_test_verified
            and spec_obj
            and sdk_evidence
            and bundle_ok
            and bundle_hash_found
            and test_run_result is not None
        ):
            from battlelab.bots.artifacts import check_git_status

            git_commit, _ = check_git_status(get_project_root())

            launcher_h = None
            if sdk_evidence.launcher_argv:
                try:
                    launcher_p = Path(sdk_evidence.launcher_argv[0])
                    if launcher_p.is_file():
                        launcher_h = hash_file(resolve_executable_for_hash(launcher_p))
                except Exception:
                    pass

            test_evidence = RuleTestEvidence(
                schema_version="1.0.0",
                spec_hash=spec_hash or "",
                source_bundle_hash=bundle_hash_found,
                sdk_executable_sha256=sdk_evidence.executable_sha256,
                launcher_executable_sha256=launcher_h,
                git_commit=git_commit or "unknown",
                dirty_worktree=False,
                test_node_ids=all_node_ids,
                test_file_hashes=test_file_hashes,
                collection_command_hash=test_run_result.collection_command_hash,
                execution_command_hash=test_run_result.execution_command_hash,
                collection_exit_code=test_run_result.collection_exit_code,
                execution_exit_code=test_run_result.execution_exit_code,
                collection_stdout_hash=test_run_result.collection_stdout_hash,
                collection_stderr_hash=test_run_result.collection_stderr_hash,
                execution_stdout_hash=test_run_result.execution_stdout_hash,
                execution_stderr_hash=test_run_result.execution_stderr_hash,
                junit_xml_hash=test_run_result.junit_xml_hash,
                requested_count=test_run_result.requested_count,
                collected_count=test_run_result.collected_count,
                passed_count=test_run_result.passed_count,
                failed_count=test_run_result.failed_count,
                errored_count=test_run_result.errored_count,
                skipped_count=test_run_result.skipped_count,
                xfailed_count=test_run_result.xfailed_count,
                deselected_count=test_run_result.deselected_count,
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
            memory_enforcement_status=mem_status,
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
            "memory_enforcement_status": report.memory_enforcement_status.value,
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
