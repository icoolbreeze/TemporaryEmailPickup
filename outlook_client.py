"""Outlook mailbox reader with Graph and OAuth2 IMAP fallback."""

from __future__ import annotations

import base64
import email
import imaplib
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
import zipfile
from dataclasses import dataclass, field
from email.message import Message as EmailMessage
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

import requests

from outlook_imap import decode_header_value as _decode_header
from outlook_imap import format_size as _format_size
from outlook_imap import _is_transient_error, received_at_sort_key
from pickup_tunnel import DEFAULT_SSH_HOST, ensure_pickup_tunnel
from rootsh_client import InboxUpdate, Message


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
IMAP_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SERVER = "outlook.office365.com"
IMAP_PORT = 993


class OutlookError(RuntimeError):
    pass


# Thunderbird's public Azure client id only requests IMAP/POP/SMTP scopes, so its
# tokens can never call Microsoft Graph and must go straight to OAuth2 IMAP.
IMAP_ONLY_CLIENT_IDS = frozenset({"9e5f94bc-e8a4-4e73-b8be-63364c29d753"})

# Clash Fake-IP: DNS answers with a 198.18.x.x address, then the proxy kills 993.
CLASH_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")

CLASH_FAKE_IP_HINT = "令牌有效，但本机无法建立 Outlook IMAP TLS（993）。当前 DNS 将 outlook.office365.com 解析到代理 Fake-IP（198.18.x.x），IMAP 被掐断。请在 Clash 中将 outlook.office365.com / office365.com 直连，或换一个放行 993 的节点。"
CLASH_TLS_HINT = "令牌有效，但本机无法建立 Outlook IMAP TLS（993）。请检查本机代理或防火墙：请在 Clash 中将 outlook.office365.com / office365.com 设为直连（DIRECT），或换一个放行 993 的节点。"


@dataclass(frozen=True, slots=True)
class OutlookPickupConfig:
    """HTTP endpoint of the cloud pickup service, reached via an SSH tunnel."""

    base_url: str  # e.g. http://127.0.0.1:18793
    api_key: str
    ssh_host: str = DEFAULT_SSH_HOST


_default_pickup: OutlookPickupConfig | None = None
_default_pickup_lock = threading.Lock()


def set_default_pickup(config: OutlookPickupConfig | None) -> None:
    """Set the pickup endpoint used by every new ``OutlookClient``."""
    global _default_pickup
    with _default_pickup_lock:
        _default_pickup = config


def get_default_pickup() -> OutlookPickupConfig | None:
    with _default_pickup_lock:
        return _default_pickup


def _is_imap_only_client(client_id: str) -> bool:
    return str(client_id or "").strip().lower() in IMAP_ONLY_CLIENT_IDS


def _is_compact_msa_token(token: str) -> bool:
    """MSA compact tokens (``EwA…``, no dot separators) carry IMAP scopes only."""
    return bool(token) and token.startswith("Ew") and "." not in token


