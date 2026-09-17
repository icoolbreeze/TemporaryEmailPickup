"""Creative Fabrica login/registration helper for one isolated browser profile."""

from __future__ import annotations

import html
import json
import re
import os
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError

from cdp_client import CDPClient, CDPError, find_page_target
from rootsh_client import html_to_text


LOGIN_URL = "https://www.creativefabrica.com/login/"
ACCOUNT_URL = "https://www.creativefabrica.com/my-account/"
HOME_URL = "https://www.creativefabrica.com/"
SIGNUP_URL = "https://www.creativefabrica.com/signup/"
STUDIO_URL = "https://studio.creativefabrica.com/"
DEFAULT_NEW_ACCOUNT_POINTS = 5000
VERIFICATION_POLL_SECONDS = 10
PAGE_READY_TIMEOUT_SECONDS = 20
HUMAN_VERIFICATION_TIMEOUT_SECONDS = 600
PAGE_PROBE_RETRY_SECONDS = 8
NAVIGATION_CHALLENGE_GRACE_SECONDS = 5
LOGOUT_WAIT_SECONDS = 10
LOGIN_FORM_SELECTOR = "main form.woocommerce-form-login"
REGISTER_FORM_SELECTOR = "main form.woocommerce-form-register"

StatusCallback = Callable[[str], None]
PointsCallback = Callable[[int], None]
AuthenticatedCallback = Callable[[], None]


def browser_display_name(browser_path: Path) -> str:
    """Return a friendly name for a supported Chromium browser binary."""
    name = browser_path.name.lower()
    if "brave" in name:
        return "Brave"
    if "chrome" in name:
        return "Chrome"
    if "edge" in name or name == "msedge.exe":
        return "Edge"
    return browser_path.stem or "Chromium 浏览器"


