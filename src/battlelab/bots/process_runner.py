"""Subprocess bot execution with hard timeouts, process isolation, and tree cleanup."""

from __future__ import annotations

import json
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil


def terminate_process_tree(proc: subprocess.Popen, timeout_seconds: float = 1.0) -> None:
    """Terminate a process and all of its spawned child processes recursively."""
    if proc.poll() is not None:
        return

    pid = proc.pid
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        # Kill children first
        for child in children:
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    try:
        proc.kill()
        proc.wait(timeout=timeout_seconds)
    except Exception:
        pass


def check_memory_limit_support() -> tuple[bool, str]:
    """Check whether portable memory limits are supported on this operating system."""
    if platform.system() in ("Linux", "Darwin"):
        return True, "resource.setrlimit supported"
    return (
        False,
        "Windows Job Object memory limits require win32 extensions (unsupported in stdlib)",
    )


class BotSubprocess:
    """Manages an isolated bot subprocess running over stdin/stdout line-protocol."""

    def __init__(self, entrypoint_path: Path, cwd: Path, env: dict[str, str] | None = None) -> None:
        self.entrypoint_path = entrypoint_path
        self.cwd = cwd
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.stdout_queue: queue.Queue[tuple[str | None, str | None]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self.is_alive = False

    def start(self) -> None:
        """Launch the bot in an unbuffered subprocess."""
        cmd = [sys.executable, "-u", str(self.entrypoint_path)]
        merged_env = None
        if self.env:
            import os

            merged_env = os.environ.copy()
            merged_env.update(self.env)
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            env=merged_env,
        )
        self.is_alive = True

        # Background thread reading stdout into queue
        def _read_stdout():
            try:
                assert self.proc is not None and self.proc.stdout is not None
                for line in iter(self.proc.stdout.readline, ""):
                    self.stdout_queue.put((line.strip(), None))
            except Exception as e:
                self.stdout_queue.put((None, str(e)))
            finally:
                self.stdout_queue.put((None, "EOF"))

        # Background thread capturing stderr
        def _read_stderr():
            try:
                assert self.proc is not None and self.proc.stderr is not None
                for line in iter(self.proc.stderr.readline, ""):
                    if line:
                        self.stderr_lines.append(line.rstrip())
            except Exception:
                pass

        self._stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def send_turn(
        self, observation: dict[str, Any], timeout_seconds: float = 5.0
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Send JSON observation to bot and wait for action within strict deadline.

        Returns:
            (action_dict, status_dict)
            status_dict contains: timed_out, crashed, malformed, raw_output, stderr, elapsed_ms
        """
        if not self.is_alive or not self.proc or self.proc.poll() is not None:
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "raw_output": "",
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": 0.0,
            }

        start_time = time.perf_counter()
        # 1. Send observation to bot stdin
        try:
            line = json.dumps(observation) + "\n"
            assert self.proc.stdin is not None
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except Exception as e:
            self.is_alive = False
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "raw_output": "",
                "stderr": f"Failed writing to stdin: {e}\n" + "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        # 2. Wait for action line from stdout queue with timeout
        try:
            out_line, err = self.stdout_queue.get(timeout=timeout_seconds)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        except queue.Empty:
            # HARD TIMEOUT: Kill process immediately
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.stop()
            return None, {
                "timed_out": True,
                "crashed": False,
                "malformed": False,
                "raw_output": "",
                "stderr": f"Execution exceeded hard timeout of {timeout_seconds * 1000:.1f}ms\n"
                + "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        if out_line is None:
            # Process exited or EOF reached
            self.is_alive = False
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "raw_output": "",
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        # 3. Parse JSON action
        try:
            action = json.loads(out_line)
            if not isinstance(action, dict):
                return None, {
                    "timed_out": False,
                    "crashed": False,
                    "malformed": True,
                    "raw_output": out_line,
                    "stderr": "\n".join(self.stderr_lines),
                    "elapsed_ms": elapsed_ms,
                }
            return action, {
                "timed_out": False,
                "crashed": False,
                "malformed": False,
                "raw_output": out_line,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }
        except json.JSONDecodeError:
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "raw_output": out_line,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

    def stop(self) -> None:
        """Safely terminate bot process tree and close pipes."""
        self.is_alive = False
        if self.proc:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    try:
                        self.proc.stdin.write('{"event": "SHUTDOWN"}\n')
                        self.proc.stdin.flush()
                        self.proc.stdin.close()
                    except Exception:
                        pass
                terminate_process_tree(self.proc)
            except Exception:
                pass
            finally:
                self.proc = None
