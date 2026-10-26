"""
Windows-native supervision backend for Aquarius Bot Manager.

Replaces tmux (process supervision), /proc (CPU/RSS monitoring), and
systemd-run/cgroups (resource limits) with Windows-native equivalents. Mirrors
the function contracts of the Linux implementation in manager.py so the rest
of the file (web UI, CLI, order/routing logic) needs no changes:

    start(inst) -> status string
    stop(inst) -> status string
    instance_status(inst) -> "stopped" | "crashed" | "running"
    instance_stats(inst) -> {"pid", "cpu_pct", "rss", "procs"} or None
    logs(inst, lines) -> str
    send_command(inst, command) -> "sent"  (raises ValueError if not running)
    rename(old_name, new_name) -> None
    adopt(inst, pid) -> bool

ARCHITECTURE -- why a detached helper process, not just a thread:
tmux is a long-lived daemon that outlives any single `manager.py` invocation,
so the Linux CLI's `start` in one process and a later `send`/`status`/`stop`
in a separate process both talk to the same tmux server. Python has no
equivalent daemon, and `python manager.py start ...` is a short-lived
process that exits almost immediately after launching the bot -- an
in-process reader thread would die with it before capturing any output, and
a later CLI invocation has no access to another process's pipe handles at
all on Windows.

So win_start() launches a small DETACHED HELPER (this same file, run as
`python platform_win.py _helper <name> <config_path>`) instead of launching
the bot directly. The helper: owns the actual bot Popen (stdout/stderr/stdin
pipes), runs the reader thread that appends raw bytes to <name>.log, and
listens on a localhost TCP port for control commands (LOGS/SEND/STOP) so
ANY later process -- the CLI or `serve` -- can reach the bot's live stdin
and recent output, not just whichever process happened to call win_start.
The helper's own PID and port are written to <name>.pid; that file is the
only state any other function in this module needs to read.

This gives the Windows path the same operational shape tmux gives Linux:
start from one process, manage from any other, including live console
input -- which a simpler "redirect stdout to a file" design could not do.

ONE REAL GAP VS. TMUX, STRUCTURAL, NOT A BUG:
Adopted instances (an already-running java.exe found via WMI, not launched
by us -- see the Adoption section below) have no helper, so no stdin pipe
and no log file capturing from process start -- only status/stats/stop work
for them, via the bare PID. send_command()/logs() raise/report this
explicitly rather than silently doing nothing.
"""

import collections
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import psutil

CREATE_NEW_PROCESS_GROUP = 0x00000200
CTRL_BREAK_EVENT = 1
DETACHED_PROCESS = 0x00000008
LOG_BUFFER_LINES = 2000
LOG_TAIL_READ_BYTES = 256 * 1024  # cap how much of the on-disk log file a read scans
CONTROL_TIMEOUT_SEC = 5

kernel32 = ctypes.windll.kernel32