def manual_browser_command(
    browser_path: Path,
    profile_path: Path,
    *,
    worker_id: str | None = None,
    profile_directory: str | None = None,
    incognito: bool = False,
    remote_debugging: bool = True,
) -> list[str]:
    """Build a normal Chromium launch command with no WebDriver involvement.

    When ``profile_directory`` is provided (e.g. ``"Profile 4"`` for a named
    Brave/Chrome profile), ``--profile-directory=...`` is appended so the
    browser skips the "Who's using …?" profile picker and opens the
    selected profile directly.

    When ``incognito`` is True, ``--incognito`` is appended so the window's
    cookies / local storage stay ephemeral. ``--user-data-dir`` still points
    at the isolated TemporaryEmailPickup container so the profile never
    touches the user's daily browser data.

    With ``remote_debugging=False`` (the 手动打开 path) the command omits
    ``--remote-debugging-port=0``: the window stays a plain user window
    with no DevTools listener, so ChromeDriver can never attach and
    ``navigator.webdriver`` / ``cdc_*`` never appear — Studio's invisible
    Turnstile then passes the human check. Ignored when ``worker_id`` is
    set; the VirtualBrowser path never exposed a debug port anyway.
    """
    command = [
        str(browser_path),
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if incognito:
        command.append("--incognito")
    if profile_directory:
        command.append(f"--profile-directory={profile_directory}")
    if worker_id:
        command.append(f"--worker-id={worker_id}")
    elif remote_debugging:
        # Expose the local DevTools port so the app can attach read-only and
        # track the window's URL, login state and Studio points. 手动打开
        # passes ``remote_debugging=False`` instead: a DevTools listener is
        # what lets ChromeDriver attach and flip the automation signals the
        # Turnstile widget reacts to.
        command.append("--remote-debugging-port=0")
    # Open Studio directly: the main ``www.creativefabrica.com`` site
    # is protected by Cloudflare's "Just a moment…" interstitial, while
    # ``studio.creativefabrica.com`` is reachable without a challenge.
    command.append(STUDIO_URL)
    return command


def existing_debugger_address(profile_path: Path) -> str | None:
    """Return Chrome's debugger address for a running persistent profile."""
    try:
        port_text = (profile_path / "DevToolsActivePort").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        port = int(port_text)
    except (OSError, ValueError, IndexError):
        return None
    if not 1 <= port <= 65535:
        return None
    return f"127.0.0.1:{port}"


def debugger_address_is_live(address: str, *, timeout: float = 0.5) -> bool:
    """Return whether Chrome is still listening on its recorded DevTools port."""
    try:
        host, port_text = address.rsplit(":", 1)
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


MANUAL_BROWSER_NO_DEBUGGER_MESSAGE = (
    "该邮箱的手动浏览器窗口已经打开，但手动窗口没有调试端口，"
    "程序无法在不触发人机验证的情况下接入。请先关闭该手动窗口，"
    "再点击“自动打开”。"
)


def launch_plain_chromium(
    browser_path: Path,
    profile_path: Path,
    *,
    timeout_seconds: float = 15.0,
    manual_process: subprocess.Popen[Any] | None = None,
) -> str:
    """Launch a plain user Chromium for incognito 自动打开 and return its CDP address.

    The command is the 手动打开 command (``--incognito`` on the isolated
    TemporaryEmailPickup container) plus ``--remote-debugging-port=0``: the
    process has no ChromeDriver, no ``--enable-automation`` and no ``cdc_*``
    globals. The returned address is for the raw CDP WebSocket only; it must
    never be passed into ``CreativeFabricaBrowser`` / ``webdriver.Chrome``.

    A live ``DevToolsActivePort`` is reused (second 自动打开 click CDPs into
    the existing window instead of taking the profile lock a second time).
    A still-running 手动打开 window has no debugger and can never be attached
    to, so the caller is asked to close it first.
    """
    existing = existing_debugger_address(profile_path)
    if existing and debugger_address_is_live(existing):
        return existing
    if manual_process is not None and manual_process.poll() is None:
        raise RuntimeError(MANUAL_BROWSER_NO_DEBUGGER_MESSAGE)
    command = manual_browser_command(
        browser_path,
        profile_path,
        incognito=True,
        remote_debugging=True,
    )
    try:
        subprocess.Popen(command, cwd=str(browser_path.parent))
    except OSError as exc:
        raise RuntimeError(f"启动 {browser_path.name} 失败：{exc}") from exc
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        candidate = existing_debugger_address(profile_path)
        if candidate and debugger_address_is_live(candidate):
            return candidate
        time.sleep(0.1)
    raise RuntimeError(
        f"无法在 {int(timeout_seconds)} 秒内连接 {browser_path.name} 的调试端口；"
        "请关闭窗口后再次点击“自动打开”。"
    )


def browser_was_closed(error: BaseException) -> bool:
    """Recognize Selenium errors caused by the user closing the Chrome window."""
    detail = " ".join(
        (
            type(error).__name__,
            str(getattr(error, "msg", "") or ""),
            str(error),
        )
    ).lower()
    markers = (
        "invalidsessionid",
        "invalid session id",
        "session deleted as the browser has closed",
        "not connected to devtools",
        "no such window",
        "target window already closed",
        "web view not found",
    )
    return any(marker in detail for marker in markers)


def is_human_verification_content(
    url: str,
    title: str,
    body_text: str,
    *,
    has_challenge_frame: bool = False,
    has_site_form: bool = False,
) -> bool:
    """Recognize Cloudflare/Turnstile pages that require a person to continue.

    ``has_site_form`` advertises that a first-party login or register form is
    present on the main document. When that is true and the page is not one of
    the hard interstitial markers, the page is treated as site-ready even if a
    leftover Turnstile widget is still on the page.
    """
    title_text = (title or "").strip().lower()
    if title_text.startswith("just a moment"):
        return True
    haystack = " ".join((url or "", title or "", body_text or "")).lower()
    if "cdn-cgi/challenge-platform" in haystack:
        return True
    if has_site_form:
        # Login or register form is on the page; a leftover Turnstile widget
        # beside it should not freeze the automation loop.
        return False
    if has_challenge_frame:
        return True
    markers = (
        "performing security verification",
        "verify you are human",
        "checking your browser before accessing",
        "security verification by cloudflare",
        "cdn-cgi/challenge-platform",
    )
    return any(marker in haystack for marker in markers)


def fetch_debugger_tabs(port: int, *, timeout: float = 0.5) -> list[dict[str, str]]:
    """Return ``{"title", "url"}`` metadata for every page-type DevTools tab."""
    last_error: Exception | None = None
    for path in ("/json", "/json/list"):
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                payload = response.read().decode("utf-8", errors="replace")
            data = json.loads(payload)
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
        if not isinstance(data, list):
            return []
        tabs: list[dict[str, str]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("type") != "page":
                continue
            tabs.append(
                {
                    "title": str(entry.get("title") or ""),
                    "url": str(entry.get("url") or ""),
                }
            )
        return tabs
    if last_error is not None:
        return []
    return []


def is_main_site_authenticated(
    url: str, *, has_logout: bool, has_login_form: bool
) -> bool:
    """Resolve Creative Fabrica's account state even when it keeps /login/ in the URL."""
    if has_logout:
        return True
    lowered_url = url.lower()
    if "/login" in lowered_url or "/signup" in lowered_url:
        return False
    return not has_login_form


def _list_system_browser_processes() -> list[dict[str, str]]:
    """Return read-only info about running brave.exe / chrome.exe processes.

    Each entry is ``{"name", "pid", "command_line"}``. Helper processes
    (``--type=crashpad-handler``, ``--type=renderer``, etc.) are included so
    callers can see the full picture. This deliberately does **not** kill
    anything; it is a probe used to detect a user-launched Brave/Chrome
    sitting on the real User Data without ``--remote-debugging-port``.
    """
    if os.name != "nt":
        return []
    # Filter in WQL itself: enumerating every Win32_Process stalls for many
    # seconds on some machines. Output shape stays Name/ProcessId/CommandLine.
    script = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'brave.exe' OR Name = 'chrome.exe'\" | "
        "Select-Object Name, ProcessId, CommandLine | "
        "ConvertTo-Json -Compress -Depth 1"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    payload = (result.stdout or "").strip()
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except ValueError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    rows: list[dict[str, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "name": str(entry.get("Name") or ""),
                "pid": str(entry.get("ProcessId") or ""),
                "command_line": str(entry.get("CommandLine") or ""),
            }
        )
    return rows


def find_system_browser_on_user_data(user_data_dir: Path) -> list[dict[str, str]]:
    """Return info about system brave.exe/chrome.exe processes that conflict.

    A process counts as "system" — i.e. blocks the app from launching its
    own named-profile window — when **all** of these are true:

    * it is a **main** process (the command line does **not** contain
      ``--type=`` so renderers, GPU helpers, crashpad-handler etc. are
      ignored),
    * its binary name is ``brave.exe`` or ``chrome.exe`` matching the
      selected browser, inferred from ``user_data_dir`` (Brave paths
      match ``brave.exe``; Chrome paths match ``chrome.exe``),
    * it is **not** a TemporaryEmailPickup-managed launch
      (``TemporaryEmailPickup`` is absent from the command line), and
    * it does **not** include ``--remote-debugging-port`` (so the app
      cannot attach a ChromeDriver to it).

    The command line does **not** have to contain ``--user-data-dir`` —
    a Brave started from the taskbar / "Who's using Brave?" picker
    typically omits that flag and still owns the real User Data. The
    caller is expected to refuse the launch and tell the user to fully
    quit the existing browser first. This helper is read-only.
    """
    target = str(user_data_dir).lower()
    if "brave" in target:
        target_name = "brave.exe"
    elif "chrome" in target:
        target_name = "chrome.exe"
    else:
        # Unknown User Data root; fall back to either Chromium browser so
        # tests and unusual paths still see both kinds of conflicts.
        target_name = ""
    rows = _list_system_browser_processes()
    matches: list[dict[str, str]] = []
    for row in rows:
        command = row.get("command_line", "")
        if not command:
            continue
        if "--type=" in command:
            continue
        name = (row.get("name") or "").lower()
        if target_name:
            if name != target_name:
                continue
        else:
            if name not in {"brave.exe", "chrome.exe"}:
                continue
        if "TemporaryEmailPickup" in command:
            continue
        if "--remote-debugging-port" in command:
            continue
        matches.append(row)
    return matches


SYSTEM_BROWSER_PORT_REQUIRED_MESSAGE = (
    "检测到 Brave 已在运行。请先在任务栏完全退出 Brave，再由本程序点击"
    "“手动打开”或“自动打开”。直接点图标打开的 Brave 没有调试端口，"
    "程序无法接入「CF」配置，且会落到空白隔离配置从而循环人机验证。"
)


def terminate_stale_profile_browser(profile_path: Path, browser_path: Path) -> bool:
    """Close only browser processes that use this app-owned profile on Windows.

    Refuses to touch paths that do not live under the app's
    ``TemporaryEmailPickup`` directory: the real Brave/Chrome User Data
    is the user's daily browser, and the app must never reach into it.
    Returns ``False`` immediately in that case without spawning any
    PowerShell process.
    """
    if os.name != "nt":
        return False
    try:
        resolved = str(Path(profile_path).resolve())
    except OSError:
        resolved = str(profile_path)
    if "TemporaryEmailPickup" not in resolved:
        return False
    profile = resolved
    env = os.environ.copy()
    env["TEMP_EMAIL_PROFILE_TO_CLOSE"] = profile
    env["TEMP_EMAIL_BROWSER_PROCESS"] = browser_path.name
    script = r"""
$profile = [Environment]::GetEnvironmentVariable('TEMP_EMAIL_PROFILE_TO_CLOSE')
$processName = [Environment]::GetEnvironmentVariable('TEMP_EMAIL_BROWSER_PROCESS')
$matches = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -ieq $processName -and $_.CommandLine -and $_.CommandLine.Contains($profile)
})
$main = @($matches | Where-Object { -not $_.CommandLine.Contains('--type=') })
foreach ($item in $main) {
    $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
    if ($process) { [void]$process.CloseMainWindow() }
}
Start-Sleep -Milliseconds 1200
$remaining = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -ieq $processName -and $_.CommandLine -and $_.CommandLine.Contains($profile)
})
foreach ($item in $remaining) {
    Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Output $matches.Count
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        count = int((result.stdout or "0").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False
    if count:
        time.sleep(0.8)
    try:
        (profile_path / "DevToolsActivePort").unlink(missing_ok=True)
    except OSError:
        pass
    return count > 0


def extract_verification_code(subject: str, body: str = "") -> str | None:
    """Extract the short numeric account-verification code from a CF email."""
    subject_patterns = (
        r"^\s*(\d{4,8})\s*[-–—:]",
        r"(?:verification|verify|验证码|code)\D{0,24}(\d{4,8})",
    )
    for pattern in subject_patterns:
        match = re.search(pattern, subject, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    body_patterns = (
        r"(?:verification|verify|验证码|code)\D{0,40}(\d{4,8})",
        r"\b(\d{4,8})\b",
    )
    for pattern in body_patterns:
        match = re.search(pattern, body, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _is_creative_fabrica_message(sender: str, subject: str) -> bool:
    haystack = f"{sender} {subject}".lower()
    return "creative fabrica" in haystack or "creativefabrica" in haystack


def extract_points(values: list[str]) -> int | None:
    """Extract a Studio coin balance from taskbar text or accessible labels."""
    patterns = (
        r"(\d[\d ,.]*|\d+(?:\.\d+)?[km])\s*(?:ai\s*)?(?:coins?|积分)",
        r"(?:coins?|积分)\s*[:：]?\s*(\d[\d ,.]*|\d+(?:\.\d+)?[km])",
    )
    for value in values:
        normalized = " ".join(str(value or "").split()).lower()
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if not match:
                continue
            token = match.group(1).strip().lower()
            multiplier = 1
            if token.endswith("k"):
                multiplier = 1000
                token = token[:-1]
            elif token.endswith("m"):
                multiplier = 1_000_000
                token = token[:-1]
            if multiplier > 1:
                try:
                    return int(float(token.replace(",", "")) * multiplier)
                except ValueError:
                    continue
            compact = re.sub(r"[ ,.]+", "", token)
            if compact.isdigit():
                return int(compact)
    return None


# --- Raw-CDP Studio login (无痕 自动打开; no ChromeDriver anywhere) ---------

STUDIO_CDP_SUCCESS = "success"
STUDIO_CDP_CAPTCHA_PENDING = "captcha_pending"
STUDIO_CDP_FAILED = "failed"

#: Normal submit wait, mirroring ``_ensure_studio_login``'s 180 s poll.
STUDIO_LOGIN_POLL_SECONDS = 180
#: How long to wait for the points chip to paint after a successful login.
STUDIO_POINTS_WAIT_SECONDS = 25
#: Login dialog open / register-toggle re-query waits.
STUDIO_DIALOG_WAIT_SECONDS = 15
STUDIO_TOGGLE_DIALOG_WAIT_SECONDS = 10

_STUDIO_HEADER_LOGIN_SELECTOR = (
    "header button, header a, button[aria-label], a[aria-label]"
)
_STUDIO_DIALOG_SELECTOR = "[role='dialog']"
_STUDIO_DIALOG_CONTROL_SELECTOR = "button, a, span"
_STUDIO_DIALOG_ERROR_SELECTOR = (
    "[role='dialog'] [role='alert'], [role='dialog'] .text-red-500"
)
#: Main-document captcha markers only — challenge iframes are never queried.
_STUDIO_CAPTCHA_IFRAME_SELECTOR = (
    "iframe[src*='recaptcha'], iframe[title*='challenge']"
)
_STUDIO_POINTS_SELECTOR = (
    "header button, header a, [aria-label*='coin' i], "
    "[title*='coin' i], [data-testid*='coin' i], [class*='coin' i]"
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class StudioCDPResult:
    """Outcome of the raw-CDP Studio login."""

    status: str
    points: int | None = None
    detail: str | None = None


def _cdp_plain_text(markup: str) -> str:
    """Return the human-readable text of a small outerHTML fragment."""
    if not markup:
        return ""
    return " ".join(html.unescape(_HTML_TAG_RE.sub(" ", markup)).split())


def _cdp_control_label(client: CDPClient, node_id: int) -> str:
    """Combine a node's text, aria-label and title, like ``_studio_login_control``."""
    attrs = client.attributes(node_id)
    parts = (
        _cdp_plain_text(client.outer_html(node_id)),
        attrs.get("aria-label", ""),
        attrs.get("title", ""),
    )
    return " ".join(part for part in parts if part).strip().lower()


def _cdp_label_means_login(label: str) -> bool:
    compact = label.replace(" ", "")
    return (
        "login" in compact
        or "signin" in compact
        or "log in" in label
        or "sign in" in label
    )


def _cdp_find_login_control(client: CDPClient) -> int:
    """Return a visible header Log in / Sign in node id, or 0."""
    root = client.document()
    for node_id in client.query_all(_STUDIO_HEADER_LOGIN_SELECTOR, root):
        if client.box_model(node_id) is None:
            continue
        if _cdp_label_means_login(_cdp_control_label(client, node_id)):
            return node_id
    return 0


def _cdp_visible_dialog(client: CDPClient) -> int:
    dialog = client.query(_STUDIO_DIALOG_SELECTOR)
    if dialog and client.box_model(dialog) is not None:
        return dialog
    return 0


def _cdp_wait_dialog(client: CDPClient, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while True:
        dialog = _cdp_visible_dialog(client)
        if dialog:
            return dialog
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.25)


def _cdp_find_text_control(
    client: CDPClient, scope_id: int, texts: tuple[str, ...]
) -> int:
    """Return a visible button/link/span in ``scope_id`` whose text matches."""
    wanted = {text.strip().upper() for text in texts}
    for node_id in client.query_all(_STUDIO_DIALOG_CONTROL_SELECTOR, scope_id):
        if client.box_model(node_id) is None:
            continue
        label = _cdp_plain_text(client.outer_html(node_id)).strip().upper()
        if label in wanted:
            return node_id
    return 0


def _cdp_has_captcha(client: CDPClient) -> bool:
    root = client.document()
    return any(
        client.box_model(node_id) is not None
        for node_id in client.query_all(_STUDIO_CAPTCHA_IFRAME_SELECTOR, root)
    )


def _cdp_dialog_error(client: CDPClient) -> str:
    root = client.document()
    for node_id in client.query_all(_STUDIO_DIALOG_ERROR_SELECTOR, root):
        if client.box_model(node_id) is None:
            continue
        text = _cdp_plain_text(client.outer_html(node_id)).strip()
        if text:
            return text
    return ""


def _cdp_read_points(client: CDPClient) -> int | None:
    root = client.document()
    candidates: list[str] = []
    for node_id in client.query_all(_STUDIO_POINTS_SELECTOR, root)[:80]:
        attrs = client.attributes(node_id)
        text = _cdp_plain_text(client.outer_html(node_id))
        combined = " ".join(
            part.strip()
            for part in (
                text,
                attrs.get("aria-label", ""),
                attrs.get("title", ""),
                attrs.get("data-testid", ""),
            )
            if part and part.strip()
        )
        if combined:
            candidates.append(combined)
    return extract_points(candidates)


def _cdp_wait_studio_tab(
    address: str,
    *,
    status: StatusCallback,
    background: bool,
    browser_name: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Wait for a non-blocking Studio page target.

    Returns ``(target, captcha_pending)``. A blocking "Just a moment"
    interstitial aborts a background run immediately (pending); a foreground
    run waits the same 10-minute human-verification budget the Selenium flow
    uses.
    """
    deadline = time.monotonic() + HUMAN_VERIFICATION_TIMEOUT_SECONDS
    announced = False
    while True:
        target = find_page_target(address, "studio.creativefabrica.com")
        if target is not None:
            blocking = is_human_verification_content(
                str(target.get("url") or ""),
                str(target.get("title") or ""),
                "",
            )
            if not blocking:
                return target, False
            if not announced:
                status(
                    f"检测到 Studio AI 人机验证，请在 {browser_name} 窗口中手动完成；"
                    "完成后程序会自动继续"
                )
                announced = True
            if background:
                return None, True
        if time.monotonic() >= deadline:
            return None, False
        time.sleep(0.5)


def studio_login_via_cdp(
    debugger_address: str,
    *,
    email: str,
    status: StatusCallback,
    background: bool = False,
    browser_name: str = "Brave",
) -> StudioCDPResult:
    """Open / attach the Studio tab over raw CDP and log in, without ChromeDriver.

    Mirrors ``CreativeFabricaBrowser._ensure_studio_login`` but every action
    is a ``DOM`` / ``Input`` CDP command: trusted clicks and typing, no
    ``Runtime.enable`` and no ``Runtime.evaluate``. The browser window is
    left open on every non-success outcome.
    """
    target, pending = _cdp_wait_studio_tab(
        debugger_address,
        status=status,
        background=background,
        browser_name=browser_name,
    )
    if target is None:
        if pending:
            return StudioCDPResult(STUDIO_CDP_CAPTCHA_PENDING)
        status(
            "等待 Studio AI 页面加载超时；浏览器窗口已保留，"
            "完成后请再次点击“自动打开”。"
        )
        return StudioCDPResult(STUDIO_CDP_FAILED, detail="Studio 页面加载超时")
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="浏览器调试连接缺少 WebSocket 地址"
        )
    try:
        with CDPClient(str(ws_url)) as client:
            return _cdp_studio_login(
                client,
                email=email,
                status=status,
                background=background,
                browser_name=browser_name,
            )
    except CDPError as exc:
        return StudioCDPResult(STUDIO_CDP_FAILED, detail=str(exc))


def _cdp_studio_login(
    client: CDPClient,
    *,
    email: str,
    status: StatusCallback,
    background: bool,
    browser_name: str,
) -> StudioCDPResult:
    # Wait for a Log in control or for positive logged-in evidence (points).
    ready_deadline = time.monotonic() + PAGE_READY_TIMEOUT_SECONDS
    while True:
        control = _cdp_find_login_control(client)
        if control:
            break
        points = _cdp_read_points(client)
        if points is not None:
            status(f"{email} 已保持登录，正在进入 Studio AI…")
            status(f"{email} 当前积分：{points:,}")
            return StudioCDPResult(STUDIO_CDP_SUCCESS, points=points)
        if time.monotonic() >= ready_deadline:
            # No header Log in control — treat the session as already signed
            # in and try to read points, instead of touching the www login.
            points = _cdp_read_points(client)
            return StudioCDPResult(STUDIO_CDP_SUCCESS, points=points)
        time.sleep(0.25)

    status("正在同步 Creative Fabrica 登录状态到 Studio AI…")
    if not client.click_node(control):
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="Studio 登录按钮不可点击，窗口已保留"
        )
    dialog = _cdp_wait_dialog(client, STUDIO_DIALOG_WAIT_SECONDS)
    if not dialog:
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="Studio 登录窗口未打开，窗口已保留"
        )

    if _cdp_find_text_control(client, dialog, ("REGISTER FOR FREE",)):
        toggle = _cdp_find_text_control(client, dialog, ("Log in",))
        if not toggle:
            return StudioCDPResult(
                STUDIO_CDP_FAILED, detail="未找到登录/注册切换按钮，窗口已保留"
            )
        if not client.click_node(toggle):
            return StudioCDPResult(
                STUDIO_CDP_FAILED, detail="登录切换按钮不可点击，窗口已保留"
            )
        dialog = _cdp_wait_dialog(client, STUDIO_TOGGLE_DIALOG_WAIT_SECONDS)
        if not dialog:
            return StudioCDPResult(
                STUDIO_CDP_FAILED, detail="Studio 登录窗口未打开，窗口已保留"
            )

    email_node = client.query("input[name='email']", dialog) or client.query(
        "input[name='email']"
    )
    password_node = client.query("input[name='password']", dialog) or client.query(
        "input[name='password']"
    )
    if not email_node or not password_node:
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="未找到 Studio 登录表单，窗口已保留"
        )
    client.fill_text(email_node, email)
    client.fill_text(password_node, email)

    submit = 0
    for node_id in client.query_all("button[type='submit']", dialog):
        if client.box_model(node_id) is None:
            continue
        if _cdp_plain_text(client.outer_html(node_id)).strip().upper() == "LOG IN":
            submit = node_id
            break
    if not submit:
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="未找到 LOG IN 提交按钮，窗口已保留"
        )
    if not client.click_node(submit):
        return StudioCDPResult(
            STUDIO_CDP_FAILED, detail="LOG IN 按钮不可点击，窗口已保留"
        )

    deadline = time.monotonic() + STUDIO_LOGIN_POLL_SECONDS
    captcha_deadline = time.monotonic() + HUMAN_VERIFICATION_TIMEOUT_SECONDS
    announced_captcha = False
    while time.monotonic() < (
        captcha_deadline if announced_captcha else deadline
    ):
        visible_login = _cdp_find_login_control(client) != 0
        visible_dialog = _cdp_visible_dialog(client) != 0
        if not visible_login and not visible_dialog:
            if announced_captcha:
                status("Studio AI 人机验证已完成，继续读取积分…")
            points = _cdp_wait_points(client, email, status)
            return StudioCDPResult(STUDIO_CDP_SUCCESS, points=points)
        if _cdp_has_captcha(client):
            if not announced_captcha:
                status(
                    f"Studio AI 登录需要 CAPTCHA，请在 {browser_name} 中完成"
                )
                announced_captcha = True
                if background:
                    return StudioCDPResult(STUDIO_CDP_CAPTCHA_PENDING)
        else:
            error_text = _cdp_dialog_error(client)
            if error_text:
                status(f"Studio AI 登录失败：{error_text}；浏览器窗口已保留")
                return StudioCDPResult(STUDIO_CDP_FAILED, detail=error_text)
        time.sleep(1)

    if announced_captcha:
        # Kept the window the whole foreground wait; finishing here must look
        # like the manual-follow-up state, never like a ChromeDriver fallback.
        status(
            f"Studio AI 人机验证仍在进行；请在 {browser_name} 窗口完成后再次点击"
            "“自动打开”"
        )
        return StudioCDPResult(STUDIO_CDP_CAPTCHA_PENDING)
    return StudioCDPResult(
        STUDIO_CDP_FAILED, detail="Studio AI 登录等待超时，窗口已保留"
    )


def _cdp_wait_points(
    client: CDPClient, email: str, status: StatusCallback
) -> int | None:
    status("正在读取 Studio AI 积分…")
    deadline = time.monotonic() + STUDIO_POINTS_WAIT_SECONDS
    while True:
        points = _cdp_read_points(client)
        if points is not None:
            status(f"{email} 当前积分：{points:,}")
            return points
        if time.monotonic() >= deadline:
            status("Studio AI 已登录，但任务栏中暂未识别到积分")
            return None
        time.sleep(1)


class CreativeFabricaBrowser:
    """Drive one Chromium window while keeping the browser profile independent."""

    def __init__(
        self,
        *,
        address: str,
        profile_path: Path | None,
        browser_path: Path | None,
        mail_client: Any,
        status: StatusCallback,
        points_updated: PointsCallback,
        background: bool = False,
        authenticated: AuthenticatedCallback | None = None,
        debugger_address: str | None = None,
        browser_name: str | None = None,
        browser_version: str | None = None,
        launch_arguments: tuple[str, ...] = (),
        reset_site_session: bool = False,
        clear_site_data_on_logout: bool = False,
        driver: Any | None = None,
    ) -> None:
        self.address = address
        self.profile_path = profile_path
        self.browser_path = browser_path
        self.browser_name = browser_name or (
            browser_display_name(browser_path) if browser_path else "Donut"
        )
        self.debugger_address = debugger_address
        self.browser_version = browser_version
        self.launch_arguments = launch_arguments
        self.reset_site_session = reset_site_session
        self.clear_site_data_on_logout = clear_site_data_on_logout
        self.mail_client = mail_client
        self.status = status
        self.points_updated = points_updated
        self.background = background
        self.authenticated = authenticated or (lambda: None)
        self.driver = driver
        self.human_verification_pending = False

    def run(self):
        """Open the selected browser, try login, then register when needed.

        The session now starts on ``studio.creativefabrica.com`` instead of
        the main www site, because Cloudflare's "Just a moment…"
        interstitial blocks the www login/register flow. Only when Studio
        rejects the login (Cloudflare, dialog error, background CAPTCHA
        abort) do we fall back to ``_navigate(LOGIN_URL)`` +
        ``_continue_login_flow`` for the legacy www register / OTP path.
        """
        webdriver, By, WebDriverWait = self._selenium_imports()
        if self.driver is not None:
            if self._begin_studio_session(By, WebDriverWait):
                return self.driver
            if self.reset_site_session:
                self._logout_before_reassignment(By)
            self._navigate(LOGIN_URL, By)
            return self._continue_login_flow(By, WebDriverWait)
        if self.debugger_address:
            self.status(f"正在连接 {self.address} 的 {self.browser_name} 指纹配置…")
            self.driver = self._attach_to_debugger(webdriver)
            if self._begin_studio_session(By, WebDriverWait):
                return self.driver
            if self.reset_site_session:
                self._logout_before_reassignment(By)
            self._navigate(LOGIN_URL, By)
            return self._continue_login_flow(By, WebDriverWait)

        if self.profile_path is None or self.browser_path is None:
            raise RuntimeError("浏览器启动参数不完整")

        debugger_address = existing_debugger_address(self.profile_path)
        debugger_live = bool(
            debugger_address and debugger_address_is_live(debugger_address)
        )
        if self.background and debugger_live:
            raise RuntimeError(
                f"该邮箱的 {self.browser_name} 正在打开，请先关闭窗口后再执行后台注册"
            )
        self.driver = None
        if debugger_address and debugger_live:
            # A live manual window is already on the profile. Try to attach
            # to it. On failure we must NOT terminate it (the user has the
            # Cloudflare clearance there) and must NOT relaunch a new browser
            # (that would throw away the clearance and the cookies).
            attach_options = self._attach_options(webdriver, debugger_address)
            self.status(f"正在连接 {self.address} 已打开的 {self.browser_name}…")
            try:
                self.driver = webdriver.Chrome(options=attach_options)
                # Creating ChromeDriver can succeed even when the detached target
                # disappears during attachment. Force a real round trip now.
                if not self.driver.window_handles:
                    raise RuntimeError(f"{self.browser_name} 没有可用窗口")
            except Exception as exc:
                self.driver = None
                if browser_was_closed(exc):
                    raise
                raise RuntimeError(
                    f"{self.browser_name} 手动窗口已打开但无法连接；"
                    "请保持窗口打开，再次点击“自动打开”重试。"
                ) from exc
        elif debugger_address:
            # Stale DevToolsActivePort left by a previous session. Clean up
            # the orphaned process and relaunch a fresh browser so this run
            # can proceed.
            if terminate_stale_profile_browser(
                self.profile_path, self.browser_path
            ):
                self.status(
                    f"{self.address} 的旧 {self.browser_name} 调试连接已失效，正在恢复窗口…"
                )
            options = self._browser_options(webdriver)
            options.add_argument(f"--user-data-dir={self.profile_path}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            self._configure_window_options(options)
            if self.background:
                self.status(f"正在后台处理 {self.address} 的登录/注册…")
            else:
                self.status(f"正在启动 {self.address} 的 {self.browser_name}…")
            self.driver = webdriver.Chrome(options=options)
        else:
            options = self._browser_options(webdriver)
            options.add_argument(f"--user-data-dir={self.profile_path}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            self._configure_window_options(options)
            if self.background:
                self.status(f"正在后台处理 {self.address} 的登录/注册…")
            else:
                self.status(f"正在启动 {self.address} 的 {self.browser_name}…")
            self.driver = webdriver.Chrome(options=options)
        if self._begin_studio_session(By, WebDriverWait):
            return self.driver
        if self.reset_site_session:
            self._logout_before_reassignment(By)
        self._navigate(LOGIN_URL, By)
        return self._continue_login_flow(By, WebDriverWait)

    def _begin_studio_session(self, By, WebDriverWait) -> bool:
        """Start the session on Studio instead of the Cloudflare-blocked www site.

        Navigates to ``STUDIO_URL`` and runs the Studio-side login. On
        success ``authenticated()`` and ``_update_points`` are called and
        the caller MUST NOT open ``LOGIN_URL`` or call
        ``_continue_login_flow``. On failure the caller should fall back
        to ``_navigate(LOGIN_URL)`` + ``_continue_login_flow`` so the
        legacy www register / OTP path still works.
        """
        self._adopt_studio_tab()
        self._navigate(STUDIO_URL, By)
        if not self._ensure_studio_login(By, WebDriverWait):
            return False
        self.authenticated()
        self._update_points(By, WebDriverWait)
        return True

    def _adopt_studio_tab(self) -> None:
        """Keep one Studio tab; close restored www.creativefabrica.com extras.

        Named Brave profiles restore the last session, so ``--new-window``
        plus ``STUDIO_URL`` often leaves a leftover www tab beside Studio.
        Login and points only run on the current handle, so extras stay
        logged out and look like a second homepage.
        """
        try:
            handles = list(self.driver.window_handles)
        except Exception:
            return
        if not handles:
            return

        studio_handles: list[str] = []
        www_handles: list[str] = []
        current = None
        try:
            current = self.driver.current_window_handle
        except Exception:
            current = None

        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                url = (self.driver.current_url or "").lower()
            except Exception:
                continue
            if "studio.creativefabrica.com" in url:
                studio_handles.append(handle)
            elif "www.creativefabrica.com" in url:
                www_handles.append(handle)

        keep = studio_handles[0] if studio_handles else current or handles[0]
        extras = [
            handle
            for handle in (*www_handles, *studio_handles[1:])
            if handle != keep
        ]
        for handle in extras:
            try:
                self.driver.switch_to.window(handle)
                self.driver.close()
            except Exception:
                continue
        try:
            self.driver.switch_to.window(keep)
        except Exception:
            try:
                remaining = list(self.driver.window_handles)
            except Exception:
                return
            if remaining:
                try:
                    self.driver.switch_to.window(remaining[0])
                except Exception:
                    return

    def _configure_window_options(self, options: Any) -> None:
        """Show automated browser windows while detaching only interactive flows."""
        if self.background:
            options.add_argument("--start-maximized")
            options.add_argument("--window-size=1440,1000")
            return
        options.add_argument("--start-maximized")
        options.add_experimental_option("detach", True)

    def _logout_before_reassignment(self, By) -> None:
        """Log out at the main account page before assigning a different account."""
        # Studio AI has no logout entry. Always inspect the main account page
        # first; its logout link/button is the authoritative login signal.
        self._navigate(ACCOUNT_URL, By)
        controls = self._wait_for_account_logout_controls(By)
        if not controls:
            self.status("Creative Fabrica 账户页未检测到已登录账号，继续登录目标账号…")
            self._navigate(LOGIN_URL, By)
            return

        self.status("正在 Creative Fabrica 账户页退出旧账号…")
        try:
            controls[0].click()
        except Exception:
            raise RuntimeError("无法点击 Creative Fabrica 账户页的退出按钮。")

        deadline = time.monotonic() + LOGOUT_WAIT_SECONDS
        while True:
            still_logged_in = bool(self._account_logout_controls(By))
            if not still_logged_in:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Creative Fabrica 账户页退出未完成，已停止登录新账号。"
                )
            time.sleep(0.2)
        if self.clear_site_data_on_logout:
            try:
                self.driver.delete_all_cookies()
                self.driver.execute_script(
                    "window.localStorage.clear(); window.sessionStorage.clear();"
                )
            except Exception:
                pass
        self._navigate(LOGIN_URL, By)
        self._verify_logged_out_login_page(By)

    def _wait_for_account_logout_controls(self, By) -> list[Any]:
        """Wait until the account page proves whether a session is signed in."""
        deadline = time.monotonic() + PAGE_READY_TIMEOUT_SECONDS
        while True:
            self._dismiss_account_dialogs(By)
            controls = self._account_logout_controls(By)
            if controls:
                return controls
            try:
                login_forms = self.driver.find_elements(
                    By.CSS_SELECTOR, "main form.woocommerce-form-login"
                )
            except Exception:
                login_forms = []
            if login_forms:
                return []
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Creative Fabrica 账户页加载超时，无法确认旧账号是否已登录。"
                )
            time.sleep(0.25)

    def _dismiss_account_dialogs(self, By) -> bool:
        """Close promotional dialogs that cover the account-page logout control."""
        try:
            dialogs = self.driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
        except Exception:
            return False
        dismissed = False
        for dialog in dialogs:
            try:
                if hasattr(dialog, "is_displayed") and not dialog.is_displayed():
                    continue
                buttons = dialog.find_elements(By.CSS_SELECTOR, "button")
            except Exception:
                continue
            close_button = None
            for button in reversed(buttons):
                try:
                    label = " ".join(
                        part
                        for part in (
                            button.text,
                            button.get_attribute("aria-label"),
                            button.get_attribute("title"),
                        )
                        if part
                    ).strip().lower()
                except Exception:
                    label = ""
                if any(marker in label for marker in ("close", "dismiss", "关闭")):
                    close_button = button
                    break
                if label:
                    continue
                try:
                    if button.find_elements(By.CSS_SELECTOR, "svg, [data-icon]"):
                        close_button = button
                        break
                except Exception:
                    continue
            if close_button is None:
                continue
            try:
                close_button.click()
            except Exception:
                try:
                    self.driver.execute_script("arguments[0].click();", close_button)
                except Exception:
                    continue
            dismissed = True
            self.status("已关闭 Creative Fabrica 账户页弹窗，继续检查 Logout…")
            time.sleep(0.2)
        return dismissed

    def _verify_logged_out_login_page(self, By) -> None:
        """Refuse to continue when the old account still owns the shared session."""
        deadline = time.monotonic() + PAGE_READY_TIMEOUT_SECONDS
        while True:
            self._dismiss_account_dialogs(By)
            if self._account_logout_controls(By):
                raise RuntimeError(
                    "Creative Fabrica 旧账号仍处于登录状态，已停止登录新账号。"
                )
            try:
                login_forms = self.driver.find_elements(
                    By.CSS_SELECTOR, "main form.woocommerce-form-login"
                )
            except Exception:
                login_forms = []
            if login_forms or self._has_human_verification(By):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Creative Fabrica 退出后未显示登录页，已停止登录新账号。"
                )
            time.sleep(0.25)

    def _account_logout_controls(self, By) -> list[Any]:
        """Find either version of Creative Fabrica's account-page logout UI."""
        try:
            candidates = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a[href*='customer-logout'], a[href*='logout'], button",
            )
        except Exception:
            return []
        controls: list[Any] = []
        for candidate in candidates:
            try:
                href = (candidate.get_attribute("href") or "").lower()
            except Exception:
                href = ""
            try:
                label = " ".join(
                    part
                    for part in (
                        candidate.text,
                        candidate.get_attribute("aria-label"),
                        candidate.get_attribute("title"),
                    )
                    if part
                ).lower()
            except Exception:
                label = ""
            if "logout" in href or "logout" in label.replace(" ", ""):
                controls.append(candidate)
        return controls

    def _continue_login_flow(self, By, WebDriverWait):
        """Run the shared login/registration flow after a driver is ready."""

        self.status(f"正在尝试登录 {self.address}…")
        login_form = self._wait_for_page_form(
            By,
            "main form.woocommerce-form-login",
            page_name="登录页",
        )
        if login_form is None:
            return self.driver
        if login_form is True:
            self.status(f"{self.address} 已保持登录，正在进入 Studio AI…")
            self.authenticated()
            self._update_points(By, WebDriverWait)
            return self.driver

        login_form.find_element(By.CSS_SELECTOR, "input[name='username']").send_keys(self.address)
        login_form.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(self.address)
        login_form.find_element(By.CSS_SELECTOR, "button[name='login']").click()

        login_result = self._wait_for_login_result(By, timeout=12)
        if login_result is True:
            self.status(f"{self.address} 已登录 Creative Fabrica")
            self.authenticated()
            self._update_points(By, WebDriverWait)
            return self.driver
        if login_result is None:
            return self.driver

        if self.reset_site_session:
            # A reused profile must never carry an authenticated account into
            # registration, including when the preceding login attempt failed.
            self.status("注册前正在确认当前 Creative Fabrica 账户已退出…")
            self._logout_before_reassignment(By)
        self._navigate(SIGNUP_URL, By)
        register_form = self._wait_for_page_form(
            By,
            "main form.woocommerce-form-register",
            page_name="注册页",
        )
        if register_form is None or register_form is True:
            return self.driver
        register_form.find_element(By.CSS_SELECTOR, "input[name='email']").send_keys(self.address)
        register_form.find_element(By.CSS_SELECTOR, "input[name='password']").send_keys(self.address)

        self.status(f"登录未成功，正在直接注册 {self.address}…")
        register_form.find_element(By.CSS_SELECTOR, "button[name='register']").click()
        self.status("注册信息已提交；如浏览器显示 CAPTCHA，请手动完成")

        otp_input = self._wait_for_otp_field(By, timeout=180)
        if otp_input is None:
            if self._is_logged_in(By):
                self.status(f"{self.address} 注册并登录成功")
                self.authenticated()
                self.points_updated(DEFAULT_NEW_ACCOUNT_POINTS)
                self._update_points(By, WebDriverWait)
            else:
                self.status(f"仍在等待网站验证；请查看 {self.browser_name} 窗口")
            return self.driver

        self.status("网站正在等待邮箱验证码，开始检查临时邮箱…")
        code = self._wait_for_mail_code(timeout=240)
        if not code:
            self.status("未在有效时间内读取到验证码，请在收件箱中手动查看")
            return self.driver

        otp_input.clear()
        otp_input.send_keys(code)
        self.status(f"已读取验证码 {code}，正在完成验证…")
        submit = self.driver.find_element(
            By.CSS_SELECTOR, "main form.woocommerce-form-register button[name='register']"
        )
        submit.click()
        verification_result = self._wait_for_login_result(By, timeout=25)
        if verification_result is True:
            self.status(f"{self.address} 注册并登录成功")
            self.authenticated()
            self.points_updated(DEFAULT_NEW_ACCOUNT_POINTS)
            self._update_points(By, WebDriverWait)
        elif verification_result is False:
            self.status(f"验证码已填写并提交，请在 {self.browser_name} 窗口确认结果")
        return self.driver

    def _attach_options(self, webdriver, debugger_address: str):
        """ChromeOptions for connecting to an already-running Chromium.

        ChromeDriver rejects launch-only keys such as ``excludeSwitches``
        when ``debuggerAddress`` is set (``unrecognized chrome option``).
        Keep this object to debugger address + optional browser version.
        """
        options = webdriver.ChromeOptions()
        options.add_experimental_option("debuggerAddress", debugger_address)
        if self.browser_version:
            options.browser_version = self.browser_version
        return options

    def _attach_to_debugger(self, webdriver):
        """Attach ChromeDriver to a browser process launched by Donut."""
        options = self._attach_options(webdriver, self.debugger_address or "")
        driver = webdriver.Chrome(options=options)
        if not driver.window_handles:
            raise RuntimeError(f"{self.browser_name} 没有可用窗口")
        return driver

    def refresh_points_only(self, driver):
        """Refresh Studio points using an already controlled Chrome window."""
        self.driver = driver
        _webdriver, By, WebDriverWait = self._selenium_imports()
        self._update_points(By, WebDriverWait)
        return self.driver

    def _update_points(self, By, WebDriverWait) -> None:
        """Open Studio and replace the fallback balance with the visible value."""
        try:
            self.status("正在读取 Studio AI 积分…")
            # Prefer ``_navigate`` over ``driver.get`` so we don't re-trigger
            # Cloudflare when the page is already on Studio.
            self._navigate(STUDIO_URL, By)
            WebDriverWait(self.driver, 25).until(
                lambda driver: driver.execute_script("return document.readyState")
                in {"interactive", "complete"}
            )
            if not self._ensure_studio_login(By, WebDriverWait):
                self.status("主站已登录，但 Studio AI 登录未完成，无法读取积分")
                return
            holder: dict[str, int] = {}

            def find_points(_driver) -> bool:
                points = self._read_studio_points(By)
                if points is None:
                    return False
                holder["points"] = points
                return True

            try:
                WebDriverWait(self.driver, 35).until(find_points)
            except Exception:
                self.status("Studio AI 已登录，但任务栏中暂未识别到积分")
                return
            points = holder["points"]
            self.points_updated(points)
            self.status(f"{self.address} 当前积分：{points:,}")
        except Exception as exc:
            self.status(f"登录成功，积分更新失败：{exc}")

    def _ensure_studio_login(self, By, WebDriverWait) -> bool:
        """Log into Studio's own account dialog when SSO has not propagated.

        Waits for any Cloudflare challenge on Studio to clear (reusing the
        10-minute human-verification wait) before checking the header
        "Log in" button. Studio never exposes a WooCommerce login form, so
        we deliberately do **not** wait for ``main form.woocommerce-form-login``.
        """
        if not self._wait_for_studio_ready(By, WebDriverWait):
            return False
        login_button = self._wait_for_studio_login_control(By)
        if login_button is None:
            return self._studio_logged_in_evidence(By)

        self.status("正在同步 Creative Fabrica 登录状态到 Studio AI…")
        login_button.click()
        dialog = WebDriverWait(self.driver, 15).until(
            lambda driver: driver.find_element(By.CSS_SELECTOR, "[role='dialog']")
        )
        register_buttons = dialog.find_elements(
            By.XPATH, ".//button[normalize-space()='REGISTER FOR FREE']"
        )
        if any(item.is_displayed() for item in register_buttons):
            toggles = dialog.find_elements(
                By.XPATH, ".//span[normalize-space()='Log in']"
            )
            toggle = next((item for item in toggles if item.is_displayed()), None)
            if toggle is None:
                return False
            toggle.click()
            dialog = WebDriverWait(self.driver, 10).until(
                lambda driver: driver.find_element(By.CSS_SELECTOR, "[role='dialog']")
            )

        email_input = dialog.find_element(By.CSS_SELECTOR, "input[name='email']")
        password_input = dialog.find_element(By.CSS_SELECTOR, "input[name='password']")
        email_input.clear()
        email_input.send_keys(self.address)
        password_input.clear()
        password_input.send_keys(self.address)
        submit_buttons = dialog.find_elements(By.CSS_SELECTOR, "button[type='submit']")
        submit = next(
            (
                item
                for item in submit_buttons
                if item.is_displayed() and item.text.strip().upper() == "LOG IN"
            ),
            None,
        )
        if submit is None:
            return False
        submit.click()

        deadline = time.monotonic() + 180
        announced_captcha = False
        while time.monotonic() < deadline:
            visible_login = self._studio_login_control(By) is not None
            visible_dialog = any(
                item.is_displayed()
                for item in self.driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
            )
            if not visible_login and not visible_dialog:
                if announced_captcha:
                    self.human_verification_pending = False
                    self.status("Studio AI 人机验证已完成，继续读取积分…")
                return True
            captcha = self.driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='recaptcha'], iframe[title*='challenge']",
            )
            if captcha and not announced_captcha:
                self.human_verification_pending = True
                self.status(f"Studio AI 登录需要 CAPTCHA，请在 {self.browser_name} 中完成")
                announced_captcha = True
                if self.background:
                    return False
            errors = self.driver.find_elements(
                By.CSS_SELECTOR,
                "[role='dialog'] [role='alert'], [role='dialog'] .text-red-500",
            )
            if any(item.is_displayed() and item.text.strip() for item in errors):
                return False
            time.sleep(1)
        return False

    def _wait_for_studio_login_control(self, By):
        """Wait briefly for a Studio Log in control to paint."""
        deadline = time.monotonic() + PAGE_READY_TIMEOUT_SECONDS
        while True:
            control = self._studio_login_control(By)
            if control is not None:
                return control
            if self._studio_logged_in_evidence(By):
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    def _studio_login_control(self, By):
        """Return a visible Log in / Sign in control, or None."""
        try:
            candidates = self.driver.find_elements(
                By.CSS_SELECTOR,
                "header button, header a, button[aria-label], a[aria-label]",
            )
        except Exception:
            candidates = []
        for item in candidates:
            try:
                if hasattr(item, "is_displayed") and not item.is_displayed():
                    continue
                label = " ".join(
                    part
                    for part in (
                        getattr(item, "text", None),
                        item.get_attribute("aria-label") if hasattr(item, "get_attribute") else "",
                        item.get_attribute("title") if hasattr(item, "get_attribute") else "",
                    )
                    if part
                ).strip().lower()
            except Exception:
                continue
            compact = label.replace(" ", "")
            if (
                "login" in compact
                or "signin" in compact
                or "log in" in label
                or "sign in" in label
            ):
                return item
        return None

    def _studio_logged_in_evidence(self, By) -> bool:
        """True only with positive logged-in evidence, not 'no Log in button'."""
        try:
            return self._read_studio_points(By) is not None
        except Exception:
            return False

    def _wait_for_studio_ready(self, By, WebDriverWait) -> bool:
        """Wait until Studio stops showing a blocking Cloudflare challenge.

        Reuses the 10-minute human-verification wait used by the www flow.
        Returns True once the page is no longer an interstitial, False when
        the wait timed out (background workers) or the user closed the
        browser.
        """
        deadline = time.monotonic() + HUMAN_VERIFICATION_TIMEOUT_SECONDS
        announced = False
        while True:
            if self._has_human_verification(By):
                self.human_verification_pending = True
                if not announced:
                    self.status(
                        f"检测到 Studio AI 人机验证，请在 {self.browser_name} 窗口中手动完成；"
                        "完成后程序会自动继续"
                    )
                    announced = True
                if self.background:
                    return False
                if time.monotonic() >= deadline:
                    self.status(
                        "等待 Studio AI 人机验证超时；浏览器窗口已保留，完成后请再次点击"
                    )
                    return False
                time.sleep(0.5)
                continue
            if announced:
                self.human_verification_pending = False
                self.status("Studio AI 人机验证已完成，继续检查登录状态…")
            return True

    def _read_studio_points(self, By) -> int | None:
        candidates: list[str] = []
        elements = self.driver.find_elements(
            By.CSS_SELECTOR,
            "header button, header a, [aria-label*='coin' i], "
            "[title*='coin' i], [data-testid*='coin' i], [class*='coin' i]",
        )
        for element in elements[:80]:
            parts = [
                element.text,
                element.get_attribute("aria-label") or "",
                element.get_attribute("title") or "",
                element.get_attribute("data-testid") or "",
            ]
            combined = " ".join(part.strip() for part in parts if part and part.strip())
            if combined:
                candidates.append(combined)
            try:
                parent = element.find_element(By.XPATH, "..")
                parent_parts = [
                    parent.text,
                    parent.get_attribute("aria-label") or "",
                    parent.get_attribute("title") or "",
                ]
                parent_combined = " ".join(
                    part.strip() for part in parent_parts if part and part.strip()
                )
            except Exception:
                parent_combined = ""
            if parent_combined:
                candidates.append(parent_combined)
        return extract_points(candidates)

    @staticmethod
    def _selenium_imports():
        try:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
        except ImportError as exc:
            raise RuntimeError("缺少 Selenium，请运行：python -m pip install -r requirements.txt") from exc
        return webdriver, By, WebDriverWait

    def _browser_options(self, webdriver):
        """Build options compatible with the selected Chromium browser."""
        options = webdriver.ChromeOptions()
        if self.browser_path is not None:
            options.binary_location = str(self.browser_path)
        if self.browser_name == "Brave":
            # Brave 150 exits during startup when ChromeDriver injects
            # --test-type=webdriver. Excluding only that crashing switch keeps
            # the normal WebDriver automation indicator enabled.
            options.add_experimental_option("excludeSwitches", ["test-type"])
        for argument in self.launch_arguments:
            options.add_argument(argument)
        return options

    def _is_logged_in(self, By) -> bool:
        url = self.driver.current_url.lower()
        logout = self.driver.find_elements(By.CSS_SELECTOR, "a[href*='logout']")
        login_form = self.driver.find_elements(By.CSS_SELECTOR, "main form.woocommerce-form-login")
        return is_main_site_authenticated(
            url,
            has_logout=bool(logout),
            has_login_form=bool(login_form),
        )

    def _has_human_verification(self, By) -> bool:
        """Return whether the current page is a Cloudflare/Turnstile challenge.

        Polling rules:

        * Read ``current_url`` and ``title`` first; either is enough to flag
          the known interstitial markers.
        * Detect first-party site forms with selectors already used by the
          flow. Their presence means the page is site-ready even if a leftover
          Turnstile widget still sits on the form.
        * Only inspect **main-document** challenge markers
          (``#challenge-running``, ``#challenge-stage``) when computing
          ``has_challenge_frame``. Walking challenge iframes resets the
          Turnstile widget and is forbidden.
        * Read ``body.text`` only as a last resort; never walk challenge
          iframes to gather text.
        """
        try:
            url = self.driver.current_url or ""
        except Exception:
            url = ""
        try:
            title = self.driver.title or ""
        except Exception:
            title = ""

        title_text = (title or "").strip().lower()
        if title_text.startswith("just a moment"):
            return True
        if "cdn-cgi/challenge-platform" in (url or "").lower():
            return True

        try:
            login_forms = self.driver.find_elements(
                By.CSS_SELECTOR, LOGIN_FORM_SELECTOR
            )
        except Exception:
            login_forms = []
        try:
            register_forms = self.driver.find_elements(
                By.CSS_SELECTOR, REGISTER_FORM_SELECTOR
            )
        except Exception:
            register_forms = []
        has_site_form = bool(login_forms) or bool(register_forms)

        try:
            challenge_elements = self.driver.find_elements(
                By.CSS_SELECTOR, "#challenge-running, #challenge-stage"
            )
        except Exception:
            challenge_elements = []
        has_challenge_frame = bool(challenge_elements)

        if has_site_form or has_challenge_frame:
            return is_human_verification_content(
                url,
                title,
                "",
                has_challenge_frame=has_challenge_frame,
                has_site_form=has_site_form,
            )

        # Inconclusive from title/url/form/stage; read the main-document body
        # text only.
        try:
            bodies = self.driver.find_elements(By.TAG_NAME, "body")
        except Exception:
            bodies = []
        body_text = ""
        if bodies:
            try:
                body_text = bodies[0].text or ""
            except Exception:
                body_text = ""
        return is_human_verification_content(
            url,
            title,
            body_text,
            has_challenge_frame=False,
            has_site_form=False,
        )

    def _navigate(self, url: str, By) -> None:
        """Navigate without failing when ChromeDriver races a visible challenge page.

        When the current page is already the requested URL (trailing slash and
        case ignored) and the user is not staring at a blocking interstitial,
        do not reload. Reloading ``/login/`` right after the user just passed
        Cloudflare often triggers a brand-new challenge.
        """
        target_normalized = self._normalize_url(url)
        try:
            current_url = self.driver.current_url or ""
        except Exception:
            current_url = ""
        if target_normalized and self._normalize_url(current_url) == target_normalized:
            if not self._has_human_verification(By):
                return
            # Same URL but a blocking interstitial is on top. Treat this as
            # the wait loop's responsibility rather than reloading.
            self.human_verification_pending = True
            return

        try:
            self.driver.get(url)
            return
        except Exception as exc:
            if browser_was_closed(exc):
                raise
            navigation_error = exc

        deadline = time.monotonic() + NAVIGATION_CHALLENGE_GRACE_SECONDS
        while True:
            if self._has_human_verification(By):
                self.human_verification_pending = True
                # Chrome sometimes reports an empty navigation error even though
                # Cloudflare's challenge is already visible and interactive.
                # The normal wait loop can safely take over from here.
                return
            if time.monotonic() >= deadline:
                raise navigation_error
            time.sleep(0.25)

    @staticmethod
    def _normalize_url(value: str) -> str:
        return (value or "").rstrip("/").lower()

    @staticmethod
    def _retry_page_probe(
        error: Exception,
        *,
        now: float,
        retry_deadline: float | None,
    ) -> float:
        """Return a retry deadline for a transient page-query failure."""
        if browser_was_closed(error):
            raise error
        if retry_deadline is None:
            retry_deadline = now + PAGE_PROBE_RETRY_SECONDS
        if now >= retry_deadline:
            raise error
        return retry_deadline

    def _wait_for_page_form(self, By, selector: str, *, page_name: str):
        """Wait for a main-site form, pausing safely for manual verification."""
        normal_deadline = time.monotonic() + PAGE_READY_TIMEOUT_SECONDS
        verification_deadline: float | None = None
        announced_verification = False
        probe_retry_deadline: float | None = None
        while True:
            now = time.monotonic()
            if self._has_human_verification(By):
                self.human_verification_pending = True
                probe_retry_deadline = None
                if not announced_verification:
                    self.status(
                        f"检测到 CAPTCHA 人机验证，请在 {self.browser_name} 窗口中手动完成；"
                        "完成后程序会自动继续"
                    )
                    announced_verification = True
                    verification_deadline = (
                        now + HUMAN_VERIFICATION_TIMEOUT_SECONDS
                    )
                if self.background:
                    return None
                if verification_deadline is not None and now >= verification_deadline:
                    self.status(
                        "等待人机验证超时；浏览器窗口已保留，完成后请再次点击“打开”"
                    )
                    return None
                time.sleep(0.5)
                continue

            if announced_verification:
                self.human_verification_pending = False
                self.status(f"人机验证已完成，继续处理 {page_name}…")
                normal_deadline = now + PAGE_READY_TIMEOUT_SECONDS
                announced_verification = False
                verification_deadline = None

            try:
                forms = self.driver.find_elements(By.CSS_SELECTOR, selector)
                if forms:
                    return forms[0]
                if self._is_logged_in(By):
                    return True
                probe_retry_deadline = None
            except Exception as exc:
                probe_retry_deadline = self._retry_page_probe(
                    exc,
                    now=now,
                    retry_deadline=probe_retry_deadline,
                )
                time.sleep(0.5)
                continue

            if now >= normal_deadline:
                raise RuntimeError(f"{page_name}加载超时，未找到网站表单")
            time.sleep(0.5)

    def _wait_for_login_result(self, By, *, timeout: float) -> bool | None:
        deadline = time.monotonic() + timeout
        verification_deadline: float | None = None
        announced_verification = False
        probe_retry_deadline: float | None = None
        while True:
            now = time.monotonic()
            if self._has_human_verification(By):
                self.human_verification_pending = True
                probe_retry_deadline = None
                if not announced_verification:
                    self.status(
                        f"检测到 CAPTCHA 人机验证，请在 {self.browser_name} 窗口中手动完成；"
                        "完成后程序会自动继续"
                    )
                    announced_verification = True
                    verification_deadline = (
                        now + HUMAN_VERIFICATION_TIMEOUT_SECONDS
                    )
                if self.background:
                    return None
                if verification_deadline is not None and now >= verification_deadline:
                    self.status(
                        "等待人机验证超时；浏览器窗口已保留，完成后请再次点击“打开”"
                    )
                    return None
                time.sleep(0.5)
                continue
            if announced_verification:
                self.human_verification_pending = False
                self.status("人机验证已完成，继续检查登录结果…")
                deadline = now + timeout
                announced_verification = False
                verification_deadline = None

            try:
                if self._is_logged_in(By):
                    return True
                errors = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".woocommerce-error, .c-notification--error, [role='alert']",
                )
                if any(item.is_displayed() and item.text.strip() for item in errors):
                    return False
                probe_retry_deadline = None
            except Exception as exc:
                probe_retry_deadline = self._retry_page_probe(
                    exc,
                    now=now,
                    retry_deadline=probe_retry_deadline,
                )
                time.sleep(0.5)
                continue

            if now >= deadline:
                return False
            time.sleep(0.5)

    def _wait_for_otp_field(self, By, *, timeout: float):
        deadline = time.monotonic() + timeout
        verification_deadline: float | None = None
        announced_captcha = False
        probe_retry_deadline: float | None = None
        while True:
            now = time.monotonic()
            if self._has_human_verification(By):
                self.human_verification_pending = True
                probe_retry_deadline = None
                if not announced_captcha:
                    self.status(
                        f"检测到 CAPTCHA 人机验证，请在 {self.browser_name} 窗口中手动完成；"
                        "完成后程序会自动继续"
                    )
                    announced_captcha = True
                    verification_deadline = (
                        now + HUMAN_VERIFICATION_TIMEOUT_SECONDS
                    )
                if self.background:
                    return None
                if verification_deadline is not None and now >= verification_deadline:
                    self.status(
                        "等待人机验证超时；浏览器窗口已保留，完成后请再次点击“打开”"
                    )
                    return None
                time.sleep(0.5)
                continue

            if announced_captcha:
                self.human_verification_pending = False
                self.status("人机验证已完成，继续等待邮箱验证码输入框…")
                deadline = now + timeout
                announced_captcha = False
                verification_deadline = None

            try:
                if self._is_logged_in(By):
                    return None
                fields = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "main form.woocommerce-form-register input[name='otp']",
                )
                if fields and fields[0].is_enabled() and fields[0].is_displayed():
                    return fields[0]
                probe_retry_deadline = None
            except Exception as exc:
                probe_retry_deadline = self._retry_page_probe(
                    exc,
                    now=now,
                    retry_deadline=probe_retry_deadline,
                )
                time.sleep(0.5)
                continue

            if now >= deadline:
                return None
            time.sleep(1)

    def _wait_for_mail_code(self, *, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        cursor = 0
        inspected: set[str] = set()
        while time.monotonic() < deadline:
            update = self.mail_client.get_mail(self.address, cursor)
            cursor = update.cursor
            for message in reversed(update.messages):
                if message.message_id in inspected:
                    continue
                inspected.add(message.message_id)
                if not _is_creative_fabrica_message(message.sender, message.subject):
                    continue
                code = extract_verification_code(message.subject)
                if code:
                    return code
                source = self.mail_client.fetch_message_html(self.address, message.message_id)
                code = extract_verification_code(message.subject, html_to_text(source))
                if code:
                    return code
            time.sleep(VERIFICATION_POLL_SECONDS)
        return None
