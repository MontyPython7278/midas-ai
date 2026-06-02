# Midas AI — Dev Log

## Entry: May 31, 2026 (Day 1)

### What I set out to do
Get Midas AI from "a pile of code with bugs" to a clean, verified,
deployable system before the official June 4 start of my summer build.

### What I actually did
- Full code audit — found and fixed 19 issues across four severity tiers
- Fixed all crash bugs: missing certifi, broken .gitignore, Sharpe crash
  in backtester, deprecated asyncio call
- Fixed all silent bugs: blocking network call in async loop, duplicated
  capital constant, single-position backtest not matching live system
- Removed dead code: unused imports, dead functions, inline imports
- Put it on GitHub with real commit history
- Set up Claude Code — installed Node, got CLI running, generated CLAUDE.md
- Built Streamlit dashboard — equity, P&L, kill switch, trade table
- Added CSV logging so trades and equity persist to dised dashboard
- Built --check preflight self-test — 5/5 checks passed including live
  Alpaca key authentication
- Rewrote README as real project documentation
- Wrote 21 passing unit tests for the risk manager
- Updated CLAUDE.md to match current 9-module architecture

### The honest part
Ran a backtest. The strategy lost money on synthetic data. That is
information, not failure. Key findings:
- 50%+ of trades hit the stop loss before the take profit
- Realized R:R came in at ~1:1, not the 1.5:1 the strategy targets
- ATR x1.5 stop is probably too tight — gets picked off by normal noise
- Synthetic data is unreliable; real test is live paper trading

### What I learned
- A negative backtest on a system you understand beats a green one you
  don't. Now I know exactly which two parameters to test first.
- "Done" claimeby a tool is not "done" — always verify with your own
  eyes and a grep.
- My laptop is the wrong long-term home for an always-on bot. A cheap
  cloud server is the right Phase 2 mo## Open threads
1. Wednesday June 4, 9:30 ET — first live paper trading session
2. Run as-is, collect a clean baseline, do NOT touch parameters yet
3. After ~1 week of data: A/B test stop multiplier (1.5 vs 2.0) and
   RSI threshold (35 vs 30) — one variable at a time
4. README still has no performance section — add after Wednesday data

### Status
System is feature-complete and verified. Bot launches, connects, logs,
dashboard displays, preflight passes 5/5. Three days ahead of schedule.
