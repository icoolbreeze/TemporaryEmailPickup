from __future__ import annotations

import json
import unittest

from outlook_client import OutlookClient, OutlookCredentials, OutlookError, parse_outlook_import
from secret_store import protect_secret, unprotect_secret


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
        self.token_form: dict | None = None
        self.get_params: dict | None = None
        self.get_url = ""

    def post(self, _url: str, *, data: dict, timeout: float) -> FakeResponse:
        self.token_form = data
        return FakeResponse(200, {"access_token": "access", "expires_in": 3600})

    def get(self, _url: str, **_kwargs) -> FakeResponse:
        self.get_url = _url
        self.get_params = _kwargs.get("params")
        return FakeResponse(
            200,
            {
                "value": [
                    {
                        "id": "message-1",
                        "subject": "7196 - Verify your Creative Fabrica account",
                        "from": {"emailAddress": {"name": "Creative Fabrica", "address": "otp@example.com"}},
                        "receivedDateTime": "2026-07-14T12:00:00Z",
                    }
                ]
            },
        )

    def close(self) -> None:
        return None


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


if __name__ == "__main__":
    unittest.main()
