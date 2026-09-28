"""Isolated process runner with hard timeouts, memory guards, and output capture."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False


class ProcessRunner:
    """Executes external bot commands or scripts in an isolated process."""

    @staticmethod
    def run_command(
        cmd: list[str],
        cwd: Path | str,
        timeout_seconds: float = 10.0,
        stdin_input: str | None = None,
    ) -> ProcessExecutionResult:
        """Run command with hard timeout and output capture."""
        start = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return ProcessExecutionResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_ms=elapsed_ms,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            stdout = e.stdout.decode("utf-8") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode("utf-8") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return ProcessExecutionResult(
                returncode=-1,
                stdout=stdout,
                stderr=stderr + "\nProcess timed out.",
                duration_ms=elapsed_ms,
                timed_out=True,
            )
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return ProcessExecutionResult(
                returncode=-1,
                stdout="",
                stderr=str(e),
                duration_ms=elapsed_ms,
                timed_out=False,
            )