def _helper_launch_args():
    """Argv prefix that runs this file's _helper mode as a subprocess.
    Unfrozen (plain `python manager.py ...`): sys.executable IS a real
    interpreter and __file__ IS this real .py file, so `[sys.executable,
    __file__]` works exactly as it always has.
    Frozen (PyInstaller): sys.executable is the frozen exe itself (not a
    real interpreter) and there's no standalone platform_win.py file to
    hand it -- the exe's own build bundles a minimal real Python runtime
    plus a copy of this script under a 'pyrt/' subdirectory next to the
    exe for exactly this purpose (see docs/windows-exe-build.md), and this
    returns a path into THAT bundle instead."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        bundled_python = os.path.join(base, "pyrt", "python.exe")
        bundled_script = os.path.join(base, "pyrt", "platform_win.py")
        if not os.path.exists(bundled_python) or not os.path.exists(bundled_script):
            raise RuntimeError(
                f"frozen build is missing its bundled helper runtime "
                f"(expected {bundled_python} and {bundled_script} -- "
                f"see docs/windows-exe-build.md's manager.exe build step)")
        return [bundled_python, bundled_script]
    return [sys.executable, os.path.abspath(__file__)]


# Where state files (<name>.pid, <name>.log) live, and the instances.json
# path itself -- both set once by manager.py's load_config() at startup,
# since platform_win.py has no config object of its own and win_start() needs
# to pass the config path to the detached helper process (see win_start).
# Falls back to a local "state" dir if never set, so this module still works
# if imported standalone (e.g. in a test).
_STATE_DIR = None
_CONFIG_PATH = None


def set_state_dir(path):
    global _STATE_DIR
    _STATE_DIR = path
    os.makedirs(path, exist_ok=True)


def set_config_path(path):
    global _CONFIG_PATH
    _CONFIG_PATH = path


def _state_dir():
    if _STATE_DIR is None:
        set_state_dir(os.path.join(os.getcwd(), ".abm-state"))
    return _STATE_DIR


def _pid_file(name):
    return os.path.join(_state_dir(), f"{name}.pid")


def _log_file(name):
    return os.path.join(_state_dir(), f"{name}.log")


def _lock_file(name):
    return os.path.join(_state_dir(), f"{name}.startlock")


LOCK_STALE_SEC = 30  # a lock older than this is assumed abandoned by a dead/crashed process


class _StartLock:
    """Serializes win_start()'s check-then-launch sequence for one instance
    name across processes. Without this, two 'start' calls close together
    (a manual CLI start racing the service's own autostart, say) can both
    pass the "not already running" check before either writes its pid file
    -- launching two independent helper+bot pairs, where the second one's
    pid-file write silently orphans the first (confirmed hitting this for
    real while testing: a helper+java pair left running with no pid file
    pointing at it anymore, unreachable and unstoppable through the manager).
    Implemented as an exclusive-create lock file -- plain and dependency-free,
    which is all this needs since the critical section is just a few
    milliseconds of file I/O, not something worth a real IPC mutex for."""

    def __init__(self, name):
        self.path = _lock_file(name)
        self._fd = None

    def __enter__(self):
        deadline = time.time() + 10
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(time.time()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > LOCK_STALE_SEC:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue  # someone else just removed it; retry the create
                if time.time() > deadline:
                    raise TimeoutError(f"timed out waiting for start lock on {self.path}")
                time.sleep(0.1)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        try:
            os.remove(self.path)
        except OSError:
            pass


def _write_pid_file(name, helper_pid, port, launch_cmd):
    with open(_pid_file(name), "w") as f:
        json.dump({"pid": helper_pid, "port": port, "launch_cmd": launch_cmd,
                   "started": time.time()}, f)


def _read_pid_file(name):
    try:
        with open(_pid_file(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _remove_pid_file(name):
    try:
        os.remove(_pid_file(name))
    except OSError:
        pass


def _is_alive(pidinfo):
    """A helper record is "alive" if its process exists AND it still answers
    on its control port -- catches the case where the helper process died
    but something else later reused the same PID."""
    if not pidinfo or not psutil.pid_exists(pidinfo.get("pid", -1)):
        return False
    return _control_request(pidinfo, "PING") == "PONG"


def _control_request(pidinfo, request, timeout=CONTROL_TIMEOUT_SEC):
    """Send one line to the helper's control port, return its one-line reply,
    or None on any failure (helper gone, port closed, timeout)."""
    if not pidinfo or not pidinfo.get("port"):
        return None
    try:
        with socket.create_connection(("127.0.0.1", pidinfo["port"]), timeout=timeout) as s:
            s.sendall((request + "\n").encode("utf-8"))
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None


def _merged_env(inst):
    env = os.environ.copy()
    extra = inst.get("env") or {}
    env.update({str(k): str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------------
# Public API: start / stop / status / logs / send_command
# ---------------------------------------------------------------------------

def win_start(inst):
    name = inst["name"]

    if inst.get("adopted_pid"):
        pid = inst["adopted_pid"]
        return "already running" if psutil.pid_exists(pid) else \
            "error: adopted process is no longer running (un-adopt it to launch fresh)"

    # Serializes the whole check-then-launch sequence across processes --
    # see _StartLock's docstring for the real orphaned-helper bug this
    # closes (two concurrent starts for the same name both passing the
    # "not already running" check before either's pid file existed).
    try:
        with _StartLock(name):
            return _win_start_locked(inst)
    except TimeoutError as e:
        return f"error: {e}"


def _win_start_locked(inst):
    name = inst["name"]

    existing = _read_pid_file(name)
    if _is_alive(existing):
        return "already running"

    d = inst["dir"]
    if not os.path.isdir(d):
        return f"error: dir not found: {d}"

    launch_cmd, note = _win_launch_command(inst)
    lim = inst.get("limits") or {}
    mem_bytes = None
    if lim.get("memory"):
        try:
            from manager import parse_mem_to_bytes
            mem_bytes = parse_mem_to_bytes(lim["memory"])
        except Exception:
            note += " (memory limit not applied: could not parse limit value)"

    if not _CONFIG_PATH:
        return "error: internal: config path not set (platform_win.set_config_path was never called)"
    try:
        helper_args = _helper_launch_args() + ["_helper", name, _CONFIG_PATH]
    except RuntimeError as e:
        return f"error: {e}"
    if mem_bytes:
        helper_args += ["--mem-limit", str(int(mem_bytes))]

    # DETACHED_PROCESS + its own process group: the helper must survive this
    # CLI process exiting, and not receive Ctrl+C/console-close signals meant
    # for whoever invoked `manager.py` interactively.
    helper = subprocess.Popen(
        helper_args,
        cwd=d,
        env=_merged_env(inst),
        creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )

    # Wait for the helper to report its control port via the pid file (it
    # writes this itself once its listener socket is bound).
    deadline = time.time() + 10
    port = None
    while time.time() < deadline:
        info = _read_pid_file(name)
        if info and info.get("pid") == helper.pid and info.get("port"):
            port = info["port"]
            break
        if helper.poll() is not None:
            return "error: helper process exited before starting (check logs)"
        time.sleep(0.1)
    if port is None:
        return "error: helper did not become ready in time"

    return "started" + note


def win_stop(inst):
    name = inst["name"]

    if inst.get("adopted_pid"):
        pid = inst["adopted_pid"]
        if psutil.pid_exists(pid):
            try:
                psutil.Process(pid).terminate()
            except psutil.Error:
                pass
        return "stopped"

    pidinfo = _read_pid_file(name)
    if not _is_alive(pidinfo):
        _remove_pid_file(name)
        return "not running"

    timeout = int(inst.get("stop_timeout", 15))
    reply = _control_request(pidinfo, f"STOP {timeout}", timeout=timeout + 5)
    _remove_pid_file(name)
    if reply is None:
        # helper didn't answer at all -- force-kill whatever's left (helper + bot)
        _kill_tree(pidinfo["pid"])
    return "stopped"


def win_instance_status(inst):
    name = inst["name"]

    if inst.get("adopted_pid"):
        pid = inst["adopted_pid"]
        return "running" if psutil.pid_exists(pid) else "stopped"

    pidinfo = _read_pid_file(name)
    if not pidinfo:
        return "stopped"
    reply = _control_request(pidinfo, "STATUS")
    if reply in ("RUNNING", "CRASHED", "STOPPED"):
        if reply != "RUNNING":
            _remove_pid_file(name)
        return reply.lower()
    # helper process itself is gone (killed externally, box rebooted, etc.)
    _remove_pid_file(name)
    return "crashed" if psutil.pid_exists(pidinfo.get("pid", -1)) else "stopped"


def win_logs(inst, lines=300):
    name = inst["name"]

    if inst.get("adopted_pid"):
        return ("(adopted process: no captured output -- Aquarius Bot Manager did not "
                "launch this process, so it has no stdout pipe to read from)")

    pidinfo = _read_pid_file(name)
    if _is_alive(pidinfo):
        reply = _control_request(pidinfo, f"LOGS {int(lines)}")
        if reply is not None:
            return reply

    # Helper unreachable/gone: fall back to the tail of the on-disk log file
    # (still useful right after a crash, before the pid file gets cleaned up).
    path = _log_file(name)
    if not os.path.exists(path):
        return "(not running)"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_READ_BYTES))
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-int(lines):])
    except OSError as e:
        return f"(error reading logs: {e})"


def win_send_command(inst, command):
    name = inst["name"]

    if inst.get("adopted_pid"):
        raise ValueError("adopted process: no input pipe available (Aquarius Bot Manager did not launch this process)")

    pidinfo = _read_pid_file(name)
    if not _is_alive(pidinfo):
        raise ValueError("instance is not running")
    command = (command or "").rstrip("\n")
    reply = _control_request(pidinfo, f"SEND {command}")
    if reply != "OK":
        raise ValueError(f"could not write to instance stdin: {reply or 'helper unreachable'}")
    return "sent"


def win_rename(old_name, new_name):
    pidinfo = _read_pid_file(old_name)
    if _is_alive(pidinfo):
        _control_request(pidinfo, f"RENAME {new_name}")
    for mover in (_pid_file, _log_file):
        old_path, new_path = mover(old_name), mover(new_name)
        if os.path.exists(old_path):
            try:
                os.replace(old_path, new_path)
            except OSError:
                pass


def live_session_count():
    """How many .pid files on disk point at a still-alive helper -- the
    durable, cross-process analog of len(tmux list-sessions)."""
    d = _state_dir()
    try:
        names = [f[:-4] for f in os.listdir(d) if f.endswith(".pid")]
    except OSError:
        return 0
    return sum(1 for name in names if _is_alive(_read_pid_file(name)))


def win_pid_for(inst):
    """Live PID of the actual bot process (not the helper) for stats/monitoring,
    or None. Works regardless of which process started it."""
    if inst.get("adopted_pid"):
        pid = inst["adopted_pid"]
        return pid if psutil.pid_exists(pid) else None

    pidinfo = _read_pid_file(inst["name"])
    if not _is_alive(pidinfo):
        return None
    reply = _control_request(pidinfo, "PID")
    try:
        return int(reply)
    except (TypeError, ValueError):
        return None


def _kill_tree(pid):
    try:
        root = psutil.Process(pid)
        for p in [root] + root.children(recursive=True):
            try:
                p.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


# ---------------------------------------------------------------------------
# Adoption: attach to an already-running, unmanaged process via WMI/psutil.
#
# Persisted as inst["adopted_pid"] in instances.json itself (see
# manager.py's adopt_session_win) -- the Windows analog of Linux's
# inst["session"] pin. No helper, no state file -- every function above
# checks inst.get("adopted_pid") first and goes straight to the bare PID.
# ---------------------------------------------------------------------------

def list_candidate_processes():
    """List java.exe (and similar) processes with their command line, for the
    adoption UI to match against an instance's launch_cmd/jar name."""
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            if "java" in name or "javaw" in name:
                out.append({
                    "pid": info["pid"],
                    "name": info.get("name"),
                    "cmdline": " ".join(info.get("cmdline") or []),
                    "cwd": info.get("cwd"),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def adopt(inst, pid):
    """Confirm pid is alive before manager.py persists it as inst['adopted_pid'].
    Returns True on success, False if the PID is no longer alive."""
    return psutil.pid_exists(pid)


# ---------------------------------------------------------------------------
# Resource limits: Windows Job Objects (memory cap only for v1 -- CPU
# throttling via JOBOBJECT_CPU_RATE_CONTROL_INFORMATION is a deferred
# fast-follow, same as the Linux side's cgroup-unavailable degrade path).
# Applied by the HELPER process (see _HelperServer._launch below), since
# AssignProcessToJobObject needs a handle to the bot process, which only
# the helper (its direct parent) has.
# ---------------------------------------------------------------------------

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200


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
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _apply_job_memory_limit(proc_handle, mem_bytes):
    """Create a Job Object capping total memory, assign proc_handle to it.
    Returns (job_handle_or_None, error_note_str)."""
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, " (memory limit not applied: CreateJobObjectW failed)"

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(mem_bytes)

        ok = kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(job)
            return None, " (memory limit not applied: SetInformationJobObject failed)"

        ok = kernel32.AssignProcessToJobObject(job, proc_handle)
        if not ok:
            kernel32.CloseHandle(job)
            return None, " (memory limit not applied: AssignProcessToJobObject failed)"

        return job, ""
    except Exception as e:
        return None, f" (memory limit not applied: {e})"


def job_objects_supported():
    """Windows Job Objects are available on every supported Windows version;
    this always returns True and exists only to mirror the Linux side's
    _supports_cgroup_limits() check/UI-note shape."""
    return True


def _win_launch_command(inst):
    """Shell string for Popen(shell=True). CPU limits aren't enforced yet on
    Windows (deferred fast-follow) -- note that explicitly, same shape as the
    Linux "(limits not enforced)" degrade path."""
    launch = inst["launch_cmd"]
    lim = {k: v for k, v in (inst.get("limits") or {}).items() if v}
    if lim.get("cpu"):
        return launch, " (CPU limit not enforced on Windows yet — memory limits only)"
    return launch, ""


# ---------------------------------------------------------------------------
# Host-level sysinfo (replaces /proc/meminfo, os.statvfs, os.getloadavg, etc.)
# ---------------------------------------------------------------------------

def win_sysinfo():
    info = {}
    try:
        vm = psutil.virtual_memory()
        info["mem_total"] = vm.total
        info["mem_available"] = vm.available
    except Exception:
        info["mem_total"] = info["mem_available"] = None
    try:
        du = shutil.disk_usage("C:\\")
        info["disk_total"] = du.total
        info["disk_free"] = du.free
    except Exception:
        info["disk_total"] = info["disk_free"] = None
    # No Windows equivalent of load average -- omit rather than approximate.
    info["load"] = None
    try:
        import platform as _platform
        info["os"] = _platform.platform()
    except Exception:
        info["os"] = None
    return info


# ---------------------------------------------------------------------------
# System actions: winget / shutdown (replaces apt-get / sudo reboot).
# Dropped on Windows: the "pip" action (installs python3-pip via apt --
# meaningless on Windows, where this script already requires a real Python).
# Gated by the same settings["system_actions_enabled"] flag as Linux.
#
# Consumed by manager.py's run_system_action_win(), which reuses the existing
# SYS_JOB-based output streaming ("update") and detached-Popen ("reboot") paths
# rather than duplicating that logic here.
# ---------------------------------------------------------------------------

SYSTEM_COMMANDS_WIN = {
    "update": ["winget", "upgrade", "--all", "--silent",
               "--accept-source-agreements", "--accept-package-agreements"],
    "reboot": ["shutdown", "/r", "/t", "5"],
}


# ---------------------------------------------------------------------------
# The detached helper process itself: `python platform_win.py _helper <name> <config_path>`
# ---------------------------------------------------------------------------

class _HelperServer:
    """Owns the bot's Popen, the reader thread, and a localhost TCP control
    socket. One instance per helper process (one helper per bot instance)."""

    def __init__(self, name, inst, mem_limit_bytes):
        self.name = name
        self.inst = inst
        self.mem_limit_bytes = mem_limit_bytes
        self.proc = None
        self.job_handle = None
        self.log = collections.deque(maxlen=LOG_BUFFER_LINES)
        self.log_lock = threading.Lock()
        self.log_fh = None
        self.expected_stop = False
        self.sock = None

    def launch(self):
        d = self.inst["dir"]
        launch_cmd, _note = _win_launch_command(self.inst)
        log_path = _log_file(self.name)
        try:
            open(log_path, "wb").close()  # fresh log for a fresh run, like a new tmux pane
        except OSError:
            pass
        self.proc = subprocess.Popen(
            launch_cmd,
            cwd=d,
            env=_merged_env(self.inst),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
        try:
            self.log_fh = open(log_path, "ab")
        except OSError:
            self.log_fh = None
        if self.mem_limit_bytes:
            self.job_handle, _note = _apply_job_memory_limit(int(self.proc._handle), self.mem_limit_bytes)
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def _reader_loop(self):
        try:
            for raw_line in self.proc.stdout:
                with self.log_lock:
                    self.log.append(raw_line)
                if self.log_fh:
                    try:
                        self.log_fh.write(raw_line)
                        self.log_fh.flush()
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            if self.log_fh:
                try:
                    self.log_fh.close()
                except OSError:
                    pass

    def status(self):
        rc = self.proc.poll()
        if rc is None:
            return "RUNNING"
        return "STOPPED" if self.expected_stop else "CRASHED"

    def logs(self, lines):
        with self.log_lock:
            buf = list(self.log)[-lines:]
        try:
            return b"".join(buf).decode("utf-8", errors="replace")
        except Exception:
            return "(error decoding log buffer)"

    def send(self, command):
        if self.proc.poll() is not None:
            return "ERR instance is not running"
        try:
            self.proc.stdin.write((command + "\r\n").encode())
            self.proc.stdin.flush()
            return "OK"
        except (OSError, ValueError) as e:
            return f"ERR {e}"

    def stop(self, timeout):
        # self.proc is the cmd.exe wrapper (see _win_launch_command/launch()
        # above) -- terminating just that one process orphans the real java
        # child instead of killing it, so every exit path here must kill the
        # whole tree (cmd.exe + java.exe + any of java's own children), via
        # _kill_tree, not self.proc.terminate()/.kill() alone.
        self.expected_stop = True
        if self.proc.poll() is not None:
            return
        try:
            kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, self.proc.pid)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return
            time.sleep(0.4)
        _kill_tree(self.proc.pid)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def close_job(self):
        if self.job_handle:
            try:
                kernel32.CloseHandle(self.job_handle)
            except Exception:
                pass
            self.job_handle = None


def _run_helper(name, config_path, mem_limit_bytes):
    """Entry point when this file is run as `python platform_win.py _helper ...`.
    Loads just enough of instances.json to find this one instance's launch
    config (not manager.py's full config machinery, to keep this process's
    own footprint tiny and independent of the rest of the app)."""
    with open(config_path) as f:
        data = json.load(f)
    inst = next((i for i in data.get("instances", []) if i["name"] == name), None)
    if inst is None:
        print(f"platform_win helper: no such instance '{name}' in {config_path}", file=sys.stderr)
        sys.exit(1)
    inst.setdefault("launch_cmd", "./launch.sh")

    set_state_dir(os.path.join(os.path.dirname(os.path.abspath(config_path)), ".abm-state"))

    srv = _HelperServer(name, inst, mem_limit_bytes)
    srv.launch()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    _write_pid_file(name, os.getpid(), port, inst["launch_cmd"])

    def handle_conn(conn):
        try:
            conn.settimeout(CONTROL_TIMEOUT_SEC)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            line = data.decode("utf-8", errors="replace").strip()
            if line == "PING":
                conn.sendall(b"PONG")
            elif line == "STATUS":
                conn.sendall(srv.status().encode())
            elif line == "PID":
                conn.sendall(str(srv.proc.pid).encode())
            elif line.startswith("LOGS"):
                parts = line.split(maxsplit=1)
                n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 300
                conn.sendall(srv.logs(n).encode("utf-8", errors="replace"))
            elif line.startswith("SEND "):
                conn.sendall(srv.send(line[5:]).encode())
            elif line.startswith("STOP"):
                parts = line.split(maxsplit=1)
                timeout = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 15
                srv.stop(timeout)
                conn.sendall(b"OK")
                clean_stop.set()
                shutdown_event.set()
            elif line.startswith("RENAME "):
                # best-effort live rename of the in-memory name; the pid/log
                # file rename itself is done by win_rename() in the caller's
                # process, not here.
                srv.name = line[7:]
                conn.sendall(b"OK")
            else:
                conn.sendall(b"ERR unknown command")
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    shutdown_event = threading.Event()
    clean_stop = threading.Event()  # set only by an explicit STOP command, never by a crash
    listener.settimeout(1.0)  # periodic wakeup so the exit-watcher below is actually checked

    def exit_watcher():
        # The bot can die on its own (crash) with nobody ever polling again --
        # this is what makes the helper exit anyway instead of lingering as a
        # dead listener forever. On a crash we deliberately do NOT delete the
        # pid file ourselves (see the finally block below): the file still
        # points at this helper's now-dead PID once it exits, so the next
        # win_instance_status call's "helper process itself is gone" branch
        # reports CRASHED and cleans the file up itself at that point. The
        # grace sleep below keeps the helper alive and answering STATUS/LOGS
        # for a few seconds after the crash, so a poller checking right away
        # gets "crashed" directly from the still-running helper, not from the
        # (also correct, but less immediate) gone-pid-file fallback.
        while not shutdown_event.is_set():
            if srv.proc.poll() is not None:
                time.sleep(5)
                shutdown_event.set()
                break
            time.sleep(1)

    threading.Thread(target=exit_watcher, daemon=True).start()

    try:
        while not shutdown_event.is_set():
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()
    finally:
        srv.close_job()
        if clean_stop.is_set():
            _remove_pid_file(name)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "_helper":
        _name, _config_path = sys.argv[2], sys.argv[3]
        _mem_limit = None
        if "--mem-limit" in sys.argv:
            _mem_limit = int(sys.argv[sys.argv.index("--mem-limit") + 1])
        _run_helper(_name, _config_path, _mem_limit)
    else:
        print("platform_win.py is a support module for manager.py, not meant to be run directly "
              "(except as its own '_helper' subcommand, which manager.py launches itself).",
              file=sys.stderr)
        sys.exit(1)
