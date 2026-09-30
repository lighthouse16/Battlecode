"""Windows AppExecLink reparse point resolution for official subsystem hashing.

This module is intentionally narrow: it resolves only Windows Store
AppExecLink reparse points so that executable integrity hashes can be computed
without weakening the generic ``hash_file`` implementation or following arbitrary
symlinks/reparse points.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
FSCTL_GET_REPARSE_POINT = 0x000900A8


def resolve_appexeclink(path: Path | str) -> Path | None:
    """Resolve a Windows Store AppExecLink reparse point to its target.

    Returns ``None`` on non-Windows, if the file is not a reparse point, or if
    the reparse tag is not ``IO_REPARSE_TAG_APPEXECLINK``.

    The returned target path is the third UTF-16LE string in the AppExecLink
    reparse buffer (package family name, application ID, target executable,
    optional trailing flags).
    """
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CreateFileW = kernel32.CreateFileW
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    CreateFileW.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    DeviceIoControl = kernel32.DeviceIoControl
    DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    DeviceIoControl.restype = wintypes.BOOL

    file_path = Path(path)
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    handle = CreateFileW(
        str(file_path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        return None

    try:
        buf_size = 16384
        buf = ctypes.create_string_buffer(buf_size)
        bytes_returned = wintypes.DWORD(0)
        ok = DeviceIoControl(
            handle,
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buf,
            buf_size,
            ctypes.byref(bytes_returned),
            None,
        )
        if not ok:
            return None

        # ReparseDataBuffer header:
        #   DWORD ReparseTag
        #   WORD  ReparseDataLength
        #   WORD  Reserved
        # AppExecLink payload:
        #   DWORD Version
        #   UTF-16LE null-terminated strings
        raw = buf.raw
        tag = int.from_bytes(raw[:4], "little")
        if tag != IO_REPARSE_TAG_APPEXECLINK:
            return None

        version_offset = 8
        version = int.from_bytes(raw[version_offset : version_offset + 4], "little")
        if version != 3:
            # Future versions may change layout; be conservative.
            return None

        payload_start = version_offset + 4
        payload = raw[payload_start : bytes_returned.value]
        try:
            text = payload.decode("utf-16-le")
        except UnicodeDecodeError:
            return None

        parts = text.split("\0")
        # Expected: package family name, app ID, target executable, [flags...]
        if len(parts) < 3 or not parts[2]:
            return None
        return Path(parts[2])
    finally:
        CloseHandle(handle)


def resolve_executable_for_hash(path: Path | str) -> Path:
    """Return the path that should be used to hash ``path``.

    If ``path`` is a Windows Store AppExecLink (or a symlink to one), this
    returns the underlying target executable after verifying it exists and is
    a regular non-symlink file. Otherwise, returns ``path.resolve()``.

    The returned path is meant for hashing only; callers must continue to use
    the original ``path`` for command execution and identity checks.
    """
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        pass
    resolved = resolve_appexeclink(p)
    if resolved is None:
        return p

    target = Path(resolved)
    try:
        st = os.lstat(target)
    except OSError as e:
        raise ValueError(f"Failed to inspect AppExecLink target: {e}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ValueError(f"AppExecLink target cannot be a symlink: {target}")
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"AppExecLink target must be a regular file: {target}")
    return target
