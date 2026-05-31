"""
MIDAS AI — Live Dashboard
Reads ~/midas_ai/state/state.json and ~/midas_ai/logs/trades.csv.
Auto-refreshes every 30 seconds.  Run with: streamlit run dashboard.py
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# ── Paths ──────────────────────────────────────────────────────────────────────
HOME            = Path.home()
MIDAS_DIR       = HOME / "midas_ai"
STATE_FILE      = MIDAS_DIR / "state" / "state.json"
TRADES_CSV      = MIDAS_DIR / "logs" / "trades.csv"
EQUITY_CSV      = MIDAS_DIR / "logs" / "equity_history.csv"
INITIAL_CAPITAL = 15_000.0
REFRESH_SECS    = 30

# ── Page config (must be the first Streamlit call) ─────────────────────────────
st.set_page_config(
    page_title="MIDAS AI",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* ── Background ── */
  [data-testid="stAppViewContainer"] { background-color: #0d1117; }
  [data-testid="stHeader"]           { background-color: #0d1117; }
  [data-testid="stToolbar"]          { display: none; }
  [data-testid="block-container"]    { padding-top: 1.4rem; }
  section[data-testid="stSidebar"]   { display: none; }

  /* ── Metric cards ── */
  .mcard {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 18px 22px 16px;
  }
  .mcard-label {
    color: #8b949e;
    font-size: 0.70rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
  }
  .mcard-value {
    color: #e6edf3;
    font-size: 1.70rem;
    font-weight: 700;
    margin: 5px 0 3px;
    line-height: 1.1;
  }
  .mcard-sub {
    color: #8b949e;
    font-size: 0.76rem;
  }

  /* ── Colour helpers ── */
  .pos          { color: #3fb950; }
  .neg          { color: #f85149; }
  .ok-badge     { color: #3fb950; font-weight: 700; }
  .halted-badge { color: #f85149; font-weight: 700; }

  /* ── Subheadings ── */
  h3 { color: #e6edf3 !important; margin-top: 1.4rem !important; }

  /* ── Divider ── */
  hr { border-color: #21262d !important; margin: 0.6rem 0 1rem; }

  /* ── Footer caption ── */
  .footer { color: #484f58; font-size: 0.74rem; margin-top: 2rem; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  DATA LOADERS
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> Optional[dict]:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def load_trades() -> pd.DataFrame:
    """Read trades.csv written by the bot; return empty DataFrame if absent."""
    if not TRADES_CSV.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(TRADES_CSV)
    except Exception:
        return pd.DataFrame()


def load_equity_history() -> pd.DataFrame:
    """Read equity_history.csv if available (bot appends a row each tick)."""
    if not EQUITY_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(EQUITY_CSV, parse_dates=["timestamp"])
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
#  HEADER
# ══════════════════════════════════════════════════════════════════════════════

ts_now = datetime.now()

st.markdown("## MIDAS AI — Live Dashboard")
st.caption(
    f"Last refreshed: **{ts_now.strftime('%Y-%m-%d  %H:%M:%S')}**  "
    f"·  auto-refreshes every {REFRESH_SECS}s"
)
st.divider()

# ══════════════════════════════════════════════════════════════════════════════
#  LOAD + GUARD
# ══════════════════════════════════════════════════════════════════════════════

state   = load_state()
trades  = load_trades()
eq_hist = load_equity_history()

if state is None:
    st.info(
        "**Awaiting first session** — `state.json` has not been created yet.  \n"
        "Start the bot with `python3 main.py`, then return here.",
        icon="⏳",
    )
    st.caption(f"Watching: `{STATE_FILE}`")
    time.sleep(REFRESH_SECS)
    st.rerun()

# ── Extract AccountState fields ────────────────────────────────────────────────
equity           = float(state.get("equity",           INITIAL_CAPITAL))
peak_equity      = float(state.get("peak_equity",      INITIAL_CAPITAL))
day_start_equity = float(state.get("day_start_equity", INITIAL_CAPITAL))
halted           = bool(state.get("halted",   False))
halt_reason      = str(state.get("halt_reason", ""))
last_reset_iso   = str(state.get("last_reset",  ""))

daily_pnl_dollar = equity - day_start_equity
daily_pnl_pct    = (daily_pnl_dollar / day_start_equity * 100) if day_start_equity > 0 else 0.0
dd_from_peak_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0

pnl_cls  = "pos" if daily_pnl_dollar >= 0 else "neg"
pnl_sign = "+" if daily_pnl_dollar >= 0 else ""


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL 1 — Metric Cards
# ══════════════════════════════════════════════════════════════════════════════

c1, c2, c3, c4 = st.columns(4, gap="medium")

with c1:
    total_return_pct = (equity / INITIAL_CAPITAL - 1) * 100
    tr_cls = "pos" if total_return_pct >= 0 else "neg"
    tr_sign = "+" if total_return_pct >= 0 else ""
    st.markdown(f"""
    <div class="mcard">
      <div class="mcard-label">Current Equity</div>
      <div class="mcard-value">${equity:,.2f}</div>
      <div class="mcard-sub {tr_cls}">{tr_sign}{total_return_pct:.2f}% total return</div>
    </div>""", unsafe_allow_html=True)

with c2:
    st.markdown(f"""
    <div class="mcard">
      <div class="mcard-label">Daily P&amp;L</div>
      <div class="mcard-value {pnl_cls}">{pnl_sign}${daily_pnl_dollar:,.2f}</div>
      <div class="mcard-sub {pnl_cls}">{pnl_sign}{daily_pnl_pct:.2f}%  vs day open</div>
    </div>""", unsafe_allow_html=True)

with c3:
    dd_cls = "neg" if dd_from_peak_pct > 0.5 else "pos"
    st.markdown(f"""
    <div class="mcard">
      <div class="mcard-label">Peak Equity</div>
      <div class="mcard-value">${peak_equity:,.2f}</div>
      <div class="mcard-sub {dd_cls}">DD from peak: {dd_from_peak_pct:.2f}%</div>
    </div>""", unsafe_allow_html=True)

with c4:
    if halted:
        badge = '<span class="halted-badge">⛔  HALTED</span>'
        reason_short = (halt_reason[:52] + "…") if len(halt_reason) > 52 else (halt_reason or "—")
        sub = reason_short
    else:
        badge = '<span class="ok-badge">✅  OK</span>'
        sub = "Kill switch inactive"
    st.markdown(f"""
    <div class="mcard">
      <div class="mcard-label">Kill Switch</div>
      <div class="mcard-value">{badge}</div>
      <div class="mcard-sub">{sub}</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL 2 — Equity Curve
