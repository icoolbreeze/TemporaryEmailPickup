import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from virtual_browser import VirtualBrowserError, VirtualBrowserPool, discover_workers


class VirtualBrowserPoolTests(unittest.TestCase):
    def test_discovers_only_numbered_worker_directories(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("3", "1", "cache", "0"):
                (root / name).mkdir()
            self.assertEqual([worker.worker_id for worker in discover_workers(root)], ["1", "3"])

    def test_allocates_least_recent_idle_worker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("1", "2", "3"):
                (root / name).mkdir()
            pool = VirtualBrowserPool(root=root, pool_size=3)
            worker = pool.acquire(
                occupied_worker_ids={"1"},
                last_used_at={"2": 20.0, "3": 10.0},
            )
            self.assertEqual(worker.worker_id, "3")

    def test_requires_fixed_pool_size(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "1").mkdir()
            pool = VirtualBrowserPool(root=root, pool_size=2)
            with self.assertRaisesRegex(VirtualBrowserError, "需要 2 个环境"):
                pool.workers()


if __name__ == "__main__":
    unittest.main()
