"""
╔══════════════════════════════════════════════════════════════════╗
║               MIDAS AI — CONFIG MODULE (ALPACA)                 ║
║      $15,000 paper account · Mac local · Shorts enabled         ║
╚══════════════════════════════════════════════════════════════════╝

ACCOUNT CONTEXT
───────────────
Starting capital:  $15,000
PDT status:        ACTIVE — below $25,000 threshold
                   Maximum 3 day-trades per rolling 5-session window
Short selling:     ENABLED
Data feed:         Alpaca free IEX feed
Environment:       macOS local (Cursor IDE)

PDT IMPACT AT $15,000
──────────────────────
With $15k you are subject to FINRA's Pattern Day Trader rule.
The bot will allow a maximum of 3 completed round-trips (open + close
same day) across any rolling 5 NYSE sessions.

After the 3rd day trade, the bot blocks new entries for the remainder
of that 5-session window.  Swing trades (held overnight) do NOT count
as day trades but are NOT part of this strategy — the EOD flatten
ensures you are always flat before 16:00 ET.

Reaching $25,000 equity lifts the PDT restriction automatically.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


# ──────────────────────────────────────────────
#  MAC-COMPATIBLE PATHS
#  All paths are relative to the user's home directory.
#  ~/midas_ai/ is created automatically on first run.
# ──────────────────────────────────────────────

HOME            = Path.home()
MIDAS_DIR       = HOME / "midas_ai"
LOG_FILE        = str(MIDAS_DIR / "logs" / "bot.log")
STATE_FILE      = str(MIDAS_DIR / "state" / "state.json")


# ──────────────────────────────────────────────
#  BROKER ENDPOINTS
# ──────────────────────────────────────────────

ALPACA_PAPER_BASE_URL  = "https://paper-api.alpaca.markets"
ALPACA_LIVE_BASE_URL   = "https://api.alpaca.markets"
ALPACA_DATA_BASE_URL   = "https://data.alpaca.markets"


# ──────────────────────────────────────────────
#  SYMBOL WATCHLIST
#  ~30 liquid, IEX-traded, shortable S&P 500 equities.
#
#  WHY A WATCHLIST INSTEAD OF THE ENTIRE MARKET?
#  ──────────────────────────────────────────────
#  The free IEX feed caps at 200 API requests/minute.
#  Fetching 2 timeframes × 30 symbols = 60 requests/tick — safe.
#  Fetching the entire S&P 500 (500 symbols × 2 TF) = 1,000 req/tick
#  — would immediately breach the rate limit and get the key suspended.
#
#  These 30 symbols are chosen for:
#   • High daily volume (>$1B average) — tight spreads, good fills
#   • Active options markets (institutional interest = liquidity)
#   • Confirmed shortable on Alpaca IEX feed
#   • Mix of sectors to reduce correlation on down-market days
# ──────────────────────────────────────────────

WATCHLIST: List[str] = [
    # ETFs — broad market and sector
    "SPY",  "QQQ",  "IWM",  "XLF",  "XLE",  "XLK",

    # Mega-cap tech (high volume, tight spreads)
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA",

    # Finance
    "JPM",  "BAC",  "GS",

    # Healthcare
    "UNH",  "JNJ",  "PFE",

    # Energy & industrials
    "XOM",  "CVX",  "BA",  "CAT",

    # Consumer
    "COST", "WMT",  "HD",

    # Semiconductors (high beta, good for mean-reversion)
    "AMD",  "INTC", "QCOM",

    # Other high-volume names
    "DIS",  "NFLX", "CRM",
]

# Timeframe strings — Alpaca format (NOT ccxt format)
TIMEFRAME   = "1Min"
HIGHER_TF   = "5Min"


# ──────────────────────────────────────────────
#  MARKET HOURS (NYSE) — ALL TIMES EASTERN
# ──────────────────────────────────────────────

TIMEZONE               = "America/New_York"
MARKET_OPEN_ET         = "09:30"
MARKET_CLOSE_ET        = "16:00"
NO_ENTRY_BUFFER_MINS   = 15    # No new entries after 15:45 ET
OPENING_BUFFER_MINS    = 5     # No new entries before 09:35 ET


# ──────────────────────────────────────────────
#  ACCOUNT & CAPITAL
# ──────────────────────────────────────────────

INITIAL_CAPITAL        = 15_000.0
PAPER_BALANCE          = 15_000.0
COMPOUNDING_RATE       = 0.01
MIN_TRADE_USD          = 10.0


# ──────────────────────────────────────────────
#  RISK PARAMETERS
# ──────────────────────────────────────────────

MAX_RISK_PER_TRADE     = 0.005     # 0.5% of account per trade = $75 max risk
DAILY_DRAWDOWN_LIMIT   = 0.03     # 3% = $450 daily loss limit → kill switch
RISK_OF_RUIN_THRESHOLD = 0.85

# Fees — Alpaca free tier, IEX feed
FEE_RATE               = 0.0
SEC_FEE_RATE           = 0.0000278
FINRA_TAF_RATE         = 0.000166
SLIPPAGE_ESTIMATE      = 0.0003
ROUND_TRIP_COST        = (SLIPPAGE_ESTIMATE + SEC_FEE_RATE) * 2


# ──────────────────────────────────────────────
#  SHORT SELLING
# ──────────────────────────────────────────────

# Set True because your Alpaca account has short selling enabled.
# The screener will check each symbol's shortability via Alpaca's
# assets endpoint before submitting a short order.
ALLOW_SHORTS           = True


# ──────────────────────────────────────────────
#  PATTERN DAY TRADER GUARD
# ──────────────────────────────────────────────

PDT_MIN_EQUITY         = 25_000.0   # FINRA threshold
MAX_DAY_TRADES_ROLLING = 3          # Max per 5-session window at $15k
BUYING_POWER_MULT      = 4.0


# ──────────────────────────────────────────────
#  STRATEGY PARAMETERS
# ──────────────────────────────────────────────

ATR_PERIOD             = 14
ATR_STOP_MULT          = 1.5
ATR_TARGET_MULT        = 2.25
RSI_PERIOD             = 14
RSI_OVERSOLD           = 35
RSI_OVERBOUGHT         = 65
MACD_FAST              = 12
MACD_SLOW              = 26
MACD_SIGNAL            = 9
MAX_OPEN_TRADES        = 2         # Max simultaneous positions across all symbols


# ──────────────────────────────────────────────
#  INFRASTRUCTURE
# ──────────────────────────────────────────────

HEARTBEAT_URL          = os.getenv("HEARTBEAT_URL", "https://hc-ping.com/YOUR-UUID")
HEARTBEAT_INTERVAL     = 60
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")


# ──────────────────────────────────────────────
#  MODE FLAG
# ──────────────────────────────────────────────

PAPER_TRADING          = True    # Always True until 30-session validation done


# ──────────────────────────────────────────────
#  TYPED CONFIG DATACLASS
# ──────────────────────────────────────────────

@dataclass
class MidasConfig:
    """Single source of truth injected into every module."""

    # Paths (Mac-compatible)
    log_file: str              = LOG_FILE
    state_file: str            = STATE_FILE

    # Broker
    alpaca_paper_url: str      = ALPACA_PAPER_BASE_URL
    alpaca_live_url: str       = ALPACA_LIVE_BASE_URL
    alpaca_data_url: str       = ALPACA_DATA_BASE_URL

    # Universe
    watchlist: List[str]       = field(default_factory=lambda: WATCHLIST)
    timeframe: str             = TIMEFRAME
    higher_tf: str             = HIGHER_TF

    # Market hours
    timezone: str              = TIMEZONE
    market_open_et: str        = MARKET_OPEN_ET
    market_close_et: str       = MARKET_CLOSE_ET
    no_entry_buffer_mins: int  = NO_ENTRY_BUFFER_MINS
    opening_buffer_mins: int   = OPENING_BUFFER_MINS

    # Capital
    initial_capital: float     = INITIAL_CAPITAL
    compounding_rate: float    = COMPOUNDING_RATE

    # Risk
    max_risk_per_trade: float  = MAX_RISK_PER_TRADE
    daily_dd_limit: float      = DAILY_DRAWDOWN_LIMIT
    ror_threshold: float       = RISK_OF_RUIN_THRESHOLD
    fee_rate: float            = FEE_RATE
    sec_fee_rate: float        = SEC_FEE_RATE
    finra_taf_rate: float      = FINRA_TAF_RATE
    slippage: float            = SLIPPAGE_ESTIMATE

    # Shorts
    allow_shorts: bool         = ALLOW_SHORTS

    # PDT
    pdt_min_equity: float      = PDT_MIN_EQUITY
    max_day_trades: int        = MAX_DAY_TRADES_ROLLING
    buying_power_mult: float   = BUYING_POWER_MULT

    # Strategy
    atr_period: int            = ATR_PERIOD
    atr_stop_mult: float       = ATR_STOP_MULT
    atr_target_mult: float     = ATR_TARGET_MULT
    rsi_period: int            = RSI_PERIOD
    rsi_oversold: float        = RSI_OVERSOLD
    rsi_overbought: float      = RSI_OVERBOUGHT
    macd_fast: int             = MACD_FAST
    macd_slow: int             = MACD_SLOW
    macd_signal: int           = MACD_SIGNAL
    max_open_trades: int       = MAX_OPEN_TRADES

    # Mode
    paper_trading: bool        = PAPER_TRADING

    @property
    def base_url(self) -> str:
        return self.alpaca_paper_url if self.paper_trading else self.alpaca_live_url

    def ensure_directories(self) -> None:
        """Create all required local directories on first run."""
        for path_str in [self.log_file, self.state_file]:
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)
