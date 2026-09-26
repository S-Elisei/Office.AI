"""Find and kill processes the office started, by a mark the processes carry."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from office.config import load_config

_log = logging.getLogger("office.marks")

MARK_ENV = "OFFICE_MARK"

_IS_WINDOWS = os.name == "nt"

_VERDICT_SECONDS = 3.0

_CMDLINE_CHARS = 200

_STILL_ALIVE = "terminated and still running"
_UNREACHABLE = "could not be opened for termination"


@dataclass(frozen=True)
class Stray:
    pid: int
    mark: str
    cmdline: str

    def describe(self) -> str:
        line = self.cmdline or "(command line unreadable)"
        if len(line) > _CMDLINE_CHARS:
            line = line[:_CMDLINE_CHARS] + "..."
        return f"pid {self.pid} [{self.mark}] {line}"


@dataclass
class Sweep:
    killed: list[Stray] = field(default_factory=list)
    survived: list[Stray] = field(default_factory=list)
    unreachable: list[Stray] = field(default_factory=list)


# ---------------------------------------------------------------- the mark

_id_lock = threading.Lock()
_office_id: str | None = None


def office_id() -> str:
    """Eight hex characters standing for this office's data directory."""
    global _office_id
    with _id_lock:
        if _office_id is None:
            root = str(Path(load_config().root).resolve()).casefold()
            _office_id = hashlib.blake2s(root.encode("utf-8"), digest_size=4).hexdigest()
        return _office_id


def mark_for(agent: str, turn: str | None = None) -> str:
    """The value of MARK_ENV for something started on `agent`'s behalf."""
    return f"{office_id()}:{agent}:{turn or '-'}"


def run_mark(agent: str, handle: str) -> str:
    """The mark of one stage run: the mark of `agent`'s commands, then `<handle>:`.

    It is its own sweep scope, and agent_scope(agent) covers it.
    """
    return f"{mark_for(agent)}:{handle}:"


def preparation_mark(stage: str) -> str:
    """The mark of a stage's preparation, and its own sweep scope."""
    return f"{office_id()}:office:{stage}:"


def office_scope() -> str:
    """Everything this office has running, anywhere."""
    return f"{office_id()}:"


def agent_scope(agent: str) -> str:
    """Everything this office has running on one agent's behalf."""
    return f"{office_id()}:{agent}:"


# ---------------------------------------------------------------- reporting

_survivors_lock = threading.Lock()
_survivors: list[tuple[Stray, str, str]] = []

_survivor_hook = None


def set_survivor_hook(hook) -> None:
    global _survivor_hook
    _survivor_hook = hook


def survivor_warnings() -> list[str]:
    """One warning per trouble and owner still standing."""
    with _survivors_lock:
        remembered = list(_survivors)
    still_there = [entry for entry in remembered if _is_still(entry[0])]
    with _survivors_lock:
        _survivors[:] = still_there
    out = []
    for trouble in (_STILL_ALIVE, _UNREACHABLE):
        for who in dict.fromkeys(w for _s, t, w in still_there if t == trouble):
            group = [s for s, t, w in still_there if t == trouble and w == who]
            out.append(_trouble_text(trouble, group, who))
    return out


def _trouble_text(trouble: str, group: list[Stray], who: str) -> str:
    count = "a process" if len(group) == 1 else f"{len(group)} processes"
    they = "it is" if len(group) == 1 else "they are"
    them = "it" if len(group) == 1 else "them"
    if trouble == _UNREACHABLE:
        opening = (f"the office could not get hold of {count} started by {who} in order "
                   f"to stop {'it' if len(group) == 1 else 'them'}, and {they} still running")
    else:
        opening = (f"the office terminated {count} started by {who} and {they} "
                   "still running")
    return (opening + ": " + "; ".join(s.describe() for s in group)
            + f". The office has nothing further to try. Stop {them} by hand.")


def _report(scope: str, who: str, result: Sweep) -> None:
    if result.killed:
        _log.info(
            "stopped %d process(es) left running by %s: %s",
            len(result.killed), who, "; ".join(s.describe() for s in result.killed),
        )
    troubles = [(_STILL_ALIVE, result.survived), (_UNREACHABLE, result.unreachable)]
    for trouble, group in troubles:
        if not group:
            continue
        with _survivors_lock:
            known = {(s.pid, t) for s, t, _w in _survivors}
            _survivors.extend((s, trouble, who) for s in group
                              if (s.pid, trouble) not in known)
        text = _trouble_text(trouble, group, who)
        _log.error("%s (scope %s)", text, scope)
        hook = _survivor_hook
        if hook is None:
            continue
        try:
            hook(text)
        except Exception:
            _log.exception("could not put the surviving processes in the owner's journal")


