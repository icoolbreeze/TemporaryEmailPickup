import json
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import (
    ISOLATED_SESSION_MODE,
    REUSED_SESSION_MODE,
    ManagedMailbox,
    TemporaryMailManagerApp,
    format_imported_at,
)


class FakeBy:
    CSS_SELECTOR = "css"


def make_queue_app():
    app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
    app.closed = False
    app.browser_session_mode_var = Mock()
    app.browser_session_mode_var.get.return_value = REUSED_SESSION_MODE
    app.browser_workers = set()
    app.reused_registration_queue = deque()
    app.reused_registration_paused_key = None
    app.registration_outcomes = {}
    app.registration_last_result = ""
    app.registration_last_kind = "info"
    app.registration_feedback_var = Mock()
    app.registration_feedback_label = Mock()
    app.shared_browser_driver = None
    app.shared_browser_owner = None
    app.manual_browser_processes = {}
    app.mailboxes = {}
    app.status_var = Mock()
    app._layout_browser_buttons = Mock()
    app._save_mailboxes = Mock()
    app._update_active_header = Mock()
    app._update_action_states = Mock()
    app._release_virtual_worker = Mock()
    started: list[str] = []

    def start(mailbox) -> None:
        started.append(mailbox.key)
        app.browser_workers.add(mailbox.key)

    app._start_background_register = start
    return app, started


def mailbox(key: str):
    return SimpleNamespace(
        key=key,
        address=f"{key}@example.com",
        registered=False,
        status="待注册",
    )


class ReusedRegistrationQueueTests(unittest.TestCase):
    def test_multiple_clicks_are_processed_in_fifo_order(self) -> None:
        app, started = make_queue_app()
        accounts = [mailbox(key) for key in ("one", "two", "three")]
        app.mailboxes = {item.key: item for item in accounts}

        for item in accounts:
            app.background_register(item)

        self.assertEqual(started, ["one"])
        self.assertEqual(tuple(app.reused_registration_queue), ("two", "three"))
        self.assertEqual(accounts[1].status, "后台注册排队（第 1 位）")
        self.assertEqual(accounts[2].status, "后台注册排队（第 2 位）")

        app.browser_workers.clear()
        app._start_next_reused_registration()

        self.assertEqual(started, ["one", "two"])
        self.assertEqual(tuple(app.reused_registration_queue), ("three",))

    def test_captcha_pause_blocks_next_queue_item(self) -> None:
        app, started = make_queue_app()
        waiting = mailbox("waiting")
        app.mailboxes = {waiting.key: waiting}
        app.reused_registration_queue.append(waiting.key)
        app.reused_registration_paused_key = "captcha-account"

        app._start_next_reused_registration()

        self.assertEqual(started, [])
        self.assertEqual(tuple(app.reused_registration_queue), (waiting.key,))

    def test_authenticated_account_is_reported_as_success_when_points_fail(self) -> None:
        app, _started = make_queue_app()
        account = mailbox("registered")
        app.mailboxes = {account.key: account}
        app.browser_workers.add(account.key)

        app._finish_background_register(
            account.key,
            RuntimeError("Studio points unavailable"),
            None,
            False,
            True,
        )

        self.assertTrue(account.registered)
        self.assertEqual(account.status, "注册成功 · 积分读取失败")
        self.assertEqual(app.registration_outcomes[account.key], "success")
        self.assertIn("注册成功", app.registration_last_result)

    def test_unauthenticated_error_is_reported_as_failure(self) -> None:
        app, _started = make_queue_app()
        account = mailbox("failed")
        app.mailboxes = {account.key: account}
        app.browser_workers.add(account.key)

        app._finish_background_register(
            account.key,
            RuntimeError("login failed"),
            None,
            False,
            False,
        )

        self.assertFalse(account.registered)
        self.assertEqual(account.status, "注册失败")
        self.assertEqual(app.registration_outcomes[account.key], "failed")


