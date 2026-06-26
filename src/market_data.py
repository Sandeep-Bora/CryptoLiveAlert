"""Binance market data helpers (klines for ORB)."""

from datetime import datetime, timezone
from os import getenv

import requests

from .config import BINANCE_KLINES_URL_GLOBAL, BINANCE_KLINES_URL_US
from .logger import logger


def get_binance_klines_url() -> str:
    location = (getenv("LOCATION") or "global").lower()
    if location == "us":
        return BINANCE_KLINES_URL_US
    return BINANCE_KLINES_URL_GLOBAL


def fetch_klines(
    pair: str,
    interval: str = "1m",
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 100,
) -> list[list]:
    """
    Fetch OHLCV klines from Binance.

    Returns raw kline rows:
    [open_time, open, high, low, close, volume, close_time, ...]
    """
    symbol = pair.replace("/", "").upper()
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms

    url = get_binance_klines_url()
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning(f"Binance klines fetch failed for {pair} {interval}: {exc}")
        raise


def klines_to_ohlc(klines: list[list]) -> list[dict]:
    """Parse Binance kline rows into OHLC dicts."""
    result = []
    for row in klines:
        result.append(
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
            }
        )
    return result


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
