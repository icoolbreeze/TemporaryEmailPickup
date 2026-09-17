"""Loopback-only HTTP facade over Outlook OAuth2 IMAP for the Windows pickup app.

The Windows GUI cannot open ``outlook.office365.com:993`` (its Clash TUN kills
that TLS handshake), so this service — deployed on the Ubuntu VPS — performs
the IMAP work and exposes it over ``127.0.0.1`` only. The GUI reaches it
through an OpenSSH local forward, so tokens never leave the SSH tunnel.

Run: ``PICKUP_API_KEY=... python3 server.py`` (see outlook-pickup.service).
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests


def _load_outlook_imap():
    """Import ``outlook_imap`` whether run as a script or as a package module.

    On the VPS both files sit in the same directory; in the repo the shared
    module lives one level above ``pickup_service/``.
    """
    try:
        import outlook_imap

        return outlook_imap
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent):
        if str(candidate) not in sys.path:
            sys.path.append(str(candidate))
    import outlook_imap

    return outlook_imap


outlook_imap = _load_outlook_imap()

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 18793
MAX_BODY_BYTES = 1_000_000
DEFAULT_TOP = 50
MAX_TOP = 100
UNAUTHORIZED_BODY = {"ok": False, "error": "unauthorized"}
INTERNAL_ERROR_BODY = {"ok": False, "error": "服务器内部错误"}


class _BadRequest(ValueError):
    pass


class PickupHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], api_key: str) -> None:
        if not api_key:
            raise ValueError("PICKUP_API_KEY 不能为空")
        self.api_key = api_key
        self.request_count = 0
        self._count_lock = threading.Lock()
        super().__init__(server_address, PickupRequestHandler)

    def count_request(self) -> None:
        with self._count_lock:
            self.request_count += 1


class PickupRequestHandler(BaseHTTPRequestHandler):
    server_version = "OutlookPickup/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        # Request line + status only: bodies and headers carry refresh tokens.
        sys.stderr.write("[pickup] %s %s\n" % (self.address_string(), format % args))

    # -- plumbing ---------------------------------------------------------

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/v1/"):
            self._drain_request_body()
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._drain_request_body()
            self._send_json(401, UNAUTHORIZED_BODY)
            return
        handlers: dict[str, Callable[[dict], dict]] = {
            "/v1/validate": self._run_validate,
            "/v1/mail": self._run_mail,
            "/v1/eml": self._run_eml,
        }
        handler = handlers.get(path)
        if handler is None:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if isinstance(self.server, PickupHTTPServer):
            self.server.count_request()
        try:
            payload = self._read_json()
        except _BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            body = handler(payload)
        except _BadRequest as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        except outlook_imap.OutlookImapError as exc:
            # Expected OAuth/IMAP failure: HTTP 200 with a short Chinese reason.
            self._send_json(200, _failure_body(path, _clean(exc)))
            return
        except Exception as exc:  # unexpected bug: 5xx, details to stderr only
            sys.stderr.write(
                f"[pickup] {path} internal error: {type(exc).__name__}: {_clean(exc)}\n"
            )
            self._send_json(500, INTERNAL_ERROR_BODY)
            return
        self._send_json(200, body)

    def _authorized(self) -> bool:
        expected = getattr(self.server, "api_key", "")
        if not expected:
            return False
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            candidate = header[7:].strip()
            if candidate and hmac.compare_digest(candidate, expected):
                return True
        alternate = (self.headers.get("X-Pickup-Key") or "").strip()
        return bool(alternate) and hmac.compare_digest(alternate, expected)

    def _drain_request_body(self) -> None:
        """Read and discard an unconsumed request body before answering.

        The 401/404 paths never parse the body. Closing a socket that still
        has unread inbound data makes the peer see RST instead of the
        response we just sent, so drain it to keep 401s clean.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(length, MAX_BODY_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise _BadRequest("Content-Length 无效") from exc
        if length <= 0:
            raise _BadRequest("请求体为空")
        if length > MAX_BODY_BYTES:
            raise _BadRequest("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _BadRequest("请求体不是有效的 JSON") from exc
        if not isinstance(data, dict):
            raise _BadRequest("请求体必须是 JSON 对象")
        return data

    def _send_json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # urllib has no connection pool; without this it can read a
        # half-closed keep-alive socket as ConnectionResetError.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(raw)

    # -- endpoints --------------------------------------------------------

    def _run_validate(self, payload: dict) -> dict:
        email_value, client_id, refresh_token = self._account_fields(payload)
        with requests.Session() as session:
            rotated = outlook_imap.validate_mailbox(
                session, client_id, refresh_token, email_value
            )
        return {"ok": True, "mode": "IMAP", "new_refresh_token": rotated, "error": ""}

    def _run_mail(self, payload: dict) -> dict:
        email_value, client_id, refresh_token = self._account_fields(payload)
        top = _clamp_top(payload.get("top"))
        with requests.Session() as session:
            messages, rotated = outlook_imap.list_messages(
                session, client_id, refresh_token, email_value, top=top
            )
        return {
            "ok": True,
            "messages": [asdict(message) for message in messages],
            "new_refresh_token": rotated,
            "error": "",
        }

    def _run_eml(self, payload: dict) -> dict:
        email_value, client_id, refresh_token = self._account_fields(payload)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            raise _BadRequest("请求缺少 uid")
        with requests.Session() as session:
            source, rotated = outlook_imap.fetch_message_source(
                session, client_id, refresh_token, email_value, uid
            )
        return {
            "ok": True,
            "eml_base64": base64.b64encode(source).decode("ascii"),
            "new_refresh_token": rotated,
            "error": "",
        }

    @staticmethod
    def _account_fields(payload: dict) -> tuple[str, str, str]:
        email_value = str(payload.get("email") or "").strip()
        client_id = str(payload.get("client_id") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "")
        if not email_value or not client_id or not refresh_token:
            raise _BadRequest("请求缺少 email / client_id / refresh_token")
        return email_value, client_id, refresh_token


def _clamp_top(value: Any) -> int:
    try:
        top = int(value)
    except (TypeError, ValueError):
        top = DEFAULT_TOP
    return min(MAX_TOP, max(1, top))


def _failure_body(path: str, error: str) -> dict:
    body = {"ok": False, "new_refresh_token": "", "error": error}
    if path == "/v1/mail":
        body["messages"] = []
    elif path == "/v1/eml":
        body["eml_base64"] = ""
    else:
        body["mode"] = ""
    return body


def _clean(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or type(exc).__name__)[:300]


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def main() -> int:
    bind = _env("PICKUP_BIND", DEFAULT_BIND)
    raw_port = _env("PICKUP_PORT", str(DEFAULT_PORT))
    api_key = _env("PICKUP_API_KEY", "")
    if not api_key:
        print("缺少 PICKUP_API_KEY 环境变量，拒绝启动", file=sys.stderr)
        return 2
    try:
        port = int(raw_port)
    except ValueError:
        print(f"PICKUP_PORT 无效：{raw_port}", file=sys.stderr)
        return 2
    try:
        httpd = PickupHTTPServer((bind, port), api_key)
    except OSError as exc:
        print(f"无法监听 {bind}:{port}：{exc}", file=sys.stderr)
        return 2
    print(f"outlook-pickup listening on http://{bind}:{port}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