# ══════════════════════════════════════════════════════════════════════════════

st.subheader("Equity Curve")

if not eq_hist.empty and {"timestamp", "equity"}.issubset(eq_hist.columns):
    chart_df = eq_hist.set_index("timestamp")[["equity"]]
    st.line_chart(chart_df, use_container_width=True, color="#3fb950")
else:
    # Synthesise a two-point stub from state.json: session-start → now
    try:
        ts_start = pd.to_datetime(last_reset_iso, utc=True).to_pydatetime() \
                   if last_reset_iso else ts_now
    except Exception:
        ts_start = ts_now
    stub = pd.DataFrame(
        {"timestamp": [ts_start, ts_now], "equity": [day_start_equity, equity]}
    ).set_index("timestamp")
    st.line_chart(stub[["equity"]], use_container_width=True, color="#3fb950")
    st.caption(
        f"Tick-by-tick equity history will appear at `{EQUITY_CSV}` once the bot runs."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL 3 — Last 20 Trades
# ══════════════════════════════════════════════════════════════════════════════

st.subheader("Last 20 Trades")

if trades.empty:
    st.info(
        "No trades recorded yet — trades appear here once the bot executes its first order.",
        icon="📋",
    )
else:
    # Columns match TradeResult dataclass in midas_executor.py
    WANT_COLS = ["symbol", "side", "entry_price", "exit_price",
                 "pnl_dollar", "pnl_pct", "outcome"]
    present   = [c for c in WANT_COLS if c in trades.columns]
    tbl       = trades[present].tail(20).copy().reset_index(drop=True)

    RENAME = {
        "symbol":      "Symbol",
        "side":        "Side",
        "entry_price": "Entry $",
        "exit_price":  "Exit $",
        "pnl_dollar":  "P&L $",
        "pnl_pct":     "P&L %",
        "outcome":     "Outcome",
    }
    tbl.rename(columns={k: v for k, v in RENAME.items() if k in tbl.columns}, inplace=True)

    if "Entry $" in tbl.columns:
        tbl["Entry $"] = tbl["Entry $"].map("${:.2f}".format)
    if "Exit $" in tbl.columns:
        tbl["Exit $"]  = tbl["Exit $"].map("${:.2f}".format)
    if "P&L $" in tbl.columns:
        tbl["P&L $"]   = tbl["P&L $"].map("{:+.2f}".format)
    if "P&L %" in tbl.columns:
        tbl["P&L %"]   = tbl["P&L %"].map("{:+.3f}%".format)

    st.dataframe(tbl, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
#  PANEL 4 — Summary Statistics
# ══════════════════════════════════════════════════════════════════════════════

st.subheader("Summary Statistics")

if trades.empty:
    st.info("No statistics yet.", icon="📊")
else:
    total  = len(trades)
    wins   = int((trades["outcome"] == "tp").sum()) if "outcome" in trades.columns else 0
    losses = int((trades["outcome"] == "sl").sum()) if "outcome" in trades.columns else 0
    other  = total - wins - losses
    win_rate = wins / total * 100 if total > 0 else 0.0

    has_pnl = "pnl_dollar" in trades.columns
    avg_win  = trades.loc[trades["outcome"] == "tp", "pnl_dollar"].mean() \
               if has_pnl and wins > 0 else None
    avg_loss = trades.loc[trades["outcome"] == "sl", "pnl_dollar"].mean() \
               if has_pnl and losses > 0 else None
    gross_pnl = float(trades["pnl_dollar"].sum()) if has_pnl else None

    win_sum  = trades.loc[trades["pnl_dollar"] > 0, "pnl_dollar"].sum() if has_pnl else 0.0
    loss_sum = trades.loc[trades["pnl_dollar"] < 0, "pnl_dollar"].sum() if has_pnl else 0.0
    profit_factor = abs(win_sum / loss_sum) if has_pnl and loss_sum != 0 else None

    s1, s2, s3, s4, s5, s6 = st.columns(6, gap="medium")

    s1.metric("Total Trades",  total)
    s2.metric("Win Rate",      f"{win_rate:.1f}%")
    s3.metric("Wins / Losses", f"{wins} / {losses}")
    s4.metric("Avg Win",       f"${avg_win:+.2f}" if avg_win is not None else "—")
    s5.metric("Avg Loss",      f"${avg_loss:+.2f}" if avg_loss is not None else "—")
    s6.metric("Gross P&L",     f"${gross_pnl:+.2f}" if gross_pnl is not None else "—")

    if profit_factor is not None:
        pf_str = f"{profit_factor:.2f}" if profit_factor < 999 else "∞"
        st.caption(f"Profit factor: **{pf_str}**  ·  Other outcomes: {other}")


# ══════════════════════════════════════════════════════════════════════════════
#  FOOTER + AUTO-REFRESH
# ══════════════════════════════════════════════════════════════════════════════

st.divider()
st.markdown(
    f'<div class="footer">MIDAS AI  ·  Paper Trading  ·  Alpaca IEX  ·  '
    f'{ts_now.strftime("%Y-%m-%d")}</div>',
    unsafe_allow_html=True,
)

time.sleep(REFRESH_SECS)
st.rerun()