class BrowserTrackingTests(unittest.TestCase):
    def test_publish_then_skip_prevents_re_offering_the_same_driver(self) -> None:
        """After the tracker offers its driver to auto, a later
        ``browser_workers`` iteration must not put the same object back into
        ``tracker_drivers``. The helper must report ``"publish"`` the first
        time and ``"skip"`` once ``offered`` is set.
        """
        first = TemporaryMailManagerApp._tracker_next_action(
            auto_running=True,
            offered=False,
            has_local_driver=True,
            has_browser_driver=False,
        )
        self.assertEqual(first, "publish")

        # After auto pops the driver, the local slot empties; the helper
        # must now skip instead of re-publishing the same object.
        skipped = TemporaryMailManagerApp._tracker_next_action(
            auto_running=True,
            offered=True,
            has_local_driver=False,
            has_browser_driver=False,
        )
        self.assertEqual(skipped, "skip")

        # Same shape with the local slot still populated (e.g. caller did
        # not null ``driver`` yet): still must not re-publish.
        skipped_with_local = TemporaryMailManagerApp._tracker_next_action(
            auto_running=True,
            offered=True,
            has_local_driver=True,
            has_browser_driver=False,
        )
        self.assertEqual(skipped_with_local, "skip")

    def test_instance_can_invoke_tracker_next_action_helper(self) -> None:
        """The live tracker thread calls ``self._tracker_next_action(...)``,
        so the helper must remain reachable from an instance even though it
        is a ``@staticmethod``. Without ``@staticmethod`` this would raise
        ``TypeError: takes 0 positional arguments but 1 was given`` and
        kill the manual-mode tracker on the first loop.
        """
        app = make_queue_app()[0]
        self.assertEqual(
            app._tracker_next_action(
                auto_running=True,
                offered=False,
                has_local_driver=True,
                has_browser_driver=False,
            ),
            "publish",
        )

    def test_existing_browser_driver_means_observe_instead_of_attach(self) -> None:
        """When ``browser_drivers[key]`` already holds a driver, the tracker
        must observe with that one and never attach a second ChromeDriver.
        """
        action = TemporaryMailManagerApp._tracker_next_action(
            auto_running=False,
            offered=True,
            has_local_driver=False,
            has_browser_driver=True,
        )
        self.assertEqual(action, "observe_existing")
        self.assertNotEqual(action, "attach")

        # Even when nothing was offered yet, an existing driver still wins
        # over attaching a new ChromeDriver.
        action_fresh = TemporaryMailManagerApp._tracker_next_action(
            auto_running=False,
            offered=False,
            has_local_driver=False,
            has_browser_driver=True,
        )
        self.assertEqual(action_fresh, "observe_existing")
        self.assertNotEqual(action_fresh, "attach")

    def test_tracker_helper_full_state_matrix(self) -> None:
        """Cover the remaining helper branches for completeness."""
        # Auto not running, no drivers anywhere, never offered -> attach.
        # (Default allow_attach=True keeps the legacy behaviour.)
        self.assertEqual(
            TemporaryMailManagerApp._tracker_next_action(
                auto_running=False,
                offered=False,
                has_local_driver=False,
                has_browser_driver=False,
            ),
            "attach",
        )
        # Auto not running, no drivers anywhere, already offered -> exit.
        self.assertEqual(
            TemporaryMailManagerApp._tracker_next_action(
                auto_running=False,
                offered=True,
                has_local_driver=False,
                has_browser_driver=False,
            ),
            "exit",
        )
        # Auto not running, local driver present -> keep observing it.
        self.assertEqual(
            TemporaryMailManagerApp._tracker_next_action(
                auto_running=False,
                offered=True,
                has_local_driver=True,
                has_browser_driver=False,
            ),
            "observe_local",
        )

    def test_tracker_without_attach_permission_waits_instead_of_attaching(self) -> None:
        """The manual tracker calls the helper with ``allow_attach=False``:
        the state that used to answer ``"attach"`` must come back as
        ``"wait_json"`` so the loop sleeps (and at most polls /json) instead
        of starting a ChromeDriver session. Studio's invisible Turnstile
        fails the human check the moment ``cdc_*`` globals appear, so no
        manual window may ever receive a fresh attach.
        """
        action = TemporaryMailManagerApp._tracker_next_action(
            auto_running=False,
            offered=False,
            has_local_driver=False,
            has_browser_driver=False,
            allow_attach=False,
        )
        self.assertEqual(action, "wait_json")
        self.assertNotEqual(action, "attach")

    def test_login_state_requires_logout_link_or_auth_page(self) -> None:
        class WithLogout:
            def find_elements(self, _kind, selector):
                return [object()] if "logout" in selector else []

        class WithoutLogout:
            def find_elements(self, _kind, selector):
                return []

        self.assertTrue(
            TemporaryMailManagerApp._observed_login_state(
                WithLogout(), FakeBy, "https://www.creativefabrica.com/my-account/"
            )
        )
        self.assertIsNone(
            TemporaryMailManagerApp._observed_login_state(
                WithoutLogout(), FakeBy, "https://www.creativefabrica.com/fonts/"
            )
        )

    def test_login_state_is_false_on_login_pages(self) -> None:
        driver = Mock()
        driver.find_elements.return_value = []
        self.assertFalse(
            TemporaryMailManagerApp._observed_login_state(
                driver, FakeBy, "https://www.creativefabrica.com/login/"
            )
        )
        self.assertFalse(
            TemporaryMailManagerApp._observed_login_state(
                driver, FakeBy, "https://www.creativefabrica.com/signup/"
            )
        )


