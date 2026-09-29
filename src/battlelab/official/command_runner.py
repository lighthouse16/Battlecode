"""Secure official command runner with bounded output, isolation, and process-tree termination."""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from battlelab.bots.process_runner import terminate_process_tree
from battlelab.official.models import CommandResult

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


class OfficialCommandRunner:
    """Secure, isolated external command runner for official competition executables."""

    def __init__(self, allowed_env_vars: frozenset[str] = DEFAULT_ALLOWED_ENV_VARS) -> None:
        self.allowed_env_vars = allowed_env_vars

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
    ) -> CommandResult:
        """Execute external command with strict security and termination guarantees."""
        # 1. Input validation
        if not argv or not isinstance(argv, (list, tuple)):
            raise ValueError("argv must be a non-empty list of strings")

        for arg in argv:
            if not isinstance(arg, str):
                raise TypeError(f"argv elements must be strings, got {type(arg).__name__}")
            if "\0" in arg:
                raise ValueError("NUL character not permitted in command arguments")

        import math

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError(
                f"timeout_seconds must be a finite non-negative number, got {timeout_seconds!r}"
            )
        if (
            isinstance(stdout_limit_bytes, bool)
            or not isinstance(stdout_limit_bytes, int)
            or stdout_limit_bytes < 0
        ):
            raise ValueError(
                f"stdout_limit_bytes must be a non-negative integer, got {stdout_limit_bytes!r}"
            )
        if (
            isinstance(stderr_limit_bytes, bool)
            or not isinstance(stderr_limit_bytes, int)
            or stderr_limit_bytes < 0
        ):
            raise ValueError(
                f"stderr_limit_bytes must be a non-negative integer, got {stderr_limit_bytes!r}"
            )

        cwd_path = Path(cwd)
        if not cwd_path.exists() or not cwd_path.is_dir():
            raise ValueError(f"Working directory does not exist or is not a directory: {cwd_path}")

        # 2. Redacted argv representation
        redacted_argv = [self._redact(a, secrets) for a in argv]

        # 3. Dry run mode
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

        # 4. Filter environment variables
        filtered_env: dict[str, str] = {}
        for var_name in self.allowed_env_vars:
            if var_name in os.environ:
                filtered_env[var_name] = os.environ[var_name]
        filtered_env["PYTHONDONTWRITEBYTECODE"] = "1"

        if env is not None:
            if not isinstance(env, dict):
                raise TypeError("env must be a dictionary")
            for k, v in env.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise TypeError("env variable names and values must be strings")
                if "\0" in k or "\0" in v:
                    raise ValueError(
                        "NUL character not permitted in environment variable name or value"
                    )
                if k not in self.allowed_env_vars:
                    raise ValueError(f"Environment variable '{k}' is not permitted by allowlist")
                filtered_env[k] = v

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

        start_time = time.monotonic()
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except Exception as e:
            redacted_err = self._redact(str(e), secrets)
            raise RuntimeError(f"Failed to start command: {redacted_err}") from None

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
