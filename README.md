# Midas AI — Setup Guide (macOS + Cursor)

This guide takes you from zero to a running paper trading bot.
Follow every step in order. Do not skip ahead.

---

## What you need before starting

- A Mac with internet access
- Cursor IDE (already installed)
- An Alpaca account (free — paper trading only to start)

---

## Step 1 — Create your Alpaca Paper Trading account

1. Go to **https://alpaca.markets** and click **Get Started**
2. Sign up with your email address (free account)
3. Once logged in, click **Paper Trading** in the left sidebar
4. Go to **Your API Keys** (top right of the paper dashboard)
5. Click **Generate New Key**
6. Copy both values and save them somewhere safe:
   - **API Key ID** (looks like: `PKXXXXXXXXXXXXXX`)
   - **Secret Key** (looks like: `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`)

> ⚠️ These are your **paper** keys — they only work with fake money.
> You will need separate live keys later if you ever go live.

---

## Step 2 — Open the project in Cursor

1. Download and unzip `midas_ai_alpaca.zip`
2. Open **Cursor**
3. Go to **File → Open Folder**
4. Select the `midas_ai` folder you just unzipped

You should see these files in the left sidebar:
```
midas_ai/
├── main.py
├── midas_config.py
├── midas_executor.py
├── midas_risk.py
├── midas_screener.py
├── midas_strategy.py
├── midas_backtest.py
├── requirements.txt
└── README.md
```

---

## Step 3 — Open the terminal inside Cursor

1. In Cursor, go to **Terminal → New Terminal** (or press `` Ctrl+` ``)
2. A terminal panel opens at the bottom of the screen

### Check your Python version

```bash
python3 --version
```

You need **Python 3.9 or higher**. If you see 3.9, 3.10, 3.11, 3.12 — you're good.

If you see Python 2.x or an error, install Python 3 from **https://python.org/downloads**

---

## Step 4 — Create a virtual environment

A virtual environment keeps this project's packages separate from everything
else on your Mac. Always do this for Python projects.

```bash
python3 -m venv venv
```

Now activate it:

```bash
source venv/bin/activate
```

Your terminal prompt should now start with `(venv)`. This means it's active.

> Every time you open a new terminal for this project, run `source venv/bin/activate` first.

---

## Step 5 — Install the dependencies

```bash
pip install -r requirements.txt
```

This installs aiohttp, pandas, numpy, and the other packages the bot needs.
It will take about 30–60 seconds.

---

## Step 6 — Set your API keys

The bot reads your Alpaca keys from **environment variables** — this keeps
them out of your code files (never paste keys directly into Python files).

Run these two commands in your terminal, replacing the placeholder text
with your actual keys from Step 1:

```bash
export ALPACA_API_KEY="PKXXXXXXXXXXXXXX"
export ALPACA_API_SECRET="your-secret-key-here"
```

> ⚠️ These `export` commands only last for the current terminal session.
> If you close the terminal and reopen it, you need to run them again.

### Optional: Make the keys permanent

To avoid re-entering them every session, add them to your shell profile.
In the terminal:

```bash
echo 'export ALPACA_API_KEY="PKXXXXXXXXXXXXXX"' >> ~/.zshrc
echo 'export ALPACA_API_SECRET="your-secret-key-here"' >> ~/.zshrc
source ~/.zshrc
```

---

## Step 7 — Verify the setup

Run this quick check to confirm Python can see your keys:

```bash
python3 -c "import os; print('Key set:', bool(os.getenv('ALPACA_API_KEY')))"
```

You should see: `Key set: True`

Then confirm the packages installed correctly:

```bash
python3 -c "import aiohttp, pandas, numpy; print('All packages OK')"
```

You should see: `All packages OK`

---

## Step 8 — Run the bot (paper mode)

The bot defaults to paper trading. Run it with:

```bash
python3 main.py
```

You will see output like this in the terminal:

```
2024-01-15T14:30:00Z  INFO     [midas.main]  🚀 MIDAS AI — ALPACA PAPER
2024-01-15T14:30:00Z  INFO     [midas.main]  Capital:   $15,000.00
2024-01-15T14:30:00Z  INFO     [midas.main]  Symbols:   30 in watchlist
2024-01-15T14:30:00Z  INFO     [midas.main]  Shorts:    ENABLED
2024-01-15T14:30:00Z  INFO     [midas.main]  🔴 NYSE closed — tick skipped.
```

The last line is normal if the market is currently closed.
The bot will run and wait. When the NYSE opens (9:30 AM ET on weekdays),
it will start scanning symbols and generating signals.

---

## Step 9 — Stop the bot

Press **Ctrl+C** in the terminal. The bot will:
1. Close any open paper positions
2. Save its state to `~/midas_ai/state/state.json`
3. Exit cleanly

---

## Where your files are stored

The bot creates these folders automatically on first run:

| Location | Contents |
|---|---|
| `~/midas_ai/logs/bot.log` | Full trading log (rotates at 10MB) |
| `~/midas_ai/state/state.json` | Account state — equity, peak, compounding day |

To view the log in real time (while the bot is running, in a second terminal):

```bash
tail -f ~/midas_ai/logs/bot.log
```

---

## Understanding the output

Each minute during market hours you'll see a block like this:

```
──── TICK #00042  09:47:00 ET  Watching 30 symbols ────
Equity: $15,127.50  Peak: $15,210.00
Screener: 2 signal(s): NVDA(L=0.82), AMD(S=0.71)
📨 NVDA LONG  0.1234 sh @ ~$487.50  notional=$60.10  risk=0.401%
📄 SIM ORDER  NVDA LONG  0.1234 sh  Entry ≈ $487.50  SL $480.00  TP $498.50
Equity $15,127.50  |  Target $15,302.50  |  Gap -1.15%  |  PDT 1/3
```

| Field | Meaning |
|---|---|
| `Equity` | Current paper account value |
| `Peak` | Highest equity ever reached |
| `Screener` | Symbols with qualifying signals this tick |
| `L` / `S` | Long / Short direction |
| `0.82` | Signal quality score (0–1, higher = better) |
| `notional` | Dollar value of the position |
| `risk` | % of account at risk on this trade |
| `PDT 1/3` | Day trades used / maximum allowed |
| `Gap` | How far behind/ahead of the 1% daily target |

---

## Frequently asked questions

**Q: The bot says "NYSE closed" for the entire day.**
A: Markets are closed on weekends and US public holidays. This is correct.

**Q: I see "No qualifying signals this tick" every minute.**
A: This is normal, especially early in validation. The strategy is selective
by design — it only trades high-confidence setups. Fewer, better trades
outperforms many mediocre ones.

**Q: How do I check my paper trades on Alpaca's website?**
A: Log in at https://app.alpaca.markets, go to Paper Trading, and click
on "Orders" or "Positions" to see what the bot has placed.

**Q: Can I change which stocks the bot watches?**
A: Yes. Open `midas_config.py` in Cursor, find the `WATCHLIST` list, and
add or remove ticker symbols. Stick to large-cap, liquid US stocks.

**Q: When should I consider going live?**
A: Not before completing all of these:
- 30 full NYSE trading sessions of paper trading (about 6 weeks)
- Maximum drawdown below 8% across those sessions
- Sharpe ratio above 1.0
- No single losing day exceeding 2.5% (well inside the 3% kill switch)
- You understand every part of the log output

---

## Getting help

If you see an error, copy the **full red text** from the terminal
(not just the last line) and share it. The most useful information
is always the complete error traceback.
