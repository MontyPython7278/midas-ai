"""
╔══════════════════════════════════════════════════════════════════╗
║            MIDAS AI — EXECUTOR MODULE (ALPACA)                  ║
║   Internal paper simulation · Alpaca Paper API · Alpaca Live    ║
╚══════════════════════════════════════════════════════════════════╝

Two execution modes controlled by config.paper_trading:

  PaperTradingEngine — In-process simulation. No API keys needed
                       for the engine itself (data still uses keys).
                       Applies slippage + SEC/FINRA fees realistically.

  AlpacaExecutor     — Connects to Alpaca REST + Data APIs via aiohttp.
                       paper_trading=True  → paper-api.alpaca.markets
                       paper_trading=False → api.alpaca.markets

SHORT SELLING NOTE
──────────────────
allow_shorts is read from MidasConfig (set True in your config).
The screener pre-validates shortability via Alpaca's asset endpoint.
This executor will reject a short order if allow_shorts=False as a
final safety net, even if the screener somehow passed it through.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import pandas as pd

from midas_config import MidasConfig
from midas_risk import RiskManager
from midas_strategy import EntrySignal

logger = logging.getLogger("midas.executor")


# ══════════════════════════════════════════════════════════════════
#  SHARED DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════

@dataclass
class OpenPosition:
    trade_id:        str
    symbol:          str
    side:            str
    entry_price:     float
    stop_loss:       float
    take_profit:     float
    shares:          float
    notional:        float
    entry_time:      str
    alpaca_order_id: Optional[str] = None


@dataclass
class TradeResult:
    trade_id:    str
    symbol:      str
    side:        str
    entry_price: float
    exit_price:  float
    shares:      float
    notional:    float
    pnl_dollar:  float
    pnl_pct:     float
    outcome:     str
    duration_s:  int


# ══════════════════════════════════════════════════════════════════
#  MODE 1 — INTERNAL PAPER TRADING ENGINE
# ══════════════════════════════════════════════════════════════════

class PaperTradingEngine:
    """
    In-process simulation with realistic fee and slippage modelling.
    No API keys required to use this engine.
    Data fetching (bars) still requires Alpaca keys.
    """

    def __init__(self, config: MidasConfig, risk_manager: RiskManager):
        self.cfg             = config
        self.rm              = risk_manager
        self.balance         = config.initial_capital
        self.positions: dict[str, OpenPosition] = {}
        self.trade_log: list[TradeResult]       = []
        self._trade_counter  = 0

    # ── Fee helpers ───────────────────────────────────────────────

    def _entry_fill(self, price: float, side: str) -> float:
        return price * (1 + self.cfg.slippage) if side == "long" \
               else price * (1 - self.cfg.slippage)

    def _exit_fill(self, price: float, side: str) -> float:
        return price * (1 - self.cfg.slippage) if side == "long" \
               else price * (1 + self.cfg.slippage)

    def _exit_fees(self, exit_price: float, shares: float) -> float:
        notional = exit_price * shares
        sec_fee  = notional * self.cfg.sec_fee_rate
        taf_fee  = min(shares * self.cfg.finra_taf_rate, 8.30)
        return sec_fee + taf_fee

    # ── Position lifecycle ────────────────────────────────────────

    def open_position(self, symbol: str, signal: EntrySignal, sizing: dict) -> Optional[OpenPosition]:
        if signal.direction == "short" and not self.cfg.allow_shorts:
            logger.info(f"{symbol}: short suppressed — allow_shorts=False")
            return None

        self._trade_counter += 1
        trade_id   = f"PAPER-{self._trade_counter:04d}-{symbol}"
        fill_price = self._entry_fill(signal.entry_price, signal.direction)
        shares     = sizing["position_size_shares"]
        notional   = shares * fill_price

        self.balance -= notional

        pos = OpenPosition(
            trade_id    = trade_id,
            symbol      = symbol,
            side        = signal.direction,
            entry_price = fill_price,
            stop_loss   = signal.stop_loss,
            take_profit = signal.take_profit,
            shares      = shares,
            notional    = notional,
            entry_time  = datetime.now(timezone.utc).isoformat(),
        )
        self.positions[trade_id] = pos
        logger.info(
            f"📄 [SIM] {symbol} {signal.direction.upper()} "
            f"{shares:.4f} sh @ ${fill_price:.2f}  "
            f"notional=${notional:.2f}  "
            f"SL=${signal.stop_loss:.2f}  TP=${signal.take_profit:.2f}"
        )
        return pos

    def check_exits(self, current_prices: dict[str, float]) -> list[TradeResult]:
        """Check all open positions against their SL/TP at current prices."""
        closed = []
        for tid, pos in list(self.positions.items()):
            price = current_prices.get(pos.symbol)
            if price is None:
                continue

            exit_price, outcome = None, None
            if pos.side == "long":
                if price <= pos.stop_loss:
                    exit_price, outcome = pos.stop_loss, "sl"
                elif price >= pos.take_profit:
                    exit_price, outcome = pos.take_profit, "tp"
            else:
                if price >= pos.stop_loss:
                    exit_price, outcome = pos.stop_loss, "sl"
                elif price <= pos.take_profit:
                    exit_price, outcome = pos.take_profit, "tp"

            if exit_price:
                result = self._close_position(pos, exit_price, outcome)
                closed.append(result)
                del self.positions[tid]
        return closed

    def _close_position(self, pos: OpenPosition, exit_price: float, outcome: str) -> TradeResult:
        fill_exit = self._exit_fill(exit_price, pos.side)
        fees      = self._exit_fees(fill_exit, pos.shares)

        if pos.side == "long":
            pnl_dollar = pos.shares * fill_exit - pos.notional - fees
        else:
            pnl_dollar = pos.notional - pos.shares * fill_exit - fees

        self.balance += pos.notional + pnl_dollar
        pnl_pct       = pnl_dollar / pos.notional * 100 if pos.notional > 0 else 0
        duration      = int(
            (datetime.now(timezone.utc) -
             datetime.fromisoformat(pos.entry_time)).total_seconds()
        )
        self.rm.update_peak(self.balance)

        result = TradeResult(
            trade_id    = pos.trade_id,
            symbol      = pos.symbol,
            side        = pos.side,
            entry_price = pos.entry_price,
            exit_price  = fill_exit,
            shares      = pos.shares,
            notional    = pos.notional,
            pnl_dollar  = round(pnl_dollar, 4),
            pnl_pct     = round(pnl_pct, 4),
            outcome     = outcome,
            duration_s  = duration,
        )
        icon = "✅" if outcome == "tp" else "❌"
        logger.info(
            f"{icon} [SIM] {pos.symbol} {pos.side.upper()} closed "
            f"@ ${fill_exit:.2f}  PnL ${pnl_dollar:+.2f} ({pnl_pct:+.3f}%)  "
            f"[{outcome.upper()}]  fees=${fees:.4f}"
        )
        self.trade_log.append(result)
        return result

    def close_all(self, current_prices: dict[str, float], reason: str = "manual") -> None:
        """Emergency / EOD close of all open positions."""
        logger.warning(f"⚠️  [SIM] Closing all positions. Reason: {reason}")
        for tid, pos in list(self.positions.items()):
            price = current_prices.get(pos.symbol, pos.entry_price)
            self._close_position(pos, price, reason)
        self.positions.clear()

    @property
    def open_symbols(self) -> set[str]:
        return {p.symbol for p in self.positions.values()}


# ══════════════════════════════════════════════════════════════════
#  MODE 2 — ALPACA REST EXECUTOR
# ══════════════════════════════════════════════════════════════════

class AlpacaExecutor:
    """
    Async REST client for Alpaca Trading + Data APIs.
    Works for both paper (paper-api.alpaca.markets) and live endpoints.
    """

    _TIMEFRAME_MAP = {
        "1Min": "1Min", "5Min": "5Min",
        "15Min": "15Min", "1Hour": "1Hour", "1Day": "1Day",
    }

    def __init__(self, config: MidasConfig):
        self.cfg         = config
        self._api_key    = os.environ.get("ALPACA_API_KEY", "")
        self._api_secret = os.environ.get("ALPACA_API_SECRET", "")
        self._headers    = {
            "APCA-API-KEY-ID":     self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "Content-Type":        "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self.positions: dict[str, OpenPosition] = {}

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self._session = aiohttp.ClientSession(
                headers=self._headers, timeout=timeout
            )
        return self._session

    # ── Account ──────────────────────────────────────────────────

    async def fetch_balance(self) -> float:
        s = await self._sess()
        async with s.get(f"{self.cfg.base_url}/v2/account") as resp:
            resp.raise_for_status()
            data = await resp.json()
        return float(data["equity"])

    async def fetch_buying_power(self) -> float:
        s = await self._sess()
        async with s.get(f"{self.cfg.base_url}/v2/account") as resp:
            resp.raise_for_status()
            data = await resp.json()
        return float(data.get("daytrading_buying_power", data["buying_power"]))

    async def is_pdt_flagged(self) -> bool:
        s = await self._sess()
        async with s.get(f"{self.cfg.base_url}/v2/account") as resp:
            resp.raise_for_status()
            data = await resp.json()
        return bool(data.get("pattern_day_trader", False))

    # ── Market clock ─────────────────────────────────────────────

    async def fetch_clock(self) -> dict:
        s = await self._sess()
        async with s.get(f"{self.cfg.base_url}/v2/clock") as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Data ─────────────────────────────────────────────────────

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
        """Fetch bars for a single symbol (used for per-symbol detail)."""
        tf    = self._TIMEFRAME_MAP.get(timeframe, timeframe)
        end   = datetime.now(timezone.utc)
        start = end - timedelta(minutes=limit * 3)

        s = await self._sess()
        params = {
            "symbols":   symbol,
            "timeframe": tf,
            "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":     limit,
            "feed":      "iex",
            "sort":      "asc",
        }
        async with s.get(f"{self.cfg.alpaca_data_url}/v2/stocks/bars", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        bars = data.get("bars", {}).get(symbol, [])
        if not bars:
            raise ValueError(f"No bars for {symbol} [{timeframe}]")

        df = pd.DataFrame(bars).rename(columns={
            "t": "timestamp", "o": "open", "h": "high",
            "l": "low",       "c": "close","v": "volume",
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.set_index("timestamp", inplace=True)
        return df[["open","high","low","close","volume"]].astype(float).tail(limit)

    # ── Orders ───────────────────────────────────────────────────

    async def submit_bracket_order(
        self,
        symbol:      str,
        side:        str,
        shares:      float,
        stop_loss:   float,
        take_profit: float,
    ) -> dict:
        """
        Submit a native Alpaca bracket (OTOCO) order.
        Entry (market) + TP (limit) + SL (stop-limit) in one atomic request.
        Alpaca manages the cancel-the-other-leg logic automatically.
        """
        if side == "short" and not self.cfg.allow_shorts:
            raise ValueError("Short orders disabled in config (allow_shorts=False)")

        order_side = "buy" if side == "long" else "sell"
        sl_limit   = round(stop_loss * 0.9995, 2) if side == "long" \
                     else round(stop_loss * 1.0005, 2)

        payload = {
            "symbol":        symbol,
            "qty":           str(round(shares, 4)),
            "side":          order_side,
            "type":          "market",
            "time_in_force": "day",
            "order_class":   "bracket",
            "take_profit":   {"limit_price": str(round(take_profit, 2))},
            "stop_loss":     {
                "stop_price":  str(round(stop_loss, 2)),
                "limit_price": str(sl_limit),
            },
        }

        s = await self._sess()
        async with s.post(f"{self.cfg.base_url}/v2/orders", json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                logger.error(f"Order rejected ({resp.status}): {body}")
                return {"error": body, "status": resp.status}
            data = await resp.json()

        order_id = data["id"]
        logger.info(
            f"📨 [{'PAPER' if self.cfg.paper_trading else 'LIVE'}] "
            f"Bracket {side.upper()} {shares:.4f} {symbol}  "
            f"SL=${stop_loss:.2f}  TP=${take_profit:.2f}  id={order_id}"
        )
        return {"entry_id": order_id, "status": data["status"]}

    async def cancel_all_orders(self) -> None:
        s = await self._sess()
        async with s.delete(f"{self.cfg.base_url}/v2/orders") as resp:
            resp.raise_for_status()
        logger.warning("All open orders cancelled.")

    async def flatten_all_positions(self) -> None:
        await self.cancel_all_orders()
        s = await self._sess()
        async with s.delete(f"{self.cfg.base_url}/v2/positions") as resp:
            resp.raise_for_status()
        logger.warning("All positions liquidated.")

    async def fetch_open_positions(self) -> list[dict]:
        s = await self._sess()
        async with s.get(f"{self.cfg.base_url}/v2/positions") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def open_symbols(self) -> set[str]:
        return {p.symbol for p in self.positions.values()}


# Alias kept for backward compatibility
LiveExecutor = AlpacaExecutor
