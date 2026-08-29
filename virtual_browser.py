"""Local VirtualBrowser worker discovery and least-recently-used allocation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping


class VirtualBrowserError(RuntimeError):
    """A user-facing VirtualBrowser configuration error."""


@dataclass(frozen=True, slots=True)
class VirtualBrowserWorker:
    worker_id: str
    profile_path: Path


def default_worker_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VirtualBrowser" / "Workers"


def discover_workers(root: Path | None = None) -> list[VirtualBrowserWorker]:
    """Return the numbered worker profiles created by the VirtualBrowser UI."""
    worker_root = root or default_worker_root()
    if not worker_root.is_dir():
        raise VirtualBrowserError(
            f"找不到 VirtualBrowser worker 目录：{worker_root}。请先在 VirtualBrowser 中创建环境。"
        )
    workers = [
        VirtualBrowserWorker(child.name, child)
        for child in worker_root.iterdir()
        if child.is_dir() and child.name.isdecimal() and int(child.name) > 0
    ]
    return sorted(workers, key=lambda worker: int(worker.worker_id))


def find_browser_executable(program_files: Path | None = None) -> Path:
    """Locate VirtualBrowser's Chromium executable, not the management shell."""
    base = program_files or Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    install_root = base / "VirtualBrowser"
    nested = sorted(
        install_root.glob("VirtualBrowser/*/VirtualBrowser.exe"),
        key=lambda candidate: candidate.parent.name,
        reverse=True,
    )
    candidates = [*nested, install_root / "VirtualBrowser.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise VirtualBrowserError(
        "未找到 VirtualBrowser 浏览器内核。请确认 VirtualBrowser 已安装，然后重试。"
    )


class VirtualBrowserPool:
    """Select an idle worker by least-recent use within a fixed pool size."""

    def __init__(self, *, root: Path | None = None, pool_size: int = 3) -> None:
        if pool_size < 1:
            raise VirtualBrowserError("worker 池数量必须至少为 1。")
        self.root = root or default_worker_root()
        self.pool_size = pool_size

    def workers(self) -> list[VirtualBrowserWorker]:
        workers = discover_workers(self.root)
        if len(workers) < self.pool_size:
            raise VirtualBrowserError(
                f"VirtualBrowser worker 池需要 {self.pool_size} 个环境，但目前只有 {len(workers)} 个。"
            "请先在 VirtualBrowser 中补齐环境。"
        )
        return workers[: self.pool_size]

    def acquire(
        self,
        *,
        occupied_worker_ids: Collection[str],
        last_used_at: Mapping[str, float],
    ) -> VirtualBrowserWorker:
        occupied = set(occupied_worker_ids)
        candidates = [worker for worker in self.workers() if worker.worker_id not in occupied]
        if not candidates:
            raise VirtualBrowserError(
                "所有 VirtualBrowser worker 都在使用中；请等待一个任务结束，或增加 worker 池数量。"
            )
        return min(
            candidates,
            key=lambda worker: (float(last_used_at.get(worker.worker_id, 0.0)), int(worker.worker_id)),
        )
