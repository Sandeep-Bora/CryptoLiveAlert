import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from os import getenv
from time import sleep

from .alert_processes import CEXAlertProcess, TechnicalAlertProcess
from .telegram import TelegramBot
from .user_configuration import get_whitelist
from .utils import handle_env
from .indicators import TaapiioProcess
from .logger import logger
from .setup import do_setup


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal handler so platforms like Render detect an open port and uptime pingers keep the service awake."""

    def do_GET(self):
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

    taapiio_process = None
    if getenv("TAAPIIO_APIKEY"):
        # Create global Taapi.io process for the aggregator and telegram bot to sync calls
        taapiio_process = TaapiioProcess(taapiio_apikey=getenv("TAAPIIO_APIKEY"))

    # Create the Telegram bot to listen to commands and send messages
    telegram_bot = TelegramBot(
        bot_token=getenv("TELEGRAM_BOT_TOKEN"), taapiio_process=taapiio_process
    )

    # Run the TG bot in a daemon thread
    threading.Thread(target=telegram_bot.run, daemon=True).start()

    # Run the CEXAlertProcess in a daemon thread
    threading.Thread(
        target=CEXAlertProcess(telegram_bot=telegram_bot).run, daemon=True
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
