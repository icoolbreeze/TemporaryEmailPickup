"""Tests for ``chromium_profiles`` discovery helpers.

The tests use a fake ``Local State`` JSON document and a temp directory so
they run on any platform without requiring Brave or Chrome to be
installed.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cf_browser import STUDIO_URL, manual_browser_command
from chromium_profiles import (
    ChromiumNamedProfile,
    cf_pool_profiles,
    parse_local_state_file,
    parse_local_state_payload,
)


def _build_local_state(profile_info: dict[str, str]) -> dict[str, object]:
    return {
        "profile": {
            "info_cache": {
                directory: {"name": name} for directory, name in profile_info.items()
            }
        }
    }


class ParseLocalStateTests(unittest.TestCase):
    def test_parses_named_profiles_from_fake_payload(self) -> None:
        payload = _build_local_state(
            {
                "Default": "工作",
                "Profile 4": "CF",
                "Profile 5": "CF2",
                "Profile 7": "CF3",
                "Profile 3": "CHATGPT",
                "Profile 10": "GPT LOGIN",
            }
        )
        profiles = parse_local_state_payload(
            payload,
            browser="Brave",
            user_data_dir=Path("/tmp/brave"),
        )

        by_directory = {item.directory: item for item in profiles}
        self.assertEqual(by_directory["Default"].name, "工作")
        self.assertEqual(by_directory["Default"].browser, "Brave")
        self.assertEqual(by_directory["Default"].user_data_dir, Path("/tmp/brave"))
        self.assertEqual(by_directory["Profile 4"].name, "CF")
        self.assertEqual(by_directory["Profile 5"].name, "CF2")
        self.assertEqual(by_directory["Profile 7"].name, "CF3")
        self.assertEqual(by_directory["Profile 3"].name, "CHATGPT")
        self.assertEqual(by_directory["Profile 10"].name, "GPT LOGIN")

    def test_cf_pool_keeps_only_cf_profiles(self) -> None:
        payload = _build_local_state(
            {
                "Default": "工作",
                "Profile 3": "CHATGPT",
                "Profile 4": "CF",
                "Profile 5": "CF2",
                "Profile 7": "CF3",
                "Profile 8": "CF10",
                "Profile 9": "ChatCF",
            }
        )
        profiles = parse_local_state_payload(
            payload, browser="Brave", user_data_dir=Path("/tmp/brave")
        )
        pool = cf_pool_profiles(profiles)
        names = [item.name for item in pool]
        # CF10 is included now: the regex is ^CF\d*$ (any non-empty digit
        # suffix), so the user-created CF10 pool entry is fair game.
        self.assertEqual(names, ["CF", "CF2", "CF3", "CF10"])

    def test_cf_pool_is_case_insensitive(self) -> None:
        payload = _build_local_state(
            {
                "Default": "工作",
                "Profile 4": "cf",
                "Profile 5": "Cf2",
            }
        )
        profiles = parse_local_state_payload(
            payload, browser="Brave", user_data_dir=Path("/tmp/brave")
        )
        pool = cf_pool_profiles(profiles)
        self.assertEqual([item.directory for item in pool], ["Profile 4", "Profile 5"])

    def test_empty_or_missing_name_falls_back_to_directory(self) -> None:
        payload = {
            "profile": {
                "info_cache": {
                    "Default": {"name": ""},
                    "Profile 1": {},  # missing name -> dropped
                    "Profile 2": {"name": None},  # explicit None -> dropped
                }
            }
        }
        profiles = parse_local_state_payload(
            payload, browser="Chrome", user_data_dir=Path("/tmp/chrome")
        )
        self.assertEqual([(item.directory, item.name) for item in profiles], [("Default", "Default")])

    def test_malformed_payload_returns_empty(self) -> None:
        self.assertEqual(
            parse_local_state_payload(None, browser="Brave", user_data_dir=Path("/x")),
            [],
        )
        self.assertEqual(
            parse_local_state_payload(
                {"profile": "not a dict"}, browser="Brave", user_data_dir=Path("/x")
            ),
            [],
        )
        self.assertEqual(
            parse_local_state_payload(
                {"profile": {"info_cache": "not a dict"}},
                browser="Brave",
                user_data_dir=Path("/x"),
            ),
            [],
        )

    def test_parse_local_state_file_reads_real_file(self) -> None:
        with TemporaryDirectory() as directory:
            user_data = Path(directory)
            payload = _build_local_state(
                {"Default": "工作", "Profile 4": "CF", "Profile 5": "CF2"}
            )
            (user_data / "Local State").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            profiles = parse_local_state_file(user_data / "Local State", browser="Brave")
            names = sorted(item.name for item in profiles)
            self.assertEqual(names, ["CF", "CF2", "工作"])
            self.assertTrue(all(item.user_data_dir == user_data for item in profiles))

    def test_parse_local_state_file_handles_missing_or_invalid(self) -> None:
        with TemporaryDirectory() as directory:
            user_data = Path(directory)
            missing = user_data / "Local State"
            self.assertEqual(parse_local_state_file(missing, browser="Brave"), [])
            broken = user_data / "Local State"
            broken.write_text("{ not json", encoding="utf-8")
            self.assertEqual(parse_local_state_file(broken, browser="Brave"), [])


class ManualBrowserCommandProfileDirectoryTests(unittest.TestCase):
    def test_profile_directory_appends_profile_switch(self) -> None:
        user_data = Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data")
        command = manual_browser_command(
            Path("brave.exe"),
            user_data,
            profile_directory="Profile 4",
        )
        self.assertIn("--profile-directory=Profile 4", command)
        self.assertIn(f"--user-data-dir={user_data}", command)
        self.assertIn("--no-first-run", command)
        self.assertIn("--no-default-browser-check", command)
        self.assertIn("--new-window", command)
        self.assertIn("--remote-debugging-port=0", command)
        # Worker id is unchanged: not added when omitted.
        self.assertFalse(any(part.startswith("--worker-id=") for part in command))

    def test_no_profile_directory_keeps_existing_command(self) -> None:
        """Without ``profile_directory`` the command must be unchanged from the
        pre-existing behaviour (existing tests still valid)."""
        command = manual_browser_command(Path("brave.exe"), Path("worker-3"))
        self.assertNotIn("--profile-directory", " ".join(command))
        self.assertIn("--user-data-dir=worker-3", command)
        self.assertIn("--remote-debugging-port=0", command)
        # Explicit default: 无痕模式 must never leak into a normal launch.
        self.assertNotIn("--incognito", command)

    def test_worker_id_and_profile_directory_coexist(self) -> None:
        """The worker-id switch is the VirtualBrowser path; named profiles are
        for Brave/Chrome. They are orthogonal, so the helper must allow both."""
        command = manual_browser_command(
            Path("chrome.exe"),
            Path("C:/.../User Data"),
            worker_id="3",
            profile_directory="Profile 4",
        )
        self.assertIn("--profile-directory=Profile 4", command)
        self.assertIn("--worker-id=3", command)
        # Worker-id path does NOT request a remote debugging port.
        self.assertNotIn("--remote-debugging-port=0", command)

    def test_incognito_appends_switch_and_keeps_command_shape(self) -> None:
        """无痕模式 keeps the isolated ``--user-data-dir`` (the manual-window
        tracker reads ``DevToolsActivePort`` from it) but appends
        ``--incognito`` so the window's cookies / local storage stay
        ephemeral."""
        isolated = Path(
            "C:/Users/me/AppData/Local/TemporaryEmailPickup/"
            "browser_profiles_brave/key"
        )
        command = manual_browser_command(
            Path("brave.exe"),
            isolated,
            incognito=True,
        )
        self.assertIn("--incognito", command)
        self.assertIn(f"--user-data-dir={isolated}", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertNotIn("--profile-directory", " ".join(command))
        # Studio URL stays the final argument.
        self.assertEqual(command[-1], STUDIO_URL)

    def test_incognito_false_by_default_without_profile_directory(self) -> None:
        """Passing nothing must keep the legacy command byte-for-byte
        identical: no ``--incognito`` even on the no-profile-directory
        path used by the isolated modes."""
        command = manual_browser_command(Path("brave.exe"), Path("worker-3"))
        self.assertNotIn("--incognito", command)
        self.assertNotIn("--incognito", " ".join(command))


if __name__ == "__main__":
    unittest.main()
