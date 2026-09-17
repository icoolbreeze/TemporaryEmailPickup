"""Outlook OAuth2 IMAP access shared by the cloud pickup service and the GUI.

The module is deliberately standalone: it must import cleanly on the Ubuntu VPS
(next to ``pickup_service/server.py``), so it never imports ``rootsh_client``,
``app.py`` or tkinter. The local :mod:`outlook_client` reuses only the header /
size helpers; token refresh and IMAP live here for the cloud side.
"""

from __future__ import annotations

import email
import imaplib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime

import requests

LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
IMAP_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
IMAP_TOKEN_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_SERVER = "outlook.office365.com"
IMAP_FALLBACK_SERVER = "imap-mail.outlook.com"
IMAP_PORT = 993
DEFAULT_TIMEOUT = 30.0
IMAP_CONNECT_ATTEMPTS = 3
IMAP_OPERATION_ATTEMPTS = 5
NOT_CONNECTED_HINT = "authenticated but not connected"
_TRANSIENT_NEEDLES = (
    "authenticated but not connected",
    "连接失败",
    "timed out",
    "timeout",
    "temporarily",
    "unavailable",
    "unexpected eof",
    "connection reset",
    "connection aborted",
    "broken pipe",
    " bye",
    "server closed",
    "搜索邮件失败",
    "读取邮件失败",
    "未能打开收件箱",
)

# Serialise refresh-token rotation: MSA refresh tokens rotate on use, so two
# concurrent requests trading the same token would invalidate one result.
_token_lock = threading.Lock()


class OutlookImapError(RuntimeError):
    """OAuth/IMAP failure carrying a short, user-facing Chinese message."""


@dataclass(frozen=True, slots=True)
class ImapMessage:
    """Same fields as ``rootsh_client.Message`` for an IMAP message."""

    sender_name: str
    sender_address: str
    subject: str
    received_at: str
    message_id: str
    size: str


def decode_header_value(value: str) -> str:
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