class AssignCfProfileTests(unittest.TestCase):
    """``_assign_cf_profile`` must hand out unused CF* directories to
    isolated mailboxes and refuse to give two live windows the same one."""

    def _make_app_with_pool(self, directories: list[str]):
        from chromium_profiles import ChromiumNamedProfile

        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        app.named_chromium_profiles = [
            ChromiumNamedProfile(
                browser="Brave",
                directory=directory,
                name=f"CF{'' if directory == 'Profile 4' else directories.index(directory) + 1}",
                user_data_dir=Path("/tmp/brave"),
            )
            for directory in directories
        ]
        app.browser_workers = set()
        app.mailboxes = {}
        return app

    def test_first_isolated_mailbox_gets_first_pool_entry(self) -> None:
        from app import _chromium_assign_cf_profile_for_tests

        app = self._make_app_with_pool(["Profile 4", "Profile 5", "Profile 7"])
        mailbox = SimpleNamespace(
            key="a", address="a@example.com", chromium_profile_directory=None
        )
        app.mailboxes[mailbox.key] = mailbox

        directory = _chromium_assign_cf_profile_for_tests(app, mailbox)

        self.assertEqual(directory, "Profile 4")
        self.assertEqual(mailbox.chromium_profile_directory, "Profile 4")

    def test_second_isolated_mailbox_gets_next_pool_entry(self) -> None:
        from app import _chromium_assign_cf_profile_for_tests

        app = self._make_app_with_pool(["Profile 4", "Profile 5", "Profile 7"])
        first = SimpleNamespace(
            key="a", address="a@example.com", chromium_profile_directory="Profile 4"
        )
        second = SimpleNamespace(
            key="b", address="b@example.com", chromium_profile_directory=None
        )
        app.mailboxes = {first.key: first, second.key: second}
        app.browser_workers = {first.key}

        directory = _chromium_assign_cf_profile_for_tests(app, second)

        self.assertEqual(directory, "Profile 5")
        self.assertEqual(second.chromium_profile_directory, "Profile 5")

    def test_pool_exhausted_raises_chinese_error(self) -> None:
        from app import CF_POOL_EXHAUSTED_MESSAGE, _chromium_assign_cf_profile_for_tests

        app = self._make_app_with_pool(["Profile 4", "Profile 5"])
        first = SimpleNamespace(
            key="a", address="a@example.com", chromium_profile_directory="Profile 4"
        )
        second = SimpleNamespace(
            key="b", address="b@example.com", chromium_profile_directory="Profile 5"
        )
        third = SimpleNamespace(
            key="c", address="c@example.com", chromium_profile_directory=None
        )
        app.mailboxes = {
            first.key: first,
            second.key: second,
            third.key: third,
        }
        app.browser_workers = {first.key, second.key}

        with self.assertRaisesRegex(RuntimeError, CF_POOL_EXHAUSTED_MESSAGE):
            _chromium_assign_cf_profile_for_tests(app, third)


class IncognitoRoutingTests(unittest.TestCase):
    """无痕模式 + Brave/Chrome routes 自动打开 and 后台注册 through the
    plain-window + raw-CDP path. ChromeDriver (``CreativeFabricaBrowser.run``
    / ``webdriver.Chrome``) must never start — its ``cdc_*`` /
    ``navigator.webdriver`` fingerprint makes Studio's Turnstile fail.
    """

    def _make_app(self):
        app, _started = make_queue_app()
        # Isolated (not 复用会话) so the shared-session guards are skipped.
        app.browser_session_mode_var.get.return_value = ISOLATED_SESSION_MODE
        app._chromium_profile_mode = "incognito"
        app.browser_var = Mock()
        app.browser_var.get.return_value = "Brave"
        app._start_incognito_cdp_browser = Mock()
        app._start_incognito_cdp_background = Mock()
        return app

    def test_open_browser_uses_cdp_starter_and_never_selenium(self) -> None:
        app = self._make_app()
        account = mailbox("incog")
        app.mailboxes = {account.key: account}
        with (
            patch("app.CreativeFabricaBrowser") as browser_cls,
            patch("selenium.webdriver.Chrome") as chrome,
        ):
            app.open_browser(account)
        app._start_incognito_cdp_browser.assert_called_once_with(account)
        browser_cls.assert_not_called()
        chrome.assert_not_called()

    def test_background_register_uses_cdp_starter_and_never_selenium(self) -> None:
        app = self._make_app()
        account = mailbox("incog-bg")
        app.mailboxes = {account.key: account}
        with (
            patch("app.CreativeFabricaBrowser") as browser_cls,
            patch("selenium.webdriver.Chrome") as chrome,
        ):
            # The queue fixture replaces ``_start_background_register`` with
            # a stub, so invoke the real unbound method on the app instance.
            TemporaryMailManagerApp._start_background_register(app, account)
        app._start_incognito_cdp_background.assert_called_once_with(account)
        browser_cls.assert_not_called()
        chrome.assert_not_called()


