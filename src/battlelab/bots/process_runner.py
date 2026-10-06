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


class BotStartupError(RuntimeError):
    """Raised when an isolated bot subprocess fails to start or establish containment."""


def check_memory_limit_support() -> tuple[bool, str]:
    """Check whether portable memory limits are supported on this operating system."""
    if platform.system() in ("Linux", "Darwin"):
        return True, "resource.setrlimit supported on POSIX child launch"
    return (
        False,
        "Windows Job Object memory limits are not implemented/configured in Battlelab",
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
            else "None (Job Object memory limits are not implemented/configured in Battlelab)"
        ),
        "detail": (
            "resource.setrlimit supported on POSIX child launch"
            if is_posix
            else "Job Object memory limits are not implemented/configured in Battlelab"
        ),
    }


MAX_PROTOCOL_LINE_BYTES: int = 64 * 1024
MAX_STDERR_BYTES: int = 64 * 1024
MAX_STDERR_LINES: int = 500
MAX_STDOUT_QUEUE_CAPACITY: int = 16

SAFE_ENV_ALLOWLIST = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
}


MOCK_PYTHON_CODE_BOUNDARY: str = "IMMUTABLE_ARTIFACT + PYTHON_STDLIB"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    TH32CS_SNAPTHREAD = 0x00000004
    THREAD_SUSPEND_RESUME = 0x0002
    CREATE_SUSPENDED = 0x00000004

    # 64-bit safe ctypes function prototypes
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32First.restype = wintypes.BOOL

    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32Next.restype = wintypes.BOOL

    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE

    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class WindowsJobObject:
        """Manages a Windows Job Object configured with KILL_ON_JOB_CLOSE."""

        def __init__(self) -> None:
            self.handle: int | None = kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                raise OSError(f"CreateJobObjectW failed: {ctypes.get_last_error()}")
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            res = kernel32.SetInformationJobObject(
                self.handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not res:
                err = ctypes.get_last_error()
                self.close()
                raise OSError(f"SetInformationJobObject failed: {err}")

        def assign_process(self, process_handle: int) -> bool:
            if not self.handle or not process_handle:
                return False
            res = kernel32.AssignProcessToJobObject(self.handle, process_handle)
            return bool(res)

        def terminate(self, exit_code: int = 1) -> bool:
            if not self.handle:
                return False
            res = kernel32.TerminateJobObject(self.handle, exit_code)
            return bool(res)

        def close(self) -> None:
            if self.handle:
                kernel32.CloseHandle(self.handle)
                self.handle = None

        def __del__(self) -> None:
            self.close()

    def resume_process_threads(pid: int) -> bool:
        """Resume all suspended threads belonging to process pid."""
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        invalid_handle = wintypes.HANDLE(-1).value
        if not snap or snap == invalid_handle:
            return False
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        resumed = False
        all_resumed = True
        try:
            if kernel32.Thread32First(snap, ctypes.byref(entry)):
                while True:
                    if entry.th32OwnerProcessID == pid:
                        h_th = kernel32.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                        if h_th:
                            try:
                                res = kernel32.ResumeThread(h_th)
                                if res == 0xFFFFFFFF:
                                    all_resumed = False
                                else:
                                    resumed = True
                            finally:
                                kernel32.CloseHandle(h_th)
                        else:
                            all_resumed = False
                    if not kernel32.Thread32Next(snap, ctypes.byref(entry)):
                        break
        finally:
            kernel32.CloseHandle(snap)
        return resumed and all_resumed


def get_containment_capabilities() -> dict[str, dict[str, Any]]:
    """Return structured containment capability status across all dimensions."""
    is_posix = platform.system() in ("Linux", "Darwin")
    is_win = platform.system() == "Windows"
    return {
        "process_isolation": {
            "status": "ENFORCED_AND_TESTED",
            "mechanism": "OS subprocess with stripped SAFE_ENV_ALLOWLIST and stdin/stdout protocol",
            "detail": "Coordinator secrets and parent PYTHONPATH stripped; bot code runs out-of-process in isolated Python (-I -S)",
            "tested": True,
        },
        "process_tree_cleanup": {
            "status": "ENFORCED_AND_TESTED",
            "mechanism": (
                "Windows Job Object (TerminateJobObject + KILL_ON_JOB_CLOSE)"
                if is_win
                else "psutil recursive descendant kill with CREATE_NEW_PROCESS_GROUP / setsid"
            ),
            "detail": "Descendants are contained within kernel Job Object on Windows; rapid child-spawning races eliminated via CREATE_SUSPENDED launch",
            "tested": True,
        },
        "memory_enforcement": {
            "status": "ENFORCED_AND_TESTED" if is_posix else "UNSUPPORTED",
            "mechanism": (
                "resource.setrlimit(RLIMIT_AS)"
                if is_posix
                else "None (Job Object memory limits are not implemented/configured in Battlelab)"
            ),
            "detail": (
                "Supported on Linux/macOS child launch; Job Object memory limits are not implemented/configured in Battlelab on Windows"
                if not is_posix
                else "resource.setrlimit supported on POSIX child launch"
            ),
            "tested": is_posix,
        },
        "filesystem_isolation": {
            "status": "BEST_EFFORT",
            "mechanism": "CWD pinned to immutable artifact snapshot directory; post-match tampering detection",
            "detail": "Tamper detection only (not OS filesystem access prevention); TOCTOU limitation: process under same OS user could transiently modify and restore state before post-match check",
            "tested": True,
        },
        "network_isolation": {
            "status": "NOT_IMPLEMENTED",
            "mechanism": "None (No OS-level network namespace without root/cgroups)",
            "detail": "Outbound socket capability is not blocked in local mock environment; credentials stripped from environment",
            "tested": True,
        },
        "worker_death_child_containment": {
            "status": "ENFORCED_AND_TESTED" if is_win else "PLATFORM_DEPENDENT",
            "mechanism": (
                "Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and CREATE_SUSPENDED launch"
                if is_win
                else "Process group termination / session cleanup (hard parent-death descendant containment not guaranteed without cgroups/systemd)"
            ),
            "detail": (
                "If worker process is abruptly hard-killed (TerminateProcess), child bot processes and all descendants are automatically terminated by Windows kernel Job Object cleanup"
                if is_win
                else "On POSIX, child processes inherit process group; hard parent death (SIGKILL) may leave orphaned descendants unless wrapped in container or systemd cgroup"
            ),
            "tested": is_win,
        },
    }


def get_minimal_bot_environment(
    custom_env: dict[str, str] | None = None,
    artifact_dir: Path | None = None,
) -> dict[str, str]:
    """Construct safe minimal environment allowlist, stripping coordinator secrets and parent PYTHONPATH."""
    import os

    safe_env: dict[str, str] = {}
    for key in SAFE_ENV_ALLOWLIST:
        if key in os.environ:
            safe_env[key] = os.environ[key]

    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    safe_env["PYTHONUNBUFFERED"] = "1"
    safe_env["PYTHONIOENCODING"] = "utf-8"
    safe_env["PYTHONUTF8"] = "1"

    if custom_env:
        # Strip any parent-leaked Python or coordinator secrets from custom_env as well
        for k, v in custom_env.items():
            if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
                safe_env[k] = v

    return safe_env


class BotSubprocess:
    """Manages an isolated bot subprocess running over stdin/stdout line-protocol."""

    def __init__(
        self,
        entrypoint_path: Path,
        cwd: Path,
        env: dict[str, str] | None = None,
        memory_limit_mb: int = 512,
        cancel_event: threading.Event | None = None,
        max_line_bytes: int = MAX_PROTOCOL_LINE_BYTES,
        max_stderr_bytes: int = MAX_STDERR_BYTES,
        max_stderr_lines: int = MAX_STDERR_LINES,
        max_stdout_queue_capacity: int = MAX_STDOUT_QUEUE_CAPACITY,
    ) -> None:
        self.entrypoint_path = entrypoint_path
        self.cwd = cwd
        self.env = env
        self.memory_limit_mb = memory_limit_mb
        self.cancel_event = cancel_event
        self.max_line_bytes = max_line_bytes
        self.max_stderr_bytes = max_stderr_bytes
        self.max_stderr_lines = max_stderr_lines
        self.max_stdout_queue_capacity = max_stdout_queue_capacity
        self.current_request_id: int = 0
        self.proc: subprocess.Popen | None = None
        self.stdout_queue: queue.Queue[tuple[str | None, str | None]] = queue.Queue(
            maxsize=self.max_stdout_queue_capacity
        )
        self.max_observed_queue_size: int = 0
        self.stderr_lines: list[str] = []
        self.stderr_bytes_captured: int = 0
        self.protocol_violation: bool = False
        self.protocol_violation_reason: str = ""
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._job_object: Any | None = None
        self.is_alive = False
        self.turn_durations_ms: list[float] = []

    def _abort_uncontained_child(self, reason: str) -> None:
        """Immediately terminate an uncontained or partially started child process."""
        self.is_alive = False
        if self.proc is not None:
            proc_handle = getattr(self.proc, "_handle", None)
            if proc_handle and sys.platform == "win32":
                try:
                    kernel32.TerminateProcess(int(proc_handle), 1)
                except Exception:
                    pass
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                self.proc.poll()
            except Exception:
                pass
            self.proc = None
        if self._job_object is not None:
            try:
                self._job_object.terminate(1)
                self._job_object.close()
            except Exception:
                pass
            self._job_object = None

    def start(self) -> None:
        """Launch the bot in an unbuffered subprocess with isolated python and process containment."""
        extra_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            try:
                job_obj: WindowsJobObject | None = WindowsJobObject()
            except Exception as e:
                raise BotStartupError(f"Windows Job Object creation failed: {e}") from e

            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) | CREATE_SUSPENDED
            )
            extra_kwargs["creationflags"] = creationflags
        else:
            job_obj = None
            extra_kwargs["start_new_session"] = True

        # Construct isolated python launch command
        # MOCK_PYTHON_CODE_BOUNDARY = IMMUTABLE_ARTIFACT + PYTHON_STDLIB
        # -I: isolated mode (ignores PYTHONPATH, PYTHONHOME, user site-packages)
        # -S: don't imply 'import site' on initialization (disables site-packages, editable .pth files)
        # -B: don't write .pyc files
        # -u: unbuffered binary stdout and stderr
        artifact_root = str(self.cwd.resolve())
        entrypoint_str = str(self.entrypoint_path.resolve())

        bootstrap_lines = [
            "import os, runpy, sys",
            "if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')",
            "if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')",
        ]
        if platform.system() in ("Linux", "Darwin") and self.memory_limit_mb > 0:
            mem_bytes = int(self.memory_limit_mb * 1024 * 1024)
            bootstrap_lines.append(
                f"import resource; resource.setrlimit(resource.RLIMIT_AS, ({mem_bytes}, {mem_bytes}))"
            )

        bootstrap_lines.extend(
            [
                f"_art = os.path.abspath({repr(artifact_root)})",
                "_cwd = os.path.abspath('.')",
                "_safe_stdlib = []",
                "for _p in list(sys.path):",
                "    if not _p or not isinstance(_p, str): continue",
                "    _abs = os.path.abspath(_p)",
                "    _low = _abs.lower()",
                "    if 'site-packages' in _low or 'dist-packages' in _low: continue",
                "    if _abs == _cwd or _abs == _art: continue",
                "    if _abs not in _safe_stdlib: _safe_stdlib.append(_abs)",
                "sys.path = [_art, *_safe_stdlib]",
                f"sys.argv = [{repr(entrypoint_str)}] + sys.argv[1:]",
                f"runpy.run_path({repr(entrypoint_str)}, run_name='__main__')",
            ]
        )
        bootstrap = "\n".join(bootstrap_lines)
        cmd = [sys.executable, "-I", "-S", "-B", "-u", "-c", bootstrap]

        merged_env = get_minimal_bot_environment(self.env, artifact_dir=self.cwd)

        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=merged_env,
                **extra_kwargs,
            )
        except Exception as e:
            if sys.platform == "win32" and job_obj is not None:
                job_obj.close()
            raise BotStartupError(f"Failed to spawn bot process: {e}") from e

        if sys.platform == "win32":
            assert job_obj is not None
            self._job_object = job_obj
            proc_handle = getattr(self.proc, "_handle", None)
            if not proc_handle:
                self._abort_uncontained_child("Missing process handle")
                raise BotStartupError("Bot process spawned without valid OS process handle")

            if not job_obj.assign_process(int(proc_handle)):
                err = ctypes.get_last_error()
                self._abort_uncontained_child(f"AssignProcessToJobObject failed (error {err})")
                raise BotStartupError(f"Failed to assign bot process to Windows Job Object: {err}")

            child_pid = self.proc.pid
            if not resume_process_threads(child_pid):
                self._abort_uncontained_child("Failed to resume suspended bot process threads")
                raise BotStartupError(f"Failed to resume suspended threads for bot PID {child_pid}")

        self.is_alive = True

        # Background thread reading stdout into bounded queue
        def _read_stdout():
            try:
                assert self.proc is not None and self.proc.stdout is not None
                while True:
                    raw_chunk = self.proc.stdout.readline(self.max_line_bytes + 1)
                    if not raw_chunk:
                        break
                    if len(raw_chunk) > self.max_line_bytes or (
                        len(raw_chunk) == self.max_line_bytes and not raw_chunk.endswith(b"\n")
                    ):
                        self.protocol_violation = True
                        self.protocol_violation_reason = f"Protocol message exceeded maximum allowed bytes limit of {self.max_line_bytes}"
                        try:
                            if self.proc:
                                self.proc.terminate()
                        except Exception:
                            pass
                        try:
                            self.stdout_queue.put_nowait((None, self.protocol_violation_reason))
                        except (queue.Full, Exception):
                            pass
                        break

                    try:
                        text_line = raw_chunk.decode("utf-8")
                    except UnicodeDecodeError as e:
                        self.protocol_violation = True
                        self.protocol_violation_reason = (
                            f"Malformed UTF-8 byte sequence in protocol message: {e}"
                        )
                        try:
                            if self.proc:
                                self.proc.terminate()
                        except Exception:
                            pass
                        try:
                            self.stdout_queue.put_nowait((None, self.protocol_violation_reason))
                        except (queue.Full, Exception):
                            pass
                        break

                    stripped = text_line.strip()
                    try:
                        self.stdout_queue.put_nowait((stripped, None))
                        self.max_observed_queue_size = max(
                            self.max_observed_queue_size, self.stdout_queue.qsize()
                        )
                    except queue.Full:
                        self.max_observed_queue_size = self.max_stdout_queue_capacity
                        self.protocol_violation = True
                        self.protocol_violation_reason = f"Stdout queue capacity of {self.max_stdout_queue_capacity} exceeded (unsolicited message spam)"
                        # Explicitly terminate bot process immediately on queue overflow
                        try:
                            if self.proc:
                                self.proc.terminate()
                        except Exception:
                            pass
                        break
            except Exception as e:
                self.protocol_violation = True
                self.protocol_violation_reason = f"Stdout read error: {e}"
            finally:
                try:
                    self.stdout_queue.put_nowait((None, "EOF"))
                except (queue.Full, Exception):
                    pass

        # Background thread capturing stderr with bounded 4KB chunk reads and byte limits
        def _read_stderr():
            line_buf = bytearray()
            try:
                assert self.proc is not None and self.proc.stderr is not None
                while True:
                    chunk = self.proc.stderr.read(4096)
                    if not chunk:
                        if line_buf and len(self.stderr_lines) < self.max_stderr_lines:
                            err_text = line_buf.decode("utf-8", errors="replace").rstrip()
                            self.stderr_lines.append(err_text)
                        break

                    if (self.stderr_bytes_captured + len(chunk)) > self.max_stderr_bytes:
                        allowed = max(0, self.max_stderr_bytes - self.stderr_bytes_captured)
                        if allowed > 0:
                            line_buf.extend(chunk[:allowed])
                            self.stderr_bytes_captured += allowed
                        if line_buf and len(self.stderr_lines) < self.max_stderr_lines:
                            err_text = line_buf.decode("utf-8", errors="replace").rstrip()
                            self.stderr_lines.append(err_text)
                            line_buf.clear()
                        if not self.protocol_violation:
                            self.protocol_violation = True
                            self.protocol_violation_reason = f"Stderr exceeded limit of {self.max_stderr_lines} lines / {self.max_stderr_bytes} bytes"
                        if len(self.stderr_lines) < self.max_stderr_lines + 1:
                            self.stderr_lines.append(
                                "[STDERR TRUNCATED: Exceeded line/byte limits]"
                            )
                        try:
                            if self.proc:
                                self.proc.terminate()
                        except Exception:
                            pass
                        # Drain remaining stderr to avoid pipe deadlock without accumulating memory
                        try:
                            while self.proc.stderr.read(4096):
                                pass
                        except Exception:
                            pass
                        break

                    self.stderr_bytes_captured += len(chunk)
                    line_buf.extend(chunk)
                    while b"\n" in line_buf:
                        pos = line_buf.index(b"\n")
                        line_bytes = bytes(line_buf[:pos])
                        del line_buf[: pos + 1]
                        if len(self.stderr_lines) >= self.max_stderr_lines:
                            if not self.protocol_violation:
                                self.protocol_violation = True
                                self.protocol_violation_reason = f"Stderr exceeded limit of {self.max_stderr_lines} lines / {self.max_stderr_bytes} bytes"
                            if len(self.stderr_lines) < self.max_stderr_lines + 1:
                                self.stderr_lines.append(
                                    "[STDERR TRUNCATED: Exceeded line/byte limits]"
                                )
                            try:
                                if self.proc:
                                    self.proc.terminate()
                            except Exception:
                                pass
                            try:
                                while self.proc.stderr.read(4096):
                                    pass
                            except Exception:
                                pass
                            break
                        self.stderr_lines.append(
                            line_bytes.decode("utf-8", errors="replace").rstrip()
                        )
                    if self.protocol_violation:
                        break
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
            status_dict contains: timed_out, crashed, malformed, protocol_violation, raw_output, stderr, elapsed_ms
        """
        start_time = time.perf_counter()

        # Check for prior protocol violation from background readers
        if self.protocol_violation:
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": self.protocol_violation_reason,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": 0.0,
            }

        if not self.is_alive or not self.proc or self.proc.poll() is not None:
            if self.protocol_violation:
                self.stop()
                return None, {
                    "timed_out": False,
                    "crashed": False,
                    "malformed": True,
                    "protocol_violation": True,
                    "raw_output": self.protocol_violation_reason,
                    "reason": self.protocol_violation_reason,
                    "stderr": "\n".join(self.stderr_lines),
                    "elapsed_ms": 0.0,
                }
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "protocol_violation": False,
                "raw_output": "",
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": 0.0,
            }

        # Check for unsolicited / pre-answered stdout before sending observation
        if not self.stdout_queue.empty():
            unsolicited, _ = self.stdout_queue.get_nowait()
            self.protocol_violation = True
            self.protocol_violation_reason = (
                f"Unsolicited output received before observation: {unsolicited}"
            )
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": self.protocol_violation_reason,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": 0.0,
            }

        # Tag observation with request ID
        self.current_request_id += 1
        tagged_obs = dict(observation)
        tagged_obs["_battlelab_request_id"] = self.current_request_id

        # 1. Send observation to bot stdin
        try:
            line = json.dumps(tagged_obs) + "\n"
            assert self.proc.stdin is not None
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
        except Exception as e:
            self.is_alive = False
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            self.turn_durations_ms.append(elapsed_ms)
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "protocol_violation": False,
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
                    "malformed": False,
                    "protocol_violation": False,
                    "raw_output": "",
                    "stderr": "Execution cancelled due to lost lease",
                    "elapsed_ms": elapsed_ms,
                }

            if self.protocol_violation:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                self.stop()
                return None, {
                    "timed_out": False,
                    "crashed": False,
                    "malformed": True,
                    "protocol_violation": True,
                    "raw_output": self.protocol_violation_reason,
                    "reason": self.protocol_violation_reason,
                    "stderr": "\n".join(self.stderr_lines),
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
                    "protocol_violation": False,
                    "raw_output": "",
                    "stderr": f"Execution exceeded hard timeout of {timeout_seconds * 1000:.1f}ms\n"
                    + "\n".join(self.stderr_lines),
                    "elapsed_ms": elapsed_ms,
                }
            try:
                out_line, err = self.stdout_queue.get(timeout=min(0.05, remaining))
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                self.turn_durations_ms.append(elapsed_ms)
                if err:
                    if err != "EOF":
                        self.protocol_violation = True
                        self.protocol_violation_reason = err
                        self.stop()
                        return None, {
                            "timed_out": False,
                            "crashed": False,
                            "malformed": True,
                            "protocol_violation": True,
                            "raw_output": err,
                            "reason": self.protocol_violation_reason,
                            "stderr": "\n".join(self.stderr_lines),
                            "elapsed_ms": elapsed_ms,
                        }
                    else:
                        out_line = None
                break
            except queue.Empty:
                continue

        if out_line is None:
            # Process exited or EOF reached
            self.is_alive = False
            if self.protocol_violation:
                return None, {
                    "timed_out": False,
                    "crashed": False,
                    "malformed": True,
                    "protocol_violation": True,
                    "raw_output": self.protocol_violation_reason,
                    "reason": self.protocol_violation_reason,
                    "stderr": "\n".join(self.stderr_lines),
                    "elapsed_ms": elapsed_ms,
                }
            return None, {
                "timed_out": False,
                "crashed": True,
                "malformed": False,
                "protocol_violation": False,
                "raw_output": "",
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        # Check for multiple actions immediately queued in this turn
        if not self.stdout_queue.empty():
            extra_line, _ = self.stdout_queue.get_nowait()
            self.protocol_violation = True
            self.protocol_violation_reason = (
                f"Multiple actions emitted for single turn: {extra_line}"
            )
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": extra_line,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        # 3. Parse JSON action and correlate request ID
        try:
            action = json.loads(out_line)
        except json.JSONDecodeError as e:
            self.protocol_violation = True
            self.protocol_violation_reason = f"Malformed JSON in response: {e}"
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": out_line,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        if not isinstance(action, dict):
            self.protocol_violation = True
            self.protocol_violation_reason = (
                f"Expected JSON dictionary, got: {type(action).__name__}"
            )
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": out_line,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        if "_battlelab_request_id" not in action:
            self.protocol_violation = True
            self.protocol_violation_reason = (
                "Response missing required internal request ID '_battlelab_request_id'"
            )
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": out_line,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        resp_req_id = action.pop("_battlelab_request_id")
        if resp_req_id != self.current_request_id:
            self.protocol_violation = True
            self.protocol_violation_reason = f"Response request ID mismatch: expected {self.current_request_id}, got {resp_req_id}"
            self.stop()
            return None, {
                "timed_out": False,
                "crashed": False,
                "malformed": True,
                "protocol_violation": True,
                "raw_output": out_line,
                "reason": self.protocol_violation_reason,
                "stderr": "\n".join(self.stderr_lines),
                "elapsed_ms": elapsed_ms,
            }

        return action, {
            "timed_out": False,
            "crashed": False,
            "malformed": False,
            "protocol_violation": False,
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
        if self._job_object:
            try:
                self._job_object.terminate(1)
                self._job_object.close()
            except Exception:
                pass
            finally:
                self._job_object = None
        if self.proc:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    try:
                        self.proc.stdin.write(b'{"event": "SHUTDOWN"}\n')
                        self.proc.stdin.flush()
                        self.proc.stdin.close()
                    except Exception:
                        pass
                terminate_process_tree(self.proc, timeout_seconds=1.5)
            except Exception:
                pass
            finally:
                self.proc = None

    def __del__(self) -> None:
        job_obj = getattr(self, "_job_object", None)
        if job_obj is not None:
            try:
                job_obj.close()
            except Exception:
                pass
            self._job_object = None
