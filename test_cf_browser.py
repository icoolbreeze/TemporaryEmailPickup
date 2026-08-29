import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cf_browser import (
    ACCOUNT_URL,
    LOGIN_URL,
    SIGNUP_URL,
    CreativeFabricaBrowser,
    browser_display_name,
    browser_was_closed,
    debugger_address_is_live,
    existing_debugger_address,
    extract_points,
    extract_verification_code,
    is_human_verification_content,
    is_main_site_authenticated,
    manual_browser_command,
)


class FakeBy:
    CSS_SELECTOR = "css"


def make_browser(*, background: bool = False, browser_path: Path = Path("chrome.exe")):
    statuses: list[str] = []
    browser = CreativeFabricaBrowser(
        address="mailbox@example.com",
        profile_path=Path("profile"),
        browser_path=browser_path,
        mail_client=object(),
        status=statuses.append,
        points_updated=lambda _points: None,
        background=background,
    )
    return browser, statuses


class VerificationCodeTests(unittest.TestCase):
    def test_browser_display_names(self) -> None:
        self.assertEqual(browser_display_name(Path("brave.exe")), "Brave")
        self.assertEqual(browser_display_name(Path("chrome.exe")), "Chrome")

    def test_manual_mode_uses_a_normal_browser_command(self) -> None:
        command = manual_browser_command(
            Path("virtualbrowser.exe"), Path("worker-3"), worker_id="3"
        )
        self.assertIn("--user-data-dir=worker-3", command)
        self.assertIn("--worker-id=3", command)
        self.assertIn("--new-window", command)
        self.assertFalse(any("webdriver" in part.lower() for part in command))

    def test_manual_mode_exposes_debug_port_for_tracking(self) -> None:
        command = manual_browser_command(Path("chrome.exe"), Path("profile"))
        self.assertIn("--remote-debugging-port=0", command)
        self.assertFalse(any("webdriver" in part.lower() for part in command))

    def test_brave_excludes_crashing_test_type_switch(self) -> None:
        browser, _statuses = make_browser(browser_path=Path("brave.exe"))

        class Options:
            def __init__(self) -> None:
                self.binary_location = ""
                self.experimental: dict[str, object] = {}

            def add_experimental_option(self, name: str, value: object) -> None:
                self.experimental[name] = value

        class WebDriver:
            ChromeOptions = Options

        options = browser._browser_options(WebDriver)
        self.assertEqual(options.binary_location, "brave.exe")
        self.assertEqual(options.experimental["excludeSwitches"], ["test-type"])

    def test_background_browser_uses_visible_window(self) -> None:
        browser, _statuses = make_browser(background=True)

        class Options:
            def __init__(self) -> None:
                self.arguments: list[str] = []
                self.experimental: dict[str, object] = {}

            def add_argument(self, value: str) -> None:
                self.arguments.append(value)

            def add_experimental_option(self, name: str, value: object) -> None:
                self.experimental[name] = value

        options = Options()
        browser._configure_window_options(options)

        self.assertIn("--start-maximized", options.arguments)
        self.assertIn("--window-size=1440,1000", options.arguments)
        self.assertNotIn("--headless=new", options.arguments)
        self.assertNotIn("detach", options.experimental)

    def test_donut_debugger_uses_remote_browser_version(self) -> None:
        statuses: list[str] = []
        browser = CreativeFabricaBrowser(
            address="mailbox@example.com",
            profile_path=None,
            browser_path=None,
            mail_client=object(),
            status=statuses.append,
            points_updated=lambda _points: None,
            debugger_address="127.0.0.1:45678",
            browser_name="Donut",
            browser_version="149.0.7827.116",
        )

        class Options:
            def __init__(self) -> None:
                self.experimental: dict[str, object] = {}
                self.browser_version = None

            def add_experimental_option(self, name: str, value: object) -> None:
                self.experimental[name] = value

        expected_driver = Mock(window_handles=["main"])

        class WebDriver:
            ChromeOptions = Options
            Chrome = Mock(return_value=expected_driver)

        driver = browser._attach_to_debugger(WebDriver)

        self.assertIs(driver, expected_driver)
        options = WebDriver.Chrome.call_args.kwargs["options"]
        self.assertEqual(options.experimental["debuggerAddress"], "127.0.0.1:45678")
        self.assertEqual(options.browser_version, "149.0.7827.116")

    def test_subject_prefix_used_by_creative_fabrica(self) -> None:
        self.assertEqual(
            extract_verification_code("7196 - Verify your Creative Fabrica account"),
            "7196",
        )

    def test_code_label_in_body(self) -> None:
        self.assertEqual(
            extract_verification_code("Welcome", "Your verification code is 482193."),
            "482193",
        )

    def test_unrelated_message_has_no_code(self) -> None:
        self.assertIsNone(extract_verification_code("Monthly newsletter", "Hello there"))

    def test_coin_balance_after_number(self) -> None:
        self.assertEqual(extract_points(["5,000 Coins"]), 5000)

    def test_coin_balance_before_number(self) -> None:
        self.assertEqual(extract_points(["Coins: 25 000"]), 25000)

    def test_compact_coin_balance(self) -> None:
        self.assertEqual(extract_points(["12.5K AI Coins"]), 12500)

    def test_accessible_label_combined_with_balance(self) -> None:
        self.assertEqual(extract_points(["View coins 5,000"]), 5000)

    def test_existing_chrome_debugger_address(self) -> None:
        with TemporaryDirectory() as directory:
            profile = Path(directory)
            (profile / "DevToolsActivePort").write_text(
                "54321\n/devtools/browser/example\n", encoding="utf-8"
            )
            self.assertEqual(existing_debugger_address(profile), "127.0.0.1:54321")

    def test_stale_chrome_debugger_address_is_rejected(self) -> None:
        with patch("cf_browser.socket.create_connection", side_effect=OSError):
            self.assertFalse(debugger_address_is_live("127.0.0.1:54321"))

    def test_user_closed_browser_error_is_recognized(self) -> None:
        error = RuntimeError(
            "invalid session id: session deleted as the browser has closed "
            "the connection; not connected to DevTools"
        )
        self.assertTrue(browser_was_closed(error))

    def test_unrelated_browser_error_is_not_treated_as_user_close(self) -> None:
        self.assertFalse(browser_was_closed(RuntimeError("login form timed out")))

    def test_logged_in_account_page_can_keep_login_url(self) -> None:
        self.assertTrue(
            is_main_site_authenticated(
                "https://www.creativefabrica.com/login/",
                has_logout=True,
                has_login_form=False,
            )
        )

    def test_real_login_form_is_not_authenticated(self) -> None:
        self.assertFalse(
            is_main_site_authenticated(
                "https://www.creativefabrica.com/login/",
                has_logout=False,
                has_login_form=True,
            )
        )

    def test_shared_session_logout_uses_the_main_account_page(self) -> None:
        browser, statuses = make_browser()

        class Link:
            clicked = False
            text = "Logout"

            def __init__(self, driver) -> None:
                self.driver = driver

            def click(self) -> None:
                self.clicked = True
                self.driver.logged_out = True

            def get_attribute(self, _name: str) -> str:
                return ""

        class Driver:
            current_url = "https://www.creativefabrica.com/login/"

            def __init__(self) -> None:
                self.urls: list[str] = []
                self.logged_out = False
                self.link = Link(self)
                self.cookies_cleared = False
                self.script_ran = False

            def get(self, url: str) -> None:
                self.urls.append(url)
                self.current_url = url

            def find_elements(self, _kind: str, selector: str):
                if "main form" in selector:
                    return [object()] if self.logged_out and self.current_url == LOGIN_URL else []
                if "logout" in selector and not self.logged_out:
                    return [self.link]
                return []

            def delete_all_cookies(self) -> None:
                self.cookies_cleared = True

            def execute_script(self, _script: str) -> None:
                self.script_ran = True

        browser.driver = Driver()
        browser._logout_before_reassignment(FakeBy)

        self.assertTrue(browser.driver.link.clicked)
        self.assertEqual(browser.driver.urls, [ACCOUNT_URL, LOGIN_URL])
        self.assertFalse(browser.driver.cookies_cleared)
        self.assertFalse(browser.driver.script_ran)
        self.assertTrue(any("账户页退出旧账号" in message for message in statuses))

    def test_shared_session_closes_promo_dialog_before_finding_logout(self) -> None:
        browser, statuses = make_browser()

        class IconButton:
            text = ""

            def __init__(self, driver) -> None:
                self.driver = driver

            def get_attribute(self, _name: str) -> str:
                return ""

            def find_elements(self, _kind: str, selector: str):
                return [object()] if "svg" in selector else []

            def click(self) -> None:
                self.driver.dialog_open = False

        class Dialog:
            def __init__(self, driver) -> None:
                self.button = IconButton(driver)

            def is_displayed(self) -> bool:
                return True

            def find_elements(self, _kind: str, _selector: str):
                return [self.button]

        class Logout:
            text = "Logout"

            def get_attribute(self, _name: str) -> str:
                return ""

        class Driver:
            current_url = ACCOUNT_URL

            def __init__(self) -> None:
                self.dialog_open = True
                self.dialog = Dialog(self)
                self.logout = Logout()

            def find_elements(self, _kind: str, selector: str):
                if selector == "[role='dialog']":
                    return [self.dialog] if self.dialog_open else []
                if "logout" in selector:
                    return [] if self.dialog_open else [self.logout]
                return []

        browser.driver = Driver()
        controls = browser._wait_for_account_logout_controls(FakeBy)

        self.assertEqual(controls, [browser.driver.logout])
        self.assertFalse(browser.driver.dialog_open)
        self.assertTrue(any("账户页弹窗" in message for message in statuses))

    def test_shared_session_stops_when_old_account_is_still_logged_in(self) -> None:
        browser, _statuses = make_browser()

        class Logout:
            text = "Logout"

            def get_attribute(self, _name: str) -> str:
                return ""

        class Driver:
            current_url = ACCOUNT_URL

            def find_elements(self, _kind: str, selector: str):
                if "logout" in selector:
                    return [Logout()]
                return []

        browser.driver = Driver()
        with self.assertRaisesRegex(RuntimeError, "旧账号仍处于登录状态"):
            browser._verify_logged_out_login_page(FakeBy)

    def test_reused_session_confirms_logout_again_before_registration(self) -> None:
        browser, statuses = make_browser()
        browser.reset_site_session = True
        events: list[str] = []

        class Field:
            def send_keys(self, _value: str) -> None:
                pass

        class Button:
            def __init__(self, name: str) -> None:
                self.name = name

            def click(self) -> None:
                events.append(self.name)

        class Form:
            def __init__(self, button_name: str) -> None:
                self.button_name = button_name

            def find_element(self, _kind: str, selector: str):
                if selector.startswith("input"):
                    return Field()
                return Button(self.button_name)

        login_form = Form("login-submit")
        register_form = Form("register-submit")
        browser.driver = object()

        with (
            patch.object(
                browser,
                "_wait_for_page_form",
                side_effect=[login_form, register_form],
            ),
            patch.object(browser, "_wait_for_login_result", return_value=False),
            patch.object(
                browser,
                "_logout_before_reassignment",
                side_effect=lambda _by: events.append("logout-check"),
            ),
            patch.object(
                browser,
                "_navigate",
                side_effect=lambda url, _by: events.append(url),
            ),
            patch.object(browser, "_wait_for_otp_field", return_value=None),
            patch.object(browser, "_is_logged_in", return_value=False),
        ):
            browser._continue_login_flow(FakeBy, object())

        self.assertLess(events.index("logout-check"), events.index(SIGNUP_URL))
        self.assertLess(events.index(SIGNUP_URL), events.index("register-submit"))
        self.assertTrue(any("注册前正在确认" in message for message in statuses))

    def test_cloudflare_verification_page_is_recognized(self) -> None:
        self.assertTrue(
            is_human_verification_content(
                "https://www.creativefabrica.com/login/",
                "Just a moment...",
                "Performing security verification. Verify you are human.",
            )
        )

    def test_cloudflare_challenge_iframe_is_recognized(self) -> None:
        self.assertTrue(
            is_human_verification_content(
                "https://www.creativefabrica.com/signup/",
                "Sign up",
                "",
                has_challenge_frame=True,
            )
        )

    def test_regular_login_page_is_not_verification(self) -> None:
        self.assertFalse(
            is_human_verification_content(
                "https://www.creativefabrica.com/login/",
                "Login - Creative Fabrica",
                "Log in Username Password",
            )
        )

    def test_navigation_error_is_ignored_when_challenge_is_visible(self) -> None:
        browser, _statuses = make_browser()

        class Driver:
            def get(self, _url: str) -> None:
                raise RuntimeError("")

        browser.driver = Driver()
        with patch.object(browser, "_has_human_verification", return_value=True):
            browser._navigate(LOGIN_URL, FakeBy)

    def test_page_probe_retries_empty_driver_error(self) -> None:
        browser, _statuses = make_browser()
        expected_form = object()

        class Driver:
            calls = 0

            def find_elements(self, _kind: str, _selector: str):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("")
                return [expected_form]

        browser.driver = Driver()
        with (
            patch.object(browser, "_has_human_verification", return_value=False),
            patch("cf_browser.time.monotonic", return_value=0),
            patch("cf_browser.time.sleep"),
        ):
            result = browser._wait_for_page_form(
                FakeBy,
                "main form.woocommerce-form-login",
                page_name="登录页",
            )
        self.assertIs(result, expected_form)

    def test_challenge_takes_priority_over_stale_page_form(self) -> None:
        browser, statuses = make_browser()
        expected_form = object()

        class Driver:
            def find_elements(self, _kind: str, _selector: str):
                return [expected_form]

        browser.driver = Driver()
        with (
            patch.object(
                browser,
                "_has_human_verification",
                side_effect=[True, False],
            ),
            patch("cf_browser.time.monotonic", return_value=0),
            patch("cf_browser.time.sleep"),
        ):
            result = browser._wait_for_page_form(
                FakeBy,
                "main form.woocommerce-form-login",
                page_name="登录页",
            )
        self.assertIs(result, expected_form)
        self.assertTrue(any("检测到 CAPTCHA" in message for message in statuses))
        self.assertTrue(any("人机验证已完成" in message for message in statuses))
        self.assertFalse(browser.human_verification_pending)

    def test_background_challenge_returns_for_manual_follow_up(self) -> None:
        browser, statuses = make_browser(background=True)
        browser.driver = object()
        with (
            patch.object(browser, "_has_human_verification", return_value=True),
            patch("cf_browser.time.monotonic", return_value=0),
        ):
            result = browser._wait_for_page_form(
                FakeBy,
                "main form.woocommerce-form-login",
                page_name="登录页",
            )
        self.assertIsNone(result)
        self.assertTrue(any("检测到 CAPTCHA" in message for message in statuses))
        self.assertTrue(browser.human_verification_pending)


if __name__ == "__main__":
    unittest.main()
