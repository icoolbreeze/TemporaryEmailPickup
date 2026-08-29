from __future__ import annotations

import unittest
import time

from app import ManagedMailbox
from rootsh_client import RootshClient, encode_mailbox_path, html_to_text, parse_delay


class FakeResponse:
    status_code = 200
    apparent_encoding = "utf-8"
    encoding = "utf-8"

    def __init__(self, data: dict | None = None, text: str = "") -> None:
        self._data = data or {}
        self.text = text
        self.content = text.encode()

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return self.responses.pop(0)

    def post(self, *_args, **_kwargs) -> FakeResponse:
        return self.responses.pop(0)

    def close(self) -> None:
        return None


class ClientTests(unittest.TestCase):
    def test_managed_mailboxes_keep_independent_state(self) -> None:
        first = ManagedMailbox("one", object(), "one@bccto.cc", "one", "bccto.cc", time.monotonic() + 60)
        second = ManagedMailbox("two", object(), "two@bccto.cc", "two", "bccto.cc", time.monotonic() + 120)
        first.cursor = 99
        first.status = "监听中"
        self.assertEqual(second.cursor, 0)
        self.assertEqual(second.status, "等待收件")
        self.assertGreaterEqual(second.remaining_seconds, first.remaining_seconds)

    def test_delay_and_path_encoding(self) -> None:
        self.assertEqual(parse_delay("09:31"), 571)
        self.assertEqual(parse_delay("600"), 600)
        self.assertEqual(encode_mailbox_path("abc@bccto.cc"), "abc))^^)bccto+=_+=cc")

    def test_html_to_text_ignores_scripts(self) -> None:
        value = html_to_text("<h1>验证码</h1><p>123 456</p><script>bad()</script>")
        self.assertEqual(value, "验证码\n123 456")

    def test_session_cookies_can_be_persisted(self) -> None:
        source = RootshClient()
        source.session.cookies.set("PHPSESSID", "session-token", domain="rootsh.com", path="/")
        saved = source.export_cookies()

        restored = RootshClient()
        restored.import_cookies(saved)
        self.assertEqual(restored.session.cookies.get("PHPSESSID"), "session-token")
        source.close()
        restored.close()

    def test_apply_and_get_mail(self) -> None:
        client = RootshClient()
        client.session.close()
        client.session = FakeSession(
            [
                FakeResponse({"success": "true", "user": "demo@bccto.cc", "delay": "10:00", "tips": ""}),
                FakeResponse(
                    {
                        "success": "true",
                        "time": 123,
                        "to": "demo@bccto.cc",
                        "mail": [["Alice", "alice@example.com", "Hello", "12:30", "fid-1", "2 KB"]],
                    }
                ),
            ]
        )
        mailbox = client.apply_mailbox("demo", "bccto.cc")
        update = client.get_mail(mailbox.address)
        self.assertEqual(mailbox.lifetime_seconds, 600)
        self.assertEqual(update.cursor, 123)
        self.assertEqual(update.messages[0].subject, "Hello")
        self.assertEqual(update.messages[0].sender, "Alice <alice@example.com>")


if __name__ == "__main__":
    unittest.main()
