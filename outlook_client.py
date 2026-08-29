"""Outlook mailbox reader with Graph and OAuth2 IMAP fallback."""

from __future__ import annotations

import email
import imaplib
import json
import re
import threading
import time
import zipfile
from dataclasses import dataclass, field
from email.header import decode_header
from email.message import Message as EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from rootsh_client import InboxUpdate, Message


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
IMAP_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_SERVER = "outlook.office365.com"
IMAP_PORT = 993


class OutlookError(RuntimeError):
    pass


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


def _decode_header(value: str) -> str:
    output: list[str] = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            try:
                output.append(part.decode(charset or "utf-8", "replace"))
            except LookupError:
                output.append(part.decode("utf-8", "replace"))
        else:
            output.append(str(part))
    return "".join(output)


def _format_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


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
    base_url = GRAPH_BASE_URL

    def __init__(self, credentials: OutlookCredentials) -> None:
        self.credentials = credentials
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._graph_token = ""
        self._graph_expires_at = 0.0
        self._imap_token = ""
        self._imap_expires_at = 0.0
        self.mode = "自动"

    def clone(self) -> "OutlookClient":
        return OutlookClient(self.credentials)

    def close(self) -> None:
        self.session.close()

    def validate(self) -> str:
        try:
            self._graph_get(
                "/me/mailFolders/inbox",
                params={"$select": "id"},
            )
            self.mode = "Graph"
            return self.mode
        except (OutlookError, requests.RequestException) as graph_exc:
            try:
                connection = self._imap_connect()
            except Exception as imap_exc:
                raise OutlookError(
                    f"Graph：{graph_exc}；IMAP：{type(imap_exc).__name__} · {imap_exc}"
                ) from imap_exc
            try:
                self.mode = "IMAP"
                return self.mode
            finally:
                try:
                    connection.logout()
                except Exception:
                    pass

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
        response = self.session.get(
            f"{GRAPH_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {self._get_graph_token()}",
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
        connection = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
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

    def get_mail(self, address: str, cursor: int = 0, *, top: int = 50) -> InboxUpdate:
        try:
            update = self._get_mail_graph(address, cursor, top)
            self.mode = "Graph"
            return update
        except (OutlookError, requests.RequestException) as graph_exc:
            try:
                update = self._get_mail_imap(address, cursor, top)
            except Exception as imap_exc:
                raise OutlookError(
                    f"Graph：{graph_exc}；IMAP：{type(imap_exc).__name__} · {imap_exc}"
                ) from imap_exc
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
            return InboxUpdate(max(cursor, int(time.time())), address, tuple(messages))
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
