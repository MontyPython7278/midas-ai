"""
╔══════════════════════════════════════════════════════════════════╗
║                  MIDAS AI — BACKTESTER                          ║
║   MDD · Profit Factor · Sharpe Ratio · Walk-Forward Analysis   ║
╚══════════════════════════════════════════════════════════════════╝

BACKTESTING PHILOSOPHY
──────────────────────
A backtest is NOT a profit guarantee. It is a *falsification tool*.
Its job is to KILL bad strategies before they kill your capital.

DATA REQUIREMENTS
─────────────────
• Minimum: 3 years of 1m OHLCV tick data (~1.5M candles for BTC/USDT)
• Source: Binance Vision (https://data.binance.vision) — free, reliable
• Format: CSV with columns timestamp,open,high,low,close,volume

WALK-FORWARD ANALYSIS (WFA)
────────────────────────────
WFA prevents overfitting by never testing on the same data used to
optimise parameters. The procedure:

  1. Split data into N equal "anchored" windows, e.g.:
       IN-SAMPLE (IS):    months 1-18  → optimise ATR mult, RSI thresholds
       OUT-OF-SAMPLE (OOS): months 19-24 → test with IS-optimised params
       Then roll forward:
       IS: months 7-24, OOS: months 25-30 … and repeat

  2. For each window, grid-search over:
       atr_stop_mult ∈ [1.0, 1.5, 2.0]
       atr_target_mult ∈ [1.5, 2.25, 3.0]
       rsi_oversold ∈ [30, 35, 40]

  3. Compare IS Sharpe vs OOS Sharpe.
       • IS Sharpe >> OOS Sharpe → strategy is overfit → discard
       • IS Sharpe ≈ OOS Sharpe (within 30%) → robust → proceed

CRITICAL: If MDD in backtest > 10%, the 1% daily target is
mathematically untenable. One bad week can erase 10+ days of gains.
"""

import logging
from dataclasses import dataclass, field
from itertools import product
from typing import Optional

import numpy as np
import pandas as pd

from midas_config import MidasConfig
from midas_strategy import add_indicators, generate_signal

logger = logging.getLogger("midas.backtest")

RISK_FREE_RATE_ANNUAL = 0.05   # 5% annualized risk-free rate for Sharpe


# ──────────────────────────────────────────────
#  RESULT DATACLASS
# ──────────────────────────────────────────────

@dataclass
class BacktestResult:
    total_trades:    int
    winning_trades:  int
    losing_trades:   int
    win_rate:        float
    avg_win_pct:     float
    avg_loss_pct:    float
    profit_factor:   float
    max_drawdown:    float     # as fraction, e.g. 0.08 = 8%
    sharpe_ratio:    float
    total_return:    float     # as fraction
    final_equity:    float
    trades_df:       pd.DataFrame = field(default_factory=pd.DataFrame)

    def summary(self) -> str:
        lines = [
            "═" * 55,
            "  MIDAS AI — BACKTEST RESULTS",
            "═" * 55,
            f"  Trades:         {self.total_trades}",
            f"  Win Rate:       {self.win_rate:.1%}",
            f"  Avg Win:        {self.avg_win_pct:.3%}",
            f"  Avg Loss:       {self.avg_loss_pct:.3%}",
            f"  Profit Factor:  {self.profit_factor:.3f}  {'✅' if self.profit_factor > 1.5 else '⚠️'}",
            f"  Max Drawdown:   {self.max_drawdown:.2%}  {'✅' if self.max_drawdown < 0.10 else '🚨 DANGER'}",
            f"  Sharpe Ratio:   {self.sharpe_ratio:.3f}  {'✅' if self.sharpe_ratio > 1.0 else '⚠️'}",
            f"  Total Return:   {self.total_return:.2%}",
            f"  Final Equity:   ${self.final_equity:,.2f}",
            "═" * 55,
        ]
        return "\n".join(lines)


# ──────────────────────────────────────────────
#  VECTORISED BACKTESTING ENGINE
# ──────────────────────────────────────────────

