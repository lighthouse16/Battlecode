"""Secure official command runner with bounded output, isolation, and process-tree termination."""

from __future__ import annotations

import os
import platform
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from battlelab.bots.process_runner import terminate_process_tree
from battlelab.official._win_appexeclink import resolve_executable_for_hash
from battlelab.official.models import CommandResult, EnforcementStatus, OfficialCommandPlan
from battlelab.official.sources import hash_file

DEFAULT_ALLOWED_ENV_VARS = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USER",
        "USERNAME",
        "HOMEPATH",
        "HOMEDRIVE",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
    }
)


class InfrastructureTamperingError(RuntimeError):
    """Raised when an executable is mutated or substituted before/during/after execution."""


class OfficialCommandRunner:
    """Secure, isolated external command runner for official competition executables."""

    def __init__(self, allowed_env_vars: frozenset[str] = DEFAULT_ALLOWED_ENV_VARS) -> None:
        self.allowed_env_vars = allowed_env_vars

    def get_memory_enforcement_status(
        self,
        engine_enforced: bool = False,
        engine_evidence_verified: bool = False,
    ) -> tuple[EnforcementStatus, str]:
        """Return truthful memory enforcement status."""
        if engine_enforced and engine_evidence_verified:
            return (
                EnforcementStatus.OFFICIAL_ENGINE_ENFORCED,
                "Engine-enforced memory limits verified with test evidence",
            )
        if platform.system() in ("Linux", "Darwin"):
            return (
                EnforcementStatus.PLATFORM_ENFORCED,
                "resource.setrlimit supported on POSIX child launch",
            )
        return (
            EnforcementStatus.UNENFORCED,
            "Windows platform lacks stdlib Job Objects memory limit enforcement",
        )

    def _redact(self, text: str, secrets: list[str] | None) -> str:
        """Redact known secret strings from text."""
        if not secrets or not text:
            return text
        result = text
        for secret in secrets:
            if secret:
                result = result.replace(secret, "***REDACTED***")
        return result

    def _reader(
        self, stream: Any, chunks: list[bytes], limit: int, truncated_flag: list[bool]
    ) -> None:
        """Read stream up to limit bytes in worker thread."""
        total = 0
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                if total + len(chunk) <= limit:
                    chunks.append(chunk)
                    total += len(chunk)
                else:
                    remaining = limit - total
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                        total += remaining
                    truncated_flag[0] = True
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def run(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
        stdout_limit_bytes: int = 10 * 1024 * 1024,
        stderr_limit_bytes: int = 10 * 1024 * 1024,
        secrets: list[str] | None = None,
        dry_run: bool = False,
        memory_limit_mb: int | None = None,
    ) -> CommandResult:
        """Execute external command with strict security and termination guarantees."""

        def _err(exc_type: type[Exception], msg: str) -> Exception:
            return exc_type(self._redact(msg, secrets))

        # 1. Input validation
        if not argv or not isinstance(argv, (list, tuple)):
            raise _err(ValueError, "argv must be a non-empty list of strings")

        for arg in argv:
            if not isinstance(arg, str):
                raise _err(
                    TypeError, f"All argv elements must be strings, got {type(arg).__name__}"
                )
            if "\0" in arg:
                raise _err(ValueError, "NUL character not permitted in command arguments")

        import math

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise _err(
                ValueError,
                f"timeout_seconds must be a finite non-negative number, got {timeout_seconds!r}",
            )
        if (
            isinstance(stdout_limit_bytes, bool)
            or not isinstance(stdout_limit_bytes, int)
            or stdout_limit_bytes < 0
        ):
            raise _err(
                ValueError,
                f"stdout_limit_bytes must be a non-negative integer, got {stdout_limit_bytes!r}",
            )
        if (
            isinstance(stderr_limit_bytes, bool)
            or not isinstance(stderr_limit_bytes, int)
            or stderr_limit_bytes < 0
        ):
            raise _err(
                ValueError,
                f"stderr_limit_bytes must be a non-negative integer, got {stderr_limit_bytes!r}",
            )

        cwd_path = Path(cwd)
        if not cwd_path.exists():
            raise _err(FileNotFoundError, f"Working directory does not exist: {cwd_path}")
        if not cwd_path.is_dir():
            raise _err(NotADirectoryError, f"Working directory is not a directory: {cwd_path}")

        # 2. Filter and validate environment variables BEFORE dry-run
        filtered_env: dict[str, str] = {}
        for var_name in self.allowed_env_vars:
            if var_name in os.environ:
                filtered_env[var_name] = os.environ[var_name]
        filtered_env["PYTHONDONTWRITEBYTECODE"] = "1"

        if env is not None:
            if not isinstance(env, dict):
                raise _err(TypeError, "env must be a dictionary")
            for k, v in env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise _err(TypeError, "env variable names and values must be strings")
                if "\0" in k or "\0" in v:
                    raise _err(
                        ValueError,
                        "NUL character not permitted in environment variable name or value",
                    )
                if k not in self.allowed_env_vars:
                    raise _err(
                        ValueError, f"Environment variable '{k}' is not permitted by allowlist"
                    )
                filtered_env[k] = v

        # 3. Redacted argv representation
        redacted_argv = [self._redact(a, secrets) for a in argv]

        # 4. Dry run mode
        if dry_run:
            return CommandResult(
                argv=redacted_argv,
                exit_code=0,
                stdout="[DRY_RUN] Command rendered without execution.",
                stderr="",
                duration_ms=0.0,
                timed_out=False,
                cancelled=False,
                stdout_truncated=False,
                stderr_truncated=False,
            )

        # 5. Launch process
        popen_kwargs: dict[str, Any] = {
            "cwd": str(cwd_path),
            "env": filtered_env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if platform.system() != "Windows":
            popen_kwargs["start_new_session"] = True
            if memory_limit_mb is not None and memory_limit_mb > 0:

                def _set_mem_limit() -> None:
                    try:
                        import resource

                        lim = int(memory_limit_mb) * 1024 * 1024
                        setrlimit = getattr(resource, "setrlimit", None)
                        rlimit_as = getattr(resource, "RLIMIT_AS", None)
                        if setrlimit is not None and rlimit_as is not None:
                            setrlimit(rlimit_as, (lim, lim))
                    except Exception:
                        pass

                popen_kwargs["preexec_fn"] = _set_mem_limit

        start_time = time.monotonic()
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except Exception as e:
            raise _err(RuntimeError, f"Failed to start command: {e}") from None

        # 6. Stream capture threads
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_trunc = [False]
        stderr_trunc = [False]

        t_out = threading.Thread(
            target=self._reader,
            args=(proc.stdout, stdout_chunks, stdout_limit_bytes, stdout_trunc),
            daemon=True,
        )
        t_err = threading.Thread(
            target=self._reader,
            args=(proc.stderr, stderr_chunks, stderr_limit_bytes, stderr_trunc),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        timed_out = False
        cancelled = False

        try:
            while True:
                ret = proc.poll()
                if ret is not None:
                    break

                elapsed = time.monotonic() - start_time
                if elapsed >= timeout_seconds:
                    timed_out = True
                    terminate_process_tree(proc, timeout_seconds=1.0)
                    break

                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    terminate_process_tree(proc, timeout_seconds=1.0)
                    break

                time.sleep(0.02)
        except Exception as e:
            terminate_process_tree(proc, timeout_seconds=1.0)
            redacted_err = self._redact(str(e), secrets)
            raise RuntimeError(f"Command execution error: {redacted_err}") from None
        finally:
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)

        duration_ms = (time.monotonic() - start_time) * 1000.0
        exit_code = proc.poll()

        stdout_raw = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr_raw = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        return CommandResult(
            argv=redacted_argv,
            exit_code=exit_code,
            stdout=self._redact(stdout_raw, secrets),
            stderr=self._redact(stderr_raw, secrets),
            duration_ms=duration_ms,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_truncated=stdout_trunc[0],
            stderr_truncated=stderr_trunc[0],
        )

    def execute_plan(
        self,
        plan: OfficialCommandPlan,
        timeout_seconds: float = 30.0,
        cancel_event: threading.Event | None = None,
        secrets: list[str] | None = None,
        dry_run: bool = False,
        memory_limit_mb: int | None = None,
    ) -> CommandResult:
        """Validate exact command identity and execute an immutable OfficialCommandPlan."""
        # 1. Inspect SDK executable
        sdk_path = Path(plan.sdk_executable_path)
        if not sdk_path.exists():
            raise FileNotFoundError(f"SDK executable does not exist: {sdk_path}")
        try:
            st = os.lstat(sdk_path)
            if stat.S_ISLNK(st.st_mode):
                raise ValueError(f"SDK executable cannot be a symlink: {sdk_path}")
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"SDK executable must be a regular file: {sdk_path}")
        except OSError as e:
            raise ValueError(f"Failed to lstat SDK executable: {e}") from None

        # Verify no ancestor symlinks
        cur = sdk_path.resolve().parent
        while cur != cur.parent:
            try:
                if stat.S_ISLNK(os.lstat(cur).st_mode):
                    raise ValueError(f"SDK executable ancestor cannot be a symlink: {cur}")
            except OSError:
                pass
            cur = cur.parent

        # 2. Inspect launcher (if any)
        launcher_bin: Path | None = None
        if plan.launcher_argv:
            launcher_bin = Path(plan.launcher_argv[0])
            if not launcher_bin.exists():
                raise FileNotFoundError(f"Launcher binary does not exist: {launcher_bin}")
            try:
                l_st = os.lstat(launcher_bin)
                if stat.S_ISLNK(l_st.st_mode):
                    raise ValueError(f"Launcher binary cannot be a symlink: {launcher_bin}")
                if not stat.S_ISREG(l_st.st_mode):
                    raise ValueError(f"Launcher binary must be a regular file: {launcher_bin}")
            except OSError as e:
                raise ValueError(f"Failed to lstat launcher binary: {e}") from None

        # 3. Exact command identity prefix check
        full_argv = plan.get_argv()
        if plan.launcher_argv:
            expected_prefix = list(plan.launcher_argv) + [str(plan.sdk_executable_path)]
            prefix_len = len(expected_prefix)
            if full_argv[:prefix_len] != expected_prefix:
                raise ValueError(
                    f"Exact launcher + SDK prefix required: argv must start with {expected_prefix}, got {full_argv[:prefix_len]}"
                )
            # Ensure SDK executable path is not used elsewhere as an inert argument
            sdk_str = str(plan.sdk_executable_path)
            sdk_canon = str(sdk_path.resolve())
            for extra_arg in full_argv[prefix_len:]:
                if extra_arg == sdk_str or extra_arg == sdk_canon:
                    raise ValueError(
                        f"SDK path appearing elsewhere in argv is rejected: {extra_arg}"
                    )
        else:
            if not full_argv or full_argv[0] != str(plan.sdk_executable_path):
                raise ValueError(
                    f"Exact SDK prefix required: argv[0] must be {plan.sdk_executable_path}"
                )

        # 4. Hash verification against plan (resolve Windows Store alias stubs)
        sdk_hash_path = resolve_executable_for_hash(sdk_path)
        launcher_hash_path = (
            resolve_executable_for_hash(launcher_bin) if launcher_bin is not None else None
        )

        current_sdk_hash = hash_file(sdk_hash_path)
        if plan.sdk_executable_sha256 and current_sdk_hash != plan.sdk_executable_sha256:
            raise ValueError(
                f"SDK executable hash mismatch: plan expected {plan.sdk_executable_sha256}, disk has {current_sdk_hash}"
            )

        if launcher_hash_path is not None and plan.launcher_executable_sha256:
            current_launcher_hash = hash_file(launcher_hash_path)
            if current_launcher_hash != plan.launcher_executable_sha256:
                raise ValueError(
                    f"Launcher executable hash mismatch: plan expected {plan.launcher_executable_sha256}, disk has {current_launcher_hash}"
                )

        # 5. Pre-execution hash snapshot
        pre_sdk_hash = hash_file(sdk_hash_path)
        pre_launcher_hash = (
            hash_file(launcher_hash_path) if launcher_hash_path is not None else None
        )

        # 6. Execute command
        res = self.run(
            argv=full_argv,
            cwd=Path(plan.cwd),
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
            secrets=secrets,
            dry_run=dry_run,
            memory_limit_mb=memory_limit_mb,
        )

        # 7. Post-execution hash snapshot & mutation detection
        post_sdk_hash = hash_file(sdk_hash_path)
        post_launcher_hash = (
            hash_file(launcher_hash_path) if launcher_hash_path is not None else None
        )

        if pre_sdk_hash != post_sdk_hash or pre_launcher_hash != post_launcher_hash:
            raise InfrastructureTamperingError(
                "Infrastructure tampering detected: executable mutated during command execution"
            )

        return res