def format_size(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def received_at_sort_key(value: str) -> float:
    """Parse Graph ISO, IMAP Date, or rootsh timestamps into a comparable epoch."""
    text = " ".join(str(value or "").split())
    if not text:
        return 0.0
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError, IndexError):
            return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _describe(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    if not text:
        text = type(exc).__name__
    return text[:200]


def _quiet_logout(connection: imaplib.IMAP4_SSL) -> None:
    try:
        connection.logout()
    except Exception:
        pass


def _token_error_text(response: requests.Response) -> str:
    code = ""
    detail = ""
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
        pass
    safe = " ".join(detail.split())[:240]
    message = f"OAuth 刷新失败：HTTP {response.status_code}"
    if code:
        message += f" · {code}"
    if safe:
        message += f" · {safe}"
    return message


def refresh_access_token(
    session: requests.Session,
    client_id: str,
    refresh_token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[str, int, str]:
    """Exchange a refresh token for an IMAP access token.

    Same order as the local client: the Live endpoint without a scope first,
    then the consumer Azure AD endpoint with the IMAP scope. Returns
    ``(access_token, expires_in, rotated_refresh_token)``; the rotated token is
    empty when Microsoft did not rotate it.
    """
    errors: list[str] = []
    attempts = ((LIVE_TOKEN_URL, None), (IMAP_TOKEN_URL, IMAP_TOKEN_SCOPE))
    with _token_lock:
        for url, scope in attempts:
            form: dict[str, str] = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if scope:
                form["scope"] = scope
            try:
                response = session.post(url, data=form, timeout=timeout)
            except requests.RequestException as exc:
                errors.append(f"OAuth 刷新网络错误：{_describe(exc)}")
                continue
            if response.status_code != 200:
                errors.append(_token_error_text(response))
                continue
            try:
                data = response.json()
            except ValueError:
                errors.append("OAuth 刷新失败：响应不是 JSON")
                continue
            token = str(data.get("access_token") or "")
            if not token:
                errors.append("OAuth 刷新失败：响应中没有 access_token")
                continue
            try:
                expires = max(60, int(data.get("expires_in") or 3600))
            except (TypeError, ValueError):
                expires = 3600
            rotated = str(data.get("refresh_token") or "")
            return token, expires, rotated
    raise OutlookImapError("；".join(errors) or "无法获取 Outlook IMAP 访问令牌")


def _not_connected(*parts: object) -> bool:
    blob = " ".join(str(part) for part in parts if part is not None).lower()
    return NOT_CONNECTED_HINT in blob


def _is_transient_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(needle in text for needle in _TRANSIENT_NEEDLES)


def _run_with_retry(work):
    """Retry transient Microsoft IMAP failures across a whole pickup call."""
    last_error: OutlookImapError | None = None
    for attempt in range(IMAP_OPERATION_ATTEMPTS):
        try:
            return work()
        except OutlookImapError as exc:
            last_error = exc
            if not _is_transient_error(exc) or attempt >= IMAP_OPERATION_ATTEMPTS - 1:
                raise
            time.sleep(0.8 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _open_inbox(connection: imaplib.IMAP4_SSL, *, readonly: bool) -> tuple[str, object]:
    """Warm the Microsoft IMAP session, then EXAMINE/SELECT INBOX."""
    try:
        connection.capability()
    except Exception:
        pass
    try:
        connection.list()
    except Exception as exc:
        if _not_connected(exc):
            raise
    return connection.select("INBOX", readonly=readonly)


def _imap_connect_once(
    host: str,
    address: str,
    access_token: str,
    *,
    timeout: float,
) -> imaplib.IMAP4_SSL:
    auth = f"user={address}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
    try:
        connection = imaplib.IMAP4_SSL(host, IMAP_PORT, timeout=timeout)
    except Exception as exc:
        raise OutlookImapError(f"Outlook IMAP 连接失败：{_describe(exc)}") from exc
    try:
        connection.authenticate("XOAUTH2", lambda _challenge: auth)
    except Exception as exc:
        _quiet_logout(connection)
        raise OutlookImapError(f"Outlook IMAP 认证失败：{_describe(exc)}") from exc
    last_status: str | None = None
    last_data: object = None
    try:
        for readonly in (True, False):
            try:
                status, data = _open_inbox(connection, readonly=readonly)
            except Exception as exc:
                if _not_connected(exc):
                    last_status, last_data = "NO", exc
                    continue
                _quiet_logout(connection)
                raise OutlookImapError(f"无法打开 Outlook 收件箱：{_describe(exc)}") from exc
            if status == "OK":
                return connection
            last_status, last_data = status, data
            if not _not_connected(status, data):
                break
    except OutlookImapError:
        raise
    _quiet_logout(connection)
    if _not_connected(last_status, last_data):
        raise OutlookImapError(
            "Outlook IMAP 认证成功但未能打开收件箱（User is authenticated but not connected）。"
            "可能是账号未开启 IMAP，或微软会话尚未就绪。"
        )
    raise OutlookImapError("无法打开 Outlook 收件箱")


def imap_connect(
    address: str,
    access_token: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> imaplib.IMAP4_SSL:
    """Open INBOX on Outlook IMAP via XOAUTH2.

    Microsoft consumer IMAP often accepts AUTHENTICATE then rejects the next
    command with ``User is authenticated but not connected``. Retry the
    handshake (and ``imap-mail.outlook.com``) before failing.
    """
    hosts = (IMAP_SERVER, IMAP_FALLBACK_SERVER)
    last_error: OutlookImapError | None = None
    for attempt in range(IMAP_CONNECT_ATTEMPTS):
        host = hosts[0] if attempt < IMAP_CONNECT_ATTEMPTS - 1 else hosts[1]
        try:
            return _imap_connect_once(host, address, access_token, timeout=timeout)
        except OutlookImapError as exc:
            last_error = exc
            if "authenticated but not connected" not in str(exc).lower():
                raise
            if attempt < IMAP_CONNECT_ATTEMPTS - 1:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def validate_mailbox(
    session: requests.Session,
    client_id: str,
    refresh_token: str,
    address: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Verify OAuth + IMAP login and return the (possibly rotated) token."""
    current = refresh_token
    rotated_out = ""

    def work() -> str:
        nonlocal current, rotated_out
        token, _expires, rotated = refresh_access_token(
            session, client_id, current, timeout=timeout
        )
        if rotated:
            current = rotated
            rotated_out = rotated
        connection = imap_connect(address, token, timeout=timeout)
        _quiet_logout(connection)
        return rotated_out

    return _run_with_retry(work)


def list_messages(
    session: requests.Session,
    client_id: str,
    refresh_token: str,
    address: str,
    *,
    top: int = 50,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[list[ImapMessage], str]:
    """List up to ``top`` INBOX messages newest-first."""
    current = refresh_token
    rotated_out = ""

    def work() -> tuple[list[ImapMessage], str]:
        nonlocal current, rotated_out
        token, _expires, rotated = refresh_access_token(
            session, client_id, current, timeout=timeout
        )
        if rotated:
            current = rotated
            rotated_out = rotated
        connection = imap_connect(address, token, timeout=timeout)
        try:
            try:
                status, data = connection.uid("search", None, "ALL")
            except Exception as exc:
                raise OutlookImapError(
                    f"Outlook IMAP 搜索邮件失败：{_describe(exc)}"
                ) from exc
            if status != "OK" or not data:
                raise OutlookImapError("Outlook IMAP 搜索邮件失败")
            all_uids = data[0].split()
            window = max(int(top), 200)
            uids = all_uids[-window:] if len(all_uids) > window else all_uids
            messages: list[ImapMessage] = []
            for uid in uids:
                try:
                    status, rows = connection.uid("fetch", uid, "(RFC822)")
                except Exception as exc:
                    raise OutlookImapError(
                        f"Outlook IMAP 读取邮件失败：{_describe(exc)}"
                    ) from exc
                if status != "OK" or not rows or not isinstance(rows[0], tuple):
                    continue
                raw_source = rows[0][1]
                parsed = email.message_from_bytes(raw_source)
                sender_name, sender_address = parseaddr(
                    decode_header_value(parsed.get("From", ""))
                )
                messages.append(
                    ImapMessage(
                        sender_name=sender_name,
                        sender_address=sender_address,
                        subject=decode_header_value(parsed.get("Subject", "(无主题)")),
                        received_at=str(parsed.get("Date", "")),
                        message_id="imap:" + uid.decode("ascii", "ignore"),
                        size=format_size(len(raw_source)),
                    )
                )
            messages.sort(
                key=lambda item: received_at_sort_key(item.received_at), reverse=True
            )
            return messages[: max(1, int(top))], rotated_out
        finally:
            _quiet_logout(connection)

    return _run_with_retry(work)


def fetch_message_source(
    session: requests.Session,
    client_id: str,
    refresh_token: str,
    address: str,
    uid: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bytes, str]:
    """Fetch the raw RFC822 bytes for one IMAP UID."""
    uid_text = str(uid or "").strip()
    if not uid_text.isdigit():
        raise OutlookImapError("邮件 UID 无效")
    current = refresh_token
    rotated_out = ""

    def work() -> tuple[bytes, str]:
        nonlocal current, rotated_out
        token, _expires, rotated = refresh_access_token(
            session, client_id, current, timeout=timeout
        )
        if rotated:
            current = rotated
            rotated_out = rotated
        connection = imap_connect(address, token, timeout=timeout)
        try:
            try:
                status, rows = connection.uid("fetch", uid_text, "(RFC822)")
            except Exception as exc:
                raise OutlookImapError(
                    f"Outlook IMAP 读取邮件失败：{_describe(exc)}"
                ) from exc
            if status != "OK" or not rows or not isinstance(rows[0], tuple):
                raise OutlookImapError("Outlook IMAP 读取邮件失败")
            return rows[0][1], rotated_out
        finally:
            _quiet_logout(connection)

    return _run_with_retry(work)
