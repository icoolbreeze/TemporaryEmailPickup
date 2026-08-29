"""Creative Fabrica login/registration helper for one isolated browser profile."""

from __future__ import annotations

import re
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

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
) -> list[str]:
    """Build a normal Chromium launch command with no WebDriver involvement."""
    command = [
        str(browser_path),
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    if worker_id:
        command.append(f"--worker-id={worker_id}")
    else:
        # Expose the local DevTools port so the app can attach read-only and
        # track the window's URL, login state and Studio points.
        command.append("--remote-debugging-port=0")
    command.append(HOME_URL)
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
    url: str, title: str, body_text: str, *, has_challenge_frame: bool = False
) -> bool:
    """Recognize Cloudflare/Turnstile pages that require a person to continue."""
    if has_challenge_frame:
        return True
    haystack = " ".join((url, title, body_text)).lower()
    title_text = title.strip().lower()
    if title_text.startswith("just a moment"):
        return True
    markers = (
        "performing security verification",
        "verify you are human",
        "checking your browser before accessing",
        "security verification by cloudflare",
        "cdn-cgi/challenge-platform",
    )
    return any(marker in haystack for marker in markers)


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


def terminate_stale_profile_browser(profile_path: Path, browser_path: Path) -> bool:
    """Close only browser processes that use this app-owned profile on Windows."""
    if os.name != "nt":
        return False
    profile = str(profile_path.resolve())
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
        """Open the selected browser, try login, then register when needed."""
        webdriver, By, WebDriverWait = self._selenium_imports()
        if self.driver is not None:
            self._navigate(LOGIN_URL, By)
            if self.reset_site_session:
                self._logout_before_reassignment(By)
            return self._continue_login_flow(By, WebDriverWait)
        if self.debugger_address:
            self.status(f"正在连接 {self.address} 的 {self.browser_name} 指纹配置…")
            self.driver = self._attach_to_debugger(webdriver)
            self._navigate(LOGIN_URL, By)
            if self.reset_site_session:
                self._logout_before_reassignment(By)
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
            attach_options = self._browser_options(webdriver)
            attach_options.add_experimental_option(
                "debuggerAddress", debugger_address
            )
            self.status(f"正在连接 {self.address} 已打开的 {self.browser_name}…")
            try:
                self.driver = webdriver.Chrome(options=attach_options)
                # Creating ChromeDriver can succeed even when the detached target
                # disappears during attachment. Force a real round trip now.
                if not self.driver.window_handles:
                    raise RuntimeError(f"{self.browser_name} 没有可用窗口")
            except Exception:
                self.driver = None
        if self.driver is None:
            if terminate_stale_profile_browser(self.profile_path, self.browser_path):
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
        self._navigate(LOGIN_URL, By)
        if self.reset_site_session:
            self._logout_before_reassignment(By)
        return self._continue_login_flow(By, WebDriverWait)

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

    def _attach_to_debugger(self, webdriver):
        """Attach ChromeDriver to a browser process launched by Donut."""
        options = self._browser_options(webdriver)
        options.add_experimental_option("debuggerAddress", self.debugger_address)
        if self.browser_version:
            options.browser_version = self.browser_version
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
            self.driver.get(STUDIO_URL)
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
        """Log into Studio's own account dialog when SSO has not propagated."""
        login_buttons = self.driver.find_elements(
            By.XPATH,
            "//header//button[normalize-space()='Log in'] | "
            "//button[@aria-label='Log in']",
        )
        login_button = next((item for item in login_buttons if item.is_displayed()), None)
        if login_button is None:
            return True

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
            visible_login = any(
                item.is_displayed()
                for item in self.driver.find_elements(
                    By.XPATH,
                    "//header//button[normalize-space()='Log in'] | "
                    "//button[@aria-label='Log in']",
                )
            )
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
        """Return whether the current page is a Cloudflare/Turnstile challenge."""
        try:
            url = self.driver.current_url or ""
        except Exception:
            url = ""
        try:
            title = self.driver.title or ""
        except Exception:
            title = ""
        try:
            bodies = self.driver.find_elements(By.TAG_NAME, "body")
            body_text = bodies[0].text if bodies else ""
        except Exception:
            body_text = ""
        try:
            challenge_elements = self.driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='challenges.cloudflare.com'], "
                "iframe[src*='recaptcha'], iframe[title*='challenge' i], "
                ".cf-turnstile, [class*='cf-turnstile'], "
                "#challenge-running, #challenge-stage",
            )
            has_challenge_frame = any(
                item.is_displayed() for item in challenge_elements
            )
        except Exception:
            has_challenge_frame = False
        return is_human_verification_content(
            url,
            title,
            body_text,
            has_challenge_frame=has_challenge_frame,
        )

    def _navigate(self, url: str, By) -> None:
        """Navigate without failing when ChromeDriver races a visible challenge page."""
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
