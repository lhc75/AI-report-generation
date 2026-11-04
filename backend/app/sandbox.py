"""生成代码隔离执行：macOS Seatbelt 与 Windows 作业对象两个后端。

macOS 使用 sandbox-exec 文件/网络白名单、rlimit 资源限额和有界日志；
Windows 使用作业对象（Job Object）限制进程数、内存与 CPU 时间，超时或超限
即终止整个进程树，但不提供文件读写与网络访问白名单。
隔离不可用或自检失败时拒绝执行，不回退到普通 Python 子进程。
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

DEFAULT_TIMEOUT = 30
DEFAULT_MEM_MB = 512
DEFAULT_CPU_SECONDS = 15
MAX_LOG_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_RESULT_BYTES = 8000
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
IS_WINDOWS = sys.platform == "win32"


# 由 python -I -c 运行的受信启动代码。启动前不执行 preexec_fn，避免后台线程
# fork 后继承解释器锁；-I 阻止 workdir/resource.py 或 sitecustomize.py 替换限额逻辑。
# 脚本和限额仅经 argv 传入，所有硬上限在执行生成代码之前设置。
_BOOTSTRAP = """import resource, runpy, sys
script = sys.argv[1]
mem_bytes = int(sys.argv[2]) * 1024 * 1024
cpu_seconds = int(sys.argv[3])
max_output_bytes = int(sys.argv[4])
try:
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
except ValueError:
    if sys.platform != 'darwin':
        raise
    # Darwin 的 RLIMIT_AS 由受信父进程的整个进程组 RSS 检查补足。
resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
resource.setrlimit(resource.RLIMIT_FSIZE, (max_output_bytes, max_output_bytes))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.argv = [script]
runpy.run_path(script, run_name='__main__')
"""


def _profile(workdir: Path) -> str:
    """全局数据读取白名单；仅放行运行库、本次材料及必要系统资源。

