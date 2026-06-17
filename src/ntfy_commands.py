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

# Ignore our own outbound pushes when reading the command topic stream
_ALERT_TITLE_MARKERS = ("CEX ALERT", "TECHNICAL ALERT", "Crypto Alert", "Bot Reply")
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


def _is_outbound_echo(title: str, message: str, tags: list | None = None) -> bool:
    if title and any(marker in title for marker in _ALERT_TITLE_MARKERS):
        return True
    if message.startswith("🔔") or message.startswith("Bot Reply:"):
        return True
    if tags and "robot" in tags:
        return True
    return False


def _looks_like_command(message: str) -> bool:
    lower = message.strip().lower()
    return any(lower.startswith(p) for p in _COMMAND_PREFIXES)


def _retry_after_seconds(response: requests.Response | None, default: int) -> int:
    if response is None:
        return default
    raw = response.headers.get("Retry-After")
    if raw and raw.isdigit():
        return max(int(raw), default)
    return default


class NtfyCommandListener:
    """Subscribe to an ntfy topic and run alert commands posted from the ntfy app."""

    def __init__(self, telegram_bot):
        self.bot = telegram_bot
        self.handler = AlertCommandHandler(telegram_bot)
        self.server = (getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        self.command_topic = get_ntfy_command_topic()
        self.user_id = getenv("NTFY_COMMAND_USER_ID") or getenv("TELEGRAM_USER_ID")
        self.auth_token = getenv("NTFY_COMMAND_TOKEN") or getenv("NTFY_TOKEN")
        self._since = "30m"
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        if self.auth_token:
            return {"Authorization": f"Bearer {self.auth_token}"}
        return {}

    def _reply(self, text: str) -> None:
        """Send command response to the command topic (where the user is) and alert topic(s)."""
        topics: list[str] = []
        if self.command_topic:
            topics.append(self.command_topic)
        configuration = BaseConfig(str(self.user_id))
        for topic in configuration.get_ntfy_topics():
            if topic and topic not in topics:
                topics.append(topic)
        if not topics:
            logger.warning("ntfy command reply skipped — no topics configured")
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
        tags = payload.get("tags") or []

        if not message or _is_outbound_echo(title, message, tags):
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

    def _listen_once(self) -> None:
        """Open one long-lived JSON stream (no poll=1 loop — avoids ntfy 429 rate limits)."""
        url = f"{self.server}/{self.command_topic}/json"
        params = {"since": self._since}
        with requests.get(
            url,
            params=params,
            headers=self._headers(),
            stream=True,
            timeout=(15, 360),
        ) as response:
            if response.status_code == 429:
                wait = _retry_after_seconds(response, 60)
                raise requests.HTTPError(
                    f"429 Too Many Requests — retry after {wait}s", response=response
                )
            response.raise_for_status()
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                payload = json.loads(raw_line)
                if payload.get("id"):
                    self._since = payload["id"]
                self._handle_message(payload)

    def run(self) -> None:
        if not self.command_topic:
            logger.info("NTFY_COMMAND_TOPIC not set — ntfy command listener disabled")
            return
        if not self.user_id:
            logger.warning("TELEGRAM_USER_ID required for ntfy commands — listener disabled")
            return

        logger.info(
            f"ntfy command listener started on topic '{self.command_topic}' "
            f"(publish commands here; replies appear on this topic + NTFY_TOPIC)"
        )
        if not self.auth_token:
            logger.info(
                "Tip: set NTFY_TOKEN (ntfy.sh access token) for higher rate limits on Render"
            )

        backoff = 15
        while True:
            try:
                self._listen_once()
                backoff = 15
            except KeyboardInterrupt:
                return
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    wait = _retry_after_seconds(exc.response, backoff)
                    logger.warning(
                        f"ntfy rate limit (429) — backing off {wait}s. "
                        f"Set NTFY_TOKEN on Render for higher limits, or wait before retrying."
                    )
                    sleep(wait)
                    backoff = min(backoff * 2, 300)
                else:
                    logger.warning(f"ntfy command listener HTTP error — retry in {backoff}s: {exc}")
                    sleep(backoff)
                    backoff = min(backoff * 2, 120)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ReadTimeout,
            ) as exc:
                logger.info(f"ntfy stream ended ({exc}) — reconnecting in {backoff}s")
                sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as exc:
                logger.warning(f"ntfy command listener error — retry in {backoff}s: {exc}")
                sleep(backoff)
                backoff = min(backoff * 2, 120)


def start_ntfy_command_listener(telegram_bot) -> None:
    topic = get_ntfy_command_topic()
    if not topic:
        return
    listener = NtfyCommandListener(telegram_bot)
    threading.Thread(target=listener.run, daemon=True).start()