# ---------------------------------------------------------------- the sweep


def sweep(scope: str, who: str) -> Sweep:
    """Kill every process whose mark starts with `scope`. Never raises."""
    result = Sweep()
    candidates: list[tuple[Stray, object]] = []
    try:
        candidates = _collect(scope)
        issued = [_terminate(ref) for _stray, ref in candidates]
        deadline = time.monotonic() + _VERDICT_SECONDS
        for (stray, ref), asked in zip(candidates, issued):
            if _confirm_gone(ref, deadline):
                result.killed.append(stray)
            elif asked:
                result.survived.append(stray)
            else:
                result.unreachable.append(stray)
        _report(scope, who, result)
    except Exception:
        _log.exception("the sweep over %s failed", scope)
    finally:
        for _stray, ref in candidates:
            _release(ref)
    return result


def _collect(scope: str) -> list[tuple[Stray, object]]:
    """Every live process carrying a mark in `scope`, paired with a reference to it."""
    if _IS_WINDOWS:
        return _collect_windows(scope)
    return _collect_posix(scope)


# ---------------------------------------------------------------- POSIX


def _collect_posix(scope: str) -> list[tuple[Stray, object]]:
    found: list[tuple[Stray, object]] = []
    mine = os.getpid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        mark = _posix_mark(pid)
        if not mark or not mark.startswith(scope):
            continue
        found.append((Stray(pid, mark, _posix_cmdline(pid)), pid))
    return found


def _posix_mark(pid: int) -> str | None:
    """The mark out of /proc/<pid>/environ, or None for anything unreadable."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for item in raw.split(b"\0"):
        name, sep, value = item.partition(b"=")
        if sep and name.decode("utf-8", "replace") == MARK_ENV:
            return value.decode("utf-8", "replace")
    return None


def _posix_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\0") if part)


def _posix_terminate(pid: int) -> bool:
    """True when the signal was delivered or the process was already gone."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return True


