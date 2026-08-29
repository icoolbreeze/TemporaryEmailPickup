import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

from app import REUSED_SESSION_MODE, TemporaryMailManagerApp


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


if __name__ == "__main__":
    unittest.main()