def _is_clash_fake_ip(host: str) -> bool:
    """Return True when ``host`` resolves into Clash's Fake-IP range (198.18.0.0/15)."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            raw = info[4][0]
        except (IndexError, TypeError):
            continue
        try:
            address = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if address in CLASH_FAKE_IP_NETWORK:
            return True
    return False


def _is_unexpected_eof(exc: BaseException) -> bool:
    if isinstance(exc, ssl.SSLEOFError):
        return True
    text = str(exc).lower()
    return "unexpected eof" in text or "unexpected_eof" in text


def _imap_tls_error(exc: BaseException) -> OutlookError | None:
    """Map an IMAP 993 TLS failure to one actionable Clash hint, else ``None``."""
    fake_ip = _is_clash_fake_ip(IMAP_SERVER)
    if not fake_ip and not _is_unexpected_eof(exc):
        return None
    error = OutlookError(CLASH_FAKE_IP_HINT if fake_ip else CLASH_TLS_HINT)
    error.imap_tls_mapped = True  # marker: complete message, safe to raise alone
    return error


def _is_tls_mapped_error(exc: BaseException) -> bool:
    return isinstance(exc, OutlookError) and bool(getattr(exc, "imap_tls_mapped", False))


def _is_pickup_mapped_error(exc: BaseException) -> bool:
    """True for pickup errors: complete, actionable message with 云端取件."""
    return isinstance(exc, OutlookError) and bool(getattr(exc, "pickup_mapped", False))


@dataclass(slots=True)
class OutlookCredentials:
    address: str
    password: str
    client_id: str
    refresh_token: str
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_secret_json(self) -> str:
        return json.dumps(
            {
                "password": self.password,
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_secret_json(cls, address: str, source: str) -> "OutlookCredentials":
        data = json.loads(source)
        return cls(
            address=address,
            password=str(data.get("password") or ""),
            client_id=str(data["client_id"]),
            refresh_token=str(data["refresh_token"]),
        )


def parse_outlook_import(source: str) -> list[OutlookCredentials]:
    """Parse ``email----password----client_id----refresh_token`` lines."""
    accounts: list[OutlookCredentials] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("----", 3)]
        if len(parts) != 4 or not all(parts):
            raise OutlookError(f"第 {line_number} 行格式错误，应包含四个非空字段")
        address, password, client_id, refresh_token = parts
        if "@" not in address or "." not in address.rsplit("@", 1)[-1]:
            raise OutlookError(f"第 {line_number} 行邮箱地址无效")
        normalized = address.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        accounts.append(
            OutlookCredentials(address, password, client_id, refresh_token)
        )
    if not accounts:
        raise OutlookError("没有找到可导入的 Outlook 账号")
    return accounts


def _mail_body(message: EmailMessage) -> str:
    if message.is_multipart():
        plain = ""
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/html", "text/plain"}:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, "replace")
            if content_type == "text/html":
                return text
            plain = plain or text
        return plain
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(message.get_content_charset() or "utf-8", "replace")
    return str(message.get_payload() or "")


class OutlookClient:
    timeout = 30.0
    pickup_timeout = 90.0
    base_url = GRAPH_BASE_URL

    def __init__(
        self,
        credentials: OutlookCredentials,
        pickup: OutlookPickupConfig | None = None,
    ) -> None:
        self.credentials = credentials
        self.pickup = pickup if pickup is not None else get_default_pickup()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.pickup_session = requests.Session()
        self._graph_token = ""
        self._graph_expires_at = 0.0
        self._imap_token = ""
        self._imap_expires_at = 0.0
        self.mode = "自动"

    def clone(self) -> "OutlookClient":
        cloned = OutlookClient(self.credentials, pickup=self.pickup)
        cloned.mode = self.mode
        return cloned

    def close(self) -> None:
        self.session.close()
        self.pickup_session.close()

    def _use_imap_first(self) -> bool:
        return self.mode == "IMAP" or _is_imap_only_client(self.credentials.client_id)

    @staticmethod
    def _raise_imap_fallback_error(graph_exc: BaseException, imap_exc: BaseException) -> NoReturn:
        if _is_tls_mapped_error(imap_exc) or _is_pickup_mapped_error(imap_exc):
            raise imap_exc
        raise OutlookError(
            f"Graph：{graph_exc}；IMAP：{type(imap_exc).__name__} · {imap_exc}"
        ) from imap_exc

    def validate(self) -> str:
        if self._use_imap_first():
            return self._validate_imap()
        try:
            self._graph_get(
                "/me/mailFolders/inbox",
                params={"$select": "id"},
            )
            self.mode = "Graph"
            return self.mode
        except (OutlookError, requests.RequestException) as graph_exc:
            try:
                self._validate_imap()
            except Exception as imap_exc:
                self._raise_imap_fallback_error(graph_exc, imap_exc)
            self.mode = "IMAP"
            return self.mode

    def _validate_imap(self) -> str:
        """Verify the IMAP path — through the cloud pickup when configured."""
        if self.pickup is not None:
            credentials = self.credentials
            self._pickup_post(
                "/v1/validate",
                {
                    "email": credentials.address,
                    "client_id": credentials.client_id,
                    "refresh_token": credentials.refresh_token,
                },
            )
        else:
            connection = self._imap_connect()
            try:
                connection.logout()
            except Exception:
                pass
        self.mode = "IMAP"
        return self.mode

    def _token_error(self, response: requests.Response, action: str) -> OutlookError:
        try:
            data = response.json()
            raw_error = data.get("error")
            if isinstance(raw_error, dict):
                code = str(raw_error.get("code") or "")
                detail = str(raw_error.get("message") or "")
            else:
                code = str(raw_error or "")
                detail = str(data.get("error_description") or "")
        except (ValueError, TypeError):
            code = ""
            detail = ""
        safe = " ".join(detail.split())[:240]
        message = f"{action}失败：HTTP {response.status_code}"
        if code:
            message += f" · {code}"
        if safe:
            message += f" · {safe}"
        return OutlookError(message)

    def _request_token(self, url: str, scope: str | None) -> tuple[str, int]:
        form = {
            "client_id": self.credentials.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.credentials.refresh_token,
        }
        if scope:
            form["scope"] = scope
        response = self.session.post(url, data=form, timeout=self.timeout)
        if response.status_code != 200:
            raise self._token_error(response, "OAuth 刷新")
        data = response.json()
        token = str(data.get("access_token") or "")
        if not token:
            raise OutlookError("OAuth 刷新失败：响应中没有 access_token")
        rotated = str(data.get("refresh_token") or "")
        if rotated:
            self.credentials.refresh_token = rotated
        return token, max(60, int(data.get("expires_in") or 3600))

    def _get_graph_token(self) -> str:
        if self._graph_token and time.monotonic() < self._graph_expires_at:
            return self._graph_token
        with self.credentials.lock:
            token, expires = self._request_token(
                GRAPH_TOKEN_URL,
                "https://graph.microsoft.com/.default",
            )
        self._graph_token = token
        self._graph_expires_at = time.monotonic() + expires - 30
        return token

    def _get_imap_token(self) -> str:
        if self._imap_token and time.monotonic() < self._imap_expires_at:
            return self._imap_token
        errors: list[str] = []
        attempts = (
            (LIVE_TOKEN_URL, None),
            (
                IMAP_TOKEN_URL,
                "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            ),
        )
        with self.credentials.lock:
            for url, scope in attempts:
                try:
                    token, expires = self._request_token(url, scope)
                except OutlookError as exc:
                    errors.append(str(exc))
                    continue
                self._imap_token = token
                self._imap_expires_at = time.monotonic() + expires - 30
                return token
        raise OutlookError("；".join(errors) or "无法获取 Outlook IMAP 访问令牌")

    def _graph_get(self, path: str, *, params: dict[str, Any] | None = None) -> requests.Response:
        token = self._get_graph_token()
        if _is_compact_msa_token(token):
            # MSA compact tokens only carry IMAP/POP/SMTP scopes; Graph would 401.
            raise OutlookError("OAuth 令牌为 MSA 紧凑令牌，没有 Mail.Read 权限，改用 IMAP")
        response = self.session.get(
            f"{GRAPH_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": "outlook.body-content-type='html'",
            },
            params=params,
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise self._token_error(response, "Microsoft Graph 请求")
        return response

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        token = self._get_imap_token()
        try:
            connection = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        except Exception as exc:
            mapped = _imap_tls_error(exc)
            if mapped is None:
                raise
            raise mapped from exc
        auth = (
            f"user={self.credentials.address}\x01auth=Bearer {token}\x01\x01"
        ).encode("utf-8")
        try:
            connection.authenticate("XOAUTH2", lambda _challenge: auth)
            status, _data = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise OutlookError("无法打开 Outlook 收件箱")
            return connection
        except Exception:
            try:
                connection.logout()
            except Exception:
                pass
            raise

    @staticmethod
    def _pickup_error(message: str) -> OutlookError:
        error = OutlookError(message)
        error.pickup_mapped = True  # marker: complete message, safe to raise alone
        return error

    def _ensure_pickup_tunnel(self) -> None:
        """Bring up the loopback SSH forward when the pickup URL is local."""
        config = self.pickup
        if config is None:
            return
        parsed = urlsplit(config.base_url)
        host = (parsed.hostname or "").strip().lower()
        if host not in {"127.0.0.1", "localhost"}:
            return
        port = parsed.port or 18793
        ensure_pickup_tunnel(config.ssh_host, port, port)

    def _pickup_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one pickup API call; applies a rotated refresh token on success."""
        config = self.pickup
        if config is None:
            raise self._pickup_error("云端取件服务未配置")
        url = config.base_url.rstrip("/") + path
        body = dict(payload)
        last_error: OutlookError | None = None
        for attempt in range(3):
            try:
                self._ensure_pickup_tunnel()
            except Exception as exc:
                last_error = self._pickup_error(
                    "云端取件 SSH 隧道启动失败："
                    + " ".join(str(exc).split())[:200]
                    + "。请确认本机可以 ssh beike-server。"
                )
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise last_error from exc
            body["refresh_token"] = self.credentials.refresh_token
            try:
                response = self.pickup_session.post(
                    url,
                    json=body,
                    headers={"Authorization": f"Bearer {config.api_key}"},
                    timeout=self.pickup_timeout,
                )
            except requests.RequestException as exc:
                detail = " ".join(str(exc).split())[:200]
                last_error = self._pickup_error(
                    "云端取件服务连接失败："
                    + (detail or type(exc).__name__)
                    + f"。请确认 SSH 隧道与服务器上的 outlook-pickup 服务正在运行（{url}）。"
                )
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise last_error from exc
            if response.status_code == 401:
                raise self._pickup_error(
                    "云端取件服务认证失败（HTTP 401）：本机 API Key 与服务器不一致，"
                    "请检查 OUTLOOK_PICKUP_KEY 或重新运行 pickup_service/deploy.py。"
                )
            if response.status_code != 200:
                last_error = self._pickup_error(
                    f"云端取件服务错误：HTTP {response.status_code}"
                )
                if attempt < 2 and response.status_code in {502, 503, 504}:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise last_error
            try:
                data = response.json()
            except ValueError as exc:
                raise self._pickup_error("云端取件服务返回了无效响应（非 JSON）") from exc
            if not isinstance(data, dict):
                raise self._pickup_error("云端取件服务返回了无效响应")
            rotated = str(data.get("new_refresh_token") or "")
            if rotated:
                with self.credentials.lock:
                    self.credentials.refresh_token = rotated
            if not data.get("ok"):
                error_text = str(data.get("error") or "").strip() or "未知错误"
                last_error = self._pickup_error(f"云端取件服务返回错误：{error_text}")
                if attempt < 2 and _is_transient_error(last_error):
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise last_error
            return data
        assert last_error is not None
        raise last_error

    def _get_mail_pickup(self, address: str, cursor: int, top: int) -> InboxUpdate:
        credentials = self.credentials
        data = self._pickup_post(
            "/v1/mail",
            {
                "email": address or credentials.address,
                "client_id": credentials.client_id,
                "refresh_token": credentials.refresh_token,
                "top": min(100, max(1, int(top))),
            },
        )
        messages: list[Message] = []
        for raw in data.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            message_id = str(raw.get("message_id") or "")
            if not message_id:
                continue
            messages.append(
                Message(
                    sender_name=str(raw.get("sender_name") or ""),
                    sender_address=str(raw.get("sender_address") or ""),
                    subject=str(raw.get("subject") or "(无主题)"),
                    received_at=str(raw.get("received_at") or ""),
                    message_id=message_id,
                    size=str(raw.get("size") or "—"),
                )
            )
        messages.sort(
            key=lambda item: received_at_sort_key(item.received_at), reverse=True
        )
        return InboxUpdate(max(cursor, int(time.time())), address, tuple(messages))

    def _fetch_imap_source_pickup(self, uid: str) -> bytes:
        credentials = self.credentials
        data = self._pickup_post(
            "/v1/eml",
            {
                "email": credentials.address,
                "client_id": credentials.client_id,
                "refresh_token": credentials.refresh_token,
                "uid": uid,
            },
        )
        encoded = str(data.get("eml_base64") or "")
        if not encoded:
            raise self._pickup_error("云端取件服务返回错误：邮件内容为空")
        try:
            return base64.b64decode(encoded)
        except ValueError as exc:
            raise self._pickup_error("云端取件服务返回的邮件内容无法解码") from exc

    def get_mail(self, address: str, cursor: int = 0, *, top: int = 50) -> InboxUpdate:
        if self._use_imap_first():
            update = self._get_mail_imap(address, cursor, top)
            self.mode = "IMAP"
            return update
        try:
            update = self._get_mail_graph(address, cursor, top)
            self.mode = "Graph"
            return update
        except (OutlookError, requests.RequestException) as graph_exc:
            try:
                update = self._get_mail_imap(address, cursor, top)
            except Exception as imap_exc:
                self._raise_imap_fallback_error(graph_exc, imap_exc)
            self.mode = "IMAP"
            return update

    def _get_mail_graph(self, address: str, cursor: int, top: int) -> InboxUpdate:
        response = self._graph_get(
            "/me/mailFolders/inbox/messages",
            params={
                "$top": min(100, max(1, top)),
                "$select": "id,subject,from,receivedDateTime",
                "$orderby": "receivedDateTime desc",
            },
        )
        messages: list[Message] = []
        for raw in response.json().get("value", []):
            sender = (raw.get("from") or {}).get("emailAddress") or {}
            message_id = str(raw.get("id") or "")
            if not message_id:
                continue
            messages.append(
                Message(
                    sender_name=str(sender.get("name") or ""),
                    sender_address=str(sender.get("address") or ""),
                    subject=str(raw.get("subject") or "(无主题)"),
                    received_at=str(raw.get("receivedDateTime") or ""),
                    message_id="graph:" + message_id,
                    size="—",
                )
            )
        return InboxUpdate(max(cursor, int(time.time())), address, tuple(messages))

    def _get_mail_imap(self, address: str, cursor: int, top: int) -> InboxUpdate:
        if self.pickup is not None:
            return self._get_mail_pickup(address, cursor, top)
        connection = self._imap_connect()
        try:
            status, data = connection.uid("search", None, "ALL")
            if status != "OK" or not data:
                raise OutlookError("Outlook IMAP 搜索邮件失败")
            uids = data[0].split()[-top:][::-1]
            messages: list[Message] = []
            for uid in uids:
                status, rows = connection.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not rows or not isinstance(rows[0], tuple):
                    continue
                raw_source = rows[0][1]
                parsed = email.message_from_bytes(raw_source)
                sender_name, sender_address = parseaddr(_decode_header(parsed.get("From", "")))
                messages.append(
                    Message(
                        sender_name=sender_name,
                        sender_address=sender_address,
                        subject=_decode_header(parsed.get("Subject", "(无主题)")),
                        received_at=str(parsed.get("Date", "")),
                        message_id="imap:" + uid.decode("ascii", "ignore"),
                        size=_format_size(len(raw_source)),
                    )
                )
            messages.sort(
                key=lambda item: received_at_sort_key(item.received_at),
                reverse=True,
            )
            return InboxUpdate(
                max(cursor, int(time.time())), address, tuple(messages[:top])
            )
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    @staticmethod
    def _split_message_id(message_id: str) -> tuple[str, str]:
        if ":" not in message_id:
            raise OutlookError("无法识别 Outlook 邮件 ID")
        return tuple(message_id.split(":", 1))  # type: ignore[return-value]

    def fetch_message_html(self, address: str, message_id: str) -> str:
        provider, raw_id = self._split_message_id(message_id)
        if provider == "graph":
            response = self._graph_get(
                f"/me/messages/{quote(raw_id, safe='')}" ,
                params={"$select": "body"},
            )
            return str((response.json().get("body") or {}).get("content") or "")
        source = self._fetch_imap_source(raw_id)
        return _mail_body(email.message_from_bytes(source))

    def _fetch_imap_source(self, uid: str) -> bytes:
        if self.pickup is not None:
            return self._fetch_imap_source_pickup(uid)
        connection = self._imap_connect()
        try:
            status, rows = connection.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not rows or not isinstance(rows[0], tuple):
                raise OutlookError("Outlook IMAP 读取邮件失败")
            return rows[0][1]
        finally:
            try:
                connection.logout()
            except Exception:
                pass

    def download_message(self, address: str, message_id: str, destination: Path) -> None:
        provider, raw_id = self._split_message_id(message_id)
        if provider == "graph":
            response = self._graph_get(f"/me/messages/{quote(raw_id, safe='')}/$value")
            destination.write_bytes(response.content)
        else:
            destination.write_bytes(self._fetch_imap_source(raw_id))

    def download_mailbox(self, address: str, destination: Path) -> None:
        update = self.get_mail(address, 0, top=100)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, message in enumerate(update.messages, start=1):
                provider, raw_id = self._split_message_id(message.message_id)
                if provider == "graph":
                    response = self._graph_get(
                        f"/me/messages/{quote(raw_id, safe='')}/$value"
                    )
                    source = response.content
                else:
                    source = self._fetch_imap_source(raw_id)
                safe_subject = re.sub(r"[^\w.-]+", "_", message.subject)[:60] or "message"
                archive.writestr(f"{index:03d}_{safe_subject}.eml", source)

    def delete_messages(self, message_ids: list[str]) -> tuple[str, ...]:
        raise OutlookError("Outlook 导入账号当前使用只读 Mail.Read 权限，不能删除远程邮件")