class BrowserProfilePathModeTests(unittest.TestCase):
    """``_browser_profile_path`` must respect ``chromium_profile_mode`` so
    the app never hands the user's real Brave/Chrome User Data to
    Selenium when the user picked the blank isolated profile.
    """

    def _build_app(self, *, mode: str, directory: str | None) -> TemporaryMailManagerApp:
        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        # The app's app_data_root always lives under a TemporaryEmailPickup
        # folder on real Windows installs; mirror that here so the
        # assertion that checks for ``TemporaryEmailPickup`` matches.
        app.app_data_root = Path(
            "C:/Users/me/AppData/Local/TemporaryEmailPickup"
        )
        app.browser_profile_root = app.app_data_root / "browser_profiles"
        app._chromium_profile_mode = mode
        app._chromium_profile_directory = directory or ""
        app._is_named_chromium_browser = lambda _browser_path: True
        app.named_chromium_profiles = []
        app.mailboxes = {}
        # Default session mode is isolated (matches a fresh install). The
        # profile path then lands in ``.../browser_profiles/<key>`` so we
        # can compare with the TemporaryEmailPickup root.
        app.browser_session_mode_var = Mock()
        app.browser_session_mode_var.get.return_value = "隔离会话"
        return app

    def test_app_mode_returns_temporary_email_pickup_path(self) -> None:
        """``app`` mode keeps the blank isolated profile under
        ``TemporaryEmailPickup`` even when the real User Data is
        available."""
        app = self._build_app(mode="app", directory=None)
        real_user_data = Path(
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
        )
        with patch(
            "app.get_user_data_dir_for_browser",
            return_value=real_user_data,
        ) as get_user_data:
            # The mailbox has no chromium_profile_directory; mode "app"
            # means we never call get_user_data_dir_for_browser at all.
            from types import SimpleNamespace

            app.mailboxes = {
                "key": SimpleNamespace(
                    key="key",
                    chromium_profile_directory=None,
                )
            }
            result = app._browser_profile_path("key", Path("brave.exe"))
        self.assertNotEqual(result, real_user_data)
        self.assertIn("TemporaryEmailPickup", str(result))
        # Crucially, the real User Data was NOT consulted.
        get_user_data.assert_not_called()

    def test_named_mode_returns_real_user_data(self) -> None:
        """``named`` mode returns the real User Data root when the
        mailbox has a directory selected."""
        app = self._build_app(mode="named", directory="Profile 4")
        real_user_data = Path(
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
        )
        from types import SimpleNamespace

        app.mailboxes = {
            "key": SimpleNamespace(
                key="key",
                chromium_profile_directory="Profile 4",
            )
        }
        with patch(
            "app.get_user_data_dir_for_browser",
            return_value=real_user_data,
        ):
            result = app._browser_profile_path("key", Path("brave.exe"))
        self.assertEqual(result, real_user_data)

    def test_auto_cf_mode_returns_real_user_data(self) -> None:
        """``auto-cf`` mode with a CF* directory on the mailbox returns
        the real User Data root."""
        from chromium_profiles import ChromiumNamedProfile

        app = self._build_app(mode="auto-cf", directory=None)
        real_user_data = Path(
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
        )
        # The mailbox already has a CF directory assigned; that is what
        # the LRU pool returns. ``_chromium_profile_directory_for_mailbox``
        # calls ``_assign_cf_profile`` which inspects the pool list.
        app.named_chromium_profiles = [
            ChromiumNamedProfile(
                browser="Brave",
                directory="Profile 5",
                name="CF2",
                user_data_dir=real_user_data,
            )
        ]
        from types import SimpleNamespace

        app.mailboxes = {
            "key": SimpleNamespace(
                key="key",
                chromium_profile_directory="Profile 5",
            )
        }
        with patch(
            "app.get_user_data_dir_for_browser",
            return_value=real_user_data,
        ):
            result = app._browser_profile_path("key", Path("brave.exe"))
        self.assertEqual(result, real_user_data)

    def test_named_mode_without_directory_falls_back_to_isolated(self) -> None:
        """``named`` mode with no directory selected must keep the
        isolated profile (cannot inject WebDriver into the user's real
        User Data without a profile picker)."""
        app = self._build_app(mode="named", directory=None)
        real_user_data = Path(
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
        )
        from types import SimpleNamespace

        app.mailboxes = {
            "key": SimpleNamespace(
                key="key",
                chromium_profile_directory=None,
            )
        }
        with patch(
            "app.get_user_data_dir_for_browser",
            return_value=real_user_data,
        ) as get_user_data:
            result = app._browser_profile_path("key", Path("brave.exe"))
        self.assertNotEqual(result, real_user_data)
        self.assertIn("TemporaryEmailPickup", str(result))
        # Real User Data was not consulted because the mailbox has no
        # directory selected.
        get_user_data.assert_not_called()

    def test_outlook_mailbox_can_use_named_chromium_user_data(self) -> None:
        """Outlook rows must take the named-profile attach path.

        The crash on 自动打开 was Brave ``excludeSwitches`` sent while
        launching; Outlook mailboxes were forced off the attach path by a
        ``provider != rootsh`` check.
        """
        app = self._build_app(mode="named", directory="Profile 4")
        app.browser_var = Mock()
        app.browser_var.get.return_value = "Brave"
        app._find_browser = Mock(return_value=Path("brave.exe"))
        app._chromium_user_data_for_browser = Mock(
            return_value=Path(
                "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
            )
        )
        mailbox = SimpleNamespace(
            key="key",
            provider="outlook",
            chromium_profile_directory="Profile 4",
        )
        app.mailboxes = {mailbox.key: mailbox}
        self.assertTrue(app._uses_real_chromium_user_data(mailbox))

    def test_incognito_mode_returns_temporary_email_pickup_path(self) -> None:
        """无痕模式 keeps the isolated TemporaryEmailPickup container and
        must never consult the real Brave User Data helper."""
        app = self._build_app(mode="incognito", directory=None)
        real_user_data = Path(
            "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
        )
        app.mailboxes = {
            "key": SimpleNamespace(key="key", chromium_profile_directory=None)
        }
        with patch(
            "app.get_user_data_dir_for_browser",
            return_value=real_user_data,
        ) as get_user_data:
            result = app._browser_profile_path("key", Path("brave.exe"))
        self.assertNotEqual(result, real_user_data)
        self.assertIn("TemporaryEmailPickup", str(result))
        get_user_data.assert_not_called()

    def test_incognito_mode_does_not_use_real_chromium_user_data(self) -> None:
        """``_uses_real_chromium_user_data`` stays False in incognito mode
        even though the real User Data helper would return a path."""
        app = self._build_app(mode="incognito", directory=None)
        app.browser_var = Mock()
        app.browser_var.get.return_value = "Brave"
        app._find_browser = Mock(return_value=Path("brave.exe"))
        app._chromium_user_data_for_browser = Mock(
            return_value=Path(
                "C:/Users/me/AppData/Local/BraveSoftware/Brave-Browser/User Data"
            )
        )
        mailbox = SimpleNamespace(key="key", chromium_profile_directory=None)
        app.mailboxes = {mailbox.key: mailbox}
        self.assertFalse(app._uses_real_chromium_user_data(mailbox))
        app._chromium_user_data_for_browser.assert_not_called()

    def test_incognito_launch_arguments_include_incognito_switch(self) -> None:
        """Incognito + Brave adds ``--incognito`` and never a
        ``--profile-directory`` (incognito assigns no directory)."""
        app = self._build_app(mode="incognito", directory=None)
        app.browser_var = Mock()
        app.browser_var.get.return_value = "Brave"
        mailbox = SimpleNamespace(key="key", chromium_profile_directory=None)
        app.mailboxes = {mailbox.key: mailbox}
        args = app._chromium_launch_arguments(mailbox, None)
        self.assertIn("--incognito", args)
        self.assertFalse(
            any(arg.startswith("--profile-directory=") for arg in args)
        )

    def test_non_incognito_modes_never_add_incognito_switch(self) -> None:
        """``app`` / ``named`` / ``auto-cf`` must keep launching without
        ``--incognito``."""
        for mode, directory in (
            ("app", None),
            ("named", "Profile 4"),
            ("auto-cf", None),
        ):
            with self.subTest(mode=mode):
                app = self._build_app(mode=mode, directory=directory)
                app.browser_var = Mock()
                app.browser_var.get.return_value = "Brave"
                mailbox = SimpleNamespace(key="key", chromium_profile_directory=None)
                app.mailboxes = {mailbox.key: mailbox}
                args = app._chromium_launch_arguments(mailbox, None)
                self.assertNotIn("--incognito", args)
                if mode == "named":
                    # Sanity: the named mode still selects its directory.
                    self.assertIn("--profile-directory=Profile 4", args)

    def test_leftover_incognito_mode_is_inert_for_non_chromium_browsers(self) -> None:
        """A saved ``incognito`` mode plus a Donut/VirtualBrowser browser
        must not inject ``--incognito`` into those backends."""
        app = self._build_app(mode="incognito", directory=None)
        mailbox = SimpleNamespace(key="key", chromium_profile_directory=None)
        app.mailboxes = {mailbox.key: mailbox}
        for browser in ("Donut", "VirtualBrowser"):
            with self.subTest(browser=browser):
                app.browser_var = Mock()
                app.browser_var.get.return_value = browser
                self.assertFalse(app._uses_incognito_launch())
                args = app._chromium_launch_arguments(mailbox, None)
                self.assertNotIn("--incognito", args)

    def test_brave_options_order_builtins_before_named_profiles(self) -> None:
        """The 「配置」 combobox lists 空白隔离配置, 无痕模式, 自动分配 CF
        配置, then the discovered named profile names."""
        from chromium_profiles import ChromiumNamedProfile

        app = self._build_app(mode="app", directory=None)
        app.named_chromium_profiles = [
            ChromiumNamedProfile(
                browser="Brave",
                directory="Profile 4",
                name="CF",
                user_data_dir=Path("/tmp/brave"),
            )
        ]
        options = app._chromium_profile_options_for_browser("Brave")
        self.assertEqual(
            options[:3],
            ["空白隔离配置", "无痕模式", "自动分配 CF 配置"],
        )
        self.assertEqual(options[3], "CF")

    def test_decode_incognito_selection(self) -> None:
        app = self._build_app(mode="app", directory=None)
        self.assertEqual(
            app._decode_chromium_profile_selection("无痕模式"),
            ("incognito", ""),
        )

    def test_incognito_mode_display_label(self) -> None:
        app = self._build_app(mode="incognito", directory=None)
        self.assertEqual(
            app._chromium_profile_display_for_mode("incognito", ""),
            "无痕模式",
        )