class Backtester:
    """
    Event-driven backtest on historical OHLCV data.

    Uses a bar-by-bar simulation (not fully vectorised for clarity)
    to correctly model: slippage on fill, exit priority at open,
    and the kill-switch drawdown rule.
    """

    def __init__(self, config: MidasConfig, initial_capital: float = 10_000.0):
        self.cfg = config
        self.initial_capital = initial_capital

    def run(self, df: pd.DataFrame) -> BacktestResult:
        """
        Run a full backtest on a prepared DataFrame.
        `df` must have OHLCV columns and a UTC DatetimeIndex.
        add_indicators() is called internally.
        """
        df = add_indicators(df, self.cfg)
        return self._simulate(df)

    def _simulate(self, df: pd.DataFrame) -> BacktestResult:
        equity       = self.initial_capital
        peak_equity  = equity
        day_start_eq = equity
        current_date = df.index[0].date()

        position     = None   # {side, entry, sl, tp, size_q}
        trades       = []
        equity_curve = [equity]

        fee_slip = self.cfg.fee_rate + self.cfg.slippage  # one-side cost

        for i in range(2, len(df)):
            row      = df.iloc[i]
            prev_row = df.iloc[i - 1]
            bar_date = df.index[i].date()

            # ── Daily reset logic ──────────────────────────────────
            if bar_date != current_date:
                day_start_eq = equity
                current_date = bar_date

            # ── Kill-switch check ──────────────────────────────────
            daily_dd = (day_start_eq - equity) / day_start_eq if day_start_eq > 0 else 0
            if daily_dd >= self.cfg.daily_dd_limit:
                if position:
                    # Close at open of this bar (simulates market order)
                    trades.append(self._close_trade(position, row["open"], "kill", fee_slip))
                    equity += trades[-1]["pnl"]
                    position = None
                equity_curve.append(equity)
                continue  # No new trades today

            # ── Check exits on open bar ────────────────────────────
            if position:
                exit_price, outcome = self._check_exit_on_bar(position, row)
                if exit_price:
                    trade = self._close_trade(position, exit_price, outcome, fee_slip)
                    equity += trade["pnl"]
                    trades.append(trade)
                    position = None
                    peak_equity = max(peak_equity, equity)

            # ── New entry signal ───────────────────────────────────
            if position is None:
                sig = generate_signal(row, prev_row, self.cfg)
                if sig.direction != "none":
                    sizing = self._size_position(sig.entry_price, sig.stop_loss, equity)
                    if sizing["position_size_quote"] >= 10:
                        entry_fill = sig.entry_price * (1 + fee_slip if sig.direction == "long"
                                                        else 1 - fee_slip)
                        position = {
                            "side":    sig.direction,
                            "entry":   entry_fill,
                            "sl":      sig.stop_loss,
                            "tp":      sig.take_profit,
                            "size_q":  sizing["position_size_quote"],
                            "bar_idx": i,
                        }

            equity_curve.append(equity)

        # ── Compute metrics ────────────────────────────────────────
        return self._compute_metrics(trades, equity_curve)

    def _check_exit_on_bar(self, pos: dict, row: pd.Series) -> tuple:
        """Check if SL or TP was touched during this bar."""
        lo, hi = row["low"], row["high"]
        if pos["side"] == "long":
            if lo <= pos["sl"]:  return pos["sl"], "sl"
            if hi >= pos["tp"]:  return pos["tp"], "tp"
        else:
            if hi >= pos["sl"]:  return pos["sl"], "sl"
            if lo <= pos["tp"]:  return pos["tp"], "tp"
        return None, None

    def _close_trade(self, pos: dict, exit_price: float, outcome: str, fee_slip: float) -> dict:
        fee_exit = exit_price * fee_slip
        if pos["side"] == "long":
            pnl = (exit_price - fee_exit - pos["entry"]) / pos["entry"] * pos["size_q"]
        else:
            pnl = (pos["entry"] - exit_price - fee_exit) / pos["entry"] * pos["size_q"]
        return {"side": pos["side"], "outcome": outcome, "pnl": pnl,
                "entry": pos["entry"], "exit": exit_price, "size_q": pos["size_q"]}

    def _size_position(self, entry: float, stop: float, equity: float) -> dict:
        stop_dist = abs(entry - stop) / entry
        if stop_dist < 0.0005:
            return {"position_size_quote": 0}
        risk_dollar  = equity * self.cfg.max_risk_per_trade
        pos_quote    = min(risk_dollar / stop_dist, equity * 0.20)
        return {"position_size_quote": pos_quote}

    def _compute_metrics(self, trades: list, equity_curve: list) -> BacktestResult:
        if not trades:
            logger.warning("No trades executed in backtest period.")
            return BacktestResult(0,0,0,0,0,0,0,0,0,0, self.initial_capital)

        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]

        gross_profit = sum(t["pnl"] for t in wins)   or 0
        gross_loss   = abs(sum(t["pnl"] for t in losses)) or 1e-9
        profit_factor = gross_profit / gross_loss

        # Maximum Drawdown (MDD)
        eq = np.array(equity_curve)
        roll_max = np.maximum.accumulate(eq)
        drawdowns = (roll_max - eq) / roll_max
        mdd = float(drawdowns.max())

        # Sharpe Ratio (daily returns, annualised)
        daily_eq = pd.Series(equity_curve).resample("1D", origin="start").last().ffill()
        daily_ret = daily_eq.pct_change().dropna()
        rf_daily  = RISK_FREE_RATE_ANNUAL / 365
        excess    = daily_ret - rf_daily
        sharpe = float(excess.mean() / excess.std() * np.sqrt(365)) if excess.std() > 0 else 0

        final_equity = equity_curve[-1]
        total_return = (final_equity - self.initial_capital) / self.initial_capital

        avg_win_pct  = np.mean([t["pnl"] / t["size_q"] for t in wins])   if wins   else 0
        avg_loss_pct = np.mean([t["pnl"] / t["size_q"] for t in losses]) if losses else 0

        trades_df = pd.DataFrame(trades)

        return BacktestResult(
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins)/len(trades),
            avg_win_pct=avg_win_pct,
            avg_loss_pct=avg_loss_pct,
            profit_factor=round(profit_factor, 4),
            max_drawdown=round(mdd, 4),
            sharpe_ratio=round(sharpe, 4),
            total_return=round(total_return, 4),
            final_equity=round(final_equity, 2),
            trades_df=trades_df,
        )


