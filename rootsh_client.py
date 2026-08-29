"""Small, session-aware client for rootsh.com's temporary-mail service."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


BASE_URL = "https://rootsh.com"


class RootshError(RuntimeError):
    """Raised when rootsh.com rejects a request or returns an invalid response."""


@dataclass(frozen=True, slots=True)
class Message:
    sender_name: str
    sender_address: str
    subject: str
    received_at: str
    message_id: str
    size: str

    @property
    def sender(self) -> str:
        if self.sender_name:
            return f"{self.sender_name} <{self.sender_address}>"
        return self.sender_address


@dataclass(frozen=True, slots=True)
class Mailbox:
    address: str
    lifetime_seconds: int
    notice: str = ""


@dataclass(frozen=True, slots=True)
class InboxUpdate:
    cursor: int
    address: str
    messages: tuple[Message, ...]


def parse_delay(value: Any) -> int:
    """Convert the site's ``MM:SS`` countdown value to seconds."""
    text = str(value or "10:00").strip()
    if ":" not in text:
        try:
            return max(0, int(text))
        except ValueError:
            return 600
    minutes, seconds = text.rsplit(":", 1)
    try:
        return max(0, int(minutes) * 60 + int(seconds))
    except ValueError:
        return 600


def encode_mailbox_path(address: str) -> str:
    """Mirror the path transformation used by rootsh.com's own JavaScript."""
    transformed = address.replace("@", "))^^)", 1).replace(".", "+=_+=", 1)
    return quote(transformed, safe=")+=_^-")


class _DomainParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_domain_list = False
        self.list_depth = 0
        self.in_item = False
        self.current: list[str] = []
        self.domains: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"ul", "ol"} and attributes.get("id") == "domainlist":
            self.in_domain_list = True
            self.list_depth = 1
        elif self.in_domain_list and tag in {"ul", "ol"}:
            self.list_depth += 1
        elif self.in_domain_list and tag == "li":
            self.in_item = True
            self.current = []

    def handle_endtag(self, tag: str) -> None:
        if self.in_domain_list and tag == "li" and self.in_item:
            value = "".join(self.current).strip().lstrip("@")
            if value and value not in self.domains:
                self.domains.append(value)
            self.in_item = False
        elif self.in_domain_list and tag in {"ul", "ol"}:
            self.list_depth -= 1
            if self.list_depth <= 0:
                self.in_domain_list = False

    def handle_data(self, data: str) -> None:
        if self.in_domain_list and self.in_item:
            self.current.append(data)


class _TextParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "p",
        "pre", "section", "table", "td", "th", "tr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def html_to_text(source: str) -> str:
    parser = _TextParser()
    parser.feed(source)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line).strip()


