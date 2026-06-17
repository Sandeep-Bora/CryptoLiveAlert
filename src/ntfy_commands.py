import json
import threading
from os import getenv
from time import sleep

import requests

from .config import USE_MONGO_DB
from .alert_commands import AlertCommandHandler
from .logger import logger
from .notifications import send_ntfy
from .user_configuration import LocalUserConfiguration, MongoDBUserConfiguration

BaseConfig = LocalUserConfiguration if not USE_MONGO_DB else MongoDBUserConfiguration

# Titles/tags on outbound alert pushes — ignore these on the command topic stream
_ALERT_TITLE_MARKERS = ("CEX ALERT", "TECHNICAL ALERT", "Crypto Alert")
_COMMAND_PREFIXES = (
    "new_alert",
    "/new_alert",
    "newalert",
    "/newalert",
    "cancel_alert",
    "/cancel_alert",
    "view_alerts",
    "/view_alerts",
)


def get_ntfy_command_topic() -> str | None:
    topic = getenv("NTFY_COMMAND_TOPIC")
    if topic:
        return topic.strip()
    base = getenv("NTFY_TOPIC")
    if base:
        return f"{base.strip()}-commands"
    return None


def _is_outbound_alert_echo(title: str, message: str) -> bool:
    if title and any(marker in title for marker in _ALERT_TITLE_MARKERS):
        return True
    if message.startswith("🔔") or message.startswith("Bot Reply:"):
        return True
    return False


def _looks_like_command(message: str) -> bool:
    lower = message.strip().lower()
    return any(lower.startswith(p) for p in _COMMAND_PREFIXES)


class NtfyCommandListener:
    """Subscribe to an ntfy topic and run alert commands posted from the ntfy app."""

    def __init__(self, telegram_bot):
        self.bot = telegram_bot
        self.handler = AlertCommandHandler(telegram_bot)
        self.server = (getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        self.command_topic = get_ntfy_command_topic()
        self.user_id = getenv("NTFY_COMMAND_USER_ID") or getenv("TELEGRAM_USER_ID")
        self.auth_token = getenv("NTFY_COMMAND_TOKEN")
        self._since = "all"
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

    def _reply(self, text: str) -> None:
        configuration = BaseConfig(str(self.user_id))
        topics = configuration.get_ntfy_topics()
        if not topics:
            logger.warning("ntfy command reply skipped — no ntfy topics configured")
            return
        for topic in topics:
            send_ntfy(
                message=text,
                topic=topic,
                title="Bot Reply",
                server=self.server,
                priority="default",
                tags="robot",
            )

    def _handle_message(self, payload: dict) -> None:
        if payload.get("event") != "message":
            return

        msg_id = payload.get("id")
        if not msg_id:
            return

        with self._lock:
            if msg_id in self._seen_ids:
                return
            self._seen_ids.add(msg_id)
            if len(self._seen_ids) > 500:
                self._seen_ids = set(list(self._seen_ids)[-250:])

        message = (payload.get("message") or "").strip()
        title = (payload.get("title") or "").strip()

        if not message or _is_outbound_alert_echo(title, message):
            return
        if not _looks_like_command(message):
            return

        logger.info(f"ntfy command received: {message[:120]}")
        try:
            response = self.handler.process(str(self.user_id), message)
        except Exception as exc:
            logger.exception("ntfy command failed", exc_info=exc)
            response = f"Command failed: {exc}"

        self._reply(response)

    def run(self) -> None:
        if not self.command_topic:
            logger.info("NTFY_COMMAND_TOPIC not set — ntfy command listener disabled")
            return
        if not self.user_id:
            logger.warning("TELEGRAM_USER_ID required for ntfy commands — listener disabled")
            return

        logger.info(
            f"ntfy command listener started on topic '{self.command_topic}' "
            f"(publish commands here; replies go to your alert topic)"
        )

        while True:
            try:
                url = f"{self.server}/{self.command_topic}/json"
                params = {"poll": 1, "since": self._since}
                with requests.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    stream=True,
                    timeout=130,
                ) as response:
                    response.raise_for_status()
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not raw_line:
                            continue
                        payload = json.loads(raw_line)
                        if payload.get("id"):
                            self._since = payload["id"]
                        self._handle_message(payload)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logger.warning(f"ntfy command listener error — retrying in 10s: {exc}")
                sleep(10)


def start_ntfy_command_listener(telegram_bot) -> None:
    topic = get_ntfy_command_topic()
    if not topic:
        return
    listener = NtfyCommandListener(telegram_bot)
    threading.Thread(target=listener.run, daemon=True).start()
