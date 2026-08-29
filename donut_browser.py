"""Donut Browser local API integration for persistent mailbox profiles."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


DEFAULT_DONUT_API_URL = "http://127.0.0.1:10108"
PROFILE_TAG = "temporary-email-pickup"


class DonutBrowserError(RuntimeError):
    """A user-facing Donut Browser integration error."""


@dataclass(frozen=True, slots=True)
class DonutProfile:
    id: str
    name: str
    version: str
    is_running: bool


@dataclass(frozen=True, slots=True)
class DonutBrowserSession:
    profile_id: str
    debugger_address: str
    browser_version: str | None
    headless: bool


def debugger_address_is_live(address: str, *, timeout: float = 0.5) -> bool:
    try:
        host, port_text = address.rsplit(":", 1)
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


class DonutBrowserClient:
    """Small client for the Donut Browser loopback REST API."""

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.session = session or requests.Session()

    def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        try:
            response = self.session.request(
                method,
                f"{self.api_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DonutBrowserError(
                "无法连接 Donut Browser 本地 API。请先启动 Donut，并在“设置 → 集成 → 本地 API”中启用服务。"
            ) from exc

        if response.status_code not in expected:
            messages = {
                400: "Donut Browser 拒绝了请求；请确认已下载 Wayfern 浏览器，并接受相关使用条款。",
                401: "Donut API Token 无效或已经轮换，请重新配置 Token。",
                402: "Donut 的浏览器自动化/CDP 功能需要有效的 Pro 授权。",
                403: "Donut 尚未接受 Wayfern 使用条款，请先在 Donut 中完成确认。",
                404: "Donut 中的指纹配置不存在，程序将在下次重试时重新创建。",
                409: "该 Donut 指纹配置当前被其他实例占用。",
            }
            detail = messages.get(response.status_code)
            if not detail:
                body = response.text.strip()
                detail = f"Donut API 返回 HTTP {response.status_code}"
                if body:
                    detail += f"：{body[:300]}"
            raise DonutBrowserError(detail)

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise DonutBrowserError("Donut API 返回了无法解析的数据。") from exc

    @staticmethod
    def _profile(raw: dict[str, Any]) -> DonutProfile:
        return DonutProfile(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            version=str(raw.get("version") or ""),
            is_running=bool(raw.get("is_running")),
        )

    def get_profile(self, profile_id: str) -> DonutProfile | None:
        try:
            data = self._request("GET", f"/v1/profiles/{quote(profile_id, safe='')}")
        except DonutBrowserError as exc:
            if "不存在" in str(exc):
                return None
            raise
        raw = data.get("profile", {}) if isinstance(data, dict) else {}
        profile = self._profile(raw)
        return profile if profile.id else None

    def list_profiles(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/profiles")
        profiles = data.get("profiles", []) if isinstance(data, dict) else []
        return [item for item in profiles if isinstance(item, dict)]

    def ensure_profile(
        self,
        *,
        mailbox_key: str,
        address: str,
        preferred_id: str | None,
    ) -> DonutProfile:
        if preferred_id:
            existing = self.get_profile(preferred_id)
            if existing is not None:
                return existing

        mailbox_tag = f"{PROFILE_TAG}:{mailbox_key}"
        profile_name = f"TemporaryEmailPickup · {address} · {mailbox_key[:8]}"
        legacy_profile_name = f"TemporaryEmailPickup · {address}"
        for raw in self.list_profiles():
            tags = raw.get("tags", [])
            if isinstance(tags, list) and mailbox_tag in tags:
                profile = self._profile(raw)
                if profile.id:
                    return profile
            if str(raw.get("name") or "") in {profile_name, legacy_profile_name}:
                profile = self._profile(raw)
                if profile.id:
                    return profile

        data = self._request(
            "POST",
            "/v1/profiles",
            payload={
                "name": profile_name,
                "browser": "wayfern",
                "version": "latest",
                "wayfern_config": {},
            },
        )
        raw = data.get("profile", {}) if isinstance(data, dict) else {}
        profile = self._profile(raw)
        if not profile.id:
            raise DonutBrowserError("Donut 创建了配置，但没有返回 profile_id。")
        return profile

    def open_profile(
        self,
        profile: DonutProfile,
        *,
        existing_debugger_address: str | None,
        headless: bool,
    ) -> DonutBrowserSession:
        if existing_debugger_address and debugger_address_is_live(
            existing_debugger_address
        ):
            if headless:
                raise DonutBrowserError(
                    "该邮箱的 Donut 指纹浏览器仍在运行，请先在 Donut 中停止该配置后再执行后台注册。"
                )
            return DonutBrowserSession(
                profile_id=profile.id,
                debugger_address=existing_debugger_address,
                browser_version=self._read_browser_version(existing_debugger_address),
                headless=False,
            )

        current = self.get_profile(profile.id) or profile
        if current.is_running:
            raise DonutBrowserError(
                "该邮箱的 Donut 配置已经在运行，但原调试端口不可用。请先在 Donut 中停止该配置后重试。"
            )

        data = self._request(
            "POST",
            f"/v1/profiles/{quote(profile.id, safe='')}/run",
            payload={"headless": headless},
        )
        if not isinstance(data, dict):
            raise DonutBrowserError("Donut 启动配置后没有返回调试端口。")
        port = data.get("remote_debugging_port", data.get("cdp_port"))
        try:
            port_number = int(port)
        except (TypeError, ValueError) as exc:
            raise DonutBrowserError("Donut 启动配置后没有返回有效的 CDP 端口。") from exc
        if not 1 <= port_number <= 65535:
            raise DonutBrowserError("Donut 返回了无效的 CDP 端口。")
        address = f"127.0.0.1:{port_number}"
        version = self._wait_for_debugger(address)
        return DonutBrowserSession(
            profile_id=profile.id,
            debugger_address=address,
            browser_version=version or profile.version or None,
            headless=headless,
        )

    def kill_profile(self, profile_id: str) -> None:
        self._request(
            "POST",
            f"/v1/profiles/{quote(profile_id, safe='')}/kill",
            expected=(204,),
        )

    def _wait_for_debugger(self, address: str, *, timeout: float = 30.0) -> str | None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                version = self._read_browser_version(address)
                if version is not None or debugger_address_is_live(address):
                    return version
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(0.25)
        raise DonutBrowserError(
            "Donut 已返回 CDP 端口，但浏览器没有及时开始监听。"
        ) from last_error

    def _read_browser_version(self, address: str) -> str | None:
        try:
            response = self.session.get(
                f"http://{address}/json/version",
                timeout=min(self.timeout, 2.0),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return None
        product = str(data.get("Browser") or "") if isinstance(data, dict) else ""
        if "/" in product:
            return product.rsplit("/", 1)[-1] or None
        return product or None
