import time
from datetime import datetime, timezone

from .base import BaseAlertProcess
from ..user_configuration import (
    LocalUserConfiguration,
    MongoDBUserConfiguration,
    get_whitelist,
)
from ..logger import logger
from ..config import ORB_POLLING_PERIOD, ORB_DIRECTIONS, USE_MONGO_DB
from ..orb_logic import (
    ORBParams,
    compute_opening_range,
    check_orb_breakout,
    format_orb_alert_header,
    get_orb_phase,
)
from ..telegram import TelegramBot


class ORBAlertProcess(BaseAlertProcess):
    """LuxAlgo-style Opening Range Breakout alerts using Binance klines."""

    def __init__(self, telegram_bot: TelegramBot):
        super().__init__(telegram_bot)
        self._orb_cache: dict[tuple, object] = {}

    def poll_user_alerts(self, tg_user_id: str) -> None:
        configuration = (
            LocalUserConfiguration(tg_user_id)
            if not USE_MONGO_DB
            else MongoDBUserConfiguration(tg_user_id)
        )
        alerts_database = configuration.load_alerts()
        config = configuration.load_config()
        do_update = False
        post_queue = []
        now_utc = datetime.now(timezone.utc)

        for pair in list(alerts_database.keys()):
            for alert in alerts_database[pair]:
                if alert.get("type") != "o" or alert.get("indicator") != "ORB":
                    continue

                params = ORBParams.from_dict(alert.get("params", {}))
                direction = alert.get("comparison", "BOTH").upper()
                if direction not in ORB_DIRECTIONS:
                    continue

                phase, session_date, session_key = get_orb_phase(now_utc, params)

                if phase == "forming":
                    continue

                cache_key = (pair, session_key, params.range_minutes, params.session_start)
                orb = self._orb_cache.get(cache_key)
                if orb is None or getattr(orb, "session_key", None) != session_key:
                    try:
                        orb = compute_opening_range(pair, params, session_date)
                        if orb:
                            self._orb_cache[cache_key] = orb
                            logger.info(
                                f"ORB range set for {pair} session {session_key}: "
                                f"H={orb.or_high} L={orb.or_low}"
                            )
                    except Exception as exc:
                        logger.warning(f"ORB range compute failed for {pair}: {exc}")
                        continue

                if orb is None:
                    continue

                try:
                    price = self.telegram_bot.get_latest_binance_price(pair)
                except Exception as exc:
                    logger.warning(f"ORB price fetch failed for {pair}: {exc}")
                    continue

                state = alert.get("state", {})
                triggered, body, new_state = check_orb_breakout(
                    price, direction, orb, state, params
                )
                if new_state != state:
                    alert["state"] = new_state
                    do_update = True

                if triggered and body:
                    header = format_orb_alert_header(pair, params)
                    post_queue.append((header + body, pair))

        if do_update:
            configuration.update_alerts(alerts_database)

        if post_queue:
            for post, pair in post_queue:
                logger.info(post)
                status = self.tg_alert(
                    post=post,
                    channel_ids=config["channels"],
                    ntfy_topics=configuration.get_ntfy_topics(),
                    pair=pair,
                )
                if status[1]:
                    logger.warning(f"ORB alert delivery failed for: {status[1]}")

    def poll_all_alerts(self) -> None:
        for user in get_whitelist():
            self.poll_user_alerts(user)

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
            header="ORB ALERT",
        )

    def run(self):
        logger.warn(f"{type(self).__name__} started at {datetime.utcnow()} UTC+0")
        while True:
            try:
                self.poll_all_alerts()
                time.sleep(ORB_POLLING_PERIOD)
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logger.critical(
                    "ORB alert process error — retrying in 15s", exc_info=exc
                )
                time.sleep(15)
