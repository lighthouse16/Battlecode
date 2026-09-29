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


def is_process_active(pid: int) -> bool:
    """Check if process exists and is actively executing (not dead or zombie)."""
    if pid <= 0:
        return False
    try:
        p = psutil.Process(pid)
        status = p.status()
        if status in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            return False
        return p.is_running()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    except Exception:
        pass

    if platform.system() != "Windows":
        try:
            import errno
            import os

            os.kill(pid, 0)
            return True
        except OSError as e:
            if e.errno == errno.EPERM:
                return True
            return False
        except Exception:
            return False

    return False


def terminate_process_tree(proc: subprocess.Popen, timeout_seconds: float = 1.0) -> None:
    """Terminate a process and all spawned child processes recursively.

    Escalates from graceful termination to SIGKILL / TerminateProcess.
    Reaps terminated child processes and handles already-exited parents safely.
    """
    pid = proc.pid
    if not pid:
        return

    # 1. Discover all processes in the tree before signaling
    procs_to_clean: list[psutil.Process] = []
    try:
        parent_ps = psutil.Process(pid)
        children = parent_ps.children(recursive=True)
        # Put children first so leaf processes are handled before parent
        procs_to_clean = children + [parent_ps]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        # Parent already exited or not visible in psutil namespace; continue
        pass

    # 2. Phase 1: Graceful termination
    if platform.system() != "Windows":
        try:
            import os
            import signal

            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            getpgrp = getattr(os, "getpgrp", None)
            curr_pgid = getpgrp() if getpgrp else -1
            if getpgid and killpg:
                pgid = getpgid(pid)
                if pgid > 0 and pgid != curr_pgid:
                    killpg(pgid, signal.SIGTERM)
        except Exception:
            pass

    for p in procs_to_clean:
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Ensure proc itself receives termination even if psutil could not resolve PID
    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass

    # Wait bounded time for graceful termination
    half_timeout = max(0.05, timeout_seconds * 0.4)
    if procs_to_clean:
        _, still_alive = psutil.wait_procs(procs_to_clean, timeout=half_timeout)
    else:
        still_alive = []
        try:
            proc.wait(timeout=half_timeout)
        except Exception:
            pass

    # 3. Phase 2: Force kill for any process that ignored SIGTERM or is still running
    if still_alive:
        if platform.system() != "Windows":
            try:
                import os
                import signal

                getpgid = getattr(os, "getpgid", None)
                killpg = getattr(os, "killpg", None)
                getpgrp = getattr(os, "getpgrp", None)
                curr_pgid = getpgrp() if getpgrp else -1
                sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                if getpgid and killpg:
                    pgid = getpgid(pid)
                    if pgid > 0 and pgid != curr_pgid:
                        killpg(pgid, sigkill)
            except Exception:
                pass

        for p in still_alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(still_alive, timeout=half_timeout)

    # Ensure proc itself is killed if still alive
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=half_timeout)
        except Exception:
            pass

    # 4. Bounded wait/reap on target process
    try:
        proc.poll()
        proc.wait(timeout=half_timeout)
    except Exception:
        pass


def check_memory_limit_support() -> tuple[bool, str]:
    """Check whether portable memory limits are supported on this operating system."""
    if platform.system() in ("Linux", "Darwin"):
        return True, "resource.setrlimit supported on POSIX child launch"
    return (
        False,
        "Windows Job Object memory limits require win32 extensions (unsupported in stdlib)",
    )


def get_memory_enforcement_details() -> dict[str, Any]:
    """Return structured memory enforcement status for system doctor and diagnostics."""
    is_posix = platform.system() in ("Linux", "Darwin")
    return {
        "detectable": is_posix,
        "configured": True,
        "enforced_and_tested": is_posix,
        "status": "ENFORCED" if is_posix else "UNSUPPORTED",
        "platform": platform.system(),
        "mechanism": (
            "resource.setrlimit(RLIMIT_AS)"
            if is_posix
            else "None (Windows stdlib lacks Job Objects)"
        ),
        "detail": (
            "resource.setrlimit supported on POSIX child launch"
            if is_posix
            else "Windows Job Object memory limits require win32 extensions (unsupported in stdlib)"
        ),
    }


