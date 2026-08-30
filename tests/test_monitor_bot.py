"""
Unit tests for the ServerMonitor alerting logic.

Run with:  pytest -v

These tests mock both the Telegram bot and the HTTP layer, so they exercise
the alerting *decisions* (when to alert, when to stay quiet, when to recover)
without needing a real bot token or a real server to hit.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from monitor_bot import Config, ServerMonitor


def make_config(**overrides) -> Config:
    defaults = dict(
        token="test-token",
        chat_id="12345",
        server_url="http://example.invalid",
        check_interval=1,
        alert_repeat_interval=60,
        failure_threshold=2,
        request_timeout=1,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture
def bot():
    return MagicMock()


@pytest.fixture
def monitor(bot):
    return ServerMonitor(make_config(), bot)


class TestCheck:
    def test_check_returns_true_on_http_200(self, monitor):
        with patch.object(monitor._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert monitor.check() is True

    def test_check_returns_false_on_non_200(self, monitor):
        with patch.object(monitor._session, "get") as mock_get:
            mock_get.return_value = MagicMock(status_code=503)
            assert monitor.check() is False

    def test_check_returns_false_on_connection_error(self, monitor):
        with patch.object(monitor._session, "get", side_effect=requests.exceptions.ConnectionError):
            assert monitor.check() is False


class TestAlerting:
    def test_no_alert_on_single_failure_below_threshold(self, monitor, bot):
        """failure_threshold=2, so one bad check should NOT page anyone."""
        with patch.object(monitor, "check", return_value=False):
            monitor.run_check_cycle()
        bot.send_message.assert_not_called()
        assert monitor.is_down is False

    def test_alert_fires_once_threshold_reached(self, monitor, bot):
        with patch.object(monitor, "check", return_value=False):
            monitor.run_check_cycle()  # failure 1: below threshold
            monitor.run_check_cycle()  # failure 2: hits threshold
        assert monitor.is_down is True
        bot.send_message.assert_called_once()
        assert "CRITICAL" in bot.send_message.call_args.args[1]

    def test_repeat_alert_respects_interval(self, monitor, bot):
        monitor.config = make_config(failure_threshold=1, alert_repeat_interval=100)
        with patch.object(monitor, "check", return_value=False):
            monitor.run_check_cycle()  # first alert
            monitor.run_check_cycle()  # too soon for a repeat (< 100s elapsed)
        assert bot.send_message.call_count == 1

    def test_recovery_sends_resolved_message(self, monitor, bot):
        with patch.object(monitor, "check", return_value=False):
            monitor.run_check_cycle()
            monitor.run_check_cycle()  # now down
        bot.send_message.reset_mock()

        with patch.object(monitor, "check", return_value=True):
            monitor.run_check_cycle()  # recovers

        assert monitor.is_down is False
        bot.send_message.assert_called_once()
        assert "RESOLVED" in bot.send_message.call_args.args[1]

    def test_send_failure_does_not_crash_the_loop(self, monitor, bot):
        """A Telegram API error must never propagate out of run_check_cycle."""
        bot.send_message.side_effect = Exception("Telegram is down too")
        with patch.object(monitor, "check", return_value=False):
            monitor.run_check_cycle()
            monitor.run_check_cycle()  # triggers an alert attempt that will "fail"
        # Reaching this line without an exception is the assertion.


class TestStatusText:
    def test_status_text_up(self, monitor):
        with patch.object(monitor, "check", return_value=True):
            assert "SUCCESS" in monitor.status_text()

    def test_status_text_down(self, monitor):
        with patch.object(monitor, "check", return_value=False):
            assert "FAIL" in monitor.status_text()
