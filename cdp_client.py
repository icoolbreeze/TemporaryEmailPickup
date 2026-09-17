"""Minimal Chrome DevTools Protocol client for a plain Chromium window.

The browser is launched by the user (or by the app via ``subprocess.Popen``)
with ``--remote-debugging-port=0``. This module talks to it directly over the
DevTools HTTP endpoint and one page-target WebSocket.

Hard rules — Studio's invisible Turnstile fails the human check as soon as
ChromeDriver fingerprints appear:

* never send ``Runtime.enable`` (Cloudflare detects that CDP leak);
* never call ``Runtime.evaluate`` to click or fill (untrusted events and the
  CDP ``Runtime`` fingerprint); clicks are ``Input.dispatchMouseEvent`` at the
  element's box-model center and typing is ``DOM.focus`` + ``Input.insertText``;
* never inspect challenge iframes — captcha is detected on the main document
  only, with the same selectors the Selenium flow used.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any
from urllib.error import URLError

try:  # selenium already pulls websocket-client; the import stays lazy-safe.
    import websocket
except ImportError:  # pragma: no cover - exercised only on broken installs
    websocket = None


CDP_HTTP_TIMEOUT = 1.0
#: Alt is 1, Ctrl 2, Meta 4, Shift 8 — see Input.dispatchKeyEvent modifiers.
_CTRL_MODIFIER = 2


class CDPError(RuntimeError):
    """Raised when the DevTools endpoint or a CDP command fails."""


def list_page_targets(
    address: str, *, timeout: float = CDP_HTTP_TIMEOUT
) -> list[dict[str, Any]]:
    """Return page-type targets from ``http://<address>/json``.

    Each entry is the raw DevTools dict, so callers keep both ``url`` /
    ``title`` and ``webSocketDebuggerUrl``.
    """
    for path in ("/json", "/json/list"):
        url = f"http://{address}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        return [
            entry
            for entry in data
            if isinstance(entry, dict) and entry.get("type") == "page"
        ]
    return []


def find_page_target(address: str, url_fragment: str) -> dict[str, Any] | None:
    """Return the first page target whose URL contains ``url_fragment``."""
    fragment = url_fragment.lower()
    for entry in list_page_targets(address):
        if fragment in str(entry.get("url") or "").lower():
            return entry
    return None


class CDPClient:
    """One WebSocket connection to a single page target.

    Only ``DOM`` and ``Input`` domain commands are used; ``Runtime`` /
    ``Page`` domains are never enabled.
    """

    def __init__(self, ws_url: str, *, timeout: float = 10.0) -> None:
        if websocket is None:
            raise CDPError(
                "缺少 websocket-client，请运行：python -m pip install -r requirements.txt"
            )
        try:
            # suppress_origin: Chromium rejects WebSocket handshakes carrying
            # an Origin header on the browser-level DevTools endpoint.
            self._ws = websocket.create_connection(
                ws_url, timeout=timeout, suppress_origin=True
            )
        except Exception as exc:
            raise CDPError(f"无法连接浏览器调试端口：{exc}") from exc
        self._next_id = 0

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "CDPClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send(json.dumps(payload))
        while True:
            raw = self._ws.recv()
            if not raw:
                raise CDPError(f"{method}: 浏览器已关闭调试连接")
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            # Skip domain events; we never enable Runtime/Page, so only the
            # matching command reply is expected in practice.
            if data.get("id") != message_id:
                continue
            error = data.get("error")
            if error:
                raise CDPError(f"{method}: {error}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}

    # -- DOM -----------------------------------------------------------------

    def document(self) -> int:
        result = self.send("DOM.getDocument", {"depth": 0, "pierce": False})
        root = result.get("root") if isinstance(result, dict) else None
        return int((root or {}).get("nodeId") or 0)

    def query(self, selector: str, node_id: int | None = None) -> int:
        root_id = node_id or self.document()
        result = self.send(
            "DOM.querySelector", {"nodeId": root_id, "selector": selector}
        )
        return int(result.get("nodeId") or 0)

    def query_all(
        self, selector: str, node_id: int | None = None
    ) -> list[int]:
        root_id = node_id or self.document()
        result = self.send(
            "DOM.querySelectorAll", {"nodeId": root_id, "selector": selector}
        )
        return [int(value) for value in result.get("nodeIds", []) if value]

    def attributes(self, node_id: int) -> dict[str, str]:
        result = self.send("DOM.getAttributes", {"nodeId": node_id})
        values = result.get("attributes") or []
        return {
            str(name): str(value)
            for name, value in zip(values[0::2], values[1::2])
        }

    def outer_html(self, node_id: int) -> str:
        try:
            result = self.send("DOM.getOuterHTML", {"nodeId": node_id})
        except CDPError:
            return ""
        return str(result.get("outerHTML") or "")

    def box_model(self, node_id: int) -> dict[str, Any] | None:
        """Return the CDP box model for a laid-out, visible node, else None."""
        try:
            result = self.send("DOM.getBoxModel", {"nodeId": node_id})
        except CDPError:
            return None
        model = result.get("model") if isinstance(result, dict) else None
        if not isinstance(model, dict):
            return None
        try:
            if float(model.get("width") or 0) <= 0 or float(
                model.get("height") or 0
            ) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return model

    def focus(self, node_id: int) -> None:
        self.send("DOM.focus", {"nodeId": node_id})

    # -- Input ---------------------------------------------------------------

    def click_model(self, model: dict[str, Any]) -> bool:
        """Press and release the left mouse button at a box-model center."""
        content = model.get("content") or []
        if len(content) < 2:
            return False
        xs = [float(value) for value in content[0::2]]
        ys = [float(value) for value in content[1::2]]
        x = sum(xs) / len(xs)
        y = sum(ys) / len(ys)
        common = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self.send("Input.dispatchMouseEvent", {"type": "mousePressed", **common})
        self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **common})
        return True

    def click_node(self, node_id: int) -> bool:
        model = self.box_model(node_id)
        if model is None:
            return False
        return self.click_model(model)

    def click_at(self, x: float, y: float) -> None:
        common = {"x": float(x), "y": float(y), "button": "left", "clickCount": 1}
        self.send("Input.dispatchMouseEvent", {"type": "mousePressed", **common})
        self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **common})

    def _key(self, kind: str, key: str, code: str, key_code: int) -> None:
        self.send(
            "Input.dispatchKeyEvent",
            {
                "type": kind,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": key_code,
                "nativeVirtualKeyCode": key_code,
            },
        )

    def select_all(self) -> None:
        for kind in ("keyDown", "keyUp"):
            params = {
                "type": kind,
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
                "modifiers": _CTRL_MODIFIER,
            }
            self.send("Input.dispatchKeyEvent", params)

    def insert_text(self, text: str) -> None:
        self.send("Input.insertText", {"text": text})

    def fill_text(
        self, node_id: int, text: str, *, key_delay: float = 0.02
    ) -> None:
        """Focus the field and replace its value with ``text``.

        Every keystroke is a trusted ``Input.insertText`` event; no
        ``Runtime.evaluate`` ever touches ``input.value``.
        """
        self.focus(node_id)
        # Select whatever the field currently holds so the first inserted
        # character replaces it (the form is new, but retries may refill).
        self.select_all()
        for char in text:
            self.insert_text(char)
            if key_delay:
                time.sleep(key_delay)
