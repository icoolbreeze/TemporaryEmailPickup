"""Local OpenSSH forward to the cloud Outlook pickup service.

The pickup API listens on ``127.0.0.1`` on the VPS only, so the Windows app
reaches it through ``ssh -L``. This module starts that detached ``ssh -N``
process on demand and remembers it so ``stop_pickup_tunnel`` can kill exactly
what it started.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

DEFAULT_SSH_HOST = "beike-server"
DEFAULT_LOCAL_PORT = 18793
DEFAULT_REMOTE_PORT = 18793
_CONNECT_TIMEOUT = 0.5
_READY_TIMEOUT = 15.0

_process: subprocess.Popen | None = None
_lock = threading.Lock()


def _port_accepts(port: int, host: str = "127.0.0.1", timeout: float = _CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(port: int, timeout: float = _READY_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_accepts(port):
            return True
        time.sleep(0.25)
    return False


def _start_ssh(ssh_host: str, local_port: int, remote_port: int) -> subprocess.Popen:
    command = [
        "ssh",
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"{local_port}:127.0.0.1:{remote_port}",
        ssh_host,
    ]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # Detached: no console window pops up next to the Tkinter app.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def ensure_pickup_tunnel(
    ssh_host: str = DEFAULT_SSH_HOST,
    local_port: int = DEFAULT_LOCAL_PORT,
    remote_port: int = DEFAULT_REMOTE_PORT,
) -> int:
    """Make sure ``127.0.0.1:local_port`` reaches the pickup service.

    Returns ``0`` when an existing listener is reused, otherwise the pid of the
    ``ssh`` process this module started. Raises ``RuntimeError`` when the
    tunnel cannot be brought up.
    """
    global _process
    if _port_accepts(local_port):
        return 0
    with _lock:
        if _port_accepts(local_port):
            return 0
        if _process is not None and _process.poll() is None:
            if _wait_for_port(local_port, timeout=2.0):
                return _process.pid
            _terminate(_process)
            _process = None
        elif _process is not None:
            _process = None
        process = _start_ssh(ssh_host, local_port, remote_port)
        _process = process
        if process.poll() is not None:
            code = process.returncode
            _process = None
            raise RuntimeError(f"SSH 隧道启动失败（ssh 退出码 {code}），请检查 {ssh_host} 是否可连接")
        if not _wait_for_port(local_port):
            _terminate(process)
            _process = None
            raise RuntimeError(f"SSH 隧道已启动，但本地端口 {local_port} 未就绪")
        return process.pid


def stop_pickup_tunnel() -> None:
    """Kill only the ``ssh`` process this module started, if any."""
    global _process
    with _lock:
        process = _process
        _process = None
    if process is None:
        return
    _terminate(process)


def _reset_for_tests() -> None:
    global _process
    with _lock:
        process = _process
        _process = None
    if process is not None:
        _terminate(process)


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        pid = ensure_pickup_tunnel(
            args[0] if len(args) > 0 else DEFAULT_SSH_HOST,
            int(args[1]) if len(args) > 1 else DEFAULT_LOCAL_PORT,
            int(args[2]) if len(args) > 2 else DEFAULT_REMOTE_PORT,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"云端取件隧道启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"pickup tunnel ready (pid={pid if pid else 'reused'})")
