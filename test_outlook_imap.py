from __future__ import annotations

import unittest
from unittest import mock

from outlook_imap import (
    OutlookImapError,
    imap_connect,
    list_messages,
    received_at_sort_key,
)


class ReceivedAtSortTests(unittest.TestCase):
    def test_newer_iso_sorts_after_older_rfc2822(self) -> None:
        self.assertGreater(
            received_at_sort_key("2024-06-02T10:00:00Z"),
            received_at_sort_key("Sat, 01 Jun 2024 10:00:00 +0000"),
        )


class ImapConnectRetryTests(unittest.TestCase):
    def test_examine_not_connected_retries_select(self) -> None:
        connection = mock.Mock()
        connection.select.side_effect = [
            ("NO", [b"User is authenticated but not connected."]),
            ("OK", [b"3"]),
        ]
        with mock.patch("outlook_imap.imaplib.IMAP4_SSL", return_value=connection):
            with mock.patch("outlook_imap.time.sleep"):
                result = imap_connect("demo@hotmail.com", "token")
        self.assertIs(result, connection)
        self.assertEqual(connection.select.call_count, 2)
        self.assertTrue(connection.select.call_args_list[0].kwargs.get("readonly", True))
        self.assertFalse(connection.select.call_args_list[1].kwargs.get("readonly", True))

    def test_not_connected_reconnects_then_succeeds(self) -> None:
        failing = mock.Mock()
        failing.select.return_value = (
            "NO",
            [b"User is authenticated but not connected."],
        )
        succeeding = mock.Mock()
        succeeding.select.return_value = ("OK", [b"1"])
        with mock.patch(
            "outlook_imap.imaplib.IMAP4_SSL", side_effect=[failing, succeeding]
        ):
            with mock.patch("outlook_imap.time.sleep"):
                result = imap_connect("demo@hotmail.com", "token")
        self.assertIs(result, succeeding)
        failing.logout.assert_called()

    def test_other_select_errors_do_not_retry(self) -> None:
        connection = mock.Mock()
        connection.select.return_value = ("NO", [b"Mailbox does not exist"])
        with mock.patch("outlook_imap.imaplib.IMAP4_SSL", return_value=connection):
            with mock.patch("outlook_imap.time.sleep") as slept:
                with self.assertRaises(Exception) as ctx:
                    imap_connect("demo@hotmail.com", "token")
        self.assertIn("无法打开 Outlook 收件箱", str(ctx.exception))
        slept.assert_not_called()


class ImapOperationRetryTests(unittest.TestCase):
    def test_list_messages_retries_not_connected_then_succeeds(self) -> None:
        empty = mock.Mock()
        empty.uid.return_value = ("OK", [b""])
        failing = OutlookImapError(
            "Outlook IMAP 认证成功但未能打开收件箱（User is authenticated but not connected）。"
        )
        session = mock.Mock()
        with mock.patch(
            "outlook_imap.refresh_access_token",
            return_value=("access", 3600, "rotated"),
        ):
            with mock.patch(
                "outlook_imap.imap_connect", side_effect=[failing, failing, empty]
            ) as connect:
                with mock.patch("outlook_imap.time.sleep"):
                    messages, rotated = list_messages(
                        session, "client", "refresh", "demo@hotmail.com"
                    )
        self.assertEqual(messages, [])
        self.assertEqual(rotated, "rotated")
        self.assertEqual(connect.call_count, 3)

    def test_list_messages_does_not_retry_invalid_grant(self) -> None:
        session = mock.Mock()
        with mock.patch(
            "outlook_imap.refresh_access_token",
            side_effect=OutlookImapError("OAuth 刷新失败：HTTP 400 · invalid_grant"),
        ):
            with mock.patch("outlook_imap.time.sleep") as slept:
                with self.assertRaises(OutlookImapError) as ctx:
                    list_messages(session, "client", "refresh", "demo@hotmail.com")
        self.assertIn("invalid_grant", str(ctx.exception))
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
