# Fund Monitor · Complete User Guide

> A local desktop tool for fund portfolio tracking + AI investment research. All data stays on your machine — your holdings are never uploaded anywhere.
> Applies to: v2.0.13 (exe builds released on or after 2026-08-17)
>
> **中文版: [README.md](README.md)**

---

## Table of Contents

1. [Introduction & UI Overview](#1-introduction--ui-overview)
2. [Installation & Launch](#2-installation--launch)
3. [Portfolio Tracking (Free, No AI Needed)](#3-portfolio-tracking-free-no-ai-needed)
4. [Top Summary Cards](#4-top-summary-cards)
5. [Return Rate Chart](#5-return-rate-chart)
6. [Floating Window](#6-floating-window)
7. [AI Investment Research](#7-ai-investment-research)
8. [Prediction Review](#8-prediction-review)
9. [Signal Tracking](#9-signal-tracking)
10. [Trade Advice Review (Closed Loop)](#10-trade-advice-review-closed-loop)
11. [Settings & LLM Configuration](#11-settings--llm-configuration)
12. [Data Files](#12-data-files)
13. [FAQ](#13-faq)
14. [Disclaimer](#14-disclaimer)

---

## 1. Introduction & UI Overview

The main window has two areas:

| Area | Description |
|---|---|
| **Portfolio (default view)** | Top summary cards + return chart + holdings table + pending trades |
| **Analysis (top "分析" button)** | AI research workbench: Today / History / Review / Signals / Trade Review / Settings |

**Color convention (A-share habit)**: Red = up, Green = down.

---

## 2. Installation & Launch

### 2.1 First-time setup

1. Unzip the package to any folder (an English path is recommended, e.g. `D:\FundMonitor`)
2. Double-click `基金监控.exe` — **no installation required**
3. Requires the **WebView2 Runtime** (preinstalled on Windows 11 and most Windows 10 machines; if missing, search "WebView2 Runtime" on the Microsoft website and install it)

> Migrating to a new computer: copy all `.json` files from the old exe directory to the new exe directory.

---

## 3. Portfolio Tracking (Free, No AI Needed)

> Portfolio features are fully free and do not depend on LLM or even internet for historical data.

### 3.1 Add a holding

1. Click "**＋ 录入持仓**" (Add)
2. Enter a **6-digit fund code** (e.g. `110022`) — name is auto-detected
3. Enter the holding amount (CNY)
4. Choose the confirmation rule (T+0 / T+1 / T+2)
5. Shares are calculated automatically from the current NAV

### 3.2 Buy (add position)

1. Select the fund in the table
2. Click "**买入**" (Buy), enter the amount (CNY)
3. The confirmation NAV date is determined by that fund's T+N rule (see 3.6)

### 3.3 Sell (reduce position)

1. Select the fund, click "**卖出**" (Sell)
2. Enter the amount, or type "全部" (all) to liquidate
3. Settled by the T+N rule; proceeds arrive after T+N days

### 3.4 Change rule

- Select a row and click "**改规则**" (Rule) to set confirmation days (0 / 1 / 2)
- Typical rules: money-market funds T+0; stock/mixed/bond funds T+1 (default); QDII cross-border funds T+2

### 3.5 Edit / Delete

| Action | Description |
|---|---|
| **✎ 编辑** (Edit) | Modify holding amount + cumulative P&L (to correct historical errors) |
| **删除** (Delete) | Remove from list — **has a confirmation dialog**, think twice |

### 3.6 Fund trading rules (T+N) in detail

- **Buy**: submitted **before 15:00 on a trading day** → valued at **today's** NAV, shares confirmed after **T+N** days; submitted after 15:00 or on weekends → deferred to the **next trading day's** NAV
- **Sell**: same 15:00 cutoff; settled at the confirmation NAV date, proceeds effective after T+N days
- Pending trades are listed in the bottom "**待确认交易**" (Pending) section with NAV date and arrival date; they disappear once confirmed
- Click "**撤单**" (Cancel) to cancel a pending trade
- Note: the calendar only distinguishes weekdays/weekends; public holidays are not separately handled (they defer naturally via NAV dates)

### 3.7 Table & refresh

- **Sorting**: click any column header for multi-level sorting (name / change / amount / P&L / shares / NAV, etc.)
- **Weight column**: shows position weight; over 30% displays an orange "集中" (concentrated) warning
- **Change source tag**: next to each change % — "**预估**" (estimated, intraday), "**收盘**" (official close, NAV date = today), or "**昨日收盘**" (yesterday's close, today not yet open)
- **Official data after close**: the intraday estimate fallback chain only runs during trading hours (9:30-15:00); **after close / weekends the official NAV and change are used directly** (never overwritten by estimates)
- **Auto refresh**: every 30 seconds; or click "立即刷新" (Refresh)

---

## 4. Top Summary Cards

Seven cards:

| Card | Description |
|---|---|
| **Total Assets** | Holdings market value + idle cash (incl. unrealized P&L) |
| **Today's P&L** | Estimated: holdings value × today's change |
| **Today's Return** | Today's P&L / yesterday's value |
| **Cumulative P&L** | Current value + redeemed amount − total invested |
| **Idle Cash** | Click to edit (cash management, counted in total assets) |
| **Today's Overall Prediction** | AI's direction forecast for the portfolio (after analysis) |
| **Prediction Accuracy** | Historical direction accuracy (after review) |

Header buttons:
- **🌙 Theme toggle**: light / dark
- **收起卡片** (Collapse): collapse the card area
- **🙈 Hide amounts**: hide total assets & idle cash
- **悬浮窗** (Float): open the mini window

---

## 5. Return Rate Chart

- Click the return card to **expand/collapse**
- Daily return curve + **CSI 300 benchmark line**
- Data: sampled every minute during trading hours (9:30-15:00), kept for the last 30 days

---

## 6. Floating Window

- Independent borderless always-on-top mini window: total assets / today's P&L / today's return / cumulative return / per-fund changes
- **Drag**: hold the title bar
- **📌**: pin / unpin on top
- **⤓**: collapse to the bottom-right corner (⤢ to expand)
- **✕**: close the float window (main window unaffected)
- Click "展开完整版" (Expand) to return to the main window

---

## 7. AI Investment Research

> Requires a DeepSeek API key (see section 11).

### 7.1 Single-fund analysis

1. Click "**分析**" → "今日分析" (Today) tab
2. Pick a fund → click "**🤖 开始分析**" (Analyze)
3. Backend pipeline (about 30-60s):
   ```
   Fetch 90-day NAV history → compute MA/RSI/drawdown/volatility
   → Nine-role AI (technical/fundamental/news/sentiment → bull-bear debate → director → trader → risk control → risk director)
   → structured report
   ```
4. Report includes:
   - **Core conclusion**: one-paragraph summary
   - **Today's analysis**: trend / key levels / momentum / risk
   - **Tomorrow's prediction**: direction (UP/DOWN/FLAT) / magnitude / confidence
   - **Trade advice**: action (Buy/Add/Hold/Reduce/Sell) / position / entry zone / target / stop-loss
   - **Mid-term strategy**: judgment for the coming 1-2 weeks
   - **Key risks & catalysts**
   - **Technical indicator summary**: MA alignment, RSI, drawdown, volatility
   - **Holdings look-through**: top-10 positions and industry distribution (auto-fetched)
   - **Signals**: whether AI generates trackable signals

### 7.2 Whole-portfolio analysis

- After opening the Analysis tab, choose "全部持仓" (All holdings) if available
- Runs **per-fund first, then portfolio level**; the portfolio report includes:
  - Overall prediction (portfolio direction tomorrow)
  - Sector / industry adjustment suggestions
  - Idle cash allocation advice (combined with per-fund advice)
  - New direction recommendations
  - **Quantitative allocation**: risk-parity suggested weights for each fund
- Per-fund results are **ranked in four tiers**:
  1. Clear action (Buy/Add/Sell/Reduce)
  2. HOLD with additional notes
  3. Pure HOLD
  4. Failed analysis
  - Within a tier: confidence descending → amount descending
- **Review-linkage (basis of conclusions)**: the report shows a "🧭 复盘联动" block listing the **trade-advice review experience** (profit ratio / bias types / lessons) and the **prediction-review corrections** (direction accuracy / bias correction / baseline comparison / confidence calibration) that this analysis referenced — so you can see exactly how the conclusions were derived; a "📡 信号联动" block shows referenced historical signals and win rate

### 7.3 Risk metrics (per-fund)

- **Sharpe** ratio
- **VaR95** (95% value-at-risk, single-day worst case)
- **Downside risk**
- **Max drawdown**

---

## 8. Prediction Review

1. "分析" → "**复盘**" (Review) tab
2. **Pick a prediction target day** (dropdown shows "对 08-18 日") → click "**📋 复盘对所选日的全部预测**" (Review all predictions for the selected day)
   - Review is anchored to the **target day**: every prediction records `forecast_date` (analysis before 15:00 on a trading day → targets that day; after 15:00 or weekends → the next trading day)
   - **Cross-day aggregation**: predictions for the same target day may come from different analysis days (e.g. both the 17th evening and 18th intraday predict the 18th) — they are merged into one review
3. Compares predictions vs **official closing change** for that target day:
   - Direction correct? (UP/DOWN/FLAT vs actual)
   - Magnitude deviation
   - **Composite accuracy (0-100)**: 50 points for correct direction; +50 if magnitude error <0.3%, +30 if <0.6%
   - Portfolio actual change (market-value-weighted)
   - **Per-fund AI deviation analysis** for every fund with a deviation (wrong direction or magnitude ≥0.3%)
4. Four stat blocks at the top of the review result:
   - **Direction accuracy / average accuracy / magnitude bias** (only direction-correct samples; actual − predicted mean; positive = systematic underestimation)
   - **Baseline comparison**: AI accuracy vs momentum-follow / mean-reversion / base-rate / random baselines (only data up to each analysis day, no leakage) + excess accuracy
   - **Confidence calibration**: actual accuracy per AI self-rated confidence tier (high/medium/low)
5. **Closed loop (auto-fed to next analysis)**: after review, lessons are distilled; every subsequent analysis automatically carries:
   - Direction accuracy + magnitude bias correction (e.g. +3.09% → instructs the AI to raise its expected change)
   - **Rolling correction** (last 5 reviews × sample size × time-decay weighted, avoiding single extreme days skewing the value)
   - Baseline comparison + confidence calibration + lessons
6. The "历史预测" (History) tab lists **target days** ("对 X 日的预测") with cross-day aggregation

---

## 9. Signal Tracking

- "分析" → "**信号**" (Signals) tab
- AI generates trackable signals during analysis (e.g. "breakout above key level, bullish"); deduplicated (same fund + direction + target keeps one)
- **Status badges**: active / strengthened / weakened / realized / falsified
- **AI auto-audit**: opening the Signals page audits active signals (**no auto re-audit within 5 hours, persists across restarts**; the manual "AI 审核信号" button is never throttled), classifying them as:
  - Realized (correct) / Falsified (wrong) / Strengthened / Weakened / Maintained / Insufficient info
- **Win-rate stats**: 4 tiles (total / active / closed / hit rate)
- Supports **deleting individual signals** and **clearing all**

### 9.1 Does signal auditing affect analysis? (Logic)

**Auditing never interferes with analysis execution — but audit conclusions are reference inputs for the next analysis**:

- **Independent process**: audit runs only when you open the Signals page; it only updates signal status/outcome, **does not modify analysis logic or block analysis**. You can always start an analysis regardless.
- **Closed-loop reference**: on every analysis (single or portfolio), the app appends that fund's historical signals to the AI prompt and asks the AI to "reference them to adjust this judgment and reflect the impact honestly in the confidence score". For example:
  - A signal previously **falsified** (e.g. "bullish but fell") → next time the AI sees the failure, **lowers confidence** and avoids repeating the same directional mistake
  - A signal **realized** → the AI knows the call played out, using it as confirmation
- **Signals ≠ trade advice**: signals are directional judgment records (for AI self-correction); trade advice is concrete operation instructions (handled by the trade-advice review loop). The two mechanisms are independent but both feed the next analysis prompt.

---

## 10. Trade Advice Review (Closed Loop)

> The most distinctive feature: **AI trade advice → auto-verification → lessons distilled → fed back into the next analysis**, forming a continuous improvement loop.

### 10.1 How advice is collected

- After every "Today" or portfolio analysis, if the AI gives a **non-hold** action (Buy/Add/Sell/Reduce), it is automatically stored in the "加减仓复盘" (Trade Review) library (only the latest record per fund **per target day**)
- Advice also carries a **target-day anchor** (`forecast_date`, same rule as predictions): before 15:00 → targets that day; after 15:00 or weekends → the next trading day. Cards show "生成日 · 对 08-18 日"

### 10.2 When auto-review happens

Review runs **only on trading days** and **after the day's NAV is finalized**:

| Condition | Description |
|---|---|
| Today is a trading day | Determined via the CSI 300 index quotes; weekends **and public holidays** do not count |
| Time ≥ 23:00 | Fund NAV usually updates in the evening; data is only reliable after 23:00 |
| Advice target day ≤ today | **Only reviews advice targeting today or earlier** — "yesterday's advice for today" is reviewed that night; "advice for tomorrow" waits until its target day closes |
| NAV freshness | Before reviewing, verifies current NAV date ≥ target day: **QDII / T+N funds with lagged NAV (1-2 days) are skipped**, marked "净值未更新…待更新后自动复盘", and re-reviewed automatically once NAV catches up — avoids wrong P&L from stale NAV |

**Triggers**:
- App running: a 23:00 timer is set at startup; auto-review fires once at that time
- App opened after 23:00: an automatic catch-up runs 3 seconds after launch
- Opening the "加减仓复盘" page: auto-checks and triggers (60s debounce)

**Examples**:
- Advice from Saturday (targeting Monday) → auto-reviewed after Monday 23:00 ✅
- Advice from Friday intraday (targeting Friday) → reviewed Friday 23:00 ✅
- Computer off at Friday 23:00 → caught up on Monday 23:00 (advice is never lost, only deferred) ✅
- Monday-evening advice targeting Tuesday → reviewed Tuesday 23:00; QDII with lagged NAV auto-waits until NAV updates ✅

### 10.3 Review contents

Each advice is reviewed and given:
- **Result**: profit / loss / flat (estimated by advice direction — buy/add profits on NAV rise; sell/reduce reversed)
- **Return %**: how much you'd have made/lost if you had followed it
- **Bias type**: wrong direction / mistimed (too early or late) / chase-the-market emotional / wrong magnitude / insufficient info / other
- **Reason**: AI's P&L and deviation analysis (within 60 chars)

### 10.4 Closed loop: lessons fed back

After review completes automatically:
1. The latest 12 reviewed records (bias type / reason / P&L) are given to the AI to distill **3-6 lessons**
2. Cached to `trade_lessons.json`
3. **Every subsequent analysis** (single or portfolio) automatically carries this context:
   - Profit ratio + average return
   - Bias type distribution
   - Distilled lessons
   - Last 3 loss cases (with bias types)
4. The AI references these historical lessons when generating new advice → closed loop

### 10.5 Page features

- **Date filter**: top dropdown filters by **target day** (All + target days descending; cards show "生成日 · 对 X 日")
- **Status hint**: shows "market closed today / NAV not yet finalized" etc. in real time
- **Lessons block**: top of the page shows the cached lessons (with generation date)
- **Stats**: total advice / reviewed / profit ratio / average return

---

## 11. Settings & LLM Configuration

"分析" tab → top-right "**⚙ 设置**" (Settings):

| Item | Description |
|---|---|
| API Key | Your DeepSeek API key (apply at https://platform.deepseek.com) |
| Model | `deepseek-v4-pro` (deep reasoning, default) / `deepseek-v4-flash` (faster, cheaper) |
| Base URL | Default `https://api.deepseek.com` |

> Portfolio tracking does **not** require LLM config; only AI analysis/review needs it.

---

## 12. Data Files

All files live in the **exe directory**; copy all of them when migrating:

| File | Content |
|---|---|
| `funds_data.json` | Holdings (share model: shares/bought/sold/pending/navmap/confirm_days) |
| `idle_cash.json` | Idle cash (counted in total assets) |
| `rate_history.json` | Intraday return sampling (every minute 9:30-15:00 on trading days, kept 30 days) |
| `analysis_config.json` | LLM key / model / base_url (**local only, contains secrets — never share**) |
| `analysis_history.json` | Daily predictions (predictions/reports/portfolio) |
| `signals.json` | Signal library (win-rate stats) |
| `trade_review.json` | Trade advice review records |
| `trade_lessons.json` | Cached lessons (auto-generated, fed into next analysis) |

> ⚠️ `analysis_config.json` contains your API key; `funds_data.json` contains your private holdings — **be careful not to leak them** when backing up or sharing.

---

## 13. FAQ

**Q1: Window won't open / white screen?**
Missing WebView2 Runtime. Download "WebView2 Runtime" from the Microsoft website and reinstall.

**Q2: Shows "not found" after adding?**
Make sure it's a 6-digit fund code (e.g. 110022). This tool supports funds only, not stocks.

**Q3: Estimates not updating?**
Outside trading hours (9:30-15:00 on trading days) there is no intraday estimate; it shows the latest official NAV change instead.

**Q4: Antivirus flags the exe?**
PyInstaller single-file exes are sometimes falsely flagged — add an exception.

**Q5: Buy not confirmed for a long time?**
Make sure you bought before 15:00 on a trading day; if a public holiday intervenes, confirmation defers to the first trading day after it.

**Q6: Analysis says "configure API key"?**
"分析" tab → top-right "⚙ 设置" → enter your DeepSeek key → save.

**Q7: Analysis call fails?**
Check key validity, account balance, and network access to api.deepseek.com.

**Q8: Trade review stays "pending"?**
Check the status hint: on non-trading days or before 23:00 it won't review — that's normal; it auto-catches up after 23:00 on the next trading day.

**Q9: How to migrate to a new computer?**
Copy the exe + all `.json` files in the same directory to the new machine's exe directory.

**Q10: Intraday estimate differs from actual settlement?**
Estimates are approximate and for reference only; the fund company's settlement is authoritative.

---

## 14. Disclaimer

This tool is for **personal learning and decision support only** and does not constitute investment advice. Intraday estimates are approximate; AI analysis and predictions are based on historical data and model inference with significant uncertainty. **Markets are risky; invest with caution.**