class UiThreadFreezeTests(unittest.TestCase):
    """自动打开 / 手动打开 click handlers must return immediately: no WMI
    probe (``Get-CimInstance``) and no ``DevToolsActivePort`` polling on the
    Tk thread. Isolated ``app`` / 无痕 launches never run the daily-Brave
    conflict check; named real-User-Data launches run it inside the worker.
    """

    def _make_app(self, *, mode: str = "incognito", directory: str | None = None,
                  real_user_data: bool = False):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        app.closed = False
        app.root = Mock()
        app.browser_session_mode_var = Mock()
        app.browser_session_mode_var.get.return_value = ISOLATED_SESSION_MODE
        app.browser_workers = set()
        app.manual_browser_processes = {}
        app.browser_drivers = {}
        app.tracker_drivers = {}
        app.mailboxes = {}
        app.shared_browser_driver = None
        app.shared_browser_owner = None
        app.reused_registration_paused_key = None
        app.reused_registration_queue = deque()
        app.registration_outcomes = {}
        app.status_var = Mock()
        app.registration_feedback_var = Mock()
        app.registration_feedback_label = Mock()
        app.browser_var = Mock()
        app.browser_var.get.return_value = "Brave"
        app._chromium_profile_mode = mode
        app._chromium_profile_directory = directory or ""
        app.named_chromium_profiles = []
        app.app_data_root = Path(tmp.name) / "TemporaryEmailPickup"
        app.browser_profile_root = app.app_data_root / "browser_profiles"
        brave = Path(
            "C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe"
        )
        app._find_browser = Mock(return_value=brave)
        app._uses_real_chromium_user_data = Mock(return_value=real_user_data)
        app._system_browser_already_running = Mock(return_value=False)
        app._ensure_named_chromium_debugger = Mock(
            return_value=(
                brave,
                Path(tmp.name) / "User Data",
                "127.0.0.1:33333",
            )
        )
        app._update_active_header = Mock()
        app._layout_browser_buttons = Mock()
        app._update_action_states = Mock()
        app._update_registration_feedback = Mock()
        app._release_virtual_worker = Mock()
        app._record_registration_outcome = Mock()
        app._save_mailboxes = Mock()
        return app, Path(tmp.name)

    @staticmethod
    def _mailbox(key: str = "m"):
        item = SimpleNamespace(
            key=key,
            address=f"{key}@example.com",
            provider="outlook",
            expired=False,
            status="待注册",
        )
        item.client = Mock()
        item.client.clone.return_value = Mock()
        return item

    def test_prepare_incognito_launch_never_probes_running_browser(self) -> None:
        app, _tmp = self._make_app(mode="incognito")
        item = self._mailbox()
        with patch("app.find_system_browser_on_user_data") as find_system:
            browser_path, profile_path, browser_name = (
                app._prepare_incognito_cdp_launch(item)
            )
        app._system_browser_already_running.assert_not_called()
        find_system.assert_not_called()
        self.assertEqual(browser_name, "Brave")
        # The isolated profile directory was still created.
        self.assertTrue(profile_path.is_dir())

    def test_manual_isolated_popens_without_probing_running_browser(self) -> None:
        app, _tmp = self._make_app(mode="incognito")
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        with (
            patch("app.find_system_browser_on_user_data") as find_system,
            patch("app.subprocess.Popen") as popen,
        ):
            app._open_manual_browser(item)
        app._system_browser_already_running.assert_not_called()
        find_system.assert_not_called()
        popen.assert_called_once()
        # 手动打开 stays a plain user window: no DevTools port.
        command = popen.call_args.args[0]
        self.assertFalse(
            any(part.startswith("--remote-debugging-port") for part in command)
        )
        self.assertIn(item.key, app.manual_browser_processes)

    def test_manual_app_mode_popens_without_probing_running_browser(self) -> None:
        app, _tmp = self._make_app(mode="app")
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        with (
            patch("app.find_system_browser_on_user_data") as find_system,
            patch("app.subprocess.Popen") as popen,
        ):
            app._open_manual_browser(item)
        app._system_browser_already_running.assert_not_called()
        find_system.assert_not_called()
        popen.assert_called_once()

    def test_manual_named_profile_defers_conflict_probe_off_tk_thread(self) -> None:
        app, tmp = self._make_app(
            mode="named", directory="Profile 4", real_user_data=True
        )
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        user_data = tmp / "User Data"
        captured: list[object] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **_kwargs):
                self.target = target
                captured.append(self)

            def start(self) -> None:
                pass

        after_callbacks: list = []
        app.root.after.side_effect = lambda _delay, fn=None: (
            after_callbacks.append(fn) if fn is not None else None
        )
        with (
            patch("app.get_user_data_dir_for_browser", return_value=user_data),
            patch("app.find_system_browser_on_user_data") as find_system,
            patch("app.threading.Thread", FakeThread),
            patch("app.subprocess.Popen") as popen,
            patch("app.messagebox.showerror") as showerror,
        ):
            app._open_manual_browser(item)
            # Synchronous part: guard thread queued, but neither WMI nor
            # Popen ran on the Tk thread.
            app._system_browser_already_running.assert_not_called()
            find_system.assert_not_called()
            popen.assert_not_called()
            self.assertEqual(len(captured), 1)

            # Daily Brave conflict: worker reports the Chinese error via
            # root.after and never launches a second window.
            app._system_browser_already_running.return_value = True
            captured[0].target()
            self.assertEqual(len(after_callbacks), 1)
            after_callbacks.pop()()
            showerror.assert_called_once()
            popen.assert_not_called()

            # No conflict: worker schedules the Popen back on the Tk thread.
            app._system_browser_already_running.return_value = False
            captured[0].target()
            after_callbacks.pop()()
            popen.assert_called_once()

    def test_open_browser_isolated_prepares_without_wmi_or_port_poll(self) -> None:
        app, _tmp = self._make_app(mode="app")
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        captured: list[object] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **_kwargs):
                self.target = target
                captured.append(self)

            def start(self) -> None:
                pass

        with (
            patch("app.find_system_browser_on_user_data") as find_system,
            patch("app.subprocess.Popen") as popen,
            patch("app.threading.Thread", FakeThread),
        ):
            app.open_browser(item)
        app._system_browser_already_running.assert_not_called()
        find_system.assert_not_called()
        app._ensure_named_chromium_debugger.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(len(captured), 1)

    def test_open_browser_named_profile_ensures_debugger_inside_worker(self) -> None:
        app, tmp = self._make_app(
            mode="named", directory="Profile 4", real_user_data=True
        )
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        user_data = tmp / "User Data"
        captured: list[object] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **_kwargs):
                self.target = target
                captured.append(self)

            def start(self) -> None:
                pass

        app.root.after.side_effect = lambda _delay, fn=None: None
        with (
            patch("app.get_user_data_dir_for_browser", return_value=user_data),
            patch("app.threading.Thread", FakeThread),
            patch("app.CreativeFabricaBrowser") as browser_cls,
        ):
            app.open_browser(item)
            # Nothing slow ran before the worker thread was queued.
            app._ensure_named_chromium_debugger.assert_not_called()
            self.assertEqual(len(captured), 1)
            captured[0].target()
        app._ensure_named_chromium_debugger.assert_called_once_with(item)
        browser_cls.assert_called_once()
        kwargs = browser_cls.call_args.kwargs
        self.assertEqual(kwargs["debugger_address"], "127.0.0.1:33333")
        self.assertIsNone(kwargs["profile_path"])

    def test_background_isolated_prepares_without_wmi(self) -> None:
        app, _tmp = self._make_app(mode="app")
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        captured: list[object] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **_kwargs):
                self.target = target
                captured.append(self)

            def start(self) -> None:
                pass

        with (
            patch("app.find_system_browser_on_user_data") as find_system,
            patch("app.subprocess.Popen") as popen,
            patch("app.threading.Thread", FakeThread),
        ):
            TemporaryMailManagerApp._start_background_register(app, item)
        app._system_browser_already_running.assert_not_called()
        find_system.assert_not_called()
        app._ensure_named_chromium_debugger.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(len(captured), 1)

    def test_background_named_profile_ensures_debugger_inside_worker(self) -> None:
        app, tmp = self._make_app(
            mode="named", directory="Profile 4", real_user_data=True
        )
        item = self._mailbox()
        app.mailboxes = {item.key: item}
        user_data = tmp / "User Data"
        captured: list[object] = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **_kwargs):
                self.target = target
                captured.append(self)

            def start(self) -> None:
                pass

        app.root.after.side_effect = lambda _delay, fn=None: None
        with (
            patch("app.get_user_data_dir_for_browser", return_value=user_data),
            patch("app.threading.Thread", FakeThread),
            patch("app.CreativeFabricaBrowser") as browser_cls,
        ):
            TemporaryMailManagerApp._start_background_register(app, item)
            app._ensure_named_chromium_debugger.assert_not_called()
            self.assertEqual(len(captured), 1)
            captured[0].target()
        app._ensure_named_chromium_debugger.assert_called_once_with(item)
        browser_cls.assert_called_once()
        kwargs = browser_cls.call_args.kwargs
        self.assertEqual(kwargs["debugger_address"], "127.0.0.1:33333")
        self.assertIsNone(kwargs["profile_path"])


