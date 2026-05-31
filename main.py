"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    MIDAS AI — MAIN ORCHESTRATOR (ALPACA)                    ║
║                            main.py — Multi-symbol                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  TICK FLOW                                                                   ║
║  ─────────────────────────────────────────────────────────────────────────  ║
║  1.  Heartbeat ping (background task, every 60 s)                            ║
║  2.  Market hours guard  →  NYSE open? If not, sleep.                        ║
║  3.  New session?        →  daily_reset() + PDT reset + screener reset       ║
║  4.  Kill-switch check   →  halt if daily DD ≥ 3%                            ║
║  5.  PDT guard           →  block new entries if 3-trade limit reached       ║
║  6.  EOD flatten         →  close all positions before 16:00 ET              ║
║  7.  Opening buffer      →  skip entries before 09:35 ET                     ║
║  8.  Screener scan       →  bulk fetch + rank signals across 30 symbols      ║
║  9.  Exit check          →  SL/TP on existing positions                      ║
║  10. Risk gate           →  size + approve + submit for top-ranked signal    ║
║  11. Peak update + state persist                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

# --- SSL Certificate Fix for Python 3.14 on macOS ---
import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['SSL_CERT_DIR'] = os.path.dirname(certifi.where())
# -----------------------------------------------------

import asyncio
import logging
import logging.handlers
import signal
import sys
import time
import traceback
from collections import deque
from datetime import date, datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

from midas_config import MidasConfig, HEARTBEAT_URL, HEARTBEAT_INTERVAL
from midas_risk import RiskManager, TradeSignal
from midas_executor import AlpacaExecutor, PaperTradingEngine, OpenPosition
from midas_screener import SymbolScreener, RankedSignal

ET = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING  (Mac-compatible path from config)
# ══════════════════════════════════════════════════════════════════════════════

def _configure_logging(log_file: str) -> logging.Logger:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt  = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return logging.getLogger("midas.main")


# ══════════════════════════════════════════════════════════════════════════════
#  HEARTBEAT MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class HeartbeatMonitor:
    def __init__(self, url: str, interval: int, log: logging.Logger):
        self.url      = url
        self.interval = interval
        self.log      = log
        self._task: Optional[asyncio.Task] = None

    async def _loop(self) -> None:
        if not self.url:
            return
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(
                        self.url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        self.log.debug(f"💓 Heartbeat {r.status}")
                except Exception as e:
                    self.log.warning(f"Heartbeat failed: {e}")
                await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="heartbeat")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


