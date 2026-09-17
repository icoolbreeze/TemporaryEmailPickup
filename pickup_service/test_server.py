from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from pickup_service import server
from outlook_imap import ImapMessage, OutlookImapError

TEST_KEY = "unit-test-key-abcdef"
SAMPLE_MESSAGE = ImapMessage(
    sender_name="Creative Fabrica",
    sender_address="otp@example.com",
    subject="7196 - Verify your Creative Fabrica account",
    received_at="2026-07-14T12:00:00Z",
    message_id="imap:424242",
    size="1.2 KB",
)


class PickupServerTests(unittest.TestCase):
    httpd: server.PickupHTTPServer
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = server.PickupHTTPServer(("127.0.0.1", 0), TEST_KEY)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    # -- helpers ----------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        key: str | None = TEST_KEY,
        extra_headers: dict | None = None,
    ) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = dict(extra_headers or {})
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, body: dict | None = None, **kwargs) -> tuple[int, dict]:
        return self._request(
            "POST",
            "/v1/validate",
            body if body is not None else self._validate_body(),
            **kwargs,
        )

    @staticmethod
    def _validate_body() -> dict:
        return {
            "email": "demo@outlook.com",
            "client_id": "client-id",
            "refresh_token": "refresh-token",
        }

    # -- health & auth ----------------------------------------------------

    def test_health_needs_no_key(self) -> None:
        status, body = self._request("GET", "/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})

    def test_unknown_path_returns_404(self) -> None:
        status, body = self._request("GET", "/nope", key=TEST_KEY)
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_missing_key_returns_401(self) -> None:
        status, body = self._post(key=None)
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_wrong_key_returns_401(self) -> None:
        status, body = self._post(key="not-the-key")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_x_pickup_key_header_is_accepted(self) -> None:
        body = self._validate_body()
        with mock.patch.object(
            server.outlook_imap, "validate_mailbox", return_value=""
        ) as validate_mock:
            status, payload = self._request(
                "POST",
                "/v1/validate",
                body,
                key=None,
                extra_headers={"X-Pickup-Key": TEST_KEY},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        validate_mock.assert_called_once()
        # A wrong X-Pickup-Key is still rejected.
        status, _payload = self._request(
            "POST",
            "/v1/validate",
            body,
            key=None,
            extra_headers={"X-Pickup-Key": "wrong"},
        )
        self.assertEqual(status, 401)

    # -- /v1/validate -----------------------------------------------------

    def test_validate_success(self) -> None:
        with mock.patch.object(
            server.outlook_imap, "validate_mailbox", return_value="rotated-token"
        ) as validate_mock:
            status, body = self._post()
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            {
                "ok": True,
                "mode": "IMAP",
                "new_refresh_token": "rotated-token",
                "error": "",
            },
        )
        self.assertEqual(
            validate_mock.call_args.args,
            (mock.ANY, "client-id", "refresh-token", "demo@outlook.com"),
        )

    def test_validate_imap_failure_is_200_ok_false(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "validate_mailbox",
            side_effect=OutlookImapError("Outlook IMAP 认证失败：AUTHENTICATE failed"),
        ):
            status, body = self._post()
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["mode"], "")
        self.assertEqual(body["new_refresh_token"], "")
        self.assertIn("认证失败", body["error"])

    def test_validate_unexpected_bug_is_500(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "validate_mailbox",
            side_effect=RuntimeError("boom"),
        ):
            status, body = self._post()
        self.assertEqual(status, 500)
        self.assertEqual(body, {"ok": False, "error": "服务器内部错误"})

    def test_missing_fields_return_400(self) -> None:
        status, body = self._post({"email": "demo@outlook.com"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_invalid_json_returns_400(self) -> None:
        url = f"http://127.0.0.1:{self.port}/v1/validate"
        request = urllib.request.Request(
            url,
            data=b"{not json",
            method="POST",
            headers={"Authorization": f"Bearer {TEST_KEY}"},
        )
        try:
            urllib.request.urlopen(request, timeout=5)
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            body = json.loads(exc.read().decode("utf-8"))
        self.assertFalse(body["ok"])

    # -- /v1/mail ---------------------------------------------------------

    def test_mail_success_maps_messages(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "list_messages",
            return_value=([SAMPLE_MESSAGE], "rotated"),
        ) as list_mock:
            status, body = self._request(
                "POST", "/v1/mail", {**self._validate_body(), "top": 3}
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["new_refresh_token"], "rotated")
        self.assertEqual(body["messages"][0]["message_id"], "imap:424242")
        self.assertEqual(body["messages"][0]["sender_name"], "Creative Fabrica")
        self.assertEqual(body["messages"][0]["size"], "1.2 KB")
        self.assertEqual(list_mock.call_args.kwargs["top"], 3)

    def test_mail_top_defaults_and_clamps(self) -> None:
        with mock.patch.object(
            server.outlook_imap, "list_messages", return_value=([], "")
        ) as list_mock:
            self._request("POST", "/v1/mail", self._validate_body())
            self.assertEqual(list_mock.call_args.kwargs["top"], 50)
            self._request(
                "POST", "/v1/mail", {**self._validate_body(), "top": 500}
            )
            self.assertEqual(list_mock.call_args.kwargs["top"], 100)
            self._request("POST", "/v1/mail", {**self._validate_body(), "top": 0})
            self.assertEqual(list_mock.call_args.kwargs["top"], 1)
            self._request(
                "POST", "/v1/mail", {**self._validate_body(), "top": "not-a-number"}
            )
            self.assertEqual(list_mock.call_args.kwargs["top"], 50)

    def test_mail_imap_failure_keeps_messages_key(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "list_messages",
            side_effect=OutlookImapError("OAuth 刷新失败：HTTP 400 · invalid_grant"),
        ):
            status, body = self._request("POST", "/v1/mail", self._validate_body())
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["messages"], [])
        self.assertIn("invalid_grant", body["error"])

    # -- /v1/eml ----------------------------------------------------------

    def test_eml_success_returns_base64(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "fetch_message_source",
            return_value=(b"Subject: hi\r\n\r\nbody", ""),
        ) as fetch_mock:
            status, body = self._request(
                "POST", "/v1/eml", {**self._validate_body(), "uid": "424242"}
            )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        import base64

        self.assertEqual(base64.b64decode(body["eml_base64"]), b"Subject: hi\r\n\r\nbody")
        self.assertEqual(fetch_mock.call_args.args[-1], "424242")

    def test_eml_imap_failure_is_200_ok_false(self) -> None:
        with mock.patch.object(
            server.outlook_imap,
            "fetch_message_source",
            side_effect=OutlookImapError("邮件 UID 无效"),
        ):
            status, body = self._request(
                "POST", "/v1/eml", {**self._validate_body(), "uid": "abc"}
            )
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual(body["eml_base64"], "")
        self.assertIn("UID", body["error"])

    def test_eml_requires_uid(self) -> None:
        status, body = self._request("POST", "/v1/eml", self._validate_body())
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    # -- main() guards ----------------------------------------------------

    def test_main_requires_api_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "PICKUP_API_KEY"}
        with mock.patch.object(server.os, "environ", env):
            self.assertEqual(server.main(), 2)

    def test_main_rejects_bad_port(self) -> None:
        env = dict(os.environ)
        env["PICKUP_API_KEY"] = "some-key"
        env["PICKUP_PORT"] = "not-a-port"
        with mock.patch.object(server.os, "environ", env):
            self.assertEqual(server.main(), 2)


if __name__ == "__main__":
    unittest.main()
