import re
from os import getenv

from .config import *
from .user_configuration import (
    LocalUserConfiguration,
    MongoDBUserConfiguration,
    get_whitelist,
)
from .utils import parse_trigger_cooldown, build_new_alert_guide, strip_html_for_plain
from .logger import logger

BaseConfig = LocalUserConfiguration if not USE_MONGO_DB else MongoDBUserConfiguration


def normalize_command_text(text: str) -> str:
    """Turn ntfy/Telegram input into /command arg1 arg2 ... form."""
    text = text.strip()
    if not text:
        return text
    if not text.startswith("/"):
        text = "/" + text
    return text


class AlertCommandHandler:
    """Shared alert command logic for Telegram, ntfy, and other channels."""

    def __init__(self, bot):
        self.bot = bot

    def process(self, user_id: str, command_text: str) -> str:
        user_id = str(user_id)
        if user_id not in get_whitelist():
            return f"User {user_id} is not whitelisted."

        text = normalize_command_text(command_text)
        if not text:
            return "Empty command."

        cmd = text.split()[0][1:].lower().replace("-", "_")
        if cmd in ("new_alert", "newalert"):
            return self.process_new_alert(user_id, text)
        if cmd in ("cancel_alert", "cancelalert"):
            return self.process_cancel_alert(user_id, text)
        if cmd in ("view_alerts", "viewalerts"):
            return self.process_view_alerts(user_id, text)

        return (
            "Unknown command.\n\n"
            "Supported:\n"
            "  new_alert help\n"
            "  new_alert BTC/USDT SUPERTREND 1h default valueAdvice EQUALS long 1h\n"
            "  cancel_alert BTC/USDT 1\n"
            "  view_alerts\n"
            "  view_alerts BTC/USDT"
        )

    def process_new_alert(self, user_id: str, message_text: str) -> str:
        simple_indicators = ["PRICE", "24HRCHG"]
        technical_indicators = list(self.bot.indicators_db.keys())
        try:
            msg = self.bot.split_message(message_text)
            if len(msg) == 0 or msg[0].lower() in ("help", "examples", "?"):
                return strip_html_for_plain(
                    "\n\n".join(build_new_alert_guide(self.bot.indicators_db))
                )

            indicator = msg[1].upper()
            if indicator in simple_indicators:
                pair, indicator, comparison, target = msg[0], msg[1], msg[2], msg[3]
                indicator_instance = self.bot.parse_simple_indicator_message(message_text)
            elif indicator in technical_indicators:
                if self.bot.taapiio_cli is None:
                    return (
                        "Technical alerts unavailable. Set TAAPIIO_APIKEY on the server."
                    )
                if len(msg) < 7:
                    raise ValueError(
                        "Technical alerts require 7 arguments before optional cooldown: "
                        "PAIR INDICATOR TIMEFRAME PARAMS OUTPUT_VALUE COMPARISON TARGET"
                    )
                pair = msg[0]
                indicator = msg[1]
                output_value = msg[4]
                indicator_instance = self.bot.parse_technical_indicator_message(
                    message_text
                )
                if indicator_instance is None:
                    return (
                        "Could not match parameters to a valid technical indicator.\n"
                        "Send: new_alert help"
                    )
                try:
                    r = self.bot.get_technical_indicator(indicator_instance)
                    if output_value not in r.keys():
                        return f"Invalid output value - Options: {list(r.keys())}"
                except Exception as exc:
                    return (
                        f"taapi.io error (parameters may be invalid):\n{exc}"
                    )
            else:
                return (
                    f"Invalid indicator. Valid: {simple_indicators + technical_indicators}"
                )

        except AssertionError as exc:
            return f"Assertion Error:\n{exc}"
        except Exception:
            return (
                "Invalid message formatting.\n\n"
                "Simple:\n"
                "  new_alert PAIR/PAIR INDICATOR COMPARISON TARGET [COOLDOWN]\n\n"
                "Technical:\n"
                "  new_alert PAIR INDICATOR TIMEFRAME PARAMS OUTPUT_VALUE COMPARISON TARGET [COOLDOWN]\n\n"
                "Send: new_alert help"
            )

        try:
            configuration = BaseConfig(user_id)
            alerts_db = configuration.load_alerts()

            if MAX_ALERTS_PER_USER is not None:
                if sum(len(alerts) for alerts in alerts_db.values()) >= MAX_ALERTS_PER_USER:
                    raise OverflowError(
                        f"Maximum active alerts reached ({MAX_ALERTS_PER_USER})"
                    )

            if indicator_instance.type == "s":
                comparison = msg[2].upper()
                target = (
                    float(msg[3].strip())
                    if comparison not in ["PCTCHG", "24HRCHG"]
                    else float(msg[3].strip()) / 100
                )
                entry_price = self.bot.get_latest_binance_price(pair)
                trigger = parse_trigger_cooldown(msg[4] if len(msg) > 4 else None)
                alert = {
                    "type": indicator_instance.type,
                    "indicator": indicator_instance.indicator.upper(),
                    "comparison": comparison,
                    "entry": entry_price,
                    "target": target,
                    "params": indicator_instance.params,
                    "trigger": trigger,
                }
            else:
                output_value = msg[4]
                comparison = msg[5].upper()
                if comparison not in TECHNICAL_INDICATOR_COMPARISONS:
                    raise ValueError(
                        f"{comparison} is invalid. Options: {TECHNICAL_INDICATOR_COMPARISONS}"
                    )
                try:
                    target = float(msg[6])
                except ValueError:
                    if comparison != "EQUALS":
                        raise ValueError(
                            f"'{msg[6]}' is not a valid numeric target for {comparison}."
                        )
                    target = msg[6]
                trigger = parse_trigger_cooldown(msg[7] if len(msg) > 7 else None)
                alert = {
                    "type": indicator_instance.type,
                    "indicator": indicator_instance.indicator.upper(),
                    "comparison": comparison,
                    "interval": indicator_instance.interval,
                    "params": indicator_instance.params,
                    "output_value": output_value,
                    "target": target,
                    "trigger": trigger,
                }

            if pair in alerts_db:
                alerts_db[pair].append(alert)
            else:
                alerts_db[pair] = [alert]
            configuration.update_alerts(alerts_db)
            return "Successfully activated new alert!"
        except Exception as exc:
            return f"An error occurred:\n{exc}"

    def process_cancel_alert(self, user_id: str, message_text: str) -> str:
        try:
            pair, alert_index = self.bot.split_message(message_text)
            pair = pair.upper()
            alert_index = int(alert_index)
        except Exception:
            return "Invalid format. Use: cancel_alert TOKEN1/TOKEN2 alert_index"

        try:
            configuration = BaseConfig(user_id)
            alerts_db = configuration.load_alerts()
            rm_alert = alerts_db[pair].pop(alert_index - 1)
            all_rm = False
            if len(alerts_db[pair]) == 0:
                alerts_db.pop(pair)
                all_rm = True
            configuration.update_alerts(alerts_db)
            suffix = f" (All alerts canceled for {pair})" if all_rm else ""
            return f"Successfully canceled {pair} alert:{suffix}\n{rm_alert}"
        except Exception as exc:
            return f"Could not cancel alert:\n{exc}"

    def process_view_alerts(self, user_id: str, message_text: str) -> str:
        try:
            alerts_pair = self.bot.split_message(message_text)[0].upper()
        except IndexError:
            alerts_pair = "ALL"

        configuration = BaseConfig(user_id)
        alerts_db = configuration.load_alerts()
        output = ""
        for ticker in alerts_db:
            if ticker == alerts_pair or alerts_pair == "ALL":
                output += f"{ticker}:\n"
                for index, alert in enumerate(alerts_db[ticker]):
                    line = f"  {index + 1} - {alert['indicator']} "
                    if "output_value" in alert:
                        line += f"({alert['output_value']}) "
                    if "interval" in alert:
                        line += f"{alert['interval']} "
                    line += f"{alert['comparison']} "
                    if alert["comparison"] in ["PCTCHG", "24HRCHG"]:
                        line += f"{alert['target'] * 100}% FROM {alert['entry']}"
                    else:
                        line += str(alert["target"])
                    if alert.get("params"):
                        line += f" params: {alert['params']}"
                    output += line + "\n"
                output += "\n"
        return output if output else "Found 0 matching alerts."