class ImportedAtTests(unittest.TestCase):
    def test_missing_zero_and_negative_format_as_em_dash(self) -> None:
        for value in (None, 0, 0.0, -1, -1700000000.0):
            with self.subTest(value=value):
                self.assertEqual(format_imported_at(value), "—")

    def test_known_epoch_formats_as_local_minute(self) -> None:
        epoch = 1700000000.0
        self.assertEqual(
            format_imported_at(epoch),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)),
        )

    def test_non_numeric_and_unrepresentable_values_format_as_em_dash(self) -> None:
        for value in ("1700000000", "abc", True, False, float("inf"), float("nan")):
            with self.subTest(value=value):
                self.assertEqual(format_imported_at(value), "—")

    def test_mailbox_values_puts_formatted_epoch_in_second_column(self) -> None:
        epoch = 1700000000.0
        mailbox = ManagedMailbox(
            key="stamped",
            client=Mock(),
            address="stamped@bccto.cc",
            local_part="stamped",
            domain="bccto.cc",
            expires_at=time.monotonic() + 600,
            imported_at=epoch,
        )
        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        values = app._mailbox_values(mailbox)
        self.assertEqual(len(values), 8)
        self.assertEqual(values[0], "stamped@bccto.cc")
        self.assertEqual(
            values[1],
            time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)),
        )
        self.assertEqual(values[-2:], ("", ""))

    def test_mailbox_values_second_column_is_em_dash_without_stamp(self) -> None:
        mailbox = ManagedMailbox(
            key="unstamped",
            client=Mock(),
            address="unstamped@bccto.cc",
            local_part="unstamped",
            domain="bccto.cc",
            expires_at=time.monotonic() + 600,
        )
        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        values = app._mailbox_values(mailbox)
        self.assertEqual(values[1], "—")

    def _make_persistence_app(self) -> tuple[TemporaryMailManagerApp, TemporaryDirectory]:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = TemporaryMailManagerApp.__new__(TemporaryMailManagerApp)
        app.closed = False
        app.app_data_root = Path(tmp.name)
        app.mailboxes_path = app.app_data_root / "mailboxes.json"
        app.mailboxes = {}
        app.status_var = Mock()
        return app, tmp

    def test_save_restore_round_trips_imported_at(self) -> None:
        app, _tmp = self._make_persistence_app()
        epoch = 1700000000.0
        client = Mock()
        client.export_cookies.return_value = []
        mailbox = ManagedMailbox(
            key="round-trip",
            client=client,
            address="round-trip@bccto.cc",
            local_part="round-trip",
            domain="bccto.cc",
            expires_at=time.monotonic() + 600,
            imported_at=epoch,
        )
        app.mailboxes[mailbox.key] = mailbox

        app._save_mailboxes()
        payload = json.loads(app.mailboxes_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 3)
        self.assertEqual(len(payload["mailboxes"]), 1)
        self.assertEqual(payload["mailboxes"][0]["imported_at"], epoch)

        app.mailboxes = {}
        app.mailbox_tree = Mock()
        app._create_browser_cell_button = Mock()
        app._activate_mailbox = Mock()
        app._restore_mailboxes()

        self.assertEqual(set(app.mailboxes), {"round-trip"})
        restored = app.mailboxes["round-trip"]
        self.assertEqual(restored.imported_at, epoch)
        inserted_values = app.mailbox_tree.insert.call_args.kwargs["values"]
        self.assertEqual(
            inserted_values[1],
            time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)),
        )

    def test_save_does_not_overwrite_existing_stamp(self) -> None:
        app, _tmp = self._make_persistence_app()
        epoch = 1700000000.0
        client = Mock()
        client.export_cookies.return_value = []
        mailbox = ManagedMailbox(
            key="keep-stamp",
            client=client,
            address="keep-stamp@bccto.cc",
            local_part="keep-stamp",
            domain="bccto.cc",
            expires_at=time.monotonic() + 600,
            imported_at=epoch,
        )
        app.mailboxes[mailbox.key] = mailbox
        app._save_mailboxes()
        self.assertEqual(mailbox.imported_at, epoch)

    def test_legacy_row_without_imported_at_restores_as_none(self) -> None:
        cases = ("<missing>", None, 0, -5, "nope", True)
        for case in cases:
            with self.subTest(case=case):
                app, _tmp = self._make_persistence_app()
                row: dict[str, object] = {
                    "key": "legacy",
                    "provider": "rootsh",
                    "address": "legacy@bccto.cc",
                    "local_part": "legacy",
                    "domain": "bccto.cc",
                    "expires_at": time.time() + 600,
                    "cursor": 0,
                    "points": None,
                    "registered": False,
                    "donut_profile_id": None,
                    "donut_debugger_address": None,
                    "virtual_worker_id": None,
                    "chromium_profile_directory": None,
                    "messages": [],
                    "cookies": [],
                }
                if case != "<missing>":
                    row["imported_at"] = case
                app.app_data_root.mkdir(parents=True, exist_ok=True)
                app.mailboxes_path.write_text(
                    json.dumps(
                        {"version": 3, "saved_at": time.time(), "mailboxes": [row]}
                    ),
                    encoding="utf-8",
                )
                app.mailbox_tree = Mock()
                app._create_browser_cell_button = Mock()
                app._activate_mailbox = Mock()
                app._restore_mailboxes()
                self.assertIsNone(app.mailboxes["legacy"].imported_at)


if __name__ == "__main__":
    unittest.main()
