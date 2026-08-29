import unittest
from unittest.mock import Mock, patch

from donut_browser import (
    DonutBrowserClient,
    DonutBrowserError,
    DonutProfile,
)


class DonutBrowserClientTests(unittest.TestCase):
    def make_client(self) -> DonutBrowserClient:
        return DonutBrowserClient(
            api_url="http://127.0.0.1:10108",
            token="secret",
            session=Mock(),
        )

    def test_existing_profile_is_reused_by_mailbox_tag(self) -> None:
        client = self.make_client()
        client.list_profiles = Mock(
            return_value=[
                {
                    "id": "profile-1",
                    "name": "mailbox",
                    "version": "149.0.7827.116",
                    "is_running": False,
                    "tags": ["temporary-email-pickup:mailbox-key"],
                }
            ]
        )
        client.get_profile = Mock(return_value=None)

        profile = client.ensure_profile(
            mailbox_key="mailbox-key",
            address="mailbox@example.com",
            preferred_id="deleted-profile",
        )

        self.assertEqual(profile.id, "profile-1")

    def test_profile_creation_uses_wayfern_and_stable_name(self) -> None:
        client = self.make_client()
        client.list_profiles = Mock(return_value=[])
        client._request = Mock(
            return_value={
                "profile": {
                    "id": "created-profile",
                    "name": "mailbox",
                    "version": "149.0.7827.116",
                    "is_running": False,
                }
            }
        )

        profile = client.ensure_profile(
            mailbox_key="mailbox-key",
            address="mailbox@example.com",
            preferred_id=None,
        )

        self.assertEqual(profile.id, "created-profile")
        payload = client._request.call_args.kwargs["payload"]
        self.assertEqual(payload["browser"], "wayfern")
        self.assertEqual(payload["version"], "latest")
        self.assertIn("mailbox-", payload["name"])
        self.assertNotIn("tags", payload)

    def test_run_accepts_local_0281_remote_debugging_field(self) -> None:
        client = self.make_client()
        profile = DonutProfile("profile-1", "mailbox", "149.0.7827.116", False)
        client.get_profile = Mock(return_value=profile)
        client._request = Mock(return_value={"remote_debugging_port": 45678})
        client._wait_for_debugger = Mock(return_value="149.0.7827.116")

        session = client.open_profile(
            profile,
            existing_debugger_address=None,
            headless=False,
        )

        self.assertEqual(session.debugger_address, "127.0.0.1:45678")
        self.assertEqual(session.browser_version, "149.0.7827.116")

    def test_run_also_accepts_new_cdp_port_field(self) -> None:
        client = self.make_client()
        profile = DonutProfile("profile-1", "mailbox", "149.0.7827.116", False)
        client.get_profile = Mock(return_value=profile)
        client._request = Mock(return_value={"cdp_port": 45679})
        client._wait_for_debugger = Mock(return_value=None)

        session = client.open_profile(
            profile,
            existing_debugger_address=None,
            headless=True,
        )

        self.assertEqual(session.debugger_address, "127.0.0.1:45679")
        self.assertTrue(session.headless)

    def test_running_profile_without_saved_cdp_is_not_killed(self) -> None:
        client = self.make_client()
        profile = DonutProfile("profile-1", "mailbox", "149.0.7827.116", True)
        client.get_profile = Mock(return_value=profile)

        with self.assertRaisesRegex(DonutBrowserError, "已经在运行"):
            client.open_profile(
                profile,
                existing_debugger_address=None,
                headless=False,
            )

    def test_paid_feature_error_is_explained(self) -> None:
        client = self.make_client()
        response = Mock(status_code=402, text="", content=b"")
        client.session.request.return_value = response

        with self.assertRaisesRegex(DonutBrowserError, "Pro"):
            client._request("POST", "/v1/profiles/profile-1/run", payload={})

    def test_live_saved_debugger_is_reused(self) -> None:
        client = self.make_client()
        profile = DonutProfile("profile-1", "mailbox", "149.0.7827.116", True)
        client._read_browser_version = Mock(return_value="149.0.7827.116")

        with patch("donut_browser.debugger_address_is_live", return_value=True):
            session = client.open_profile(
                profile,
                existing_debugger_address="127.0.0.1:45678",
                headless=False,
            )

        self.assertEqual(session.debugger_address, "127.0.0.1:45678")


if __name__ == "__main__":
    unittest.main()
