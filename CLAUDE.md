# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Midas AI is an async Python trading bot for US equities via Alpaca's paper/live APIs. It runs a mean-reversion scalping strategy (ATR-gated RSI + MACD + VWAP) on a 30-symbol watchlist, firing once per minute during NYSE hours.

## Running the bot

```bash
# One-time setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Required every session (or add to ~/.zshrc permanently)
export ALPACA_API_KEY="PKXXXXXXXXXXXXXX"
export ALPACA_API_SECRET="your-secret-key"

# Run in paper mode (default)
python3 main.py

# Run in live mode (real capital — use only after 30-session validation)
PAPER_TRADING=0 python3 main.py

# Watch live log output (in a second terminal)
tail -f ~/midas_ai/logs/bot.log
```

Verify setup without running the bot:
```bash
python3 main.py --check
```

# Run the live dashboard (in a second terminal)
streamlit run dashboard.py

## Backtesting

```bash
# Run with historical CSV data (see midas_backtest.py for expected format)
python3 -c "
from midas_backtest import Backtester
from midas_config import MidasConfig
import pandas as pd
df = pd.read_csv('your_data.csv', index_col='timestamp', parse_dates=True)
bt = Backtester(MidasConfig())
result = bt.run(df)
print(result.summary())
"
```

## Architecture

The project is structured as nine modules with a strict dependency hierarchy:

```
main.py  ←  entry point and tick orchestrator
  ├── midas_config.py   — MidasConfig dataclass (single source of truth)
  ├── midas_risk.py     — RiskManager (position sizing, kill switch, compounding)
  ├── midas_screener.py — SymbolScreener (bulk OHLCV fetch → ranked signals)
  │     └── midas_strategy.py — add_indicators(), generate_signal()
  └── midas_executor.py — PaperTradingEngine | AlpacaExecutor

dashboard.py     ←  standalone Streamlit dashboard (reads CSV + state.json)
midas_backtest.py ←  offline backtester (same strategy logic, historical CSV input)
test_risk.py     ←  pytest unit tests for midas_risk
```

**`midas_config.py`** — All tuneable constants live here as module-level values and as fields on `MidasConfig`. Every other module imports `MidasConfig` and reads from it; nothing hardcodes a value that belongs in config. Change the watchlist, risk params, or strategy thresholds here.

**`midas_risk.py`** — `RiskManager` is the final authority before any order is placed. It owns: position sizing (risk-based, 0.5% max per trade, 20% notional cap), the 3% daily-drawdown kill switch, compounding target tracking (`A = P(1+r)^n`), and state persistence to `~/midas_ai/state/state.json`.

**`midas_screener.py`** — `SymbolScreener.scan()` fetches both timeframes (1m and 5m) in two concurrent bulk API calls (one call per timeframe for all 30 symbols), computes indicators for each, calls `generate_signal()`, scores signals, and returns a ranked list. Short signals are gated by a per-session shortability cache from Alpaca's asset endpoint.

**`midas_strategy.py`** — Pure functions: `add_indicators()` enriches a DataFrame with ATR (Wilder), RSI (Wilder), MACD, and VWAP. `generate_signal()` evaluates one closed 1m candle. VWAP resets at 09:30 ET (not UTC midnight) — this is intentional and load-bearing for the strategy.

**`midas_executor.py`** — Two implementations behind the same interface: `PaperTradingEngine` (in-process simulation, applies slippage + SEC/FINRA fees) and `AlpacaExecutor` (async aiohttp REST client). `AlpacaExecutor.submit_bracket_order()` sends native Alpaca OTOCO bracket orders (entry + TP limit + SL stop-limit atomically). `main.py` selects the executor based on `config.paper_trading`. Both implementations append to `~/midas_ai/logs/trades.csv` and `~/midas_ai/logs/equity_history.csv` on each close/tick.

**`main.py`** — `MidasBot` runs the tick loop (one iteration per clock minute). The tick sequence is documented in the module docstring: market hours guard → new session check → kill switch → PDT guard → EOD flatten → opening buffer → screener scan → exit check → state persist. `PDTGuard` tracks 3-in-5-session day trade limits for accounts below $25k. `MarketHoursGuard` prefers Alpaca's `/v2/clock` (holiday-aware) with a time-based fallback. Accepts `--check` flag to run preflight self-tests without starting the trading loop.

**`dashboard.py`** — Streamlit dashboard that reads `state.json`, `trades.csv`, and `equity_history.csv`. Displays equity, daily P&L, peak equity, kill-switch status, an equity curve, the last 20 trades, and summary statistics. Auto-refreshes every 30 seconds.

**`midas_backtest.py`** — Offline backtester. Accepts a historical OHLCV DataFrame and runs the same `add_indicators()` / `generate_signal()` pipeline used in live trading, producing a summary of trades and performance metrics.

**`test_risk.py`** — 21 pytest unit tests covering `calculate_position_size` (risk cap, notional cap, invalid inputs), `check_kill_switch` (trigger, persistence, no-trigger cases), and `approve_trade` (all rejection conditions and the approval path). Uses `tmp_path` fixtures so tests never touch the real state file.

## Key constraints to preserve

- **IEX rate limit**: The free IEX feed allows ~200 requests/minute. The screener uses bulk multi-symbol endpoints (2 calls/tick total). Do not add per-symbol API calls inside the scan loop.
- **PDT rule**: At $15k equity, max 3 day-trades per rolling 5-session window. The `PDTGuard` in `main.py` enforces this — don't bypass it.
- **VWAP timezone**: VWAP must group bars by NYSE session date in ET, not UTC date. Pre-market bars are zeroed out. See `compute_vwap()` in `midas_strategy.py`.
- **Paper-first**: `PAPER_TRADING = True` in config. The `main()` entry point reads `PAPER_TRADING` env var to override. Do not change the default.
- **State file**: `~/midas_ai/state/state.json` persists equity, peak, and compounding day across restarts. `RiskManager._load_state()` reads it on startup; a missing file is fine (reinitializes to `INITIAL_CAPITAL`).

## Testing

```bash
python3 -m pytest test_risk.py -v
```

Covers `RiskManager`: position sizing (risk cap, notional cap, invalid inputs), kill switch (trigger at 3% drawdown, halt persistence), and trade approval gate. All tests use a temp state file and do not touch the real bot state.

## Optional integrations

- **Telegram alerts**: Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` env vars. Used for session open, order fills, kill switch events, and tick errors.
- **Heartbeat monitor**: Set `HEARTBEAT_URL` env var (e.g. healthchecks.io UUID). Pings every 60 seconds while the bot is running.
