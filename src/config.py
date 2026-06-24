from os import mkdir, getcwd, getenv, listdir
from os.path import isdir, join, dirname, abspath, isfile, exists


"""Alert Handler Configuration"""
CEX_POLLING_PERIOD = 10  # Delay for the CEX alert handler to pull prices and check alert conditions (in seconds)
TECHNICAL_POLLING_PERIOD = 5  # Delay for the technical alert handler check technical alert conditions (in seconds)
OUTPUT_VALUE_PRECISION = 3
SIMPLE_INDICATORS = ["PRICE"]
SIMPLE_INDICATOR_COMPARISONS = ["ABOVE", "BELOW", "PCTCHG", "24HRCHG"]
# ABOVE/BELOW are numeric; EQUALS is for string outputs (e.g. SuperTrend valueAdvice: long/short)
TECHNICAL_INDICATOR_COMPARISONS = ["ABOVE", "BELOW", "EQUALS"]

"""Telegram Handler Configuration"""
MAX_ALERTS_PER_USER = (
    10  # Integer or None (Should be set in a static configuration file)
)

"""BINANCE DATA CONFIG"""
BINANCE_LOCATIONS = ["us", "global"]
BINANCE_PRICE_URL_GLOBAL = "https://api.binance.com/api/v3/ticker?symbol={}&windowSize={}"  # (e.x. BTCUSDT, 1d)
BINANCE_PRICE_URL_US = (
    "https://api.binance.us/api/v3/ticker?symbol={}&windowSize={}"  # (e.x. BTCUSDT, 1d
)
BINANCE_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "12h", "1d", "7d"]

"""SWAP DATA CONFIG"""
SWAP_POLLING_DELAY = 30  # Swap polling delay (in seconds) to handle rate limits.

"""DATABASE PREFERENCES & PATHS"""
USE_MONGO_DB = False
WHITELIST_ROOT = join(dirname(abspath(__file__)), "whitelist")
RESOURCES_ROOT = join(dirname(abspath(__file__)), "resources")
TA_DB_PATH = join(
    dirname(abspath(__file__)), "resources/indicator_format_reference.json"
)
AGG_DATA_LOCATION = join(dirname(abspath(__file__)), "temp/ta_aggregate.json")

"""NTFY PUSH NOTIFICATIONS"""
NTFY_DEFAULT_SERVER = "https://ntfy.sh"

"""TAAPI.IO"""
# Full interval list (paid plans / taapi.io docs)
TAAPI_INTERVALS_ALL = [
    "1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w",
]
# Free tier ("Limited Binance data") — verified against taapi.io free API keys
TAAPI_INTERVALS_FREE = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
INTERVALS = TAAPI_INTERVALS_ALL  # backward compatibility


def get_taapi_intervals() -> list[str]:
    """Return timeframes allowed for the configured TAAPIIO_TIER."""
    tier = (getenv("TAAPIIO_TIER") or "free").lower()
    if tier == "free":
        return list(TAAPI_INTERVALS_FREE)
    return list(TAAPI_INTERVALS_ALL)


def interval_tier_hint(interval: str) -> str:
    """Explain why an interval may fail on the current taapi.io plan."""
    if interval in TAAPI_INTERVALS_FREE:
        return ""
    if interval in TAAPI_INTERVALS_ALL:
        tier = (getenv("TAAPIIO_TIER") or "free").lower()
        if tier == "free":
            return (
                f"\n\n'{interval}' is not available on the taapi.io **free** plan "
                f"(limited Binance intervals).\n"
                f"Free plan intervals: {', '.join(TAAPI_INTERVALS_FREE)}\n"
                f"Use 15m or 1h instead of 30m, or upgrade at https://taapi.io/#pricing"
            )
    return ""
DEFAULT_EXCHANGE = "binance"
BULK_ENDPOINT = "https://api.taapi.io/bulk"
SUBSCRIPTION_TIERS = {
    "free": (1, 20),
    "basic": (5, 15),
    "pro": (30, 15),
    "expert": (75, 15),
}  # (requests, per period in seconds)
REQUEST_BUFFER = 0.05  # buffer percentage for preventing rate limit errors (e.x. 0.05 = 5% of request period, so period * 1.05)

# TA_AGGREGATE_PPERIOD = 30  # TA Aggregate polling period, to poll technical indicators