def _posix_gone(pid: int) -> bool:
    """True once the pid names nothing, or nothing but a corpse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    try:
        return stat[stat.rindex(")") + 2:].split()[0] == "Z"
    except (ValueError, IndexError):
        return False


# ---------------------------------------------------------------- Windows


@dataclass(frozen=True)
class _Layout:
    """Where the process parameters sit, for one bitness of process."""

    pointer: int
    params: int
    cmdline: int
    environment: int
    environment_size: int


_LAYOUT_64 = _Layout(pointer=8, params=0x20, cmdline=0x70,
                     environment=0x80, environment_size=0x3F0)
_LAYOUT_32 = _Layout(pointer=4, params=0x10, cmdline=0x40,
                     environment=0x48, environment_size=0x290)

if _IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=True)


    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_VM_READ = 0x0010
    _PROCESS_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0x00000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _PROCESS_BASIC_INFORMATION_CLASS = 0
    _PROCESS_WOW64_INFORMATION_CLASS = 26

    _ENVIRONMENT_MAX = 1 << 20

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", ctypes.c_void_p),
            ("PebBaseAddress", ctypes.c_void_p),
            ("Reserved2", ctypes.c_void_p * 2),
            ("UniqueProcessId", ctypes.c_void_p),
            ("Reserved3", ctypes.c_void_p),
        ]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.ReadProcessMemory.restype = wintypes.BOOL
    _k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtQueryInformationProcess.restype = wintypes.LONG
    _ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]


def _collect_windows(scope: str) -> list[tuple[Stray, object]]:
    found: list[tuple[Stray, object]] = []
    mine = os.getpid()
    for pid in _windows_pids():
        if pid in (0, 4) or pid == mine:
            continue
        reader = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
        if not reader:
            continue
        try:
            params = _peb_params(reader)
            if params is None:
                continue
            mark = (params["env"] or {}).get(MARK_ENV)
            if not mark or not mark.startswith(scope):
                continue
            killer = _k32.OpenProcess(_PROCESS_TERMINATE | _SYNCHRONIZE, False, pid)
            found.append((Stray(pid, mark, params["cmdline"] or ""), killer or None))
        finally:
            _k32.CloseHandle(reader)
    return found


def _windows_pids() -> list[int]:
    snapshot = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pids.append(int(entry.th32ProcessID))
            ok = _k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    return pids


def _peb_address(handle) -> tuple[int, _Layout] | None:
    """Which PEB to read for this process, and with which layout."""
    wow64 = ctypes.c_void_p()
    if _ntdll.NtQueryInformationProcess(handle, _PROCESS_WOW64_INFORMATION_CLASS,
                                        ctypes.byref(wow64),
                                        ctypes.sizeof(wow64), None) == 0 and wow64.value:
        return wow64.value, _LAYOUT_32
    info = _PROCESS_BASIC_INFORMATION()
    if _ntdll.NtQueryInformationProcess(handle, _PROCESS_BASIC_INFORMATION_CLASS,
                                        ctypes.byref(info),
                                        ctypes.sizeof(info), None) != 0:
        return None
    peb = ctypes.cast(info.PebBaseAddress, ctypes.c_void_p).value
    if not peb:
        return None
    return peb, _LAYOUT_64


def _peb_params(handle) -> dict | None:
    """Command line and environment of an open process."""
    located = _peb_address(handle)
    if located is None:
        return None
    peb, layout = located
    params = _read_word(handle, peb + layout.params, layout.pointer)
    if not params:
        return None
    return {
        "cmdline": _read_unicode_string(handle, params + layout.cmdline, layout),
        "env": _read_environment(handle, params, layout),
    }


def _read_memory(handle, address: int, size: int) -> bytes | None:
    buffer = (ctypes.c_char * size)()
    got = ctypes.c_size_t()
    ok = _k32.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size,
                                ctypes.byref(got))
    if not ok:
        return None
    return bytes(buffer[: got.value])


def _read_word(handle, address: int, size: int) -> int | None:
    raw = _read_memory(handle, address, size)
    return int.from_bytes(raw, "little") if raw else None


def _read_unicode_string(handle, address: int, layout: _Layout) -> str | None:
    """A UNICODE_STRING at `address`: two lengths, then a pointer to the text."""
    head = _read_memory(handle, address, layout.pointer * 2)
    if not head:
        return None
    length = int.from_bytes(head[0:2], "little")
    buffer = int.from_bytes(head[layout.pointer:], "little")
    if not length or not buffer:
        return ""
    raw = _read_memory(handle, buffer, length)
    return raw.decode("utf-16-le", "replace") if raw else None


def _read_environment(handle, params: int, layout: _Layout) -> dict[str, str] | None:
    address = _read_word(handle, params + layout.environment, layout.pointer)
    size = _read_word(handle, params + layout.environment_size, layout.pointer)
    if not address or not size or size > _ENVIRONMENT_MAX or size % 2:
        return None
    raw = _read_memory(handle, address, size)
    if not raw:
        return None
    env: dict[str, str] = {}
    for item in raw.decode("utf-16-le", "replace").split("\0"):
        if "=" not in item[1:]:
            continue
        name, _, value = item.partition("=")
        env[name.upper()] = value
    return env or None


# ---------------------------------------------------------------- kill, verdict


def _terminate(ref) -> bool:
    """Ask for the process's death. False when nothing could be asked at all."""
    if not _IS_WINDOWS:
        return _posix_terminate(ref)
    if ref is None:
        return False
    _k32.TerminateProcess(ref, 1)
    return True


def _confirm_gone(ref, deadline: float) -> bool:
    """Whether the process is actually off the machine, not merely asked to go."""
    if not _IS_WINDOWS:
        while True:
            if _posix_gone(ref):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if ref is None:
        return False
    remaining = max(0.0, deadline - time.monotonic())
    return _k32.WaitForSingleObject(ref, int(remaining * 1000)) == _WAIT_OBJECT_0


def _release(ref) -> None:
    """Let go of a reference."""
    if _IS_WINDOWS and ref is not None:
        _k32.CloseHandle(ref)


def _is_still(stray: Stray) -> bool:
    """Whether this exact process is still running under the mark it was read with."""
    if not _IS_WINDOWS:
        return _posix_mark(stray.pid) == stray.mark
    handle = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, stray.pid)
    if not handle:
        return False
    try:
        params = _peb_params(handle)
        return bool(params) and (params["env"] or {}).get(MARK_ENV) == stray.mark
    finally:
        _k32.CloseHandle(handle)
