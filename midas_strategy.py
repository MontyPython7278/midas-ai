"""
╔══════════════════════════════════════════════════════════════════╗
║            MIDAS AI — STRATEGY ENGINE (ALPACA/EQUITIES)         ║
║         Mean-Reversion Scalping with ATR / RSI / MACD           ║
╚══════════════════════════════════════════════════════════════════╝

STRATEGY: ATR-Gated RSI Mean-Reversion with MACD Confirmation
──────────────────────────────────────────────────────────────
Why Mean Reversion over pure Trend-Following for scalping?

Equity markets on 1m/5m timeframes exhibit strong mean-reversion
properties due to:
  • Market makers continuously quoting around VWAP
  • Institutional algos using TWAP/VWAP execution that suppresses
    sustained directional momentum on short timeframes
  • Post-open volatility compression once price discovery finishes

ATR role: Filter out low-volatility periods (pre-catalyst, lunchtime
lull 11:30–13:00 ET) where spreads eat any edge. Only trade when the
move potential exceeds the round-trip friction.

RSI role: Identify over-extension from the VWAP anchor. RSI < 35 +
rising MACD histogram = oversold snap-back setup.

MACD role: Confirm momentum is TURNING, not just at an extreme.
Prevents "catching falling knives" on RSI alone during news-driven
moves that genuinely persist.

ENTRY CONDITIONS (Long):
  1. Market hours guard has confirmed NYSE is open (in main.py)
  2. ATR > minimum threshold (volatility gate)
  3. RSI < RSI_OVERSOLD (35) on the current closed 1m bar
  4. MACD histogram: current bar > previous bar (turning up)
  5. Price within 0.2% of VWAP (mean-reversion anchor — not already
     recovered; if price is already back at VWAP, the move is over)
  6. Higher-TF (5m) RSI not in confirmed downtrend (no trend fighting)

STOP-LOSS:   entry − (ATR × 1.5)
TAKE-PROFIT: entry + (ATR × 2.25)  →  Risk-Reward = 1 : 1.5

KEY DIFFERENCE FROM CRYPTO VERSION
────────────────────────────────────
VWAP now resets at NYSE market open (9:30 AM ET), not UTC midnight.
  • NYSE opens at 09:30 ET = 13:30 UTC (EST) or 14:30 UTC (EDT)
  • Resetting at UTC midnight would produce a ~4-hour VWAP pre-session
    from pre-market data, making it meaningless as a session anchor
  • All session logic must use America/New_York timezone
"""

import logging
from dataclasses import dataclass
from datetime import time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from midas_config import MidasConfig
from midas_risk import TradeSignal

logger = logging.getLogger("midas.strategy")

ET = ZoneInfo("America/New_York")
MARKET_OPEN_TIME = dt_time(9, 30)    # NYSE session open in ET


# ══════════════════════════════════════════════════════════════════
#  INDICATOR LIBRARY (vectorised, zero external TA dependencies)
# ══════════════════════════════════════════════════════════════════

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Wilder ATR.  Uses ewm with adjust=False to match TradingView /
    most charting platforms.

    True Range = max(H-L, |H-Cp|, |L-Cp|)
    ATR = Wilder-smoothed TR  (alpha = 1/period)
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().rename("atr")


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder RSI via ewm smoothing.
    Identical to TradingView's default RSI implementation.
    """
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    ag    = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    al    = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs    = ag / al.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).rename("rsi")


def compute_macd(
    series: pd.Series,
    fast: int = 12, slow: int = 26, signal_p: int = 9,
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    ema_fast   = series.ewm(span=fast,   adjust=False).mean()
    ema_slow   = series.ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_p, adjust=False).mean()
    return pd.DataFrame({
        "macd_line":   macd_line,
        "signal_line": signal_line,
        "histogram":   macd_line - signal_line,
    })


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Cumulative intraday VWAP that resets at NYSE session open (9:30 AM ET).

    WHY THIS IS DIFFERENT FROM THE CRYPTO VERSION
    ──────────────────────────────────────────────
    The original implementation grouped bars by UTC date (normalise to
    midnight UTC).  For crypto, markets never close so this is fine.

    For US equities, the correct grouping key is the NYSE session date
    in Eastern Time.  Bars between 09:30–16:00 ET belong to one session.
    Pre-market bars (04:00–09:29 ET) are excluded from the VWAP
    calculation — they contain thin volume that distorts the anchor.

    Implementation:
      1. Convert UTC index to ET
      2. Identify bars that fall within the regular session (≥ 09:30)
      3. Group by ET date — cumulative TP×V / cumulative V per group
      4. Restore the original UTC index before returning

    Bars outside regular session hours return NaN for VWAP.
    """
    df_work = df.copy()

    # Convert to ET for session-aware grouping
    df_work.index = df.index.tz_convert(ET)
    df_work["session_date"] = df_work.index.normalize()
    df_work["bar_time_et"]  = pd.Series(
        [ts.time() for ts in df_work.index], index=df_work.index
    )

    # Only include bars that are inside the regular session
    in_session = df_work["bar_time_et"] >= MARKET_OPEN_TIME
    df_work["tp"]      = (df["high"] + df["low"] + df["close"]) / 3
    df_work["tp_vol"]  = df_work["tp"] * df["volume"]

    # Zero out pre-market bars so they don't pollute the cumulative sum
    df_work.loc[~in_session, "tp_vol"] = 0.0
    df_work.loc[~in_session, "volume"] = 0.0

    df_work["cum_tp_vol"] = df_work.groupby("session_date")["tp_vol"].cumsum()
    df_work["cum_vol"]    = df_work.groupby("session_date")["volume"].cumsum()

    vwap = df_work["cum_tp_vol"] / df_work["cum_vol"].replace(0, np.nan)
    vwap.index = df.index  # Restore UTC index
    return vwap.rename("vwap")