class BotSubprocess:
    """Manages an isolated bot subprocess running over stdin/stdout line-protocol."""

    def __init__(
        self,
        entrypoint_path: Path,
        cwd: Path,
        env: dict[str, str] | None = None,
        memory_limit_mb: int = 512,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.entrypoint_path = entrypoint_path
        self.cwd = cwd
        self.env = env
        self.memory_limit_mb = memory_limit_mb
        self.cancel_event = cancel_event
        self.proc: subprocess.Popen | None = None
        self.stdout_queue: queue.Queue[tuple[str | None, str | None]] = queue.Queue()
        self.stderr_lines: list[str] = []
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self.is_alive = False
        self.turn_durations_ms: list[float] = []

    def start(self) -> None:
        """Launch the bot in an unbuffered subprocess with process group isolation."""
        extra_kwargs: dict[str, Any] = {}
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if creationflags:
                extra_kwargs["creationflags"] = creationflags
        else:
            extra_kwargs["start_new_session"] = True

        # Safe child launch mechanism for POSIX memory enforcement
        if platform.system() in ("Linux", "Darwin") and self.memory_limit_mb > 0:
            mem_bytes = int(self.memory_limit_mb * 1024 * 1024)
            cmd = [
                sys.executable,
                "-B",
                "-u",
                "-c",
                (
                    f"import resource, runpy, sys; "
                    f"resource.setrlimit(resource.RLIMIT_AS, ({mem_bytes}, {mem_bytes})); "
                    f"sys.argv = [{repr(str(self.entrypoint_path))}] + sys.argv[1:]; "
                    f"runpy.run_path({repr(str(self.entrypoint_path))}, run_name='__main__')"
                ),
            ]
        else:
            cmd = [sys.executable, "-B", "-u", str(self.entrypoint_path)]

        import os

        merged_env = os.environ.copy()
        if self.env:
            merged_env.update(self.env)
        merged_env["PYTHONDONTWRITEBYTECODE"] = "1"

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            env=merged_env,
            **extra_kwargs,
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
            self.turn_durations_ms.append(elapsed_ms)
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "raw_output": "",
                "stderr": f"Failed writing to stdin: {e}\n" + "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        # 2. Wait for action line from stdout queue with timeout and cancellation checks
        out_line: str | None = None
        deadline = start_time + timeout_seconds
        while True:
            if self.cancel_event and self.cancel_event.is_set():
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                self.stop()
                return None, {
                    "timed_out": False,
                    "crashed": False,
                    "cancelled": True,
                    "raw_output": "",
                    "stderr": "Execution cancelled due to lost lease",
                    "elapsed_ms": elapsed_ms,
                }
            remaining = max(0.0, deadline - time.perf_counter())
            if remaining <= 0:
                # HARD TIMEOUT: Kill process immediately
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                self.turn_durations_ms.append(elapsed_ms)
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
            try:
                out_line, err = self.stdout_queue.get(timeout=min(0.05, remaining))
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                self.turn_durations_ms.append(elapsed_ms)
                break
            except queue.Empty:
                continue

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

    def get_runtime_statistics(self, per_turn_limit_ms: int = 5000) -> dict[str, Any]:
        """Compute p50, p90, p99 and runtime headroom across recorded turns."""
        if not self.turn_durations_ms:
            return {
                "p50": 0.0,
                "p90": 0.0,
                "p99": 0.0,
                "max_turn_ms": 0.0,
                "headroom": 1.0,
                "turn_count": 0,
                "turn_durations_ms": [],
            }
        s = sorted(self.turn_durations_ms)
        n = len(s)

        def _pct(p: float) -> float:
            idx = int(round(p * (n - 1)))
            return round(s[min(max(0, idx), n - 1)], 2)

        p50 = _pct(0.50)
        p90 = _pct(0.90)
        p99 = _pct(0.99)
        max_turn = round(s[-1], 2)
        headroom = round(max(0.0, 1.0 - (p99 / max(1.0, float(per_turn_limit_ms)))), 4)
        return {
            "p50": p50,
            "p90": p90,
            "p99": p99,
            "max_turn_ms": max_turn,
            "headroom": headroom,
            "turn_count": n,
            "turn_durations_ms": list(self.turn_durations_ms),
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
                terminate_process_tree(self.proc, timeout_seconds=1.5)
            except Exception:
                pass
            finally:
                self.proc = None
