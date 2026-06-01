# Midas AI

An algorithmic mean-reversion trading system for US equities, built on Alpaca's paper trading API.

---

## What it is

Midas AI is a Python trading bot that scans a 30-symbol watchlist of liquid US equities every market minute, identifies mean-reversion setups, and manages the full trade lifecycle — entry sizing, bracket orders, exit management, and daily risk limits. It runs on Alpaca's paper trading environment and is built and maintained solo as a learning and research project.

---

## How it works

The strategy is **ATR-gated RSI mean-reversion with MACD confirmation**, using VWAP as the intraday reversion anchor.

The core idea: on short timeframes (1m/5m), price tends to revert to the session VWAP rather than trend away from it indefinitely. The bot looks for moments when price has pulled away from VWAP, momentum is exhausted (RSI oversold/overbought), and is beginning to turn (MACD histogram inflecting) — then enters with a bracket order targeting the reversion.

**Entry conditions (long example):**
- ATR above a minimum threshold — filters out low-volatility, low-edge periods
- RSI below 35 on the closed 1m bar
- MACD histogram turning upward (momentum inflecting, not just at an extreme)
- Price at or below VWAP (not already recovered past the anchor)
- Higher-timeframe (5m) RSI not in confirmed downtrend

**Risk and execution:**
- Each trade risks at most 0.5% of current equity, sized dynamically
- Notional position capped at 20% of equity regardless of stop distance
- Stop-loss at entry ± 1.5× ATR; take-profit at entry ± 2.25× ATR (1:1.5 RR)
- Maximum 2 simultaneous open positions

**Seven-layer guard before any order is placed:**
1. NYSE market hours (holiday-aware via Alpaca's `/v2/clock`)
2. 5-minute opening buffer — skips the first 5 minutes of price discovery
3. 15-minute EOD buffer — no new entries after 15:45 ET; all positions flattened by 16:00
4. 3% daily-drawdown kill switch — halts all trading for the session if hit
5. PDT guard — enforces FINRA's 3-day-trade-per-5-session limit at accounts below $25k
6. Notional and risk caps in the risk manager
7. Per-symbol shortability validation via Alpaca's asset endpoint

---

## Architecture

| Module | Role |
|---|---|
| `midas_config.py` | Single source of truth — all constants and the `MidasConfig` dataclass injected into every other module |
| `midas_strategy.py` | Pure functions: `add_indicators()` computes ATR, RSI, MACD, VWAP; `generate_signal()` evaluates one closed candle |
| `midas_screener.py` | Fetches bars for all 30 symbols in two bulk API calls per tick (one per timeframe), scores and ranks signals |
| `midas_risk.py` | `RiskManager` — position sizing, kill switch, daily drawdown tracking, compounding target, state persistence |
| `midas_executor.py` | Two implementations: `PaperTradingEngine` (in-process simulation with slippage + fees) and `AlpacaExecutor` (live/paper REST client) |
| `main.py` | Tick orchestrator — runs the 7-layer guard sequence every market minute, routes to screener and executor |
| `dashboard.py` | Streamlit dashboard — reads `state.json`, `trades.csv`, and `equity_history.csv` for a live view of performance |
| `midas_backtest.py` | Offline backtesting against historical OHLCV CSVs using the same strategy logic |

---

## Tech stack

- **Python 3.9+** — async/await throughout (`asyncio`, `aiohttp`)
- **Alpaca API** — market data (IEX feed) and order execution
- **pandas / numpy** — indicator computation and bar handling
- **Streamlit** — live performance dashboard
- **aiohttp** — all HTTP calls to Alpaca's REST API

---

## Status

**This project is in paper trading validation. It is not running with real money.**

The bot is currently accumulating paper trading sessions to evaluate whether the strategy has a genuine edge before any consideration of live deployment. No performance results are published here because a meaningful sample does not yet exist.

> **Disclaimer:** This is a personal educational project. Nothing in this repository constitutes financial advice, a trading recommendation, or an invitation to invest. Algorithmic trading carries significant risk of capital loss. Do your own research.

---

## Setup

Requires Python 3.9+, an [Alpaca](https://alpaca.markets) account (free), and paper trading API keys.

```bash
# Clone and install
git clone https://github.com/MontyPython7278/midas-ai.git
cd midas-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set API keys (paper keys from app.alpaca.markets/paper/dashboard/overview)
export ALPACA_API_KEY="your-key-id"
export ALPACA_API_SECRET="your-secret-key"

# Verify everything is wired up correctly
python3 main.py --check

# Run the bot (paper mode by default)
python3 main.py
```

To add keys permanently, append the two `export` lines to `~/.zshrc`.

To run the dashboard in a second terminal:

```bash
source venv/bin/activate
streamlit run dashboard.py
```