macOS 加载器需读取根目录本身，因此单独允许 literal "/"，这不会允许
根目录内其它文件。运行库使用真实路径，阻止符号链接绕过。
    """
    runtime = Path(sys.base_prefix).resolve()
    if runtime in {Path("/"), Path("/usr"), Path("/opt"), Path("/Users")}:
        raise RuntimeError("Python 运行库目录过宽，无法安全建立隔离白名单")
    allowed = {
        workdir.resolve(), Path(sys.prefix).resolve(), runtime,
        Path("/System/Library"), Path("/usr/lib"), Path("/usr/share"), Path("/Library/Fonts"),
    }
    literal_allowed = {
        Path("/"), Path("/dev/urandom"), Path("/dev/random"), Path("/dev/null"),
        Path("/private/etc/apache2/mime.types"), Path("/private/etc/localtime").resolve(),
    }
    exceptions = " ".join(
        f"(require-not (subpath {json.dumps(str(path), ensure_ascii=False)}))" for path in sorted(allowed)
    )
    exceptions += " " + " ".join(
        f"(require-not (literal {json.dumps(str(path), ensure_ascii=False)}))" for path in sorted(literal_allowed)
    )
    lines = [
        "(version 1)", "(allow default)", "(deny network*)",
        "(deny process-fork)",
        f"(deny file-write* (require-not (subpath {json.dumps(str(workdir.resolve()), ensure_ascii=False)})))",
        f"(deny process-exec (require-not (subpath {json.dumps(str(runtime), ensure_ascii=False)})))",
    ]
    lines.append(f"(deny file-read-data (require-all {exceptions}))")
    return "\n".join(lines) + "\n"


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()


def _read_tail(path: Path) -> str:
    if path.is_symlink():
        raise RuntimeError("日志路径不能是符号链接")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("日志路径不是普通文件")
        handle.seek(max(0, info.st_size - MAX_RESULT_BYTES))
        return handle.read(MAX_RESULT_BYTES).decode("utf-8", errors="replace")


def _open_local_write(path: Path):
    if path.is_symlink():
        raise RuntimeError("沙箱内部路径不能是符号链接")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(descriptor, "wb")
    info = os.fstat(handle.fileno())
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        handle.close()
        raise RuntimeError("沙箱内部路径不是普通文件")
    os.ftruncate(descriptor, 0)
    return handle


def _write_local(path: Path, text: str) -> None:
    with _open_local_write(path) as handle:
        handle.write(text.encode("utf-8"))


def output_path(workdir: Path, relative: str | Path) -> Path:
    """父进程回读前验证产物路径；拒绝绝对路径、目录穿越和任何符号链接。"""
    workdir = Path(workdir).resolve()
    relative = Path(relative)
    if relative.is_absolute():
        raise ValueError("产物必须使用沙箱内部相对路径")
    candidate = workdir / relative
    if not candidate.resolve().is_relative_to(workdir):
        raise ValueError("产物路径越出沙箱工作目录")
    for component in (candidate, *candidate.parents):
        if component == workdir:
            break
        if component.is_symlink():
            raise ValueError("产物路径不能包含符号链接")
    if candidate.exists() and candidate.is_file() and candidate.stat().st_nlink != 1:
        raise ValueError("产物不能引用其它文件的硬链接")
    return candidate.resolve()


def _start_log_readers(proc: subprocess.Popen, workdir: Path, log_prefix: str,
                       max_log_bytes: int):
    """启动有界日志落盘线程；日志超限置位 exceeded，由父进程终止子进程。"""
    stdout_path, stderr_path = workdir / f"{log_prefix}.stdout.log", workdir / f"{log_prefix}.stderr.log"
    exceeded = threading.Event()
    reader_errors: list[str] = []

    def save_log(pipe, path):
        written = 0
        try:
            with _open_local_write(path) as output:
                while data := pipe.read(8192):
                    remaining = max_log_bytes - written
                    if remaining > 0:
                        output.write(data[:remaining])
                        written += min(remaining, len(data))
                    if len(data) > remaining:
                        exceeded.set()
        except Exception as exc:
            reader_errors.append(str(exc))
            exceeded.set()
        finally:
            pipe.close()

    readers = [
        threading.Thread(target=save_log, args=(proc.stdout, stdout_path), daemon=True),
        threading.Thread(target=save_log, args=(proc.stderr, stderr_path), daemon=True),
    ]
    for reader in readers:
        reader.start()
    return exceeded, reader_errors, readers, stdout_path, stderr_path


def _execute(script: Path, profile: Path, workdir: Path, *, timeout: int,
             mem_mb: int, cpu_seconds: int, log_prefix: str,
             max_log_bytes: int = MAX_LOG_BYTES, max_output_bytes: int = MAX_OUTPUT_BYTES) -> dict:
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(workdir), "TMPDIR": str(workdir),
        "LANG": "en_US.UTF-8", "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(workdir / ".mpl"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
    }
    proc = subprocess.Popen(
        [str(SANDBOX_EXEC), "-f", str(profile), sys.executable, "-I", "-u", "-c", _BOOTSTRAP,
         str(script), str(mem_mb), str(cpu_seconds), str(max_output_bytes)],
        cwd=str(workdir), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    exceeded, reader_errors, readers, stdout_path, stderr_path = _start_log_readers(
        proc, workdir, log_prefix, max_log_bytes)
    deadline = time.monotonic() + timeout
    timed_out = False
    memory_exceeded = False
    next_memory_check = 0
    try:
        while proc.poll() is None:
            if exceeded.is_set():
                _kill_group(proc)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc)
                break
            if time.monotonic() >= next_memory_check:
                memory = subprocess.run(
                    ["/bin/ps", "-o", "rss=", "-g", str(proc.pid)],
                    capture_output=True, text=True, timeout=2,
                )
                rss_kb = sum(int(value) for value in memory.stdout.split())
                if memory.returncode not in (0, 1):
                    _kill_group(proc)
                    raise RuntimeError("无法读取沙箱进程组内存，已终止执行")
                if rss_kb > mem_mb * 1024:
                    memory_exceeded = True
                    _kill_group(proc)
                    break
                next_memory_check = time.monotonic() + 0.1
            exceeded.wait(min(0.05, max(0, deadline - time.monotonic())))
        proc.wait(timeout=5)
    finally:
        _kill_group(proc)
        for reader in readers:
            reader.join(timeout=5)
    stdout, stderr = _read_tail(stdout_path), _read_tail(stderr_path)
    if timed_out:
        stderr += f"\n执行超时（>{timeout}s），已终止进程组"
    if exceeded.is_set():
        stderr += f"\n执行日志超过 {max_log_bytes} 字节，已终止进程组"
    if memory_exceeded:
        stderr += f"\n执行内存超过 {mem_mb} MB，已终止进程组"
    if reader_errors:
        stderr += "\n日志保存失败：" + "; ".join(reader_errors)
    return {
        "ok": proc.returncode == 0 and not timed_out and not exceeded.is_set() and not memory_exceeded,
        "stdout": stdout[-MAX_RESULT_BYTES:], "stderr": stderr[-MAX_RESULT_BYTES:],
        "returncode": proc.returncode, "timeout": timed_out, "output_limit": exceeded.is_set(),
        "memory_limit": memory_exceeded,
    }


# ---------------------------------------------------------------------------
# Windows 后端：作业对象（Job Object）
# 单进程上限阻断派生子进程，内存硬上限与用户态 CPU 时间限额，
# 超时或超限即终止整个进程树。与 macOS Seatbelt 不同，该后端
# 不提供文件读写与网络访问白名单。
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOB_BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOB_EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOB_BASIC_LIMITS),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_LIMIT_PROCESS_TIME = 0x00000002
    _JOB_LIMIT_JOB_TIME = 0x00000004
    _JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
    _JOB_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_LIMIT_JOB_MEMORY = 0x00000200
    _JOB_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    _JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    # 实测本机 Windows 上仅以 THREAD_SUSPEND_RESUME(0x0020) 打开挂起线程会
    # 被 ResumeThread 拒绝（WinError 5），故使用 THREAD_ALL_ACCESS。
    _THREAD_ALL_ACCESS = 0x001FFFFF
    _TH32CS_SNAPTHREAD = 0x00000004
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _CREATE_SUSPENDED = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)

    def _win_error(message: str) -> RuntimeError:
        return RuntimeError(f"{message}：{ctypes.WinError(ctypes.get_last_error())}")

    def _create_job(mem_mb: int, cpu_seconds: int):
        """作业对象限额：单进程、内存硬上限、用户态 CPU 时间；关句柄即终止进程树。"""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _win_error("无法创建作业对象")
        try:
            limits = _JOB_EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = (
                _JOB_LIMIT_ACTIVE_PROCESS | _JOB_LIMIT_PROCESS_TIME | _JOB_LIMIT_JOB_TIME
                | _JOB_LIMIT_PROCESS_MEMORY | _JOB_LIMIT_JOB_MEMORY
                | _JOB_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | _JOB_LIMIT_KILL_ON_JOB_CLOSE
            )
            limits.BasicLimitInformation.ActiveProcessLimit = 1
            units = cpu_seconds * 10_000_000  # 以 100 纳秒为单位
            limits.BasicLimitInformation.PerProcessUserTimeLimit = units
            limits.BasicLimitInformation.PerJobUserTimeLimit = units
            limits.ProcessMemoryLimit = mem_mb * 1024 * 1024
            limits.JobMemoryLimit = mem_mb * 1024 * 1024
            if not _kernel32.SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                raise _win_error("无法设置作业对象限额")
            return job
        except Exception:
            _kernel32.CloseHandle(job)
            raise

    def _job_peak_memory(job) -> int:
        limits = _JOB_EXTENDED_LIMITS()
        if not _kernel32.QueryInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                ctypes.sizeof(limits), None):
            raise _win_error("无法读取作业对象内存用量")
        return max(int(limits.PeakJobMemoryUsed), int(limits.PeakProcessMemoryUsed))

    def _resume_process_thread(pid: int) -> None:
        """Popen 不保留主线程句柄，经线程快照找到子进程线程并恢复运行。"""
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _INVALID_HANDLE:
            raise _win_error("无法枚举系统线程")
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            has_entry = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while has_entry:
                if entry.th32OwnerProcessID == pid:
                    thread = _kernel32.OpenThread(_THREAD_ALL_ACCESS, False, entry.th32ThreadID)
                    if not thread:
                        raise _win_error("无法打开沙箱子进程线程")
                    try:
                        if _kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                            raise _win_error("无法恢复沙箱子进程线程")
                    finally:
                        _kernel32.CloseHandle(thread)
                    return
                has_entry = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            raise RuntimeError("未找到沙箱子进程的主线程")
        finally:
            _kernel32.CloseHandle(snapshot)

    def _terminate_job(job) -> None:
        _kernel32.TerminateJobObject(job, 1)


def _windows_env(workdir: Path) -> dict:
    """最小环境：运行库与系统目录进 PATH，临时/家目录指向沙箱工作目录。"""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    runtime = Path(sys.base_prefix)
    path_parts = [
        str(runtime), str(runtime / "Scripts"), str(runtime / "DLLs"),
        str(runtime / "Library" / "bin"), str(runtime / "Library" / "mingw-w64" / "bin"),
        str(runtime / "Library" / "usr" / "bin"),
        os.path.join(system_root, "System32"), system_root,
        os.path.join(system_root, "System32", "Wbem"),
    ]
    return {
        "SystemRoot": system_root, "WINDIR": system_root,
        "PATH": os.pathsep.join(path_parts),
        "TEMP": str(workdir), "TMP": str(workdir),
        "HOME": str(workdir), "USERPROFILE": str(workdir),
        "HOMEDRIVE": workdir.drive or "C:", "HOMEPATH": "\\",
        "LANG": "en_US.UTF-8",
        "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(workdir / ".mpl"),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
    }


def _dir_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _windows_python_command(script: Path) -> list[str]:
    """venv 的 python.exe 是重定向器，会派生真实解释器并占满作业对象的
    单进程名额；检测到虚拟环境时直接启动基础解释器，由引导代码显式挂载
    venv 的 site-packages（-I 下忽略 PYTHONPATH，-S 跳过 site 初始化）。"""
    base = getattr(sys, "_base_executable", None)
    in_venv = (
        base
        and Path(base).resolve() != Path(sys.executable).resolve()
        and (Path(sys.prefix) / "pyvenv.cfg").is_file()
    )
    if not in_venv:
        return [sys.executable, "-I", "-X", "utf8", "-u", str(script)]
    import sysconfig

    site_dirs = sorted(d for d in {
        sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
    } if d)
    bootstrap = (
        "import runpy, site, sys\n"
        + "".join(f"site.addsitedir({d!r})\n" for d in site_dirs)
        + "script = sys.argv[1]\n"
        "sys.argv = [script]\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )
    return [base, "-I", "-S", "-X", "utf8", "-u", "-c", bootstrap, str(script)]


def _execute_windows(script: Path, workdir: Path, *, timeout: int,
                     mem_mb: int, cpu_seconds: int, log_prefix: str,
                     max_log_bytes: int = MAX_LOG_BYTES,
                     max_output_bytes: int = MAX_OUTPUT_BYTES) -> dict:
    if not IS_WINDOWS:
        raise RuntimeError("Windows 作业对象隔离仅在 win32 平台可用")
    job = _create_job(mem_mb, cpu_seconds)
    proc = None
    try:
        proc = subprocess.Popen(
            _windows_python_command(script),
            cwd=str(workdir), env=_windows_env(workdir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_CREATE_SUSPENDED,
        )
        try:
            # 先挂起创建、加入作业对象后再恢复，子进程没有机会在限额之外运行。
            if not _kernel32.AssignProcessToJobObject(job, proc._handle):
                raise _win_error("无法将子进程加入作业对象")
            _resume_process_thread(proc.pid)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
            raise
        exceeded, reader_errors, readers, stdout_path, stderr_path = _start_log_readers(
            proc, workdir, log_prefix, max_log_bytes)
        deadline = time.monotonic() + timeout
        timed_out = memory_exceeded = output_exceeded = False
        next_size_check = 0.0
        try:
            while proc.poll() is None:
                if exceeded.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if _job_peak_memory(job) > mem_mb * 1024 * 1024:
                    memory_exceeded = True
                    break
                if time.monotonic() >= next_size_check:
                    if _dir_size(workdir) > max_output_bytes:
                        output_exceeded = True
                        break
                    next_size_check = time.monotonic() + 0.5
                exceeded.wait(min(0.1, max(0, deadline - time.monotonic())))
            if proc.poll() is None:
                _terminate_job(job)
            proc.wait(timeout=5)
        finally:
            _terminate_job(job)
            for reader in readers:
                reader.join(timeout=5)
        stdout, stderr = _read_tail(stdout_path), _read_tail(stderr_path)
        if timed_out:
            stderr += f"\n执行超时（>{timeout}s），已终止进程"
        if exceeded.is_set():
            stderr += f"\n执行日志超过 {max_log_bytes} 字节，已终止进程"
        if memory_exceeded:
            stderr += f"\n执行内存超过 {mem_mb} MB，已终止进程"
        if output_exceeded:
            stderr += f"\n执行产物超过 {max_output_bytes} 字节，已终止进程"
        if reader_errors:
            stderr += "\n日志保存失败：" + "; ".join(reader_errors)
        return {
            "ok": proc.returncode == 0 and not timed_out and not exceeded.is_set()
                  and not memory_exceeded and not output_exceeded,
            "stdout": stdout[-MAX_RESULT_BYTES:], "stderr": stderr[-MAX_RESULT_BYTES:],
            "returncode": proc.returncode, "timeout": timed_out,
            "output_limit": exceeded.is_set() or output_exceeded,
            "memory_limit": memory_exceeded,
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        _kernel32.CloseHandle(job)


def _windows_self_check(workdir: Path, mem_mb: int, cpu_seconds: int,
                        max_log_bytes: int = MAX_LOG_BYTES,
                        max_output_bytes: int = MAX_OUTPUT_BYTES) -> None:
    """验证作业对象能执行脚本并阻断子进程派生；仅写沙箱内部探针文件。"""
    script = workdir / ".sandbox_probe.py"
    _write_local(script,
        "import pathlib, subprocess, sys\n"
        "pathlib.Path('.isolation_ok').write_text('ok')\n"
        "spawned = False\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], capture_output=True, timeout=5)\n"
        "    spawned = True\n"
        "except (OSError, subprocess.SubprocessError):\n"
        "    pass\n"
        "if spawned:\n"
        "    raise RuntimeError('child process spawn permitted')\n"
        "print('SANDBOX_ISOLATION_OK')\n",
    )
    try:
        result = _execute_windows(script, workdir, timeout=15, mem_mb=mem_mb,
                                  cpu_seconds=cpu_seconds, log_prefix=".sandbox_check",
                                  max_log_bytes=max_log_bytes, max_output_bytes=max_output_bytes)
        if not result["ok"] or "SANDBOX_ISOLATION_OK" not in result["stdout"]:
            raise RuntimeError(result["stderr"] or f"自检进程退出码 {result['returncode']}")
    finally:
        script.unlink(missing_ok=True)
        (workdir / ".isolation_ok").unlink(missing_ok=True)


def _self_check(workdir: Path, profile: Path, mem_mb: int, cpu_seconds: int,
                max_log_bytes: int = MAX_LOG_BYTES, max_output_bytes: int = MAX_OUTPUT_BYTES) -> None:
    # 仅探测自己创建的文件，不读取任何用户文件或配置。
    with tempfile.TemporaryDirectory(prefix="ai_report_isolation_probe_") as probe:
        outside = Path(probe).resolve() / "private.txt"
        outside.write_text("isolation-probe", encoding="utf-8")
        link = workdir / ".sandbox_probe_link"
        link.unlink(missing_ok=True)
        link.symlink_to(outside)
        script = workdir / ".sandbox_probe.py"
        _write_local(script,
            "import errno, pathlib, socket\n"
            f"outside = pathlib.Path({str(outside)!r})\n"
            f"link = pathlib.Path({str(link)!r})\n"
            "for path in (outside, link):\n"
            "    try:\n"
            "        path.read_bytes()\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise RuntimeError('outside read permitted')\n"
            "try:\n"
            "    outside.open('a').write('invalid')\n"
            "except PermissionError:\n"
            "    pass\n"
            "else:\n"
            "    raise RuntimeError('outside write permitted')\n"
            "try:\n"
            "    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    sock.settimeout(1)\n"
            "    result = sock.connect_ex(('127.0.0.1', 9))\n"
            "    sock.close()\n"
            "    if result not in (errno.EPERM, errno.EACCES):\n"
            "        raise RuntimeError('network permitted')\n"
            "except PermissionError:\n"
            "    pass\n"
            "pathlib.Path('.isolation_ok').write_text('ok')\n"
            "print('SANDBOX_ISOLATION_OK')\n",
        )
        try:
            result = _execute(script, profile, workdir, timeout=10, mem_mb=mem_mb,
                              cpu_seconds=cpu_seconds, log_prefix=".sandbox_check",
                              max_log_bytes=max_log_bytes, max_output_bytes=max_output_bytes)
            if not result["ok"] or "SANDBOX_ISOLATION_OK" not in result["stdout"]:
                raise RuntimeError(result["stderr"] or f"自检进程退出码 {result['returncode']}")
        finally:
            link.unlink(missing_ok=True)
            script.unlink(missing_ok=True)
            (workdir / ".isolation_ok").unlink(missing_ok=True)


def run_python(code: str, workdir: Path, timeout: int | None = None,
               mem_mb: int | None = None, cpu_seconds: int | None = None) -> dict:
    """返回 {ok, stdout, stderr, returncode, timeout, output_limit}。"""
    try:
        from .config import settings

        timeout = settings.SCRIPT_SANDBOX_TIMEOUT_SECONDS if timeout is None else timeout
        mem_mb = settings.SCRIPT_SANDBOX_MEMORY_MB if mem_mb is None else mem_mb
        cpu_seconds = settings.SCRIPT_SANDBOX_CPU_SECONDS if cpu_seconds is None else cpu_seconds
        max_log_bytes = settings.SCRIPT_SANDBOX_MAX_LOG_BYTES
        max_output_bytes = settings.SCRIPT_SANDBOX_MAX_OUTPUT_BYTES
        backend = settings.SCRIPT_SANDBOX_BACKEND
        if backend == "auto":
            backend = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "")
        if backend not in {"macos", "windows"}:
            raise RuntimeError(f"不支持的沙箱后端：{settings.SCRIPT_SANDBOX_BACKEND}")
        if backend == "macos" and (sys.platform != "darwin" or not SANDBOX_EXEC.is_file()):
            raise RuntimeError("Mac sandbox-exec 不可用，已拒绝执行生成代码")
        if backend == "windows" and sys.platform != "win32":
            raise RuntimeError("Windows 作业对象隔离仅在 win32 平台可用，已拒绝执行生成代码")
        if min(timeout, mem_mb, cpu_seconds, max_log_bytes, max_output_bytes) <= 0:
            raise ValueError("沙箱超时、内存和 CPU 限额必须大于零")
        workdir = Path(workdir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "out").mkdir(exist_ok=True)
        output_path(workdir, "out")
        script = workdir / "main.py"
        _write_local(script, code)
        if backend == "macos":
            profile = workdir / ".sandbox.sb"
            _write_local(profile, _profile(workdir))
            _self_check(workdir, profile, mem_mb, cpu_seconds, max_log_bytes, max_output_bytes)
            return _execute(script, profile, workdir, timeout=timeout, mem_mb=mem_mb,
                            cpu_seconds=cpu_seconds, log_prefix="execution",
                            max_log_bytes=max_log_bytes, max_output_bytes=max_output_bytes)
        _windows_self_check(workdir, mem_mb, cpu_seconds, max_log_bytes, max_output_bytes)
        return _execute_windows(script, workdir, timeout=timeout, mem_mb=mem_mb,
                                cpu_seconds=cpu_seconds, log_prefix="execution",
                                max_log_bytes=max_log_bytes, max_output_bytes=max_output_bytes)
    except Exception as exc:
        return {"ok": False, "stdout": "", "stderr": f"沙箱启动或隔离自检失败：{exc}",
                "returncode": -1, "timeout": False, "output_limit": False}


def fresh_workdir(project_id: int, task_id: int, run_token: str | None = None) -> Path:
    """每次调用创建新目录，重试、批次和导出不会覆盖仍在执行的文件。"""
    from hashlib import sha256

    base = Path(tempfile.gettempdir()) / "ai_report_sandbox" / f"p{project_id}" / f"t{task_id}"
    base.mkdir(parents=True, exist_ok=True)
    token_prefix = sha256(str(run_token).encode()).hexdigest()[:12] if run_token else "run"
    workdir = Path(tempfile.mkdtemp(prefix=f"{token_prefix}_", dir=base)).resolve()
    workdir.chmod(0o700)
    (workdir / "data").mkdir()
    (workdir / "out").mkdir()
    return workdir
