from os import getenv

import requests

from .logger import logger
from .utils import strip_html


def send_ntfy(
    message: str,
    topic: str,
    title: str = "Crypto Alert",
    server: str = None,
    priority: str = "high",
    tags: str = "chart,moneybag",
    auth_token: str = None,
) -> bool:
    """Publish a push notification to an ntfy topic."""
    server = (server or getenv("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    topic = topic.strip().lstrip("/")
    if not topic:
        return False

    token = auth_token or getenv("NTFY_TOKEN") or getenv("NTFY_COMMAND_TOKEN")
    headers = {
        "Title": title[:250],
        "Priority": priority,
        "Tags": tags,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.post(
            f"{server}/{topic}",
            data=strip_html(message).encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if not response.ok:
            logger.warning(
                f"ntfy push failed for topic '{topic}' ({response.status_code}): "
                f"{response.text[:200]}"
            )
        return response.ok
    except Exception as exc:
        logger.warning(f"ntfy push error for topic '{topic}': {exc}")
        return False


def dispatch_alerts(
    telegram_bot,
    post: str,
    channel_ids: list[str],
    ntfy_topics: list[str],
    pair: str = None,
    header: str = "ALERT",
) -> tuple[list, list]:
    """
    Send an alert to all configured Telegram channels and ntfy topics.

    Returns ([successful telegram ids], [failed telegram ids]) for backward compatibility.
    """
    full_post = f"🔔 <b>{header}:</b> 🔔\n\n" + post
    if pair:
        pair_fmt = pair.replace("/", "_")
        full_post += (
            f"\n\n<a href='https://www.binance.com/en/trade/{pair_fmt}?type=spot'>"
            f"<b>View {pair} Chart</b></a>"
        )

    tg_ok, tg_fail = [], []
    for chat_id in channel_ids:
        try:
            telegram_bot.send_message(
                chat_id=chat_id,
                text=full_post,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            tg_ok.append(chat_id)
        except Exception:
            tg_fail.append(chat_id)

    plain = strip_html(full_post)
    title = f"{header}: {pair}" if pair else header
    ntfy_ok, ntfy_fail = [], []
    for topic in dict.fromkeys(t for t in ntfy_topics if t):
        if send_ntfy(plain, topic, title=title):
            ntfy_ok.append(topic)
        else:
            ntfy_fail.append(topic)

    if ntfy_ok:
        logger.info(f"ntfy alert sent to: {', '.join(ntfy_ok)}")
    if ntfy_fail:
        logger.warning(f"Failed to send ntfy alert to: {', '.join(ntfy_fail)}")

    return tg_ok, tg_fail
