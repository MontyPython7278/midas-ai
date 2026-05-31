"""
╔══════════════════════════════════════════════════════════════════╗
║                  MIDAS AI — RISK MANAGER                        ║
║   Position sizing · Kill switch · Drawdown guard · Compounding  ║
╚══════════════════════════════════════════════════════════════════╝

CAPITAL PRESERVATION IS THE ONLY RULE THAT CANNOT BE OVERRIDDEN.
Every other module defers to this one.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from midas_config import MidasConfig

logger = logging.getLogger("midas.risk")


# ──────────────────────────────────────────────
#  DATA STRUCTURES
# ──────────────────────────────────────────────

@dataclass
class AccountState:
    equity: float              # Current total account value
    peak_equity: float         # All-time high equity (for drawdown calc)
    day_start_equity: float    # Equity at start of trading day (UTC)
    compounding_day: int = 0   # Days elapsed (for A = P(1+r)^n)
    halted: bool = False       # Kill-switch state
    halt_reason: str = ""
    last_reset: str = ""       # ISO timestamp of last daily reset


@dataclass
class TradeSignal:
    symbol: str
    side: str                  # "buy" | "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size_base: float  # e.g., BTC quantity
    position_size_quote: float # e.g., USDT notional
    risk_amount: float         # Dollar risk on this trade
    risk_pct: float            # % of account risked


# ──────────────────────────────────────────────
#  RISK MANAGER CLASS
# ──────────────────────────────────────────────

class RiskManager:
    """
    Central risk authority.  All trade decisions must pass through here
    before any order is sent to the exchange.
    """

    def __init__(self, config: MidasConfig, state_file: str = None):
        self.cfg = config
        self.state_file = Path(state_file or config.state_file)
        self.state = self._load_state()

    # ── State persistence ──────────────────────────────────────────

    def _load_state(self) -> AccountState:
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text())
                return AccountState(**raw)
            except Exception as e:
                logger.warning(f"State file corrupt, reinitializing: {e}")
        equity = self.cfg.initial_capital
        now_iso = datetime.now(timezone.utc).isoformat()
        return AccountState(
            equity=equity,
            peak_equity=equity,
            day_start_equity=equity,
            last_reset=now_iso,
        )

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(self.state.__dict__, indent=2)
        )

    # ── Daily reset (call at UTC 00:00) ───────────────────────────

    def daily_reset(self, current_equity: float) -> None:
        """
        Called once per UTC day.  Records the new day-start equity
        and lifts the kill-switch (unless a hard stop is still in effect).
        """
        self.state.day_start_equity = current_equity
        self.state.equity = current_equity
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.last_reset = datetime.now(timezone.utc).isoformat()
        self.save_state()
        logger.info(f"Daily reset complete. Day-start equity: ${current_equity:,.2f}")

    # ── Kill switch ────────────────────────────────────────────────

    def check_kill_switch(self, current_equity: float) -> bool:
        """
        HARD RULE: If equity has dropped ≥ DAILY_DRAWDOWN_LIMIT from
        the day-start equity, halt ALL trading for the rest of the day.

        Returns True if trading should be HALTED.
        """
        self.state.equity = current_equity
        day_drawdown = (self.state.day_start_equity - current_equity) / self.state.day_start_equity

        if day_drawdown >= self.cfg.daily_dd_limit:
            if not self.state.halted:
                self.state.halted = True
                self.state.halt_reason = (
                    f"Daily drawdown {day_drawdown:.2%} exceeded "
                    f"limit of {self.cfg.daily_dd_limit:.2%} at "
                    f"{datetime.now(timezone.utc).isoformat()}"
                )
                self.save_state()
                logger.critical(
                    f"🚨 KILL SWITCH FIRED — {self.state.halt_reason}"
                )
                self._send_alert(f"🚨 MIDAS KILL SWITCH: {self.state.halt_reason}")
            return True

        return self.state.halted  # Carry forward if already halted

    # ── Position sizing (Kelly-inspired, capped) ──────────────────

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_price: float,
        current_equity: float,
    ) -> dict:
        """
        Dynamic position sizing.  Risk is capped at MAX_RISK_PER_TRADE
        of current equity per trade.

        Dollar risk per trade = equity × max_risk_per_trade
        Stop distance (%) = abs(entry - stop) / entry
        Position size (quote) = dollar_risk / stop_distance_pct

        Why not full Kelly? Kelly maximises long-run log-wealth but
        requires perfect knowledge of true win rate — which we don't
        have.  Half-Kelly (or the hard cap approach used here) gives
        ~75% of Kelly's growth rate with far lower drawdown variance.
        """
        if current_equity <= 0 or entry_price <= 0 or stop_loss_price <= 0:
            raise ValueError("Invalid inputs for position sizing.")

        stop_distance_pct = abs(entry_price - stop_loss_price) / entry_price
        if stop_distance_pct < 0.0005:
            # Stop is unrealistically tight — reject to avoid oversizing
            raise ValueError(
                f"Stop distance {stop_distance_pct:.4%} is below minimum 0.05%"
            )

        max_risk_dollar  = current_equity * self.cfg.max_risk_per_trade
        position_quote   = max_risk_dollar / stop_distance_pct
        position_base    = position_quote / entry_price

        # Hard cap: notional ≤ 20% of equity (prevent leverage blowup)
        max_notional     = current_equity * 0.20
        if position_quote > max_notional:
            position_quote = max_notional
            position_base  = position_quote / entry_price
            actual_risk    = position_quote * stop_distance_pct
        else:
            actual_risk = max_risk_dollar

        return {
            "position_size_base":  round(position_base, 6),
            "position_size_quote": round(position_quote, 2),
            "risk_amount":         round(actual_risk, 2),
            "risk_pct":            round(actual_risk / current_equity * 100, 4),
            "stop_distance_pct":   round(stop_distance_pct * 100, 4),
        }

    # ── Trade gate ────────────────────────────────────────────────

    def approve_trade(self, signal: TradeSignal, open_positions: int) -> tuple[bool, str]:
        """
        Final approval gate before any order is submitted.
        Returns (approved: bool, reason: str).
        """
        if self.state.halted:
            return False, f"Bot halted: {self.state.halt_reason}"
        if open_positions >= self.cfg.max_open_trades:
            return False, f"Max open trades ({self.cfg.max_open_trades}) reached"
        if signal.risk_pct > self.cfg.max_risk_per_trade * 100:
            return False, f"Trade risk {signal.risk_pct:.3f}% exceeds limit {self.cfg.max_risk_per_trade*100:.3f}%"
        if signal.position_size_quote < 10:
            return False, f"Position too small: ${signal.position_size_quote:.2f}"
        return True, "Approved"

    # ── Compounding module ────────────────────────────────────────

    def get_compounded_target(self, days_elapsed: Optional[int] = None) -> float:
        """
        A = P × (1 + r)^n
        Where P = initial_capital, r = daily_rate, n = days elapsed.

        The compounding target is the EXPECTED equity after n days.
        We use this to gauge performance, not to force reckless trading.

        Risk-of-ruin guard: if current equity drops below
        ROR_THRESHOLD × peak_equity, compounding is suspended until
        recovery — protecting against the "re-leveraging into losses" trap.
        """
        n = days_elapsed if days_elapsed is not None else self.state.compounding_day
        P = self.cfg.initial_capital
        r = self.cfg.compounding_rate
        target = P * ((1 + r) ** n)

        ror_floor = self.state.peak_equity * self.cfg.ror_threshold
        if self.state.equity < ror_floor:
            logger.warning(
                f"⚠️  Equity ${self.state.equity:,.2f} below RoR floor "
                f"${ror_floor:,.2f}. Compounding suspended."
            )
            return self.state.equity  # Don't increase target size during drawdown

        return round(target, 2)

    def update_peak(self, current_equity: float) -> None:
        if current_equity > self.state.peak_equity:
            self.state.peak_equity = current_equity
            self.state.compounding_day += 1
            self.save_state()

    # ── Alert helper ──────────────────────────────────────────────

    def _send_alert(self, message: str) -> None:
        """Send Telegram alert (non-blocking best-effort)."""
        import threading
        from midas_config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return

        def _post() -> None:
            try:
                import requests
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                    timeout=5,
                )
            except Exception as e:
                logger.debug(f"Alert failed (non-critical): {e}")

        threading.Thread(target=_post, daemon=True).start()