class RootshClient:
    """HTTP client that preserves the cookie tying a mailbox to its browser session."""

    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Referer": f"{self.base_url}/",
            }
        )

    def close(self) -> None:
        self.session.close()

    def clone(self) -> "RootshClient":
        clone = RootshClient(self.base_url, self.timeout)
        clone.session.cookies.update(self.session.cookies)
        return clone

    def export_cookies(self) -> list[dict[str, Any]]:
        """Return the session cookies in a JSON-serializable form."""
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
            for cookie in self.session.cookies
        ]

    def import_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Restore cookies previously produced by :meth:`export_cookies`."""
        self.session.cookies.clear()
        for item in cookies:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            kwargs: dict[str, Any] = {
                "name": str(item["name"]),
                "value": str(item.get("value") or ""),
                "path": str(item.get("path") or "/"),
                "secure": bool(item.get("secure", False)),
            }
            domain = str(item.get("domain") or "")
            if domain:
                kwargs["domain"] = domain
            expires = item.get("expires")
            if isinstance(expires, (int, float)):
                kwargs["expires"] = int(expires)
            self.session.cookies.set(**kwargs)

    def bootstrap(self) -> list[str]:
        response = self.session.get(f"{self.base_url}/", timeout=self.timeout)
        self._ensure_ok(response)
        parser = _DomainParser()
        parser.feed(response.text)
        return parser.domains or ["bccto.cc"]

    def apply_mailbox(self, local_part: str, domain: str) -> Mailbox:
        address = f"{local_part}@{domain.lstrip('@')}"
        data = self._post_json("/applymail", {"mail": address})
        if str(data.get("success", "")).lower() != "true":
            detail = " ".join(str(data.get(key, "")) for key in ("user", "message")).strip()
            raise RootshError(detail or "邮箱申请失败")
        return Mailbox(
            address=str(data.get("user") or address),
            lifetime_seconds=parse_delay(data.get("delay")),
            notice=str(data.get("tips") or ""),
        )

    def get_mail(self, address: str, cursor: int = 0) -> InboxUpdate:
        data = self._post_json("/getmail", {"mail": address, "time": cursor})
        if str(data.get("success", "")).lower() != "true":
            raise RootshError(str(data.get("message") or "收件失败"))

        messages: list[Message] = []
        for raw in data.get("mail") or []:
            if not isinstance(raw, (list, tuple)) or len(raw) < 6:
                continue
            messages.append(
                Message(
                    sender_name=str(raw[0] or ""),
                    sender_address=str(raw[1] or ""),
                    subject=str(raw[2] or "(无主题)"),
                    received_at=str(raw[3] or ""),
                    message_id=str(raw[4]),
                    size=str(raw[5] or ""),
                )
            )
        try:
            next_cursor = int(data.get("time") or cursor)
        except (TypeError, ValueError):
            next_cursor = cursor
        return InboxUpdate(next_cursor, str(data.get("to") or address), tuple(messages))

    def destroy_mailbox(self) -> str:
        data = self._post_json("/destroymail", {})
        if str(data.get("success", "")).lower() != "true":
            raise RootshError("邮箱销毁失败")
        return str(data.get("mail") or "")

    def delete_messages(self, message_ids: list[str]) -> tuple[str, ...]:
        if not message_ids:
            return ()
        data = self._post_json("/delmail", {"delMail": ",".join(message_ids)})
        if str(data.get("success", "")).lower() != "true":
            raise RootshError(str(data.get("message") or "邮件删除失败"))
        return tuple(str(value) for value in (data.get("mail") or message_ids))

    def fetch_message_html(self, address: str, message_id: str) -> str:
        token = encode_mailbox_path(address)
        safe_id = quote(str(message_id), safe="")
        response = self.session.get(
            f"{self.base_url}/win/{token}/{safe_id}", timeout=self.timeout
        )
        self._ensure_ok(response)
        response.encoding = response.apparent_encoding or response.encoding
        return response.text

    def download_message(self, address: str, message_id: str, destination: Path) -> None:
        token = encode_mailbox_path(address)
        safe_id = quote(str(message_id), safe="")
        self._download(f"/download/{token}/{safe_id}", destination)

    def download_mailbox(self, address: str, destination: Path) -> None:
        token = encode_mailbox_path(address)
        self._download(f"/download/{token}/all.zip", destination)

    def _download(self, path: str, destination: Path) -> None:
        response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        self._ensure_ok(response)
        destination.write_bytes(response.content)

    def _post_json(self, path: str, form: dict[str, Any]) -> dict[str, Any]:
        payload = dict(form)
        response = self.session.post(
            f"{self.base_url}{path}",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"},
            timeout=self.timeout,
        )
        self._ensure_ok(response)
        try:
            data = response.json()
        except requests.JSONDecodeError as exc:
            raise RootshError("服务器返回了无法识别的数据") from exc
        if not isinstance(data, dict):
            raise RootshError("服务器返回的数据格式不正确")
        return data

    @staticmethod
    def _ensure_ok(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RootshError(f"网络请求失败：HTTP {response.status_code}") from exc
