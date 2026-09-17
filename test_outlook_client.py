from __future__ import annotations

import base64
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from outlook_imap import received_at_sort_key
from outlook_client import (
    IMAP_PORT,
    IMAP_SERVER,
    IMAP_ONLY_CLIENT_IDS,
    LIVE_TOKEN_URL,
    OutlookClient,
    OutlookCredentials,
    OutlookError,
    OutlookPickupConfig,
    _imap_tls_error,
    _is_clash_fake_ip,
    _is_compact_msa_token,
    _is_imap_only_client,
    parse_outlook_import,
    set_default_pickup,
)
from rootsh_client import InboxUpdate, Message
from secret_store import protect_secret, unprotect_secret


THUNDERBIRD_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# Pinned verbatim so the wording of the Fake-IP hint cannot drift unnoticed.
CLASH_FAKE_IP_HINT_EXACT = "令牌有效，但本机无法建立 Outlook IMAP TLS（993）。当前 DNS 将 outlook.office365.com 解析到代理 Fake-IP（198.18.x.x），IMAP 被掐断。请在 Clash 中将 outlook.office365.com / office365.com 直连，或换一个放行 993 的节点。"


class FakeResponse:
    def __init__(self, status_code: int, data: dict, content: bytes = b"") -> None:
        self.status_code = status_code
        self._data = data
        self.content = content

    def json(self) -> dict:
        return self._data


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.token_payload = {"access_token": "access", "expires_in": 3600}
        self.token_urls: list[str] = []
        self.token_form: dict | None = None
        self.get_status = 200
        self.get_payload: dict = {
            "value": [
                {
                    "id": "message-1",
                    "subject": "7196 - Verify your Creative Fabrica account",
                    "from": {"emailAddress": {"name": "Creative Fabrica", "address": "otp@example.com"}},
                    "receivedDateTime": "2026-07-14T12:00:00Z",
                }
            ]
        }
        self.get_params: dict | None = None
        self.get_url = ""

    def post(self, url: str, *, data: dict, timeout: float) -> FakeResponse:
        self.token_urls.append(url)
        self.token_form = data
        return FakeResponse(200, dict(self.token_payload))

    def get(self, _url: str, **_kwargs) -> FakeResponse:
        self.get_url = _url
        self.get_params = _kwargs.get("params")
        return FakeResponse(self.get_status, self.get_payload)

    def close(self) -> None:
        return None


def _fake_getaddrinfo(ip: str):
    def resolver(_host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return resolver


def _make_client(client_id: str, fake: FakeSession) -> OutlookClient:
    credentials = OutlookCredentials(
        "demo@outlook.com", "password", client_id, "refresh-token"
    )
    client = OutlookClient(credentials)
    client.session.close()
    client.session = fake
    return client


def _imap_update(address: str) -> InboxUpdate:
    return InboxUpdate(
        0,
        address,
        (
            Message(
                sender_name="Creative Fabrica",
                sender_address="otp@example.com",
                subject="7196 - Verify your Creative Fabrica account",
                received_at="2026-07-14T12:00:00Z",
                message_id="imap:424242",
                size="1.2 KB",
            ),
        ),
    )


class OutlookClientTests(unittest.TestCase):
    def test_import_format(self) -> None:
        accounts = parse_outlook_import(
            "demo@outlook.com----password----client-id----refresh-token"
        )
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].address, "demo@outlook.com")
        self.assertEqual(accounts[0].client_id, "client-id")

    def test_import_rejects_incomplete_line(self) -> None:
        with self.assertRaises(OutlookError):
            parse_outlook_import("demo@outlook.com----password")

    def test_graph_message_mapping(self) -> None:
        credentials = OutlookCredentials(
            "demo@outlook.com", "password", "client-id", "refresh-token"
        )
        client = OutlookClient(credentials)
        client.session.close()
        fake = FakeSession()
        client.session = fake
        update = client.get_mail(credentials.address, top=1)
        self.assertEqual(client.mode, "Graph")
        self.assertEqual(update.messages[0].message_id, "graph:message-1")
        self.assertEqual(update.messages[0].size, "—")
        self.assertEqual(fake.token_form["grant_type"], "refresh_token")
        self.assertNotIn("size", fake.get_params["$select"])

    def test_validation_does_not_list_messages(self) -> None:
        credentials = OutlookCredentials(
            "demo@outlook.com", "password", "client-id", "refresh-token"
        )
        client = OutlookClient(credentials)
        client.session.close()
        fake = FakeSession()
        client.session = fake
        self.assertEqual(client.validate(), "Graph")
        self.assertTrue(fake.get_url.endswith("/me/mailFolders/inbox"))
        self.assertEqual(fake.get_params, {"$select": "id"})

    def test_dpapi_round_trip(self) -> None:
        source = json.dumps(
            {"password": "not-plain", "refresh_token": "sensitive-token"}
        )
        encrypted = protect_secret(source)
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertNotIn("sensitive-token", encrypted)
        self.assertEqual(unprotect_secret(encrypted), source)


