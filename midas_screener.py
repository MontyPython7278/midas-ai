"""
╔══════════════════════════════════════════════════════════════════╗
║                  MIDAS AI — SYMBOL SCREENER                     ║
║   Scans the watchlist each tick and returns ranked signals       ║
╚══════════════════════════════════════════════════════════════════╝

HOW THE SCREENER WORKS
──────────────────────
Each tick the screener:
  1. Fetches bars for ALL watchlist symbols in a single bulk API call
     (Alpaca supports multi-symbol requests — 1 call for 30 symbols)
  2. Computes indicators for each symbol
  3. Calls generate_signal() for each symbol
  4. Returns a ranked list of valid signals, best first

RANKING LOGIC
─────────────
Signals are ranked by a composite score:
  • RSI distance from neutral (50): more extreme = higher score
  • ATR as % of price: higher volatility = larger expected move
  • MACD histogram magnitude: stronger momentum turn = higher score

The main loop takes the top-ranked signal that passes the risk gate.

IEX FREE FEED RATE LIMITS
──────────────────────────
Alpaca's free IEX feed allows ~200 requests/minute.
The bulk bar endpoint counts as ONE request regardless of how many
symbols are in the query — making it highly efficient.
We fetch: 1 bulk call (1Min, 30 symbols) + 1 bulk call (5Min, 30 symbols)
= 2 API calls per tick. Well within limits.

SHORTABILITY CHECK
──────────────────
Before adding a short signal to the results, the screener checks
Alpaca's asset record to confirm the symbol is shortable.
This is cached per session since shortability rarely changes intraday.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from midas_config import MidasConfig
from midas_strategy import add_indicators, generate_signal, EntrySignal

logger = logging.getLogger("midas.screener")


# ══════════════════════════════════════════════════════════════════
#  RANKED SIGNAL
# ══════════════════════════════════════════════════════════════════

@dataclass
class RankedSignal:
    symbol:        str
    signal:        EntrySignal
    score:         float      # Higher = better setup quality
    higher_tf_rsi: float

    def __repr__(self) -> str:
        return (
            f"RankedSignal({self.symbol} {self.signal.direction.upper()} "
            f"score={self.score:.3f} RSI={self.signal.rsi_value:.1f})"
        )


# ══════════════════════════════════════════════════════════════════
#  SCREENER
# ══════════════════════════════════════════════════════════════════

class SymbolScreener:
    """
    Scans the entire watchlist on every tick and returns ranked signals.
    Instantiated once and reused across ticks — caches shortability data.
    """

    def __init__(self, config: MidasConfig):
        self.cfg      = config
        # Cache of symbol → shortable (True/False), populated once per session
        self._shortable_cache: dict[str, bool] = {}

    # ── Session reset ─────────────────────────────────────────────

    def new_session(self) -> None:
        """Clear the shortability cache at each session open."""
        self._shortable_cache.clear()
        logger.info("Screener: shortability cache cleared for new session.")

    # ── Bulk OHLCV fetch ──────────────────────────────────────────

    async def _fetch_bulk_bars(
        self,
        symbols: list[str],
        timeframe: str,
        limit: int,
    ) -> dict[str, pd.DataFrame]:
        """
        Fetch bars for multiple symbols in a single API call.
        Returns {symbol: DataFrame} for symbols that returned data.

        Alpaca's /v2/stocks/bars endpoint accepts a comma-separated
        list of symbols — far more efficient than one call per symbol.
        """
        from datetime import datetime, timedelta, timezone
        import aiohttp

        end   = datetime.now(timezone.utc)
        start = end - timedelta(minutes=limit * 3)  # Buffer for non-trading minutes

        params = {
            "symbols":   ",".join(symbols),
            "timeframe": timeframe,
            "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":     limit,
            "feed":      "iex",
            "sort":      "asc",
        }

        headers = {
            "APCA-API-KEY-ID":     __import__("os").environ.get("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": __import__("os").environ.get("ALPACA_API_SECRET", ""),
        }

        url = f"{self.cfg.alpaca_data_url}/v2/stocks/bars"

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except Exception as e:
            logger.error(f"Bulk bar fetch failed [{timeframe}]: {e}")
            return {}

        results: dict[str, pd.DataFrame] = {}
        bars_by_symbol = data.get("bars", {})

        for sym, bars in bars_by_symbol.items():
            if not bars or len(bars) < 30:   # Need enough bars for indicator warmup
                continue
            try:
                df = pd.DataFrame(bars).rename(columns={
                    "t": "timestamp", "o": "open", "h": "high",
                    "l": "low",       "c": "close","v": "volume",
                })
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df.set_index("timestamp", inplace=True)
                keep = ["open", "high", "low", "close", "volume"]
                results[sym] = df[keep].astype(float).tail(limit)
            except Exception as e:
                logger.debug(f"Bar parse error for {sym}: {e}")

        logger.debug(f"Bulk fetch [{timeframe}]: {len(results)}/{len(symbols)} symbols returned data")
        return results

    # ── Shortability check ────────────────────────────────────────

    async def _is_shortable(self, symbol: str) -> bool:
        """
        Check Alpaca's asset record for this symbol.
        Result is cached for the session — shortability doesn't change intraday.
        """
        if symbol in self._shortable_cache:
            return self._shortable_cache[symbol]

        try:
            import aiohttp, os
            headers = {
                "APCA-API-KEY-ID":     os.environ.get("ALPACA_API_KEY", ""),
                "APCA-API-SECRET-KEY": os.environ.get("ALPACA_API_SECRET", ""),
            }
            url = f"{self.cfg.base_url}/v2/assets/{symbol}"
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        asset = await resp.json()
                        shortable = bool(asset.get("shortable", False)) and \
                                    bool(asset.get("easy_to_borrow", False))
                    else:
                        shortable = False
        except Exception as e:
            logger.debug(f"Shortability check failed for {symbol}: {e}")
            shortable = False

        self._shortable_cache[symbol] = shortable
        logger.debug(f"{symbol} shortable: {shortable}")
        return shortable

    # ── Signal scoring ────────────────────────────────────────────

    def _score_signal(self, sig: EntrySignal, higher_tf_rsi: float) -> float:
        """
        Composite signal quality score — higher is better.

        Components:
          RSI extremity (0-1):    how far RSI is from neutral 50
          ATR % of price (0-1):  scaled relative volatility
          Histogram magnitude:   strength of the momentum turn

        All components are normalised to [0, 1] so they contribute equally.
        """
        # RSI distance from 50: max useful distance is 20 points
        rsi_score = min(abs(sig.rsi_value - 50) / 20, 1.0)

        # ATR as % of price: cap at 1% (anything above is equally good)
        atr_pct   = (sig.atr_value / sig.entry_price) if sig.entry_price > 0 else 0
        atr_score = min(atr_pct / 0.01, 1.0)

        # Higher-TF RSI alignment bonus
        if sig.direction == "long" and higher_tf_rsi < 50:
            htf_bonus = (50 - higher_tf_rsi) / 50   # Up to 1.0 bonus if 5m RSI is also low
        elif sig.direction == "short" and higher_tf_rsi > 50:
            htf_bonus = (higher_tf_rsi - 50) / 50
        else:
            htf_bonus = 0.0

        return round((rsi_score * 0.4) + (atr_score * 0.3) + (htf_bonus * 0.3), 4)

    # ── Main scan ─────────────────────────────────────────────────

    async def scan(self, open_symbols: set[str]) -> list[RankedSignal]:
        """
        Scan the entire watchlist and return ranked valid signals.

        `open_symbols`: set of symbols already held — skip these to avoid
                        pyramiding into existing positions.

        Returns an empty list if no signals are found this tick.
        """
        symbols = self.cfg.watchlist

        # Fetch both timeframes concurrently
        bars_1m, bars_5m = await asyncio.gather(
            self._fetch_bulk_bars(symbols, self.cfg.timeframe, limit=200),
            self._fetch_bulk_bars(symbols, self.cfg.higher_tf,  limit=100),
        )

        if not bars_1m:
            logger.warning("No bar data returned from screener this tick.")
            return []

        ranked: list[RankedSignal] = []

        for sym in symbols:
            # Skip if already holding this symbol
            if sym in open_symbols:
                continue

            df_1m = bars_1m.get(sym)
            df_5m = bars_5m.get(sym)

            if df_1m is None or df_5m is None or len(df_1m) < 40 or len(df_5m) < 30:
                continue

            try:
                df_1m = add_indicators(df_1m, self.cfg)
                df_5m = add_indicators(df_5m, self.cfg)

                # Drop the incomplete current bar
                df_1m = df_1m.iloc[:-1]
                df_5m = df_5m.iloc[:-1]

                if df_1m.empty or len(df_1m) < 2:
                    continue

                row      = df_1m.iloc[-1]
                prev_row = df_1m.iloc[-2]
                htf_rsi  = float(df_5m["rsi"].iloc[-1])

                sig = generate_signal(row, prev_row, self.cfg, higher_tf_rsi=htf_rsi)

                if sig.direction == "none":
                    continue

                # Short selling gate — check Alpaca asset record
                if sig.direction == "short":
                    if not self.cfg.allow_shorts:
                        continue
                    if not await self._is_shortable(sym):
                        logger.debug(f"{sym}: short signal but not shortable — skipped")
                        continue

                score = self._score_signal(sig, htf_rsi)
                ranked.append(RankedSignal(
                    symbol        = sym,
                    signal        = sig,
                    score         = score,
                    higher_tf_rsi = htf_rsi,
                ))

            except Exception as e:
                logger.debug(f"Screener error on {sym}: {e}")
                continue

        # Sort best score first
        ranked.sort(key=lambda x: x.score, reverse=True)

        if ranked:
            logger.info(
                f"Screener found {len(ranked)} signal(s): "
                + ", ".join(f"{r.symbol}({r.signal.direction[0].upper()}={r.score:.2f})"
                            for r in ranked[:5])
            )
        else:
            logger.info("Screener: no qualifying signals this tick.")

        return ranked
