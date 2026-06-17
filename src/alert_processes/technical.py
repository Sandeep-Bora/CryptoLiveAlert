import time
from datetime import datetime
import os
from functools import wraps

from .base import BaseAlertProcess
from ..user_configuration import (
    LocalUserConfiguration,
    MongoDBUserConfiguration,
    get_whitelist,
)
from ..logger import logger
from ..config import *
from ..indicators import TADatabaseClient, TAAggregateClient
from ..telegram import TelegramBot


class TechnicalAlertProcess(BaseAlertProcess):
    def __init__(self, telegram_bot: TelegramBot):
        super().__init__(telegram_bot)
        self.polling = False  # Temporary variable to manage alerts
        self.ta_db = TADatabaseClient().fetch_ref()
        self.ta_agg_cli = TAAggregateClient()

    def poll_user_alerts(self, tg_user_id: str) -> None:
        """
        1. Load the user's configuration
        2. poll all alerts and create posts
        3. Remove alert conditions
        4. Send alerts if found

        :param tg_user_id: The Telegram user ID from the database
        """
        configuration = (
            LocalUserConfiguration(tg_user_id)
            if not USE_MONGO_DB
            else MongoDBUserConfiguration(tg_user_id)
        )
        alerts_database = configuration.load_alerts()
        config = configuration.load_config()

        do_update = False  # If any changes are made, update the database
        post_queue = []
        for pair in alerts_database.copy().keys():

            remove_queue = []
            for alert in alerts_database[pair]:
                if alert["type"] == "t":
                    condition, value, post_string, matched = self.get_technical_indicator(
                        pair, alert
                    )

                    # EQUALS on string outputs (e.g. SuperTrend long/short): alert on flip only
                    if alert.get("comparison") == "EQUALS" and value not in (None, "", 0):
                        flip, previous = self._process_equals_flip(alert, value)
                        do_update = True
                        if flip:
                            condition = True
                            current = str(value).strip().lower()
                            if (
                                alert.get("indicator", "").upper() == "SUPERTREND"
                                and alert.get("output_value") == "valueAdvice"
                                and matched
                            ):
                                post_string = self._format_supertrend_flip(
                                    pair, alert, current, previous, matched
                                )
                            elif previous and post_string:
                                post_string = (
                                    post_string.rstrip()
                                    + f"\nTREND FLIP: {previous} → {current}\n"
                                )
                        else:
                            condition = False

                    if condition:  # If there is a technical alert condition satisfied
                        cooldown = alert.get("trigger", {}).get("cooldown_seconds")
                        last_trigger = alert.get("trigger", {}).get("last_triggered", 0)
                        # Flip alerts (EQUALS) fire once per change; cooldown optional debounce only
                        cooldown_ok = (
                            alert.get("comparison") == "EQUALS"
                            or int(time.time()) > last_trigger + (cooldown or 0)
                        )
                        if cooldown_ok:
                            post_queue.append((post_string, pair))
                            if cooldown:
                                alert["trigger"] = {
                                    "cooldown_seconds": cooldown,
                                    "last_triggered": int(time.time()),
                                }
                            do_update = True

                        if (
                            not alert.get("trigger", {}).get("cooldown_seconds")
                            and alert.get("comparison") != "EQUALS"
                        ):
                            # One-time numeric alerts only; EQUALS flip alerts stay active
                            remove_queue.append(alert)
                            do_update = True

            for item in remove_queue:
                alerts_database[pair].remove(item)
                if len(alerts_database[pair]) == 0:
                    alerts_database.pop(pair)

        if do_update:
            configuration.update_alerts(alerts_database)

        if len(post_queue) > 0:
            self.polling = False
            for post, pair in post_queue:
                logger.info(post)
                status = self.tg_alert(
                    post=post,
                    channel_ids=config["channels"],
                    ntfy_topics=configuration.get_ntfy_topics(),
                    pair=pair,
                )
                if len(status[1]) > 0:
                    logger.warn(
                        f"Failed to send Telegram alert ({post}) to the following IDs: {status[1]}"
                    )

        if not self.polling:
            self.polling = True
            logger.info(f"Bot polling for next alert...")

    def poll_all_alerts(self) -> None:
        """
        1. Aggregate pairs across all users
        2. Fetch all pair prices
        3. Log individual user failures
        """
        for user in get_whitelist():
            self.poll_user_alerts(tg_user_id=user)

    def _process_equals_flip(self, alert: dict, value) -> tuple[bool, str | None]:
        """
        Edge-trigger for EQUALS alerts (e.g. SuperTrend valueAdvice).
        Fires only when the value changes TO the alert target (not every poll while unchanged).
        """
        current = str(value).strip().lower()
        target = str(alert["target"]).strip().lower()
        last_seen = alert.get("last_seen_value")

        if last_seen is None:
            alert["last_seen_value"] = current
            return False, None

        if last_seen == current:
            return False, None

        previous = last_seen
        alert["last_seen_value"] = current
        if current != target:
            return False, None

        return True, previous

    def _format_price(self, price: float) -> str:
        rounded = round(price, OUTPUT_VALUE_PRECISION)
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:.{OUTPUT_VALUE_PRECISION}f}"

    def _format_supertrend_flip(
        self,
        pair: str,
        alert: dict,
        trend: str,
        previous: str | None,
        matched_indicator: dict,
    ) -> str:
        """Build flip alert with spot entry price and SuperTrend line as hold/stop level."""
        st_line = matched_indicator.get("values", {}).get("value")
        try:
            spot = self.telegram_bot.get_latest_binance_price(pair)
        except Exception as exc:
            logger.warning(f"Could not fetch spot price for {pair}: {exc}")
            spot = None

        flip_line = (
            f"TREND FLIP: {previous} → {trend}" if previous else f"TREND: {trend.upper()}"
        )
        lines = [
            f"{pair} SuperTrend ({alert['interval']}) — {flip_line}",
        ]
        if spot is not None and st_line is not None:
            lines.append(
                f"{trend} from {self._format_price(spot)} and hold till {self._format_price(st_line)}"
            )
        elif spot is not None:
            lines.append(f"{trend} from {self._format_price(spot)}")
        elif st_line is not None:
            lines.append(f"{trend} — hold till {self._format_price(st_line)}")

        return "\n".join(lines) + "\n"

    def get_technical_indicator(
        self, pair: str, alert: dict
    ) -> tuple[bool, float | str, str, dict | None]:
        """
        Accounts for all of the implemented taapi.io indicators.
        Get the available indicators using the telegram command.
        References the alert against the temp/ta_aggregate.json file to check for satisfaction.

        :param pair: The crypto pair
        :param alert: An alert data dictionary as returned by src.io_handler.UserConfiguration.load_alerts()

        :returns: Tuple:
                  (Boolean) True if the indicator is satisfied, False if not
                  (Float) The current value of the indicator
                  (String) The formatted string to send with alerts
        """
        null_output = False, 0, "", None

        aggregate = self.ta_agg_cli.load_agg()
        if not aggregate:
            logger.warn(
                "Attempted to load the aggregate in get_technical_indicator() but it was empty"
            )
            return null_output

        pair_data = aggregate.get(pair)
        if not pair_data:
            return null_output

        interval_data = pair_data.get(alert["interval"])
        if not interval_data:
            return null_output

        # Match the alert to its corresponding reference in the aggregate and check the value:
        matched_indicator = None
        formatted_alert = self.ta_agg_cli.format_alert_for_match(alert)

        # Attempt to find an existing indicator match for the alert
        for indicator in interval_data:
            try:
                if all(indicator[k] == v for k, v in formatted_alert.items()):
                    matched_indicator = indicator
                    break
            except KeyError:
                continue

        if matched_indicator is None:
            logger.warn(
                f"No aggregate match yet for {pair} {alert['indicator']} "
                f"{alert['interval']} — waiting for next taapi.io update"
            )
            return null_output

        # If these tests pass, this is the correct indicator because the symbol, interval, and params pass
        value = matched_indicator["values"][alert["output_value"]]
        if value is None:
            return null_output

        satisfied = False
        if alert["comparison"] == "EQUALS":
            # String/value equality for non-numeric outputs (e.g. SuperTrend valueAdvice: long/short)
            if str(value).strip().lower() == str(alert["target"]).strip().lower():
                satisfied = True
        elif alert["comparison"] == "ABOVE":
            if value > alert["target"]:
                satisfied = True
        elif alert["comparison"] == "BELOW":
            if value < alert["target"]:
                satisfied = True
        else:
            raise ValueError(
                f"'{alert['comparison']}' IS AN INVALID COMPARISON TYPE (ABOVE, BELOW, or EQUALS)"
            )

        # Return
        if satisfied:
            indicator_str = f"{self.ta_db[alert['indicator'].upper()]['name']} ({alert['indicator'].upper()})"
            params_str = ", ".join(
                [f"{param.upper()}={v}" for param, v in alert["params"].items()]
            )
            # Numeric outputs get rounded; string outputs (e.g. long/short) are shown as-is
            try:
                value_display = f"{value:.{OUTPUT_VALUE_PRECISION}f}"
            except (ValueError, TypeError):
                value_display = str(value)
            post_str = (
                f"{pair} {indicator_str} {alert['interval']} {params_str} {alert['comparison']} {alert['target']}"
                f" AT {value_display}\n"
            )
            return True, value, post_str, matched_indicator
        else:
            if alert["comparison"] == "EQUALS":
                return False, value, "", matched_indicator
            return null_output

    def tg_alert(
        self,
        post: str,
        channel_ids: list[str],
        ntfy_topics: list[str] = None,
        pair: str = None,
    ) -> tuple:
        from ..notifications import dispatch_alerts

        return dispatch_alerts(
            telegram_bot=self.telegram_bot,
            post=post,
            channel_ids=channel_ids,
            ntfy_topics=ntfy_topics or [],
            pair=pair,
            header="TECHNICAL ALERT",
        )

    def run(self):
        logger.warn(f"{type(self).__name__} started at {datetime.utcnow()} UTC+0")
        while True:
            try:
                self.poll_all_alerts()
                time.sleep(TECHNICAL_POLLING_PERIOD)
            except NotImplementedError as exc:
                logger.critical(exc_info=exc)
                break
            except KeyboardInterrupt:
                logger.critical("KeyboardInterrupt detected. Exiting...")
                exit(0)
            except Exception as exc:
                logger.critical(
                    "An error has occurred in the technical alerts process. Trying again in 15 seconds...",
                    exc_info=exc,
                )
                time.sleep(15)