# ──────────────────────────────────────────────
#  WALK-FORWARD ANALYSER
# ──────────────────────────────────────────────

class WalkForwardAnalyser:
    """
    Implements anchored Walk-Forward Analysis to detect overfitting.

    For each fold:
      • IS window → grid-search optimal params
      • OOS window → run with IS-best params (no re-optimisation)
      • Compare IS vs OOS Sharpe to measure robustness
    """

    PARAM_GRID = {
        "atr_stop_mult":   [1.0, 1.5, 2.0],
        "atr_target_mult": [1.5, 2.25, 3.0],
        "rsi_oversold":    [30, 35, 40],
        "rsi_overbought":  [60, 65, 70],
    }

    def __init__(self, base_config: MidasConfig, initial_capital: float = 10_000.0):
        self.base_cfg  = base_config
        self.capital   = initial_capital

    def run(self, df: pd.DataFrame, n_folds: int = 6, is_ratio: float = 0.75) -> pd.DataFrame:
        """
        Split df into n_folds windows. Each window: is_ratio% IS, rest OOS.
        Returns a DataFrame of results per fold.
        """
        fold_size = len(df) // n_folds
        results   = []

        for fold in range(n_folds):
            start = fold * fold_size
            end   = start + fold_size
            window = df.iloc[start:end]
            split  = int(len(window) * is_ratio)
            is_df, oos_df = window.iloc[:split], window.iloc[split:]

            logger.info(f"Walk-Forward Fold {fold+1}/{n_folds}: "
                        f"IS {is_df.index[0].date()} → {is_df.index[-1].date()}, "
                        f"OOS {oos_df.index[0].date()} → {oos_df.index[-1].date()}")

            # Grid search on IS
            best_sharpe, best_params = -np.inf, {}
            for combo in self._param_combinations():
                cfg = self._apply_params(combo)
                bt  = Backtester(cfg, self.capital).run(is_df.copy())
                if bt.sharpe_ratio > best_sharpe:
                    best_sharpe, best_params = bt.sharpe_ratio, combo

            # Evaluate on OOS with best IS params
            best_cfg  = self._apply_params(best_params)
            oos_result = Backtester(best_cfg, self.capital).run(oos_df.copy())

            results.append({
                "fold":          fold + 1,
                "is_sharpe":     round(best_sharpe, 3),
                "oos_sharpe":    round(oos_result.sharpe_ratio, 3),
                "oos_mdd":       round(oos_result.max_drawdown, 3),
                "oos_pf":        round(oos_result.profit_factor, 3),
                "oos_return":    round(oos_result.total_return, 4),
                "best_params":   str(best_params),
                "robust":        oos_result.sharpe_ratio >= best_sharpe * 0.70,
            })

        results_df = pd.DataFrame(results)
        robust_pct = results_df["robust"].mean()
        logger.info(f"\nWFA Complete. Robust folds: {robust_pct:.0%}")
        if robust_pct < 0.60:
            logger.warning("⚠️  Less than 60% of folds are robust. Strategy may be overfit.")
        return results_df

    def _param_combinations(self):
        keys   = list(self.PARAM_GRID.keys())
        values = list(self.PARAM_GRID.values())
        for combo in product(*values):
            yield dict(zip(keys, combo))

    def _apply_params(self, params: dict) -> MidasConfig:
        import copy
        cfg = copy.copy(self.base_cfg)
        for k, v in params.items():
            setattr(cfg, k, v)
        return cfg


# ──────────────────────────────────────────────
#  CLI ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s")

    # Usage: python midas_backtest.py path/to/BTCUSDT_1m.csv
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT_1m.csv"
    logger.info(f"Loading data from {csv_path}")

    raw = pd.read_csv(csv_path, parse_dates=["timestamp"], index_col="timestamp")
    raw.index = raw.index.tz_localize("UTC")
    raw.columns = [c.lower() for c in raw.columns]

    cfg = MidasConfig()
    bt  = Backtester(cfg)

    # ── Full backtest ──────────────────────────────────────────────
    result = bt.run(raw)
    print(result.summary())

    # ── Walk-Forward Analysis ──────────────────────────────────────
    wfa  = WalkForwardAnalyser(cfg)
    wfa_df = wfa.run(raw, n_folds=6)
    print("\nWALK-FORWARD ANALYSIS\n" + "═"*55)
    print(wfa_df.to_string(index=False))
