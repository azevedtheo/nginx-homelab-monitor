"""
Nginx uptime monitor with Telegram alerting.

Runs a background thread that periodically checks an HTTP endpoint and
sends a Telegram alert when it goes down, then again when it recovers.
Also registers a /start command with an inline "ping now" button so
anyone in the chat can trigger a manual check on demand.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
import telebot
from dotenv import load_dotenv
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

load_dotenv()  # loads TELEGRAM_BOT_TOKEN etc. from a local .env file, if present


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Config:
    token: str
    chat_id: str
    server_url: str
    check_interval: int = 30          # seconds between checks
    alert_repeat_interval: int = 60   # seconds between repeated "still down" alerts
    failure_threshold: int = 2        # consecutive failed checks before alerting
    request_timeout: int = 5          # seconds

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_ACCOUNT_ID")
        if not token or not chat_id:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_ACCOUNT_ID must be set, "
                "either as environment variables or in a .env file."
            )
        return cls(
            token=token,
            chat_id=chat_id,
            server_url=os.environ.get("SERVER_URL", "http://192.168.0.193"),
            check_interval=int(os.environ.get("CHECK_INTERVAL", 30)),
            alert_repeat_interval=int(os.environ.get("ALERT_REPEAT_INTERVAL", 60)),
            failure_threshold=int(os.environ.get("FAILURE_THRESHOLD", 2)),
        )


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("nginx_monitor")


def setup_logging() -> None:
    """Attach handlers explicitly (rather than at import time) so importing
    this module — e.g. from a test suite — never creates monitor.log as a
    side effect."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("monitor.log")],
    )


# --------------------------------------------------------------------------- #
# Monitor
# --------------------------------------------------------------------------- #

def ping_button() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔍 Ping Server Now", callback_data="manual_ping"))
    return markup


class ServerMonitor:
    """Tracks the up/down state of one HTTP endpoint and alerts via Telegram."""

    def __init__(self, config: Config, bot: telebot.TeleBot):
        self.config = config
        self.bot = bot
        self._session = requests.Session()

        self.is_down = False
        self.consecutive_failures = 0
        self.last_alert_time: float = 0.0
        self.down_since: Optional[datetime] = None

    def check(self) -> bool:
        """Return True if the endpoint responds with HTTP 200."""
        try:
            resp = self._session.get(self.config.server_url, timeout=self.config.request_timeout)
            return resp.status_code == 200
        except requests.exceptions.RequestException as exc:
            log.debug("Health check failed: %s", exc)
            return False

    def _send(self, text: str) -> None:
        """Send a Telegram message; log rather than crash the loop on failure."""
        try:
            self.bot.send_message(self.config.chat_id, text, reply_markup=ping_button())
        except Exception:
            log.exception("Failed to send Telegram alert")

    def run_check_cycle(self) -> None:
        """Run one health check and update alert state accordingly."""
        up = self.check()
        now = time.time()

        if not up:
            self.consecutive_failures += 1

            just_confirmed_down = (
                self.consecutive_failures >= self.config.failure_threshold and not self.is_down
            )
            due_for_repeat_alert = (
                self.is_down and now - self.last_alert_time >= self.config.alert_repeat_interval
            )

            if just_confirmed_down:
                self.is_down = True
                self.down_since = datetime.now()
                log.warning("Server DOWN after %d consecutive failures", self.consecutive_failures)
                self._send(f"🚨 CRITICAL: {self.config.server_url} is unreachable!")
                self.last_alert_time = now
            elif due_for_repeat_alert:
                log.warning("Server still down, sending repeat alert")
                self._send(f"🚨 STILL DOWN: {self.config.server_url} has not recovered.")
                self.last_alert_time = now
        else:
            self.consecutive_failures = 0
            if self.is_down:
                outage = datetime.now() - self.down_since if self.down_since else timedelta(0)
                log.info("Server RECOVERED after %s", outage)
                self._send(
                    f"✅ RESOLVED: {self.config.server_url} is back online "
                    f"(was down for {str(outage).split('.')[0]})."
                )
                self.is_down = False
                self.down_since = None
                self.last_alert_time = 0.0

    def status_text(self) -> str:
        if self.check():
            return f"✅ SUCCESS: {self.config.server_url} is currently UP."
        return f"🚨 FAIL: {self.config.server_url} is currently DOWN or unreachable."

    def loop_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.run_check_cycle()
            stop_event.wait(self.config.check_interval)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    setup_logging()
    config = Config.from_env()
    stop_event = threading.Event()

    bot = telebot.TeleBot(config.token)
    monitor = ServerMonitor(config, bot)

    @bot.message_handler(commands=["start", "menu"])
    def send_menu(message):
        bot.send_message(
            message.chat.id,
            "Welcome to the Nginx Monitor. What would you like to do?",
            reply_markup=ping_button(),
        )

    @bot.callback_query_handler(func=lambda call: call.data == "manual_ping")
    def handle_manual_ping(call):
        bot.answer_callback_query(call.id, "Checking server...")
        bot.send_message(call.message.chat.id, monitor.status_text(), reply_markup=ping_button())

    monitor_thread = threading.Thread(
        target=monitor.loop_forever, args=(stop_event,), daemon=True, name="monitor-thread"
    )
    monitor_thread.start()
    log.info(
        "Monitoring started for %s (interval=%ss, threshold=%d failures)",
        config.server_url, config.check_interval, config.failure_threshold,
    )

    def handle_shutdown(signum, frame):
        log.info("Shutdown signal received, stopping...")
        stop_event.set()
        bot.stop_polling()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        bot.infinity_polling()
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