# ══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def send_telegram(message: str, log: logging.Logger) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"🤖 MIDAS AI\n{message}"},
                timeout=aiohttp.ClientTimeout(total=8),
            )
    except Exception as e:
        log.debug(f"Telegram failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET HOURS GUARD
# ══════════════════════════════════════════════════════════════════════════════

class MarketHoursGuard:
    """
    Authoritative NYSE market hours check using Alpaca's /v2/clock endpoint.
    Holiday-aware. Falls back to time-based check if API is unavailable.
    All times in America/New_York (ET), handling DST automatically.
    """

    def __init__(self, cfg: MidasConfig, executor):
        self.cfg      = cfg
        self.executor = executor
        self._is_open_cache: Optional[bool] = None
        self._cache_ts: float = 0.0

    def _now_et(self) -> datetime:
        return datetime.now(ET)

    async def is_market_open(self) -> bool:
        now_ts = time.monotonic()
        if now_ts - self._cache_ts < 55 and self._is_open_cache is not None:
            return self._is_open_cache

        if isinstance(self.executor, AlpacaExecutor):
            try:
                clock = await self.executor.fetch_clock()
                self._is_open_cache = bool(clock.get("is_open", False))
                self._cache_ts = now_ts
                return self._is_open_cache
            except Exception as e:
                logging.getLogger("midas.market").warning(
                    f"Clock API failed, using time fallback: {e}"
                )

        t   = self._now_et().time()
        dow = self._now_et().weekday()
        if dow >= 5:
            self._is_open_cache = False
        else:
            self._is_open_cache = dt_time(9, 30) <= t < dt_time(16, 0)
        self._cache_ts = now_ts
        return self._is_open_cache

    def is_in_opening_buffer(self) -> bool:
        t = self._now_et().time()
        return dt_time(9, 30) <= t < dt_time(9, 30 + self.cfg.opening_buffer_mins)

    def is_in_eod_buffer(self) -> bool:
        t     = self._now_et().time()
        mins  = 60 - self.cfg.no_entry_buffer_mins   # e.g. 45 for 15-min buffer
        return t >= dt_time(15, mins)

    def minutes_to_close(self) -> float:
        now_et   = self._now_et()
        close_et = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return max(0.0, (close_et - now_et).total_seconds() / 60)

    def is_new_session(self, last_session_date: Optional[date]) -> bool:
        return self._now_et().date() != last_session_date


# ══════════════════════════════════════════════════════════════════════════════
#  PDT GUARD
# ══════════════════════════════════════════════════════════════════════════════

class PDTGuard:
    """
    Tracks day trades (round-trips in same session) against the FINRA
    3-in-5-sessions limit. Auto-lifts at $25,000 equity.
    """

    def __init__(self, cfg: MidasConfig):
        self.cfg = cfg
        self._session_log: deque[tuple[date, int]] = deque(maxlen=5)
        self._current_date:  Optional[date] = None
        self._current_count: int = 0

    def new_session(self, session_date: date) -> None:
        if self._current_date is not None:
            self._session_log.append((self._current_date, self._current_count))
        self._current_date  = session_date
        self._current_count = 0

    def record_day_trade(self) -> None:
        self._current_count += 1

    def rolling_count(self) -> int:
        return sum(c for _, c in self._session_log) + self._current_count

    def can_trade(self, equity: float) -> tuple[bool, str]:
        if equity >= self.cfg.pdt_min_equity:
            return True, f"PDT exempt (equity ${equity:,.0f} ≥ $25k)"
        rolling = self.rolling_count()
        if rolling >= self.cfg.max_day_trades:
            return False, (
                f"PDT LIMIT: {rolling}/{self.cfg.max_day_trades} "
                f"day trades used. Entries blocked."
            )
        return True, f"PDT ok ({rolling}/{self.cfg.max_day_trades} used)"


# ══════════════════════════════════════════════════════════════════════════════
#  MIDAS BOT
# ══════════════════════════════════════════════════════════════════════════════

class MidasBot:

    _BACKOFF = [5, 10, 30, 60, 120, 300]

    def __init__(self, config: MidasConfig):
        self.cfg = config
        config.ensure_directories()
        self.log = _configure_logging(config.log_file)
        self.rm  = RiskManager(config)

        if config.paper_trading:
            self.executor: PaperTradingEngine | AlpacaExecutor = \
                PaperTradingEngine(config, self.rm)
            self.log.info("🧻 PAPER TRADING MODE — internal simulation")
        else:
            self.executor = AlpacaExecutor(config)
            self.log.warning("💰 ALPACA LIVE MODE — real capital at risk")

        self.market_guard = MarketHoursGuard(config, self.executor)
        self.pdt_guard    = PDTGuard(config)
        self.screener     = SymbolScreener(config)
        self.heartbeat    = HeartbeatMonitor(HEARTBEAT_URL, HEARTBEAT_INTERVAL, self.log)

        self._running            = False
        self._shutdown_event     = asyncio.Event()
        self._last_session_date: Optional[date] = None
        self._eod_flattened      = False
        self._consecutive_errors = 0
        self._tick_count         = 0

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(self._graceful_shutdown(s))
            )

    async def _graceful_shutdown(self, sig) -> None:
        self.log.warning(f"⛔ {sig.name} — shutting down…")
        self._running = False
        self._shutdown_event.set()

        if isinstance(self.executor, PaperTradingEngine):
            if self.executor.positions:
                # Build last-price dict from whatever we can get
                prices = {sym: 0.0 for sym in self.executor.open_symbols}
                self.executor.close_all(prices, reason="graceful_shutdown")
        else:
            try:
                await self.executor.cancel_all_orders()
            except Exception as e:
                self.log.error(f"Cancel orders failed: {e}")
            self.log.warning(
                "LIVE: positions left on-exchange with bracket protection. "
                "Review on Alpaca dashboard before next session."
            )

        self.rm.save_state()
        self.log.info(f"State saved → {self.rm.state_file}")
        self.heartbeat.stop()
        if isinstance(self.executor, AlpacaExecutor):
            await self.executor.close()
        self.log.info("✅ Shutdown complete.")

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def _handle_new_session(self, equity: float) -> None:
        today = datetime.now(ET).date()
        self.log.info(f"📅 New session: {today}")
        self.rm.daily_reset(equity)
        self.pdt_guard.new_session(today)
        self.screener.new_session()
        self._eod_flattened     = False
        self._last_session_date = today

        pdt_ok, pdt_msg = self.pdt_guard.can_trade(equity)
        await send_telegram(
            f"📅 Session open: {today}\n"
            f"Equity: ${equity:,.2f}\n"
            f"Target: ${self.rm.get_compounded_target():,.2f}\n"
            f"{pdt_msg}",
            self.log,
        )

    # ── EOD flatten ──────────────────────────────────────────────────────────

    async def _eod_flatten(self) -> None:
        if self._eod_flattened:
            return

        if isinstance(self.executor, PaperTradingEngine):
            if not self.executor.positions:
                self._eod_flattened = True
                return
            # Try to get last prices; fall back to entry price if unavailable
            prices: dict[str, float] = {}
            data_ex = AlpacaExecutor(self.cfg)
            try:
                for sym in self.executor.open_symbols:
                    try:
                        df = await data_ex.fetch_ohlcv(sym, self.cfg.timeframe, limit=2)
                        prices[sym] = float(df["close"].iloc[-1])
                    except Exception:
                        prices[sym] = 0.0   # Will use entry price in close_position
            finally:
                await data_ex.close()
            n = len(self.executor.positions)
            self.executor.close_all(prices, reason="eod_flatten")
            self.log.warning(f"📉 EOD: closed {n} position(s) before market close.")
            await send_telegram(f"📉 EOD flatten: {n} position(s) closed.", self.log)
        else:
            live_pos = await self.executor.fetch_open_positions()
            if live_pos:
                await self.executor.flatten_all_positions()
                self.log.warning(f"📉 EOD: liquidated {len(live_pos)} Alpaca position(s).")
                await send_telegram(
                    f"📉 EOD: {len(live_pos)} position(s) liquidated on Alpaca.", self.log
                )

        self._eod_flattened = True

    # ── Core tick ─────────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        self._tick_count += 1
        now_et = datetime.now(ET)
        self.log.info(
            f"──── TICK #{self._tick_count:05d}  "
            f"{now_et.strftime('%H:%M:%S ET')}  "
            f"Watching {len(self.cfg.watchlist)} symbols ────"
        )

        # ── 1. Current equity ─────────────────────────────────────────────────
        equity = (self.executor.balance
                  if isinstance(self.executor, PaperTradingEngine)
                  else await self.executor.fetch_balance())
        self.log.info(f"Equity: ${equity:,.2f}  Peak: ${self.rm.state.peak_equity:,.2f}")

        # ── 2. Market hours guard ─────────────────────────────────────────────
        if not await self.market_guard.is_market_open():
            self.log.info("🔴 NYSE closed — tick skipped.")
            return

        # ── 3. New session ────────────────────────────────────────────────────
        if self.market_guard.is_new_session(self._last_session_date):
            await self._handle_new_session(equity)

        # ── 4. Kill-switch ────────────────────────────────────────────────────
        if self.rm.check_kill_switch(equity):
            if isinstance(self.executor, PaperTradingEngine):
                prices = {sym: 0.0 for sym in self.executor.open_symbols}
                self.executor.close_all(prices, reason="kill_switch")
            else:
                await self.executor.cancel_all_orders()
                await self.executor.flatten_all_positions()
            self.log.warning("🚨 Kill switch ACTIVE — tick skipped.")
            return

        # ── 5. PDT guard ──────────────────────────────────────────────────────
        pdt_ok, pdt_msg = self.pdt_guard.can_trade(equity)
        if not pdt_ok:
            self.log.warning(f"🛑 {pdt_msg}")

        # ── 6. EOD flatten ────────────────────────────────────────────────────
        mins_left = self.market_guard.minutes_to_close()
        if mins_left <= self.cfg.no_entry_buffer_mins:
            await self._eod_flatten()
            self.log.info(f"⏰ EOD buffer ({mins_left:.0f} min left). No new entries.")
            return

        # ── 7. Opening buffer ─────────────────────────────────────────────────
        if self.market_guard.is_in_opening_buffer():
            self.log.info(
                f"⏳ Opening buffer (first {self.cfg.opening_buffer_mins} min). "
                "Letting price discovery settle."
            )
            return

        # ── 8. Exit check (paper) / live reconciliation ───────────────────────
        if isinstance(self.executor, PaperTradingEngine) and self.executor.positions:
            # Fetch last price for each held symbol for exit checking
            current_prices: dict[str, float] = {}
            data_ex = AlpacaExecutor(self.cfg)
            try:
                for sym in list(self.executor.open_symbols):
                    try:
                        df = await data_ex.fetch_ohlcv(sym, self.cfg.timeframe, limit=2)
                        current_prices[sym] = float(df["close"].iloc[-1])
                    except Exception as e:
                        self.log.debug(f"Price fetch failed for {sym}: {e}")
            finally:
                await data_ex.close()

            closed = self.executor.check_exits(current_prices)
            for t in closed:
                icon = "✅" if t.outcome == "tp" else "❌"
                msg  = (
                    f"{icon} {t.symbol} {t.side.upper()} [{t.outcome.upper()}]\n"
                    f"Entry ${t.entry_price:.2f} → Exit ${t.exit_price:.2f}\n"
                    f"{t.shares:.4f} sh  PnL ${t.pnl_dollar:+.2f} ({t.pnl_pct:+.3f}%)"
                )
                self.log.info(msg)
                await send_telegram(msg, self.log)
                self.pdt_guard.record_day_trade()
            equity = self.executor.balance

        elif isinstance(self.executor, AlpacaExecutor):
            # Reconcile local mirror with Alpaca's actual positions
            try:
                live = await self.executor.fetch_open_positions()
                live_syms = {p["symbol"] for p in live}
                for tid in list(self.executor.positions.keys()):
                    pos = self.executor.positions[tid]
                    if pos.symbol not in live_syms:
                        self.log.info(f"{pos.symbol} position {tid} closed on Alpaca.")
                        self.pdt_guard.record_day_trade()
                        del self.executor.positions[tid]
            except Exception as e:
                self.log.warning(f"Position reconciliation failed: {e}")

        # ── 9. Screener scan ──────────────────────────────────────────────────
        open_count   = len(self.executor.positions)
        open_symbols = self.executor.open_symbols

        if open_count >= self.cfg.max_open_trades:
            self.log.info(
                f"Max open trades ({self.cfg.max_open_trades}) reached. "
                "No new entries this tick."
            )
        elif not pdt_ok:
            self.log.info("PDT limit reached. No new entries this tick.")
        else:
            ranked_signals = await self.screener.scan(open_symbols)

            # Try signals in ranked order until one passes the risk gate
            for ranked in ranked_signals:
                if len(self.executor.positions) >= self.cfg.max_open_trades:
                    break
                submitted = await self._try_submit(ranked, equity)
                if submitted:
                    break   # One entry per tick — re-evaluate next minute

        # ── 10. Peak update + persist ──────────────────────────────────────────
        final_equity = (self.executor.balance
                        if isinstance(self.executor, PaperTradingEngine)
                        else await self.executor.fetch_balance())
        self.rm.update_peak(final_equity)
        self.rm.save_state()

        target = self.rm.get_compounded_target()
        gap    = ((final_equity / target) - 1) * 100 if target > 0 else 0
        self.log.info(
            f"Equity ${final_equity:,.2f}  |  "
            f"Target ${target:,.2f}  |  "
            f"Gap {gap:+.2f}%  |  "
            f"PDT {self.pdt_guard.rolling_count()}/{self.cfg.max_day_trades}  |  "
            f"~{mins_left:.0f} min to close  |  "
            f"Open: {len(self.executor.positions)}"
        )

    # ── Order attempt ──────────────────────────────────────────────────────────

    async def _try_submit(self, ranked: RankedSignal, equity: float) -> bool:
        """
        Run the risk gate and submit an order for the given ranked signal.
        Returns True if order was submitted successfully.
        """
        sig = ranked.signal
        sym = ranked.symbol

        try:
            sizing = self.rm.calculate_position_size(
                entry_price     = sig.entry_price,
                stop_loss_price = sig.stop_loss,
                current_equity  = equity,
            )
        except ValueError as e:
            self.log.info(f"{sym}: sizing rejected — {e}")
            return False

        shares = round(sizing["position_size_quote"] / sig.entry_price, 4)
        if shares < 0.0001:
            self.log.info(f"{sym}: position too small ({shares} sh) — skipped")
            return False
        sizing["position_size_shares"] = shares

        trade_sig = TradeSignal(
            symbol             = sym,
            side               = sig.direction,
            entry_price        = sig.entry_price,
            stop_loss          = sig.stop_loss,
            take_profit        = sig.take_profit,
            position_size_base = shares,
            position_size_quote= sizing["position_size_quote"],
            risk_amount        = sizing["risk_amount"],
            risk_pct           = sizing["risk_pct"],
        )

        approved, reason = self.rm.approve_trade(trade_sig, len(self.executor.positions))
        if not approved:
            self.log.info(f"{sym}: trade rejected — {reason}")
            return False

        self.log.info(
            f"📨 {sym} {sig.direction.upper()}  "
            f"{shares:.4f} sh @ ~${sig.entry_price:.2f}  "
            f"notional=${sizing['position_size_quote']:.2f}  "
            f"risk={sizing['risk_pct']:.3f}%  "
            f"score={ranked.score:.3f}"
        )

        if isinstance(self.executor, PaperTradingEngine):
            pos     = self.executor.open_position(sym, sig, sizing)
            success = pos is not None
            oid     = pos.trade_id if pos else "REJECTED"
        else:
            result  = await self.executor.submit_bracket_order(
                symbol      = sym,
                side        = sig.direction,
                shares      = shares,
                stop_loss   = sig.stop_loss,
                take_profit = sig.take_profit,
            )
            success = "entry_id" in result and "error" not in result
            oid     = result.get("entry_id", result.get("error", "ERROR"))

            if success:
                self.executor.positions[oid] = OpenPosition(
                    trade_id        = oid,
                    symbol          = sym,
                    side            = sig.direction,
                    entry_price     = sig.entry_price,
                    stop_loss       = sig.stop_loss,
                    take_profit     = sig.take_profit,
                    shares          = shares,
                    notional        = sizing["position_size_quote"],
                    entry_time      = datetime.now(timezone.utc).isoformat(),
                    alpaca_order_id = oid,
                )

        if success:
            msg = (
                f"{'📄 SIM' if self.cfg.paper_trading else '📈 ALPACA'} ORDER\n"
                f"{sym} {sig.direction.upper()}  {shares:.4f} sh\n"
                f"Entry ≈ ${sig.entry_price:.2f}\n"
                f"SL ${sig.stop_loss:.2f}  TP ${sig.take_profit:.2f}\n"
                f"Notional ${sizing['position_size_quote']:.2f}  "
                f"Risk {sizing['risk_pct']:.3f}%\n"
                f"Reason: {sig.reason}"
            )
            self.log.info(msg)
            await send_telegram(msg, self.log)
        else:
            self.log.error(f"{sym}: order failed — {oid}")

        return success

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        self._register_signal_handlers()
        self.heartbeat.start()

        msg = (
            f"🚀 MIDAS AI — ALPACA {'PAPER' if self.cfg.paper_trading else '⚠️ LIVE'}\n"
            f"Capital:   ${self.rm.state.equity:,.2f}\n"
            f"Symbols:   {len(self.cfg.watchlist)} in watchlist\n"
            f"Shorts:    {'ENABLED' if self.cfg.allow_shorts else 'DISABLED'}\n"
            f"PDT limit: {self.cfg.max_day_trades}/5 sessions\n"
            f"DD limit:  {self.cfg.daily_dd_limit:.0%}"
        )
        self.log.info(msg)
        await send_telegram(msg, self.log)

        while self._running:
            now         = time.time()
            next_minute = (now // 60 + 1) * 60
            wait        = next_minute - now

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                pass

            if not self._running:
                break

            t0 = time.monotonic()
            try:
                await self._tick()
                self._consecutive_errors = 0

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                self._consecutive_errors += 1
                backoff = self._BACKOFF[min(self._consecutive_errors - 1,
                                            len(self._BACKOFF) - 1)]
                self.log.error(
                    f"❌ TICK ERROR #{self._consecutive_errors} "
                    f"(backoff {backoff}s)\n{traceback.format_exc()}"
                )
                if self._consecutive_errors == 1:
                    await send_telegram(
                        f"🔴 TICK ERROR\n{type(exc).__name__}: {exc}\n"
                        f"Backoff {backoff}s then retry.",
                        self.log,
                    )
                if self._consecutive_errors >= len(self._BACKOFF):
                    self.rm.state.halted      = True
                    self.rm.state.halt_reason = "Auto-halt: too many consecutive errors"
                    self.rm.save_state()
                    await send_telegram("💀 AUTO-HALT. Manual review required.", self.log)
                    await self._shutdown_event.wait()
                    break
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass

            finally:
                self.log.debug(f"Tick completed in {time.monotonic()-t0:.3f}s")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _paper_env = os.getenv("PAPER_TRADING", "1").strip().lower()
    paper_mode = _paper_env not in ("0", "false", "no", "off")

    config = MidasConfig(paper_trading=paper_mode)
    bot    = MidasBot(config)

    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_API_SECRET"):
        print(
            "\n⚠️  ALPACA_API_KEY and ALPACA_API_SECRET are not set.\n"
            "   The bot needs these even in paper mode to fetch market data.\n"
            "   Get your paper keys at:\n"
            "   https://app.alpaca.markets/paper/dashboard/overview\n"
            "   Then follow README.md → Step 3 to set them up.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
