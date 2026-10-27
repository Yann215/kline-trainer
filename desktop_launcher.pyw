"""Start the existing trainer in a local app window on Windows."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
STATE = RUNTIME / "desktop-launcher.json"
LOG = RUNTIME / "desktop-launcher.log"
PYTHON = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def process_start(pid: int) -> int | None:
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    try:
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def child_of(pid: int, parent: int) -> bool:
    # venv's Windows launcher creates a separate base-Python child process.
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", ctypes.c_long),
                    ("flags", wintypes.DWORD), ("name", wintypes.WCHAR * 260)]
    kernel = ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return False
    try:
        item = Entry()
        item.size = ctypes.sizeof(item)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(item))
        while ok:
            if item.pid == pid:
                return item.parent == parent
            ok = kernel.Process32NextW(snapshot, ctypes.byref(item))
        return False
    finally:
        kernel.CloseHandle(snapshot)


def listener_pid(port: int) -> int | None:
    output = subprocess.check_output(["netstat", "-ano", "-p", "tcp"],
                                     creationflags=NO_WINDOW, text=True, errors="replace")
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[1] == f"127.0.0.1:{port}" and parts[3] == "LISTENING":
            return int(parts[4])
    return None


def ready(state: dict) -> bool:
    try:
        pid, port = int(state["pid"]), int(state["port"])
        if process_start(pid) != state["created"] or listener_pid(port) != pid:
            return False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            health = json.load(response)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            return health.get("status") == "ok" and response.status == 200
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return False


def start_server() -> dict:
    if not PYTHON.is_file() or not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("缺少运行环境或前端文件。请先运行项目目录中的 start.ps1 完成安装。")
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if ready(state):
            return state
    except (OSError, ValueError, TypeError):
        pass
    # The selected ephemeral port avoids interfering with other local applications.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with LOG.open("a", encoding="utf-8") as log:
        process = subprocess.Popen([str(PYTHON), str(ROOT / "run.py"), "--port", str(port)],
                                   cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"训练服务启动失败，详情请查看 {LOG}")
        pid = listener_pid(port)
        if pid and (pid == process.pid or child_of(pid, process.pid)):
            state = {"pid": pid, "created": process_start(pid), "port": port}
            if state["created"] is not None and ready(state):
                STATE.write_text(json.dumps(state), encoding="utf-8")
                return state
        time.sleep(0.5)
    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                   creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    raise RuntimeError(f"训练服务启动超时，详情请查看 {LOG}")


def main() -> None:
    RUNTIME.mkdir(exist_ok=True)
    # A named mutex prevents simultaneous clicks from launching duplicate servers.
    name = "Local\\KlineTrainer_" + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:20]
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, name)
    if not mutex:
        raise RuntimeError("无法创建启动锁")
    acquired = kernel.WaitForSingleObject(mutex, 45000) in (0, 0x80)
    if not acquired:
        kernel.CloseHandle(mutex)
        raise RuntimeError("启动等待超时，请稍后重试")
    try:
        state = start_server()
    finally:
        kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)
    url = f"http://127.0.0.1:{state['port']}"
    if "--check" in sys.argv:
        print(json.dumps(state))
        return
    candidates = [Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
                  Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
                  Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe"]
    browser = next((path for path in candidates if path.is_file()), None)
    if browser:
        subprocess.Popen([str(browser), "--app=" + url], cwd=str(ROOT))
    else:
        os.startfile(url)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--check" in sys.argv:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        ctypes.windll.user32.MessageBoxW(None, str(exc), "盘感训练启动失败", 0x10)
        sys.exit(1)
