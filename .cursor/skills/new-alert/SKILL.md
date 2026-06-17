---
name: new-alert
description: >-
  Documents Telegram Crypto Alerts /new_alert command formats, supported
  timeframes (1m–1w), and exact copy-paste examples per indicator. Use when
  the user asks how to create alerts, configure intervals, SuperTrend, RSI,
  MACD, or /new_alert syntax.
---

# /new_alert command reference

## Timeframes

Supported `TIMEFRAME` values (taapi.io): `1m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `12h`, `1d`, `1w`.

- Shortest: **1m**
- Longest: **1w** (there is **no 1-month** candle on taapi.io)

## Formats

**Simple (PRICE only):**
```
/new_alert PAIR/PAIR INDICATOR COMPARISON TARGET [COOLDOWN]
```

**Technical (MA, RSI, MACD, BBANDS, SUPERTREND, etc.):**
```
/new_alert PAIR INDICATOR TIMEFRAME PARAMS OUTPUT_VALUE COMPARISON TARGET [COOLDOWN]
```

- `PARAMS`: `default` or `period=14,multiplier=3`
- `COOLDOWN`: optional, e.g. `30s`, `5m`, `1h`
- Technical comparisons: `ABOVE`, `BELOW`, `EQUALS` (EQUALS for string outputs like SuperTrend `long`/`short`)

## Exact examples (BTC/USDT, 1h)

```
/new_alert BTC/USDT PRICE ABOVE 100000 1h
/new_alert BTC/USDT MA 1h default value ABOVE 50000 1h
/new_alert BTC/USDT EMA 1h default value ABOVE 50000 1h
/new_alert BTC/USDT SMA 1h default value ABOVE 50000 1h
/new_alert BTC/USDT RSI 1h default value ABOVE 70 1h
/new_alert BTC/USDT RSI 1h default value BELOW 30 1h
/new_alert BTC/USDT BBANDS 1h default valueUpperBand ABOVE 50000 1h
/new_alert BTC/USDT MACD 1h default valueMACD ABOVE 0 1h
/new_alert BTC/USDT SUPERTREND 1h default valueAdvice EQUALS long 1h
/new_alert BTC/USDT SUPERTREND 1h default valueAdvice EQUALS short 1h
```

Swap `1h` for any supported timeframe (e.g. `1m`, `1d`, `1w`).

## In Telegram

Users can run **`/new_alert help`** (or bare **`/new_alert`**) to receive the live guide with all bundled indicators from `indicator_format_reference.json`.

## Via ntfy (when Telegram is blocked)

Use a **separate command topic** so alert pushes are not mistaken for commands:

| Env var | Purpose |
|---------|---------|
| `NTFY_TOPIC` | Receive alert notifications |
| `NTFY_COMMAND_TOPIC` | Send commands (defaults to `{NTFY_TOPIC}-commands`) |

In the ntfy app, **publish** to the command topic (not the alert topic):

```
new_alert BTC/USDT SUPERTREND 1h default valueAdvice EQUALS long 1h
new_alert help
view_alerts
cancel_alert BTC/USDT 1
```

Replies appear on your **alert** topic (`NTFY_TOPIC`).
