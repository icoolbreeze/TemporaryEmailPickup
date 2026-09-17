import json
import unittest
from unittest.mock import Mock, patch

import cdp_client
from cdp_client import CDPClient, CDPError, list_page_targets


BOX_MODEL = {
    "width": 100,
    "height": 20,
    # Rectangle from (10,10) to (30,20); center must be (20,15).
    "content": [10, 10, 30, 10, 30, 20, 10, 20],
}


class FakeWebSocket:
    """Record every CDP command and reply with per-method canned results."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.methods: list[str] = []
        self.closed = False

    def send(self, raw: str) -> None:
        message = json.loads(raw)
        self.sent.append(message)
        self.methods.append(message["method"])

    def recv(self) -> str:
        message = self.sent[-1]
        method = message["method"]
        result: object = {}
        if method == "DOM.getDocument":
            result = {"root": {"nodeId": 1}}
        elif method == "DOM.querySelector":
            result = {"nodeId": 5}
        elif method == "DOM.querySelectorAll":
            result = {"nodeIds": [5, 6]}
        elif method == "DOM.getAttributes":
            result = {"attributes": ["aria-label", "Log in"]}
        elif method == "DOM.getOuterHTML":
            result = {"outerHTML": "<button>Log in</button>"}
        elif method == "DOM.getBoxModel":
            result = {"model": BOX_MODEL}
        return json.dumps({"id": message["id"], "result": result})

    def close(self) -> None:
        self.closed = True


def make_client() -> tuple[CDPClient, FakeWebSocket]:
    fake = FakeWebSocket()
    with patch("cdp_client.websocket.create_connection", return_value=fake):
        client = CDPClient("ws://127.0.0.1:9222/fake")
    return client, fake


class CDPClientCommandTests(unittest.TestCase):
    def test_command_ids_increment(self) -> None:
        client, fake = make_client()
        client.document()
        client.document()
        client.document()
        self.assertEqual([message["id"] for message in fake.sent], [1, 2, 3])

    def test_query_uses_dom_query_selector(self) -> None:
        client, fake = make_client()
        node_id = client.query("input[name='email']")
        self.assertEqual(node_id, 5)
        message = next(
            item for item in fake.sent if item["method"] == "DOM.querySelector"
        )
        self.assertEqual(
            message["params"],
            {"nodeId": 1, "selector": "input[name='email']"},
        )
        # No Runtime domain command is needed to resolve a CSS selector.
        self.assertFalse(
            any(method.startswith("Runtime.") for method in fake.methods)
        )

    def test_cdp_error_is_raised(self) -> None:
        fake = FakeWebSocket()
        fake.recv = lambda: json.dumps(
            {"id": fake.sent[-1]["id"] if fake.sent else 1, "error": {"message": "boom"}}
        )

        with patch("cdp_client.websocket.create_connection", return_value=fake):
            client = CDPClient("ws://127.0.0.1:9222/fake")
            with self.assertRaises(CDPError):
                client.document()

    def test_close_closes_the_socket(self) -> None:
        client, fake = make_client()
        client.close()
        self.assertTrue(fake.closed)

    def test_context_manager_closes_socket(self) -> None:
        fake = FakeWebSocket()
        with patch("cdp_client.websocket.create_connection", return_value=fake):
            with CDPClient("ws://127.0.0.1:9222/fake"):
                pass
        self.assertTrue(fake.closed)


class CDPClientInputTests(unittest.TestCase):
    def test_click_dispatches_mouse_press_and_release_at_center(self) -> None:
        client, fake = make_client()
        clicked = client.click_model(BOX_MODEL)
        self.assertTrue(clicked)
        mouse_messages = [
            message
            for message in fake.sent
            if message["method"] == "Input.dispatchMouseEvent"
        ]
        self.assertEqual(
            [message["params"]["type"] for message in mouse_messages],
            ["mousePressed", "mouseReleased"],
        )
        for message in mouse_messages:
            params = message["params"]
            self.assertEqual(params["x"], 20.0)
            self.assertEqual(params["y"], 15.0)
            self.assertEqual(params["button"], "left")
            self.assertEqual(params["clickCount"], 1)
        # The hard rule: clicks never go through Runtime.evaluate.
        self.assertNotIn("Runtime.evaluate", fake.methods)
        self.assertNotIn("Runtime.enable", fake.methods)

    def test_click_node_uses_its_box_model(self) -> None:
        client, fake = make_client()
        self.assertTrue(client.click_node(9))
        self.assertIn("DOM.getBoxModel", fake.methods)
        self.assertIn("Input.dispatchMouseEvent", fake.methods)

    def test_fill_uses_focus_and_trusted_insert_text_without_runtime(self) -> None:
        client, fake = make_client()
        with patch("cdp_client.time.sleep"):
            client.fill_text(7, "mailbox@example.com")
        self.assertIn("DOM.focus", fake.methods)
        insert_messages = [
            message
            for message in fake.sent
            if message["method"] == "Input.insertText"
        ]
        # One trusted insert per character; concatenated they spell the value.
        self.assertEqual(
            "".join(message["params"]["text"] for message in insert_messages),
            "mailbox@example.com",
        )
        # Existing content is selected first so the first keystroke replaces it.
        key_messages = [
            message
            for message in fake.sent
            if message["method"] == "Input.dispatchKeyEvent"
        ]
        self.assertEqual(
            [message["params"]["type"] for message in key_messages[:2]],
            ["keyDown", "keyUp"],
        )
        self.assertEqual(key_messages[0]["params"]["key"], "a")
        self.assertEqual(key_messages[0]["params"]["modifiers"], 2)
        # Never, under any circumstances:
        self.assertNotIn("Runtime.enable", fake.methods)
        self.assertNotIn("Runtime.evaluate", fake.methods)

    def test_fill_never_touches_challenge_iframes(self) -> None:
        client, fake = make_client()
        with patch("cdp_client.time.sleep"):
            client.fill_text(7, "secret")
        selectors = [
            message["params"].get("selector", "")
            for message in fake.sent
            if message["method"].startswith("DOM.")
        ]
        self.assertFalse(
            any("challenges.cloudflare.com" in selector for selector in selectors)
        )
        self.assertFalse(any("cf-turnstile" in selector for selector in selectors))


class PageTargetTests(unittest.TestCase):
    def test_list_page_targets_keeps_ws_url_and_filters_types(self) -> None:
        payload = json.dumps(
            [
                {
                    "type": "page",
                    "title": "Studio AI",
                    "url": "https://studio.creativefabrica.com/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1",
                },
                {"type": "browser", "title": "", "url": ""},
                {
                    "type": "page",
                    "title": "DevTools",
                    "url": "devtools://devtools/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/2",
                },
            ]
        ).encode("utf-8")

        response = Mock()
        response.read.return_value = payload
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        with patch("cdp_client.urllib.request.urlopen", return_value=response):
            targets = list_page_targets("127.0.0.1:9222")

        self.assertEqual(len(targets), 2)
        self.assertEqual(
            targets[0]["webSocketDebuggerUrl"],
            "ws://127.0.0.1:9222/devtools/page/1",
        )

    def test_list_page_targets_returns_empty_without_endpoint(self) -> None:
        with patch(
            "cdp_client.urllib.request.urlopen", side_effect=OSError("refused")
        ):
            self.assertEqual(list_page_targets("127.0.0.1:9222"), [])


if __name__ == "__main__":
    unittest.main()
