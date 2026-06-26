"""
Opening Range Breakout logic (LuxAlgo ORB-style).

Reference: https://www.luxalgo.com/library/indicator/opening-range-with-breakouts-targets/

Computes daily opening range high/low from Binance 1m candles at session open,
detects breakouts, and projects price targets from range width.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from os import getenv

from .config import (
    ORB_RANGE_MINUTES_DEFAULT,
    ORB_SESSION_START_DEFAULT,
    ORB_TARGET_PCT_DEFAULT,
    ORB_TIMEZONE_OFFSET_DEFAULT,
    OUTPUT_VALUE_PRECISION,
)
from .market_data import fetch_klines, klines_to_ohlc


@dataclass
class ORBParams:
    range_minutes: int = ORB_RANGE_MINUTES_DEFAULT
    timezone_offset: int = ORB_TIMEZONE_OFFSET_DEFAULT
    session_start: str = ORB_SESSION_START_DEFAULT
    target_pct: float = ORB_TARGET_PCT_DEFAULT
    kline_interval: str = "1m"

    @classmethod
    def from_dict(cls, params: dict) -> "ORBParams":
        return cls(
            range_minutes=int(params.get("range_minutes", ORB_RANGE_MINUTES_DEFAULT)),
            timezone_offset=int(
                params.get("timezone_offset", ORB_TIMEZONE_OFFSET_DEFAULT)
            ),
            session_start=str(params.get("session_start", ORB_SESSION_START_DEFAULT)),
            target_pct=float(params.get("target_pct", ORB_TARGET_PCT_DEFAULT)),
            kline_interval=str(params.get("kline_interval", "1m")),
        )


@dataclass
class ORBRange:
    session_key: str
    or_high: float
    or_low: float
    or_mid: float
    range_width: float
    target_above: float
    target_below: float
    phase: str  # forming | monitor


def parse_orb_param_string(args: str | None) -> dict:
    """Parse ORB params from new_alert string or 'default'."""
    result = {
        "range_minutes": int(getenv("ORB_RANGE_MINUTES") or ORB_RANGE_MINUTES_DEFAULT),
        "timezone_offset": int(
            getenv("ORB_TIMEZONE_OFFSET") or ORB_TIMEZONE_OFFSET_DEFAULT
        ),
        "session_start": getenv("ORB_SESSION_START") or ORB_SESSION_START_DEFAULT,
        "target_pct": float(getenv("ORB_TARGET_PCT") or ORB_TARGET_PCT_DEFAULT),
        "kline_interval": "1m",
    }
    if not args or args.strip().lower() == "default":
        return result

    for part in args.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if key in ("range", "range_minutes", "minutes", "timeframe"):
            result["range_minutes"] = int(val.rstrip("m"))
        elif key in ("timezone", "tz", "timezone_offset", "utc"):
            val = val.upper().replace("UTC", "").strip()
            if val.startswith("+"):
                val = val[1:]
            result["timezone_offset"] = int(val)
        elif key in ("session", "session_start", "start", "open"):
            result["session_start"] = val
        elif key in ("target", "target_pct", "targets"):
            result["target_pct"] = float(val.rstrip("%"))
    return result


def _session_tz(offset: int) -> timezone:
    return timezone(timedelta(hours=offset))


def _parse_hm(time_str: str) -> tuple[int, int]:
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return hour, minute


def _format_price(price: float) -> str:
    rounded = round(price, OUTPUT_VALUE_PRECISION)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{OUTPUT_VALUE_PRECISION}f}"


def get_orb_phase(now_utc: datetime, params: ORBParams) -> tuple[str, date, str]:
    """
    Return (phase, session_date, session_key).

    phase: forming | waiting | monitor
    """
    tz = _session_tz(params.timezone_offset)
    local = now_utc.astimezone(tz)
    sh, sm = _parse_hm(params.session_start)
    session_start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    range_end = session_start + timedelta(minutes=params.range_minutes)

    if local < session_start:
        # Before today's open — still monitoring previous session's range
        session_date = (local - timedelta(days=1)).date()
        return "monitor", session_date, str(session_date)

    if local < range_end:
        return "forming", local.date(), str(local.date())

    return "monitor", local.date(), str(local.date())


def compute_opening_range(
    pair: str, params: ORBParams, session_date: date
) -> ORBRange | None:
    """Build OR high/low from Binance klines for the session opening window."""
    tz = _session_tz(params.timezone_offset)
    sh, sm = _parse_hm(params.session_start)
    session_start_local = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        sh,
        sm,
        tzinfo=tz,
    )
    range_end_local = session_start_local + timedelta(minutes=params.range_minutes)
    start_ms = int(session_start_local.timestamp() * 1000)
    end_ms = int(range_end_local.timestamp() * 1000) - 1

    klines = fetch_klines(
        pair,
        interval=params.kline_interval,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=params.range_minutes + 5,
    )
    if not klines:
        return None

    ohlc = klines_to_ohlc(klines)
    or_high = max(c["high"] for c in ohlc)
    or_low = min(c["low"] for c in ohlc)
    or_mid = (or_high + or_low) / 2
    width = or_high - or_low
    pct = params.target_pct / 100.0
    target_above = or_high + width * pct
    target_below = or_low - width * pct

    return ORBRange(
        session_key=str(session_date),
        or_high=or_high,
        or_low=or_low,
        or_mid=or_mid,
        range_width=width,
        target_above=target_above,
        target_below=target_below,
        phase="monitor",
    )


def check_orb_breakout(
    price: float,
    direction: str,
    orb: ORBRange,
    state: dict,
    params: ORBParams,
) -> tuple[bool, str, dict]:
    """
    Check if price broke the opening range. Fires once per direction per session.

    direction: ABOVE | BELOW | BOTH
    Returns (triggered, message, updated_state)
    """
    direction = direction.upper()
    state = dict(state or {})
    session_key = orb.session_key

    if state.get("session_key") != session_key:
        state = {
            "session_key": session_key,
            "breakout_above_fired": False,
            "breakout_below_fired": False,
        }

    messages = []
    triggered = False
    pct = int(params.target_pct)

    if direction in ("ABOVE", "BOTH") and price > orb.or_high and not state.get(
        "breakout_above_fired"
    ):
        state["breakout_above_fired"] = True
        triggered = True
        messages.append(
            f"BREAKOUT ABOVE opening range\n"
            f"OR High: {_format_price(orb.or_high)} | OR Low: {_format_price(orb.or_low)} | "
            f"Mid: {_format_price(orb.or_mid)}\n"
            f"Breakout @ {_format_price(price)} → Target 1 ({pct}% range): "
            f"{_format_price(orb.target_above)}"
        )

    if direction in ("BELOW", "BOTH") and price < orb.or_low and not state.get(
        "breakout_below_fired"
    ):
        state["breakout_below_fired"] = True
        triggered = True
        messages.append(
            f"BREAKOUT BELOW opening range\n"
            f"OR High: {_format_price(orb.or_high)} | OR Low: {_format_price(orb.or_low)} | "
            f"Mid: {_format_price(orb.or_mid)}\n"
            f"Breakout @ {_format_price(price)} → Target 1 ({pct}% range): "
            f"{_format_price(orb.target_below)}"
        )

    post = "\n\n".join(messages)
    return triggered, post, state


def format_orb_alert_header(pair: str, params: ORBParams) -> str:
    tz_label = f"UTC{params.timezone_offset:+d}"
    return (
        f"LuxAlgo ORB — {pair}\n"
        f"Range: {params.range_minutes}m | Session: {params.session_start} {tz_label}\n"
    )
