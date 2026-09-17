from __future__ import annotations

import os
import subprocess
import unittest
from unittest import mock

import pickup_tunnel


class FakeProcess:
    """Minimal subprocess.Popen stand-in: never touches a real ssh."""

    def __init__(self, pid: int = 4242, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode = 0 if alive else 1
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        return 0


class PickupTunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        pickup_tunnel._reset_for_tests()

    def tearDown(self) -> None:
        pickup_tunnel._reset_for_tests()

    def test_reuses_open_local_port(self) -> None:
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=True):
            self.assertEqual(pickup_tunnel.ensure_pickup_tunnel(), 0)
        self.assertIsNone(pickup_tunnel._process)

    def test_starts_ssh_with_expected_arguments(self) -> None:
        process = FakeProcess(pid=777)
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=False), \
                mock.patch.object(pickup_tunnel, "_wait_for_port", return_value=True), \
                mock.patch.object(pickup_tunnel.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(pickup_tunnel.ensure_pickup_tunnel(), 777)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        command = args[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("-N", command)
        for option in (
            "BatchMode=yes",
            "ExitOnForwardFailure=yes",
            "ServerAliveInterval=30",
            "ServerAliveCountMax=3",
        ):
            self.assertIn(option, command)
        self.assertIn("18793:127.0.0.1:18793", command)
        self.assertEqual(command[-1], "beike-server")
        if os.name == "nt":
            self.assertEqual(kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW)
        self.assertEqual(
            kwargs.get("stdin"),
            getattr(pickup_tunnel.subprocess, "DEVNULL", None),
        )
        self.assertIs(pickup_tunnel._process, process)

    def test_custom_host_and_ports_are_used(self) -> None:
        process = FakeProcess(pid=10)
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=False), \
                mock.patch.object(pickup_tunnel, "_wait_for_port", return_value=True), \
                mock.patch.object(pickup_tunnel.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(
                pickup_tunnel.ensure_pickup_tunnel("my-vps", 2200, 9200), 10
            )
        command = popen.call_args[0][0]
        self.assertIn("2200:127.0.0.1:9200", command)
        self.assertEqual(command[-1], "my-vps")

    def test_restarts_hung_ssh_when_port_stays_closed(self) -> None:
        hung = FakeProcess(pid=11)
        fresh = FakeProcess(pid=12)
        pickup_tunnel._process = hung
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=False), \
                mock.patch.object(
                    pickup_tunnel, "_wait_for_port", side_effect=[False, True]
                ), \
                mock.patch.object(
                    pickup_tunnel.subprocess, "Popen", return_value=fresh
                ):
            self.assertEqual(pickup_tunnel.ensure_pickup_tunnel(), 12)
        self.assertTrue(hung.terminated)
        self.assertIs(pickup_tunnel._process, fresh)

    def test_reuse_after_tunnel_already_started(self) -> None:
        process = FakeProcess(pid=31)
        pickup_tunnel._process = process
        with mock.patch.object(pickup_tunnel, "_port_accepts", side_effect=[False, False, True]):
            # First check (pre-lock) closed, second (in lock) closed, wait succeeds.
            self.assertEqual(pickup_tunnel.ensure_pickup_tunnel(), 31)
        self.assertFalse(process.terminated)

    def test_raises_when_ssh_exits_immediately(self) -> None:
        process = FakeProcess(pid=5, alive=False)
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=False), \
                mock.patch.object(pickup_tunnel, "_wait_for_port") as wait_mock, \
                mock.patch.object(pickup_tunnel.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                pickup_tunnel.ensure_pickup_tunnel()
        wait_mock.assert_not_called()
        self.assertIsNone(pickup_tunnel._process)

    def test_raises_and_cleans_up_when_port_never_opens(self) -> None:
        process = FakeProcess(pid=6)
        with mock.patch.object(pickup_tunnel, "_port_accepts", return_value=False), \
                mock.patch.object(pickup_tunnel, "_wait_for_port", return_value=False), \
                mock.patch.object(pickup_tunnel.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                pickup_tunnel.ensure_pickup_tunnel()
        self.assertTrue(process.terminated)
        self.assertIsNone(pickup_tunnel._process)

    def test_stop_kills_only_the_tracked_process(self) -> None:
        process = FakeProcess(pid=99)
        pickup_tunnel._process = process
        pickup_tunnel.stop_pickup_tunnel()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)
        self.assertIsNone(pickup_tunnel._process)

    def test_stop_without_tunnel_is_a_noop(self) -> None:
        pickup_tunnel.stop_pickup_tunnel()
        self.assertIsNone(pickup_tunnel._process)

    def test_stop_after_process_already_exited(self) -> None:
        process = FakeProcess(pid=7, alive=False)
        pickup_tunnel._process = process
        pickup_tunnel.stop_pickup_tunnel()
        self.assertFalse(process.terminated)
        self.assertIsNone(pickup_tunnel._process)

    def test_wait_for_port_probes_socket(self) -> None:
        with mock.patch.object(pickup_tunnel.socket, "create_connection") as create_mock:
            self.assertTrue(pickup_tunnel._wait_for_port(18793, timeout=1.0))
        create_mock.assert_called_once_with(("127.0.0.1", 18793), timeout=0.5)

    def test_wait_for_port_times_out(self) -> None:
        with mock.patch.object(
            pickup_tunnel.socket, "create_connection", side_effect=OSError("refused")
        ), mock.patch.object(pickup_tunnel.time, "sleep", lambda _seconds: None):
            self.assertFalse(pickup_tunnel._wait_for_port(18793, timeout=0.05))


if __name__ == "__main__":
    unittest.main()