class OutlookImapFallbackTests(unittest.TestCase):
    def test_thunderbird_client_id_is_imap_only(self) -> None:
        self.assertIn(THUNDERBIRD_CLIENT_ID, IMAP_ONLY_CLIENT_IDS)
        self.assertTrue(_is_imap_only_client(THUNDERBIRD_CLIENT_ID))
        self.assertTrue(_is_imap_only_client(THUNDERBIRD_CLIENT_ID.upper()))
        self.assertFalse(_is_imap_only_client("client-id"))

    def test_compact_msa_token_detector(self) -> None:
        self.assertTrue(_is_compact_msa_token("EwAcompacttoken"))
        self.assertFalse(_is_compact_msa_token("access"))
        self.assertFalse(_is_compact_msa_token("a.b.c"))
        self.assertFalse(_is_compact_msa_token(""))

    def test_thunderbird_validate_skips_graph(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        dummy = mock.Mock()
        with mock.patch.object(client, "_imap_connect", return_value=dummy):
            self.assertEqual(client.validate(), "IMAP")
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(fake.get_url, "")
        self.assertIsNone(fake.token_form)
        dummy.logout.assert_called_once()

    def test_thunderbird_get_mail_uses_imap(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        update = _imap_update(client.credentials.address)
        with mock.patch.object(client, "_get_mail_imap", return_value=update) as imap_mock:
            result = client.get_mail(client.credentials.address)
        self.assertIs(result, update)
        self.assertEqual(result.messages[0].message_id, "imap:424242")
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(fake.get_url, "")
        self.assertIsNone(fake.token_form)
        imap_mock.assert_called_once()

    def test_generic_client_keeps_graph_first(self) -> None:
        fake = FakeSession()
        client = _make_client("client-id", fake)
        update = client.get_mail(client.credentials.address, top=1)
        self.assertEqual(client.mode, "Graph")
        self.assertTrue(fake.get_url.endswith("/me/mailFolders/inbox/messages"))
        self.assertEqual(update.messages[0].message_id, "graph:message-1")

    def test_compact_msa_token_falls_through_to_imap(self) -> None:
        fake = FakeSession()
        fake.token_payload = {"access_token": "EwAcompacttoken", "expires_in": 3600}
        client = _make_client("client-id", fake)
        update = _imap_update(client.credentials.address)
        with mock.patch.object(client, "_get_mail_imap", return_value=update):
            result = client.get_mail(client.credentials.address)
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(result.messages[0].message_id, "imap:424242")
        self.assertEqual(fake.get_url, "")
        self.assertIsNotNone(fake.token_form)

    def test_compact_msa_token_validate_uses_imap(self) -> None:
        fake = FakeSession()
        fake.token_payload = {"access_token": "EwAcompacttoken", "expires_in": 3600}
        client = _make_client("client-id", fake)
        dummy = mock.Mock()
        with mock.patch.object(client, "_imap_connect", return_value=dummy):
            self.assertEqual(client.validate(), "IMAP")
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(fake.get_url, "")

    def test_is_clash_fake_ip(self) -> None:
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo("198.18.0.221"),
        ):
            self.assertTrue(_is_clash_fake_ip("outlook.office365.com"))
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo("52.96.1.1"),
        ):
            self.assertFalse(_is_clash_fake_ip("outlook.office365.com"))
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=socket.gaierror(-2, "name resolution failure"),
        ):
            self.assertFalse(_is_clash_fake_ip("outlook.office365.com"))

    def test_imap_tls_error_eof_mentions_993_and_direct(self) -> None:
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo("52.96.1.1"),
        ):
            error = _imap_tls_error(ssl.SSLEOFError(-1, "Unexpected EOF"))
        self.assertIsInstance(error, OutlookError)
        self.assertIn("993", str(error))
        self.assertIn("直连", str(error))
        self.assertIn("放行 993", str(error))
        self.assertNotIn("Fake-IP", str(error))
        self.assertNotIn("198.18", str(error))

    def test_imap_tls_error_fake_ip_uses_exact_message(self) -> None:
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo("198.18.0.221"),
        ):
            error = _imap_tls_error(ssl.SSLEOFError(-1, "Unexpected EOF"))
        self.assertIsInstance(error, OutlookError)
        self.assertEqual(str(error), CLASH_FAKE_IP_HINT_EXACT)

    def test_imap_tls_error_ignores_other_failures(self) -> None:
        with mock.patch(
            "outlook_client.socket.getaddrinfo",
            side_effect=_fake_getaddrinfo("52.96.1.1"),
        ):
            self.assertIsNone(_imap_tls_error(OSError("connection refused")))

    def test_imap_connect_maps_ssl_eof(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        with mock.patch.object(client, "_get_imap_token", return_value="tok"):
            with mock.patch(
                "outlook_client.imaplib.IMAP4_SSL",
                side_effect=ssl.SSLEOFError(-1, "Unexpected EOF"),
            ) as ctor:
                with mock.patch(
                    "outlook_client.socket.getaddrinfo",
                    side_effect=_fake_getaddrinfo("198.18.0.221"),
                ):
                    with self.assertRaises(OutlookError) as ctx:
                        client._imap_connect()
        message = str(ctx.exception)
        self.assertIn("993", message)
        self.assertIn("直连", message)
        self.assertNotIsInstance(ctx.exception, ssl.SSLEOFError)
        ctor.assert_called_once_with(IMAP_SERVER, IMAP_PORT)

    def test_imap_token_refresh_order_keeps_live_first(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        dummy = mock.MagicMock()
        dummy.select.return_value = ("OK", [b"INBOX"])
        with mock.patch("outlook_client.imaplib.IMAP4_SSL", return_value=dummy):
            self.assertEqual(client.validate(), "IMAP")
        self.assertEqual(fake.token_urls, [LIVE_TOKEN_URL])
        self.assertNotIn("scope", fake.token_form or {})

    def test_tls_hint_is_sole_message_after_graph_attempt(self) -> None:
        fake = FakeSession()
        fake.get_status = 401
        fake.get_payload = {
            "error": {
                "code": "InvalidAuthenticationToken",
                "message": "CompactToken parsing failed",
            }
        }
        client = _make_client("client-id", fake)
        with mock.patch(
            "outlook_client.imaplib.IMAP4_SSL",
            side_effect=ssl.SSLEOFError(-1, "Unexpected EOF"),
        ):
            with mock.patch(
                "outlook_client.socket.getaddrinfo",
                side_effect=_fake_getaddrinfo("198.18.0.221"),
            ):
                with self.assertRaises(OutlookError) as ctx:
                    client.get_mail(client.credentials.address)
        message = str(ctx.exception)
        self.assertIn("993", message)
        self.assertNotIn("Graph：", message)

    def test_clone_preserves_imap_mode(self) -> None:
        client = _make_client("client-id", FakeSession())
        client.mode = "IMAP"
        cloned = client.clone()
        self.assertEqual(cloned.mode, "IMAP")
        self.assertIs(cloned.credentials, client.credentials)

    def test_clone_preserves_graph_mode(self) -> None:
        client = _make_client("client-id", FakeSession())
        client.mode = "Graph"
        self.assertEqual(client.clone().mode, "Graph")

    def test_cloned_imap_client_does_not_retry_graph(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        client.mode = "IMAP"
        cloned = client.clone()
        cloned.session.close()
        cloned.session = fake
        update = _imap_update(cloned.credentials.address)
        with mock.patch.object(cloned, "_get_mail_imap", return_value=update):
            cloned.get_mail(cloned.credentials.address)
        self.assertEqual(cloned.mode, "IMAP")
        self.assertEqual(fake.get_url, "")

    def test_imap_mode_validate_skips_graph(self) -> None:
        fake = FakeSession()
        client = _make_client("client-id", fake)
        client.mode = "IMAP"
        dummy = mock.Mock()
        with mock.patch.object(client, "_imap_connect", return_value=dummy):
            self.assertEqual(client.validate(), "IMAP")
        self.assertEqual(fake.get_url, "")
        self.assertIsNone(fake.token_form)

    def test_imap_only_validate_failure_has_no_graph_prefix(self) -> None:
        fake = FakeSession()
        client = _make_client(THUNDERBIRD_CLIENT_ID, fake)
        with mock.patch(
            "outlook_client.imaplib.IMAP4_SSL",
            side_effect=ssl.SSLEOFError(-1, "Unexpected EOF"),
        ):
            with mock.patch(
                "outlook_client.socket.getaddrinfo",
                side_effect=_fake_getaddrinfo("198.18.0.221"),
            ):
                with self.assertRaises(OutlookError) as ctx:
                    client.validate()
        message = str(ctx.exception)
        self.assertNotIn("Graph：", message)
        self.assertIn("993", message)
        self.assertEqual(fake.get_url, "")


class FakePickupResponse:
    def __init__(self, status_code: int, data: dict) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> dict:
        return self._data


class PickupTransport:
    """Stands in for ``requests.Session.request`` at the HTTP layer."""

    def __init__(self, responder) -> None:
        self.calls: list[dict] = []
        self._responder = responder

    def as_request(self):
        """Plain function for ``mock.patch("requests.Session.request")``.

        As a class attribute it binds like a method, so the first positional
        argument is the session instance.
        """
        transport = self

        def request(session, method=None, url=None, **kwargs):
            call = {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers") or {},
                "timeout": kwargs.get("timeout"),
            }
            transport.calls.append(call)
            return transport._responder(call)

        return request


def _pickup_ok(body: dict):
    return lambda _call: FakePickupResponse(200, body)


def _pickup_status(status_code: int, body: dict):
    return lambda _call: FakePickupResponse(status_code, body)


PICKUP_MESSAGES_PAYLOAD = {
    "ok": True,
    "messages": [
        {
            "sender_name": "Creative Fabrica",
            "sender_address": "otp@example.com",
            "subject": "7196 - Verify your Creative Fabrica account",
            "received_at": "2026-07-14T12:00:00Z",
            "message_id": "imap:424242",
            "size": "1.2 KB",
        }
    ],
    "new_refresh_token": "",
    "error": "",
}


class OutlookPickupTests(unittest.TestCase):
    def setUp(self) -> None:
        # Never talk to a live tunnel from unit tests.
        set_default_pickup(None)
        self._tunnel_patch = mock.patch(
            "outlook_client.ensure_pickup_tunnel", return_value=0
        )
        self._ensure_mock = self._tunnel_patch.start()

    def tearDown(self) -> None:
        self._tunnel_patch.stop()
        set_default_pickup(None)

    def _pickup_client(
        self,
        responder,
        *,
        client_id: str = THUNDERBIRD_CLIENT_ID,
        base_url: str = "http://127.0.0.1:18793/",
    ) -> tuple[OutlookClient, PickupTransport]:
        credentials = OutlookCredentials(
            "demo@outlook.com", "password", client_id, "refresh-token"
        )
        client = OutlookClient(
            credentials, pickup=OutlookPickupConfig(base_url, "pickup-key")
        )
        transport = PickupTransport(responder)
        return client, transport

    def test_pickup_reestablishes_tunnel_before_post(self) -> None:
        client, transport = self._pickup_client(
            _pickup_ok(
                {"ok": True, "mode": "IMAP", "new_refresh_token": "", "error": ""}
            )
        )
        with mock.patch("requests.Session.request", new=transport.as_request()):
            self.assertEqual(client.validate(), "IMAP")
        self._ensure_mock.assert_called()

    def test_validate_posts_to_cloud_and_applies_new_token(self) -> None:
        client, transport = self._pickup_client(
            _pickup_ok(
                {"ok": True, "mode": "IMAP", "new_refresh_token": "rotated-token", "error": ""}
            )
        )
        imap_ctor = mock.Mock()
        with mock.patch("requests.Session.request", new=transport.as_request()):
            with mock.patch("outlook_client.imaplib.IMAP4_SSL", imap_ctor):
                self.assertEqual(client.validate(), "IMAP")
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(
            [call["url"] for call in transport.calls],
            ["http://127.0.0.1:18793/v1/validate"],
        )
        self.assertEqual(
            transport.calls[0]["json"],
            {
                "email": "demo@outlook.com",
                "client_id": THUNDERBIRD_CLIENT_ID,
                "refresh_token": "refresh-token",
            },
        )
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer pickup-key")
        self.assertNotIn("refresh-token", json.dumps(headers))
        self.assertEqual(client.credentials.refresh_token, "rotated-token")
        imap_ctor.assert_not_called()

    def test_get_mail_posts_to_cloud_and_maps_imap_ids(self) -> None:
        client, transport = self._pickup_client(_pickup_ok(PICKUP_MESSAGES_PAYLOAD))
        with mock.patch("requests.Session.request", new=transport.as_request()):
            update = client.get_mail(client.credentials.address, top=3)
        self.assertEqual(client.mode, "IMAP")
        self.assertEqual(update.messages[0].message_id, "imap:424242")
        self.assertEqual(update.messages[0].sender_name, "Creative Fabrica")
        self.assertEqual(update.messages[0].size, "1.2 KB")
        self.assertEqual(transport.calls[0]["url"], "http://127.0.0.1:18793/v1/mail")
        self.assertEqual(transport.calls[0]["json"]["top"], 3)
        # Empty rotation from the server must not clobber the current token.
        self.assertEqual(client.credentials.refresh_token, "refresh-token")

    def test_mail_top_is_clamped(self) -> None:
        client, transport = self._pickup_client(_pickup_ok(PICKUP_MESSAGES_PAYLOAD))
        with mock.patch("requests.Session.request", new=transport.as_request()):
            client.get_mail(client.credentials.address, top=500)
            self.assertEqual(transport.calls[0]["json"]["top"], 100)
            client.get_mail(client.credentials.address, top=0)
            self.assertEqual(transport.calls[1]["json"]["top"], 1)

    def test_fetch_eml_posts_uid_without_prefix(self) -> None:
        client, transport = self._pickup_client(
            _pickup_ok(
                {
                    "ok": True,
                    "eml_base64": base64.b64encode(b"Subject: hi\r\n\r\nbody").decode("ascii"),
                    "new_refresh_token": "",
                    "error": "",
                }
            )
        )
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "message.eml"
            with mock.patch("requests.Session.request", new=transport.as_request()):
                client.download_message(
                    client.credentials.address, "imap:424242", destination
                )
            self.assertEqual(destination.read_bytes(), b"Subject: hi\r\n\r\nbody")
        self.assertEqual(transport.calls[0]["url"], "http://127.0.0.1:18793/v1/eml")
        self.assertEqual(transport.calls[0]["json"]["uid"], "424242")

    def test_pickup_401_maps_to_cloud_error(self) -> None:
        client, transport = self._pickup_client(
            _pickup_status(401, {"ok": False, "error": "unauthorized"})
        )
        with mock.patch("requests.Session.request", new=transport.as_request()):
            with self.assertRaises(OutlookError) as ctx:
                client.validate()
        self.assertIn("云端取件", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))

    def test_pickup_connection_error_maps_to_cloud_error(self) -> None:
        client, _transport = self._pickup_client(
            _pickup_status(200, {"ok": True, "new_refresh_token": "", "error": ""})
        )

        def fail(_session, *args, **kwargs):
            raise requests.exceptions.ConnectionError("connection refused")

        with mock.patch("requests.Session.request", new=fail):
            with mock.patch("outlook_client.time.sleep"):
                with self.assertRaises(OutlookError) as ctx:
                    client.get_mail(client.credentials.address)
        self.assertIn("云端取件", str(ctx.exception))

    def test_pickup_server_error_is_sole_message_after_graph_failure(self) -> None:
        fake = FakeSession()
        fake.get_status = 401
        fake.get_payload = {
            "error": {"code": "InvalidAuthenticationToken", "message": "CompactToken"}
        }
        credentials = OutlookCredentials(
            "demo@outlook.com", "password", "client-id", "refresh-token"
        )
        client = OutlookClient(
            credentials, pickup=OutlookPickupConfig("http://127.0.0.1:18793", "pickup-key")
        )
        client.session.close()
        client.session = fake
        transport = PickupTransport(_pickup_status(503, {"ok": False}))
        with mock.patch("requests.Session.request", new=transport.as_request()):
            with self.assertRaises(OutlookError) as ctx:
                client.get_mail(client.credentials.address)
        message = str(ctx.exception)
        self.assertIn("云端取件", message)
        self.assertNotIn("Graph：", message)

    def test_pickup_business_error_mentions_cloud(self) -> None:
        client, transport = self._pickup_client(
            _pickup_ok(
                {"ok": False, "new_refresh_token": "", "error": "OAuth 刷新失败：HTTP 400 · invalid_grant"}
            )
        )
        with mock.patch("requests.Session.request", new=transport.as_request()):
            with self.assertRaises(OutlookError) as ctx:
                client.validate()
        message = str(ctx.exception)
        self.assertIn("云端取件", message)
        self.assertIn("invalid_grant", message)

    def test_default_pickup_used_by_constructor_and_clone(self) -> None:
        credentials = OutlookCredentials(
            "demo@outlook.com", "password", THUNDERBIRD_CLIENT_ID, "refresh-token"
        )
        config = OutlookPickupConfig("http://127.0.0.1:18793", "pickup-key")
        set_default_pickup(config)
        client = OutlookClient(credentials)
        self.assertIs(client.pickup, config)
        self.assertIs(client.clone().pickup, config)
        set_default_pickup(None)
        self.assertIsNone(OutlookClient(credentials).pickup)

    def test_pickup_mail_is_sorted_newest_first(self) -> None:
        client, transport = self._pickup_client(
            _pickup_ok(
                {
                    "ok": True,
                    "new_refresh_token": "",
                    "error": "",
                    "messages": [
                        {
                            "sender_name": "Old",
                            "sender_address": "old@example.com",
                            "subject": "older",
                            "received_at": "Mon, 01 Jan 2024 08:00:00 +0000",
                            "message_id": "imap:1",
                            "size": "1 KB",
                        },
                        {
                            "sender_name": "New",
                            "sender_address": "new@example.com",
                            "subject": "newer",
                            "received_at": "Wed, 03 Jan 2024 18:00:00 +0000",
                            "message_id": "imap:2",
                            "size": "1 KB",
                        },
                        {
                            "sender_name": "Mid",
                            "sender_address": "mid@example.com",
                            "subject": "middle",
                            "received_at": "2024-01-02T12:00:00Z",
                            "message_id": "imap:3",
                            "size": "1 KB",
                        },
                    ],
                }
            )
        )
        with mock.patch("requests.Session.request", new=transport.as_request()):
            update = client.get_mail(client.credentials.address)
        self.assertEqual(
            [item.subject for item in update.messages],
            ["newer", "middle", "older"],
        )


class MailTimeSortTests(unittest.TestCase):
    def test_iso_is_newer_than_rfc2822(self) -> None:
        newer = received_at_sort_key("2024-06-02T10:00:00Z")
        older = received_at_sort_key("Sat, 01 Jun 2024 10:00:00 +0000")
        self.assertGreater(newer, older)

    def test_empty_is_oldest(self) -> None:
        self.assertEqual(received_at_sort_key(""), 0.0)
        self.assertLess(received_at_sort_key(""), received_at_sort_key("2024-01-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