# ══════════════════════════════════════════════════════════════════
#  FEATURE COMPUTATION
# ══════════════════════════════════════════════════════════════════

def add_indicators(df: pd.DataFrame, cfg: MidasConfig) -> pd.DataFrame:
    """
    Enrich a raw OHLCV DataFrame (UTC DatetimeIndex) with all
    strategy indicators.  Called once per tick after fetching bars.
    """
    df = df.copy()
    df["atr"]  = compute_atr(df, cfg.atr_period)
    df["rsi"]  = compute_rsi(df["close"], cfg.rsi_period)
    macd_df    = compute_macd(df["close"], cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    df         = pd.concat([df, macd_df], axis=1)
    df["vwap"] = compute_vwap(df)                # ← Resets at 09:30 ET now

    # Pre-compute lag columns used in generate_signal
    df["hist_prev"] = df["histogram"].shift(1)
    df["rsi_prev"]  = df["rsi"].shift(1)
    return df.dropna()


# ══════════════════════════════════════════════════════════════════
#  SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════

@dataclass
class EntrySignal:
    direction:   str      # "long" | "short" | "none"
    entry_price: float
    stop_loss:   float
    take_profit: float
    atr_value:   float
    rsi_value:   float
    reason:      str


def generate_signal(
    row:           pd.Series,
    prev_row:      pd.Series,
    cfg:           MidasConfig,
    higher_tf_rsi: Optional[float] = None,
) -> EntrySignal:
    """
    Evaluate one fully-closed 1m candle for a potential entry.

    Callers must already have confirmed:
      • Market is open (NYSE session check in main.py)
      • Opening buffer has passed (first 5 minutes excluded)
      • End-of-day buffer hasn't started (no entries after 15:45 ET)
    This function focuses purely on technical signal logic.
    """
    close     = row["close"]
    atr       = row["atr"]
    rsi       = row["rsi"]
    hist      = row["histogram"]
    hist_prev = row["hist_prev"]
    vwap      = row["vwap"]

    # ── Volatility gate ────────────────────────────────────────────────
    # Minimum ATR: 4× the round-trip cost ensures the expected move
    # at least covers fees and leaves positive EV.
    min_atr = close * cfg.slippage * 4
    if atr < min_atr or np.isnan(atr):
        return EntrySignal("none", close, 0, 0, atr, rsi,
                           f"ATR {atr:.4f} below gate {min_atr:.4f}")

    # ── VWAP availability check ────────────────────────────────────────
    # VWAP is NaN during pre-market.  If it's NaN, skip — this should
    # never happen because market_hours_guard blocks pre-market ticks,
    # but it's a useful defensive check.
    if np.isnan(vwap):
        return EntrySignal("none", close, 0, 0, atr, rsi, "VWAP not yet available")

    # ── Higher-timeframe filter ────────────────────────────────────────
    htf_ok_long  = higher_tf_rsi is None or higher_tf_rsi > 40
    htf_ok_short = higher_tf_rsi is None or higher_tf_rsi < 60

    # ── LONG SETUP ─────────────────────────────────────────────────────
    # Condition set:
    #   RSI oversold AND MACD histogram turning up AND price near VWAP
    #   (within 0.2% below — has pulled back to the mean but not already
    #   recovered past it) AND no bearish higher-TF trend
    if (
        rsi       <  cfg.rsi_oversold
        and hist  >  hist_prev              # Histogram turning up
        and close >= vwap * 0.998          # Within 0.2% of VWAP (below)
        and close <= vwap * 1.002          # Not already extended above
        and htf_ok_long
    ):
        stop_loss   = round(close - atr * cfg.atr_stop_mult,   2)
        take_profit = round(close + atr * cfg.atr_target_mult, 2)
        return EntrySignal(
            direction   = "long",
            entry_price = close,
            stop_loss   = stop_loss,
            take_profit = take_profit,
            atr_value   = atr,
            rsi_value   = rsi,
            reason      = f"RSI={rsi:.1f} oversold, MACD hist ↑, VWAP={vwap:.2f}, HTF ok",
        )

    # ── SHORT SETUP ────────────────────────────────────────────────────
    if (
        rsi       >  cfg.rsi_overbought
        and hist  <  hist_prev              # Histogram turning down
        and close >= vwap * 0.998          # Near VWAP
        and close <= vwap * 1.002
        and htf_ok_short
    ):
        stop_loss   = round(close + atr * cfg.atr_stop_mult,   2)
        take_profit = round(close - atr * cfg.atr_target_mult, 2)
        return EntrySignal(
            direction   = "short",
            entry_price = close,
            stop_loss   = stop_loss,
            take_profit = take_profit,
            atr_value   = atr,
            rsi_value   = rsi,
            reason      = f"RSI={rsi:.1f} overbought, MACD hist ↓, VWAP={vwap:.2f}, HTF ok",
        )

    return EntrySignal("none", close, 0, 0, atr, rsi, "No qualifying setup")


# ══════════════════════════════════════════════════════════════════
#  DATA HELPERS
# ══════════════════════════════════════════════════════════════════

def ohlcv_to_dataframe(raw: list) -> pd.DataFrame:
    """Convert a ccxt-style [[ts_ms, o, h, l, c, v], ...] list to DataFrame."""
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df.astype(float)
