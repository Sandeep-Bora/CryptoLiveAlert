import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from os import getenv
from time import sleep

from .alert_processes import CEXAlertProcess, TechnicalAlertProcess, ORBAlertProcess
from .telegram import TelegramBot
from .config import USE_MONGO_DB
from .user_configuration import (
    LocalUserConfiguration,
    MongoDBUserConfiguration,
    get_whitelist,
)
from .utils import handle_env, is_telegram_polling_enabled
from .indicators import TaapiioProcess
from .logger import logger
from .setup import do_setup
from .ntfy_commands import (
    start_ntfy_command_listener,
    get_ntfy_listener_status,
    get_ntfy_command_topic,
)


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal handler so platforms like Render detect an open port and uptime pingers keep the service awake."""

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            status = get_ntfy_listener_status()
            body = json.dumps(
                {
                    "status": "ok",
                    "telegram_polling": is_telegram_polling_enabled(),
                    "ntfy_listener": status,
                    "ntfy_command_topic": get_ntfy_command_topic(),
                    "whitelisted_users": len(get_whitelist()),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Telegram Crypto Alerts bot is running.")

    def log_message(self, *args):
        # Silence default stderr request logging
        return


def start_keepalive_server():
    """Bind a tiny HTTP server to the platform-provided PORT (no-op locally if PORT is unset)."""
    port = getenv("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), _HealthCheckHandler)
    logger.info(f"Keep-alive HTTP server listening on port {port}")
    server.serve_forever()


if __name__ == "__main__":
    # Start keep-alive HTTP server first so the hosting platform detects the port quickly
    threading.Thread(target=start_keepalive_server, daemon=True).start()

    # Process environment variables
    handle_env()

    # Do the setup process if the bot is not set up
    if len(get_whitelist()) == 0:
        do_setup()
        logger.info("Waiting for initialization ...")
        sleep(5)

    # Ensure NTFY_TOPIC from env is registered for every whitelisted user
    ntfy_topic = getenv("NTFY_TOPIC")
    if ntfy_topic:
        BaseConfig = (
            LocalUserConfiguration if not USE_MONGO_DB else MongoDBUserConfiguration
        )
        for uid in get_whitelist():
            try:
                BaseConfig(uid).add_ntfy_topics([ntfy_topic.strip()])
            except Exception as exc:
                logger.warning(f"Could not sync ntfy topic for user {uid}: {exc}")

    taapiio_process = None
    if getenv("TAAPIIO_APIKEY"):
        # Create global Taapi.io process for the aggregator and telegram bot to sync calls
        taapiio_process = TaapiioProcess(
            taapiio_apikey=getenv("TAAPIIO_APIKEY"),
            telegram_bot_token=getenv("TELEGRAM_BOT_TOKEN"),
        )

    # Create the Telegram bot (used for sending alerts + shared command handler)
    telegram_bot = TelegramBot(
        bot_token=getenv("TELEGRAM_BOT_TOKEN"), taapiio_process=taapiio_process
    )

    ntfy_cmd_topic = get_ntfy_command_topic()
    if getenv("NTFY_TOPIC"):
        logger.info(
            f"ntfy alerts topic: {getenv('NTFY_TOPIC')} | "
            f"commands topic: {ntfy_cmd_topic or '(unset)'}"
        )
    else:
        logger.warning(
            "NTFY_TOPIC not set — ntfy alerts disabled; set it on Render to enable push notifications"
        )

    # Start ntfy command listener FIRST (independent of Telegram polling / 409 conflicts)
    start_ntfy_command_listener(telegram_bot)

    # Telegram polling for /commands — only one instance may poll (409 if duplicated)
    if is_telegram_polling_enabled():
        threading.Thread(target=telegram_bot.run, daemon=True).start()
    else:
        logger.info("Skipping Telegram polling thread (TELEGRAM_POLLING=false)")

    # Run the CEXAlertProcess in a daemon thread
    threading.Thread(
        target=CEXAlertProcess(telegram_bot=telegram_bot).run, daemon=True
    ).start()

    # Run the ORB alert process (LuxAlgo opening range — Binance klines, no taapi.io)
    threading.Thread(
        target=ORBAlertProcess(telegram_bot=telegram_bot).run, daemon=True
    ).start()

    if taapiio_process:
        # Run the Taapi.io process in a daemon thread
        threading.Thread(target=taapiio_process.run, daemon=True).start()

        # Run the TechnicalAlertProcess in a daemon thread
        threading.Thread(
            target=TechnicalAlertProcess(telegram_bot=telegram_bot).run, daemon=True
        ).start()

    # Keep the main thread alive to listen to interrupt
    logger.info("Bot started - use Ctrl+C to stop the bot.")
    while True:
        try:
            sleep(0.5)
        except KeyboardInterrupt:
            logger.info("Bot stopped")
            exit(1)
