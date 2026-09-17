import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from cdp_client import CDPClient
from cf_browser import (
    ACCOUNT_URL,
    HOME_URL,
    LOGIN_URL,
    MANUAL_BROWSER_NO_DEBUGGER_MESSAGE,
    SIGNUP_URL,
    STUDIO_CDP_CAPTCHA_PENDING,
    STUDIO_CDP_FAILED,
    STUDIO_CDP_SUCCESS,
    STUDIO_URL,
    CreativeFabricaBrowser,
    StudioCDPResult,
    _STUDIO_CAPTCHA_IFRAME_SELECTOR,
    _STUDIO_DIALOG_CONTROL_SELECTOR,
    _STUDIO_DIALOG_ERROR_SELECTOR,
    _STUDIO_DIALOG_SELECTOR,
    _STUDIO_HEADER_LOGIN_SELECTOR,
    _STUDIO_POINTS_SELECTOR,
    browser_display_name,
    browser_was_closed,
    debugger_address_is_live,
    existing_debugger_address,
    extract_points,
    extract_verification_code,
    find_system_browser_on_user_data,
    is_human_verification_content,
    is_main_site_authenticated,
    launch_plain_chromium,
    _list_system_browser_processes,
    manual_browser_command,
    studio_login_via_cdp,
    terminate_stale_profile_browser,
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

    def test_manual_mode_default_keeps_debug_port(self) -> None:
        """``remote_debugging`` defaults to True, so every pre-existing
        call site (auto / named-profile debugger bootstrap) still gets the
        DevTools port it needs for CDP attach."""
        command = manual_browser_command(Path("chrome.exe"), Path("profile"))
        self.assertIn("--remote-debugging-port=0", command)

    def test_manual_mode_without_debugging_omits_the_port(self) -> None:
        """手动打开 must launch a plain user window: no DevTools listener,
        so ChromeDriver can never attach and Studio's invisible Turnstile
        keeps passing the human check."""
        command = manual_browser_command(
            Path("brave.exe"),
            Path("profile"),
            remote_debugging=False,
        )
        self.assertNotIn("--remote-debugging-port", " ".join(command))
        self.assertFalse(any("webdriver" in part.lower() for part in command))

    def test_manual_mode_without_debugging_keeps_studio_user_data_and_incognito(self) -> None:
        """Dropping the debug port must not disturb anything else: Studio
        stays the start page, ``--user-data-dir`` still isolates the
        profile, and 无痕 mode still appends ``--incognito``."""
        isolated = Path(
            "C:/Users/me/AppData/Local/TemporaryEmailPickup/"
            "browser_profiles_brave/key"
        )
        command = manual_browser_command(
            Path("brave.exe"),
            isolated,
            incognito=True,
            remote_debugging=False,
        )
        self.assertEqual(command[-1], STUDIO_URL)
        self.assertIn(f"--user-data-dir={isolated}", command)
        self.assertIn("--incognito", command)

    def test_worker_id_path_ignores_remote_debugging_flag(self) -> None:
        """VirtualBrowser launches never exposed a debug port; passing
        ``remote_debugging=False`` there must not change the command."""
        command = manual_browser_command(
            Path("virtualbrowser.exe"),
            Path("worker-3"),
            worker_id="3",
            remote_debugging=False,
        )
        self.assertIn("--worker-id=3", command)
        self.assertNotIn("--remote-debugging-port", " ".join(command))

    def test_manual_command_opens_studio_first(self) -> None:
        """The manual launch must open Studio, not the www home page.

        ``studio.creativefabrica.com`` is reachable without a Cloudflare
        challenge; the www home is not.
        """
        command = manual_browser_command(Path("chrome.exe"), Path("profile"))
        self.assertEqual(command[-1], STUDIO_URL)
        self.assertNotEqual(command[-1], HOME_URL)

    def test_manual_command_opens_studio_for_named_profile(self) -> None:
        """Named-profile launches (Brave ``Profile 4`` etc.) also point at Studio."""
        command = manual_browser_command(
            Path("brave.exe"),
            Path("profile"),
            profile_directory="Profile 4",
        )
        self.assertEqual(command[-1], STUDIO_URL)

    def test_manual_command_does_not_end_with_home_url(self) -> None:
        """Belt-and-suspenders check that www is never the last arg."""
        for profile_directory in (None, "Profile 7"):
            command = manual_browser_command(
                Path("chrome.exe"),
                Path("profile"),
                profile_directory=profile_directory,
            )
            self.assertNotEqual(command[-1], HOME_URL, msg=str(command))

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
        self.assertNotIn("excludeSwitches", options.experimental)

    def test_brave_attach_options_omit_exclude_switches(self) -> None:
        """Connecting to a live Brave must not send excludeSwitches.

        ChromeDriver treats that key as launch-only and raises
        ``unrecognized chrome option: excludeSwitches`` during 自动打开.
        """
        browser, _statuses = make_browser(browser_path=Path("brave.exe"))
        browser.browser_name = "Brave"
        browser.debugger_address = "127.0.0.1:9222"

        class Options:
            def __init__(self) -> None:
                self.binary_location = ""
                self.experimental: dict[str, object] = {}
                self.browser_version = None

            def add_experimental_option(self, name: str, value: object) -> None:
                self.experimental[name] = value

        class WebDriver:
            ChromeOptions = Options

        options = browser._attach_options(WebDriver, "127.0.0.1:9222")
        self.assertEqual(options.experimental["debuggerAddress"], "127.0.0.1:9222")
        self.assertNotIn("excludeSwitches", options.experimental)
        self.assertEqual(options.binary_location, "")

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

    def test_login_page_with_leftover_turnstile_is_not_blocking(self) -> None:
        """A login page that still has a Turnstile widget must not be reported
        as a blocking challenge: the user has already passed Cloudflare and
        automation should resume.
        """
        self.assertFalse(
            is_human_verification_content(
                "https://www.creativefabrica.com/login/",
                "Login - Creative Fabrica",
                "",
                has_challenge_frame=True,
                has_site_form=True,
            )
        )

    def test_cdn_cgi_url_is_always_blocking_even_with_a_form(self) -> None:
        """An interstitial served from cdn-cgi/challenge-platform must still
        be flagged as blocking even when a leftover form is on the page.
        """
        self.assertTrue(
            is_human_verification_content(
                "https://www.creativefabrica.com/cdn-cgi/challenge-platform/x",
                "Login - Creative Fabrica",
                "",
                has_challenge_frame=True,
                has_site_form=True,
            )
        )

    def test_just_a_moment_title_always_blocks_even_with_a_form(self) -> None:
        """Title prefix is the strongest signal: even when a form is also
        present, the page is still the blocking interstitial.
        """
        self.assertTrue(
            is_human_verification_content(
                "https://www.creativefabrica.com/login/",
                "Just a moment...",
                "",
                has_challenge_frame=False,
                has_site_form=True,
            )
        )

    def test_wait_loop_resumes_after_user_clicks_through_captcha(self) -> None:
        """Simulate the manual flow: the page is an interstitial for the first
        few probes, then the user clicks through and the login form appears.
        ``_wait_for_page_form`` must wait it out, announce pass, and return
        the form. ``time.sleep`` / ``monotonic`` are patched so the 10-minute
        verification timeout never elapses.
        """
        browser, statuses = make_browser()
        expected_form = object()
        title_state = {"index": 0}

        class Driver:
            @property
            def title(self) -> str:
                title_state["index"] += 1
                if title_state["index"] <= 2:
                    return "Just a moment..."
                return "Login - Creative Fabrica"

            @property
            def current_url(self) -> str:
                return LOGIN_URL

            def find_elements(self, _kind, selector):
                if "woocommerce-form-login" in selector:
                    return [expected_form] if title_state["index"] > 2 else []
                if "challenge-running" in selector or "challenge-stage" in selector:
                    return []
                return []

        browser.driver = Driver()
        monotonic_values = iter(range(0, 10_000))

        with (
            patch("cf_browser.time.sleep"),
            patch("cf_browser.time.monotonic", side_effect=lambda: next(monotonic_values)),
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

    def test_has_human_verification_does_not_query_challenge_iframes(self) -> None:
        """``_has_human_verification`` must never walk the challenge iframes
        (that resets the Turnstile widget). Login-form selectors are fine.
        """
        browser, _statuses = make_browser()
        expected_form = object()

        forbidden = (
            "challenges.cloudflare.com",
            "cf-turnstile",
            "recaptcha",
        )

        class Driver:
            title = "Login - Creative Fabrica"
            current_url = LOGIN_URL

            def find_elements(self, _kind, selector):
                for marker in forbidden:
                    if marker in selector:
                        self.fail(
                            f"_has_human_verification queried challenge selector: {selector!r}"
                        )
                if "woocommerce-form-login" in selector:
                    return [expected_form]
                if "challenge-running" in selector or "challenge-stage" in selector:
                    return []
                if selector == "body" or selector.endswith("body"):
                    return [object()]
                return []

        browser.driver = Driver()
        result = browser._has_human_verification(FakeBy)
        self.assertFalse(result)

    def test_live_debugger_is_not_killed_on_attach_failure(self) -> None:
        """If a live debugger address is reachable but ChromeDriver cannot
        attach to the user's window, ``run()`` must raise without
        ``terminate_stale_profile_browser`` being called and without
        relaunching ``webdriver.Chrome()`` with a fresh ``--user-data-dir``.
        """
        statuses: list[str] = []
        browser = CreativeFabricaBrowser(
            address="mailbox@example.com",
            profile_path=Path("profile"),
            browser_path=Path("chrome.exe"),
            mail_client=object(),
            status=statuses.append,
            points_updated=lambda _points: None,
        )

        class FakeOptions:
            def __init__(self) -> None:
                self.arguments: list[str] = []
                self.experimental: dict[str, object] = {}
                self.binary_location = ""

            def add_argument(self, value: str) -> None:
                self.arguments.append(value)

            def add_experimental_option(self, name: str, value: object) -> None:
                self.experimental[name] = value

        fake_webdriver = Mock()
        fake_webdriver.ChromeOptions = FakeOptions
        fake_webdriver.Chrome.side_effect = RuntimeError("chrome is busy")

        with (
            patch("cf_browser.existing_debugger_address", return_value="127.0.0.1:55555"),
            patch("cf_browser.debugger_address_is_live", return_value=True),
            patch("cf_browser.terminate_stale_profile_browser") as terminate,
            patch.object(
                browser,
                "_selenium_imports",
                return_value=(fake_webdriver, FakeBy, Mock()),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "手动窗口已打开但无法连接"):
                browser.run()

        terminate.assert_not_called()
        # Only one ChromeDriver attempt was made: the attach. No relaunch path
        # is allowed to add a fresh ``--user-data-dir`` argument.
        self.assertEqual(fake_webdriver.Chrome.call_count, 1)
        options = fake_webdriver.Chrome.call_args.kwargs["options"]
        arguments = list(getattr(options, "arguments", []) or [])
        for argument in arguments:
            self.assertFalse(
                str(argument).startswith("--user-data-dir="),
                f"unexpected relaunch with --user-data-dir: {argument!r}",
            )

    def test_navigate_does_not_reload_when_already_on_login_url(self) -> None:
        browser, _statuses = make_browser()

        class Driver:
            get_calls: list[str] = []
            title = "Login - Creative Fabrica"
            current_url = LOGIN_URL

            def find_elements(self, _kind, _selector):
                return []

            def get(self, url: str) -> None:
                self.get_calls.append(url)

        browser.driver = Driver()
        with patch.object(browser, "_has_human_verification", return_value=False):
            browser._navigate(LOGIN_URL, FakeBy)

        self.assertEqual(browser.driver.get_calls, [])

    def test_navigate_does_not_reload_with_trailing_slash_or_case(self) -> None:
        browser, _statuses = make_browser()
        get_calls: list[str] = []

        class Driver:
            title = "Login - Creative Fabrica"
            current_url = "HTTPS://WWW.CREATIVEFABRICA.COM/LOGIN/"

            def find_elements(self, _kind, _selector):
                return []

            def get(self, url: str) -> None:
                get_calls.append(url)

        browser.driver = Driver()
        with patch.object(browser, "_has_human_verification", return_value=False):
            browser._navigate(LOGIN_URL, FakeBy)

        self.assertEqual(get_calls, [])

    def test_begin_studio_session_opens_studio_and_authenticates(self) -> None:
        """When ``_ensure_studio_login`` returns True the helper only
        navigates to ``STUDIO_URL``, never opens ``LOGIN_URL``, and calls
        ``authenticated()`` so the caller knows the user is signed in.
        """
        browser, _statuses = make_browser()
        navigations: list[str] = []
        authenticated_calls: list[bool] = []
        browser.driver = object()

        with (
            patch.object(
                browser,
                "_navigate",
                side_effect=lambda url, _by: navigations.append(url),
            ),
            patch.object(browser, "_ensure_studio_login", return_value=True),
            patch.object(browser, "_update_points"),
            patch.object(
                browser,
                "authenticated",
                side_effect=lambda: authenticated_calls.append(True),
            ),
        ):
            result = browser._begin_studio_session(FakeBy, object())

        self.assertTrue(result)
        self.assertEqual(navigations, [STUDIO_URL])
        self.assertEqual(authenticated_calls, [True])
        self.assertNotIn(LOGIN_URL, navigations)

    def test_begin_studio_session_does_not_call_authenticated_on_failure(self) -> None:
        """When ``_ensure_studio_login`` returns False the helper bails
        out before ``authenticated()`` / ``_update_points``; the caller is
        responsible for the www fallback.
        """
        browser, _statuses = make_browser()
        navigations: list[str] = []
        authenticated_calls: list[bool] = []
        browser.driver = object()

        with (
            patch.object(
                browser,
                "_navigate",
                side_effect=lambda url, _by: navigations.append(url),
            ),
            patch.object(browser, "_ensure_studio_login", return_value=False),
            patch.object(browser, "_update_points") as update_points,
            patch.object(
                browser,
                "authenticated",
                side_effect=lambda: authenticated_calls.append(True),
            ),
        ):
            result = browser._begin_studio_session(FakeBy, object())

        self.assertFalse(result)
        self.assertEqual(navigations, [STUDIO_URL])
        self.assertEqual(authenticated_calls, [])
        update_points.assert_not_called()

    def test_run_falls_back_to_www_login_when_studio_login_fails(self) -> None:
        """``run()`` must navigate to ``STUDIO_URL`` first and only fall
        back to ``LOGIN_URL`` + ``_continue_login_flow`` when
        ``_ensure_studio_login`` cannot sign the user in.
        """
        browser, _statuses = make_browser()
        navigations: list[str] = []
        # The ``self.driver is not None`` branch in ``run()`` skips the
        # browser-launch dance, so the only setup needed is a driver.
        browser.driver = object()
        fake_webdriver = Mock()

        with (
            patch.object(
                browser,
                "_selenium_imports",
                return_value=(fake_webdriver, FakeBy, Mock()),
            ),
            patch.object(
                browser,
                "_navigate",
                side_effect=lambda url, _by: navigations.append(url),
            ),
            patch.object(browser, "_ensure_studio_login", return_value=False),
            patch.object(
                browser,
                "_continue_login_flow",
                return_value=browser.driver,
            ) as continue_flow,
        ):
            result = browser.run()

        self.assertIs(result, browser.driver)
        self.assertEqual(navigations, [STUDIO_URL, LOGIN_URL])
        self.assertEqual(continue_flow.call_count, 1)
        # The www login URL must not be opened before Studio is tried.
        self.assertLess(
            navigations.index(STUDIO_URL), navigations.index(LOGIN_URL)
        )

    def test_run_returns_driver_when_studio_login_succeeds(self) -> None:
        """``run()`` short-circuits and returns the driver when Studio
        login succeeds — it must NOT open ``LOGIN_URL`` or invoke
        ``_continue_login_flow`` in that case.
        """
        browser, _statuses = make_browser()
        navigations: list[str] = []
        browser.driver = object()
        fake_webdriver = Mock()

        with (
            patch.object(
                browser,
                "_selenium_imports",
                return_value=(fake_webdriver, FakeBy, Mock()),
            ),
            patch.object(
                browser,
                "_navigate",
                side_effect=lambda url, _by: navigations.append(url),
            ),
            patch.object(browser, "_ensure_studio_login", return_value=True),
            patch.object(browser, "_update_points"),
            patch.object(browser, "authenticated"),
            patch.object(
                browser,
                "_continue_login_flow",
            ) as continue_flow,
        ):
            result = browser.run()

        self.assertIs(result, browser.driver)
        self.assertEqual(navigations, [STUDIO_URL])
        self.assertNotIn(LOGIN_URL, navigations)
        continue_flow.assert_not_called()

    def test_ensure_studio_login_waits_for_human_verification(self) -> None:
        """``_ensure_studio_login`` must not look for the Log in button
        while a Cloudflare challenge is still on the Studio page.
        """

        class Driver:
            def find_elements(self, _kind, _selector):
                # The wait loop should not surface Log in / dialog
                # elements before verification is cleared.
                raise AssertionError(
                    "driver queried before verification cleared"
                )

        browser, _statuses = make_browser(background=True)
        browser.driver = Driver()
        with (
            patch.object(
                browser,
                "_has_human_verification",
                side_effect=lambda _by: True,
            ),
            patch("cf_browser.time.sleep"),
        ):
            result = browser._ensure_studio_login(FakeBy, object())

        self.assertFalse(result)

    def test_adopt_studio_tab_closes_restored_www_tab(self) -> None:
        browser, _statuses = make_browser()

        class Switcher:
            def __init__(self, driver) -> None:
                self.driver = driver

            def window(self, handle: str) -> None:
                self.driver.current = handle

        class Driver:
            def __init__(self) -> None:
                self.pages = {
                    "www": "https://www.creativefabrica.com/",
                    "studio": "https://studio.creativefabrica.com/",
                }
                self.current = "www"
                self.switch_to = Switcher(self)

            @property
            def window_handles(self) -> list[str]:
                return list(self.pages)

            @property
            def current_window_handle(self) -> str:
                return self.current

            @property
            def current_url(self) -> str:
                return self.pages[self.current]

            def close(self) -> None:
                self.pages.pop(self.current, None)

        browser.driver = Driver()
        browser._adopt_studio_tab()
        self.assertEqual(list(browser.driver.pages), ["studio"])
        self.assertEqual(browser.driver.current, "studio")

    def test_ensure_studio_login_is_false_without_login_or_points(self) -> None:
        browser, _statuses = make_browser()

        class Driver:
            def find_elements(self, _kind, _selector):
                return []

        browser.driver = Driver()
        with (
            patch.object(browser, "_wait_for_studio_ready", return_value=True),
            patch.object(browser, "_read_studio_points", return_value=None),
            patch("cf_browser.time.monotonic", side_effect=[0, 30]),
            patch("cf_browser.time.sleep"),
        ):
            result = browser._ensure_studio_login(FakeBy, object())
        self.assertFalse(result)

    def test_ensure_studio_login_clicks_header_link(self) -> None:
        browser, _statuses = make_browser()

        class Button:
            text = "Log in"
            clicked = False

            def is_displayed(self) -> bool:
                return True

            def get_attribute(self, _name: str) -> str:
                return ""

            def click(self) -> None:
                self.clicked = True

        button = Button()

        class Driver:
            def find_elements(self, _kind, selector):
                if "header" in str(selector) or "aria-label" in str(selector):
                    return [button]
                return []

            def find_element(self, _kind, _selector):
                raise RuntimeError("dialog missing")

        class Wait:
            def __init__(self, driver, _timeout) -> None:
                self.driver = driver

            def until(self, predicate):
                return predicate(self.driver)

        browser.driver = Driver()
        with patch.object(browser, "_wait_for_studio_ready", return_value=True):
            with self.assertRaises(RuntimeError):
                browser._ensure_studio_login(FakeBy, Wait)
        self.assertTrue(button.clicked)


class IncognitoManualCommandTests(unittest.TestCase):
    """The 无痕 自动打开 command must be a plain user Chromium: incognito
    window on the isolated profile with a DevTools port for raw CDP, but no
    ChromeDriver / automation fingerprint."""

    def test_incognito_debug_command_has_required_flags_and_studio_url(self) -> None:
        isolated = Path(
            "C:/Users/me/AppData/Local/TemporaryEmailPickup/"
            "browser_profiles_brave/key"
        )
        command = manual_browser_command(
            Path("brave.exe"),
            isolated,
            incognito=True,
            remote_debugging=True,
        )
        self.assertIn("--incognito", command)
        self.assertIn(f"--user-data-dir={isolated}", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertEqual(command[-1], STUDIO_URL)
        self.assertFalse(any("webdriver" in part.lower() for part in command))
        self.assertFalse(
            any("enable-automation" in part.lower() for part in command)
        )


class LaunchPlainChromiumTests(unittest.TestCase):
    """``launch_plain_chromium`` backs 无痕 自动打开: reuse a live debugger,
    refuse a still-running 手动打开 window without a debug port, otherwise
    Popen the incognito+debug command and wait for the port."""

    def test_live_debugger_address_is_reused_without_popen(self) -> None:
        with (
            patch(
                "cf_browser.existing_debugger_address",
                return_value="127.0.0.1:33333",
            ),
            patch("cf_browser.debugger_address_is_live", return_value=True),
            patch("cf_browser.subprocess.Popen") as popen,
        ):
            address = launch_plain_chromium(Path("brave.exe"), Path("profile"))
        self.assertEqual(address, "127.0.0.1:33333")
        popen.assert_not_called()

    def test_manual_window_without_debugger_raises_and_does_not_popen(self) -> None:
        manual_process = Mock()
        manual_process.poll.return_value = None
        with (
            patch("cf_browser.existing_debugger_address", return_value=None),
            patch("cf_browser.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(
                RuntimeError, MANUAL_BROWSER_NO_DEBUGGER_MESSAGE
            ):
                launch_plain_chromium(
                    Path("brave.exe"),
                    Path("profile"),
                    manual_process=manual_process,
                )
        popen.assert_not_called()

    def test_otherwise_popens_incognito_debug_command_and_returns_address(self) -> None:
        profile = Path(
            "C:/Users/me/AppData/Local/TemporaryEmailPickup/browser_profiles/key"
        )
        with (
            patch(
                "cf_browser.existing_debugger_address",
                side_effect=[None, "127.0.0.1:44444"],
            ),
            patch("cf_browser.debugger_address_is_live", return_value=True),
            patch("cf_browser.subprocess.Popen") as popen,
            patch("cf_browser.time.sleep"),
        ):
            address = launch_plain_chromium(Path("brave.exe"), profile)
        self.assertEqual(address, "127.0.0.1:44444")
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("--incognito", command)
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--remote-debugging-port=0", command)
        self.assertEqual(command[-1], STUDIO_URL)
        self.assertFalse(any("webdriver" in part.lower() for part in command))
        self.assertFalse(
            any("enable-automation" in part.lower() for part in command)
        )


class ScriptedCDPClient(CDPClient):
    """CDPClient double that scripts the Studio DOM and records every send.

    The real ``DOM`` / ``Input`` helper methods (``fill_text``,
    ``click_model`` …) are inherited unchanged, so the recorded command
    stream proves which CDP domain the login used.
    """

    LOGIN_NODE = 10
    DIALOG_NODE = 20
    EMAIL_NODE = 30
    PASSWORD_NODE = 31
    SUBMIT_NODE = 40
    POINTS_NODE = 50

    BOX = {"width": 10, "height": 10, "content": [0, 0, 10, 0, 10, 10, 0, 10]}

    instances: list["ScriptedCDPClient"] = []

    def __init__(self, ws_url: str, *, timeout: float = 10.0) -> None:
        self.ws_url = ws_url
        self.sent_methods: list[str] = []
        self.sent_params: list[dict] = []
        self.clicked: list[int] = []
        self.submitted = False
        self.instances.append(self)

    def __enter__(self) -> "ScriptedCDPClient":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def close(self) -> None:
        pass

    def send(self, method: str, params: dict | None = None) -> dict:
        self.sent_methods.append(method)
        self.sent_params.append(dict(params or {}))
        return {}

    # -- scripted DOM shape --------------------------------------------------

    def document(self) -> int:
        return 1

    def query(self, selector: str, node_id: int | None = None) -> int:
        if selector == _STUDIO_DIALOG_SELECTOR:
            return 0 if self.submitted else self.DIALOG_NODE
        if selector == "input[name='email']":
            return self.EMAIL_NODE
        if selector == "input[name='password']":
            return self.PASSWORD_NODE
        return 0

    def query_all(
        self, selector: str, node_id: int | None = None
    ) -> list[int]:
        if selector == _STUDIO_HEADER_LOGIN_SELECTOR:
            return [] if self.submitted else [self.LOGIN_NODE]
        if selector == _STUDIO_DIALOG_CONTROL_SELECTOR:
            # The dialog is already the login form: no REGISTER FOR FREE
            # toggle needs clicking.
            return []
        if selector == "button[type='submit']":
            return [self.SUBMIT_NODE]
        if selector == _STUDIO_POINTS_SELECTOR:
            return [self.POINTS_NODE] if self.submitted else []
        # Error / captcha markers stay empty on the successful path.
        if selector in (
            _STUDIO_DIALOG_ERROR_SELECTOR,
            _STUDIO_CAPTCHA_IFRAME_SELECTOR,
        ):
            return []
        return []

    def attributes(self, node_id: int) -> dict[str, str]:
        return {}

    def outer_html(self, node_id: int) -> str:
        if node_id == self.LOGIN_NODE:
            return "<button>Log in</button>"
        if node_id == self.SUBMIT_NODE:
            return "<button type='submit'>LOG IN</button>"
        if node_id == self.POINTS_NODE:
            return "<button>5,000 Coins</button>"
        return ""

    def box_model(self, node_id: int) -> dict | None:
        if node_id == self.DIALOG_NODE and self.submitted:
            return None
        if node_id in (
            self.LOGIN_NODE,
            self.DIALOG_NODE,
            self.SUBMIT_NODE,
            self.POINTS_NODE,
        ):
            return dict(self.BOX)
        return None

    def click_node(self, node_id: int) -> bool:
        # Reuse the real mouse dispatch so Input.dispatchMouseEvent is
        # recorded; flip the scripted phase on LOG IN submit.
        self.clicked.append(node_id)
        if node_id == self.SUBMIT_NODE:
            self.submitted = True
        return self.click_model(self.BOX)


class StudioLoginViaCDPTests(unittest.TestCase):
    """``studio_login_via_cdp`` must drive the Studio dialog with trusted
    ``DOM`` / ``Input`` CDP commands only — never ``Runtime.enable`` /
    ``Runtime.evaluate`` (the Turnstile fingerprint ChromeDriver leaks)."""

    WS_URL = "ws://127.0.0.1/devtools/page/1"

    def setUp(self) -> None:
        ScriptedCDPClient.instances = []

    def _studio_target(self) -> dict:
        return {
            "url": "https://studio.creativefabrica.com/",
            "title": "Studio AI",
            "webSocketDebuggerUrl": self.WS_URL,
        }

    def test_success_clicks_header_login_fills_fields_and_submits_without_runtime(
        self,
    ) -> None:
        email = "user@example.com"
        statuses: list[str] = []
        with (
            patch(
                "cf_browser._cdp_wait_studio_tab",
                return_value=(self._studio_target(), False),
            ),
            patch("cf_browser.CDPClient", ScriptedCDPClient),
            patch("cf_browser.time.sleep"),
            patch("cdp_client.time.sleep"),
        ):
            result = studio_login_via_cdp(
                "127.0.0.1:44444",
                email=email,
                status=statuses.append,
            )

        self.assertIsInstance(result, StudioCDPResult)
        self.assertEqual(result.status, STUDIO_CDP_SUCCESS)
        self.assertEqual(result.points, 5000)
        self.assertEqual(len(ScriptedCDPClient.instances), 1)
        client = ScriptedCDPClient.instances[0]
        self.assertEqual(client.ws_url, self.WS_URL)
        # Header "Log in" first, then the dialog "LOG IN" submit, in order.
        self.assertEqual(
            client.clicked,
            [ScriptedCDPClient.LOGIN_NODE, ScriptedCDPClient.SUBMIT_NODE],
        )

        methods = client.sent_methods
        self.assertNotIn("Runtime.enable", methods)
        self.assertNotIn("Runtime.evaluate", methods)
        self.assertFalse(
            any(method.startswith("Runtime.") for method in methods),
            msg=f"unexpected Runtime CDP commands: {methods}",
        )
        # Typing is trusted Input.insertText (fill_text focuses, selects
        # the old value and inserts one event per character); the address
        # fills both email and password fields.
        inserted = "".join(
            params["text"]
            for method, params in zip(methods, client.sent_params)
            if method == "Input.insertText"
        )
        self.assertEqual(inserted, email * 2)
        self.assertIn("DOM.focus", methods)
        # Two clicks = press + release each.
        self.assertEqual(methods.count("Input.dispatchMouseEvent"), 4)

    def test_captcha_pending_returns_pending_without_opening_cdp(self) -> None:
        with (
            patch(
                "cf_browser._cdp_wait_studio_tab",
                return_value=(None, True),
            ),
            patch("cf_browser.CDPClient") as cdp_client,
        ):
            result = studio_login_via_cdp(
                "127.0.0.1:44444",
                email="user@example.com",
                status=lambda _message: None,
                background=True,
            )
        self.assertEqual(result.status, STUDIO_CDP_CAPTCHA_PENDING)
        cdp_client.assert_not_called()

    def test_studio_tab_timeout_returns_failed_without_opening_cdp(self) -> None:
        statuses: list[str] = []
        with (
            patch(
                "cf_browser._cdp_wait_studio_tab",
                return_value=(None, False),
            ),
            patch("cf_browser.CDPClient") as cdp_client,
        ):
            result = studio_login_via_cdp(
                "127.0.0.1:44444",
                email="user@example.com",
                status=statuses.append,
            )
        self.assertEqual(result.status, STUDIO_CDP_FAILED)
        cdp_client.assert_not_called()


class FindSystemBrowserOnUserDataTests(unittest.TestCase):
    """``find_system_browser_on_user_data`` must catch Brave/Chrome processes
    that own the real User Data even when their command line omits
    ``--user-data-dir`` (e.g. the user launched Brave from the taskbar).
    """

    def _run(self, user_data: Path, rows: list[dict[str, str]]):
        with patch("cf_browser._list_system_browser_processes", return_value=rows):
            return find_system_browser_on_user_data(user_data)

    def test_bare_brave_exe_without_user_data_dir_is_a_conflict(self) -> None:
        """A Brave started from the taskbar typically has no
        ``--user-data-dir`` flag; the probe must still flag it."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": '"C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"',
                }
            ],
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "brave.exe")

    def test_brave_renderer_is_not_a_conflict(self) -> None:
        """``--type=renderer`` child processes are helpers, not main processes."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": (
                        '"...\\brave.exe" --type=renderer '
                        '--user-data-dir="C:\\Users\\me\\AppData\\Local\\BraveSoftware\\'
                        'Brave-Browser\\User Data"'
                    ),
                }
            ],
        )
        self.assertEqual(matches, [])

    def test_brave_process_with_app_path_is_not_a_conflict(self) -> None:
        """A TemporaryEmailPickup-managed launch is excluded by name."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": (
                        '"...\\brave.exe" --user-data-dir='
                        '"C:\\Users\\me\\AppData\\Local\\TemporaryEmailPickup\\'
                        'browser_profiles\\shared" --remote-debugging-port=0'
                    ),
                }
            ],
        )
        self.assertEqual(matches, [])

    def test_brave_with_debugging_port_is_not_a_conflict(self) -> None:
        """When Brave was started with ``--remote-debugging-port=...`` the
        app can attach to it, so it is not a conflict."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": (
                        '"...\\brave.exe" --user-data-dir='
                        '"C:\\Users\\me\\AppData\\Local\\BraveSoftware\\'
                        'Brave-Browser\\User Data" --remote-debugging-port=9222'
                    ),
                }
            ],
        )
        self.assertEqual(matches, [])

    def test_brave_with_explicit_user_data_dir_is_a_conflict(self) -> None:
        """Brave started with an explicit ``--user-data-dir`` flag that
        matches the real User Data still counts as a conflict when
        ``--remote-debugging-port`` is missing."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": (
                        '"...\\brave.exe" --user-data-dir='
                        '"C:\\Users\\me\\AppData\\Local\\BraveSoftware\\'
                        'Brave-Browser\\User Data" --new-window'
                    ),
                }
            ],
        )
        self.assertEqual(len(matches), 1)

    def test_other_browser_name_is_filtered_out(self) -> None:
        """A Chrome process must not be reported as a Brave conflict."""
        matches = self._run(
            Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
            [
                {
                    "name": "chrome.exe",
                    "pid": "1234",
                    "command_line": '"...\\chrome.exe"',
                }
            ],
        )
        self.assertEqual(matches, [])


class ListSystemBrowserProcessesScriptTests(unittest.TestCase):
    """The WMI probe must filter in WQL (``Name = 'brave.exe' OR ...``) so it
    never enumerates every Win32_Process — that stalls for many seconds on
    some machines and freezes the Tk UI. The JSON output shape consumed by
    ``find_system_browser_on_user_data`` is unchanged.
    """

    def _run(self, stdout: str):
        with (
            patch("cf_browser.os.name", "nt"),
            patch("cf_browser.subprocess.run") as run,
        ):
            run.return_value = Mock(stdout=stdout, stderr="", returncode=0)
            rows = _list_system_browser_processes()
        return run, rows

    def test_powershell_filters_by_name_instead_of_enumerating_all(self) -> None:
        run, rows = self._run("[]")
        self.assertEqual(rows, [])
        script = run.call_args.args[0][-1]
        self.assertIn("Win32_Process", script)
        self.assertIn("-Filter", script)
        self.assertIn("brave.exe", script)
        self.assertIn("chrome.exe", script)
        # Regression guard: no client-side loop over every process.
        self.assertNotIn("foreach", script.lower())

    def test_rows_keep_name_pid_command_line_shape(self) -> None:
        payload = (
            '[{"Name":"brave.exe","ProcessId":1234,'
            '"CommandLine":"\\"...\\\\brave.exe\\""}]'
        )
        _run, rows = self._run(payload)
        self.assertEqual(
            rows,
            [
                {
                    "name": "brave.exe",
                    "pid": "1234",
                    "command_line": '"...\\brave.exe"',
                }
            ],
        )

    def test_single_process_json_object_is_wrapped_into_a_list(self) -> None:
        payload = '{"Name":"chrome.exe","ProcessId":7,"CommandLine":"x"}'
        _run, rows = self._run(payload)
        self.assertEqual(
            rows,
            [{"name": "chrome.exe", "pid": "7", "command_line": "x"}],
        )


class TerminateStaleProfileBrowserGuardTests(unittest.TestCase):
    """``terminate_stale_profile_browser`` must refuse to touch the user's
    real Brave/Chrome User Data. The guard short-circuits before any
    PowerShell process is spawned so the daily browser is safe.
    """

    def test_real_user_data_path_returns_false_without_subprocess(self) -> None:
        with patch("cf_browser.subprocess.run") as run:
            result = terminate_stale_profile_browser(
                Path("C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"),
                Path("brave.exe"),
            )
        self.assertFalse(result)
        run.assert_not_called()

    def test_temporary_email_pickup_path_falls_through(self) -> None:
        """When the path is app-owned, the helper is allowed to spawn the
        PowerShell terminator. We patch the subprocess so the test does
        not actually kill anything."""
        with TemporaryDirectory() as directory:
            profile = Path(directory) / "TemporaryEmailPickup" / "browser_profiles" / "shared"
            profile.mkdir(parents=True)
            with patch("cf_browser.subprocess.run") as run:
                run.return_value = Mock(stdout="0\n", stderr="", returncode=0)
                result = terminate_stale_profile_browser(profile, Path("brave.exe"))
        self.assertFalse(result)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
