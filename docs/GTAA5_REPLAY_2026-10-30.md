# GTAA-5 replay: independent verification (2026-10-30)

Script: `scripts/gtaa5_replay.py`. Research only, no trading, no keys read.

## 0. PRE-REGISTRATION (written before any data was fetched or computed)

Written 2026-10-30 (Fri) 23:5x SGT (= 11:5x ET, US market still open that day). At the time of writing, no price series had been downloaded and nothing had been computed.

### 0.1 Claim under test

A desk researcher recommended Faber's GTAA-5 (SSRN 962461) as the only bounded, silent candidate for a $10K automated book, claiming about 4.8%/yr CAGR 2007-2026 (5.4% post-2013), max daily drawdown -16.1% (Oct 2008), 2022 drawdown no worse than -12%, 12 decisions per year.

### 0.2 GATE (verbatim from the brief; this is what PASS/FAIL is judged on)

PASS if, net of 10 bp per one-way trade and with cash earning the 13-week T-bill rate:
- CAGR >= 4.5% over 2007-01 to the latest close, AND
- CAGR >= 4.0% over 2013-01 to latest, AND
- max daily-close drawdown >= -18%, AND
- calendar-2022 return >= -12%, AND
- the CAGR spread across rebalance days (1st, 10th, 20th of month) <= 2.0 points.

Otherwise FAIL. Context only (not gated): 60/40 (SPY/AGG, monthly rebalance) and SPY buy-and-hold over the same windows.

### 0.3 Method choices fixed in advance (so they cannot be tuned after seeing results)

1. Assets: SPY, EFA, AGG, VNQ, DBC. Data: yfinance 1.3.0, `auto_adjust=True` daily closes (dividends and splits adjusted, as of download time). If a requested start precedes an asset's data (DBC begins Feb 2006), the test starts at the latest asset inception plus the 10-month warm-up, and I say so.
2. Signal (month-end version, the primary strategy): at the close of the last trading day of each calendar month, SMA10 = mean of the last 10 month-end adjusted closes INCLUDING the current one. Asset is ON if its month-end close > SMA10, else OFF. ON = 20% weight in that ETF for the next month. OFF = that 20% sits in cash. Weights are reset to 20% per sleeve at every monthly decision (drift is rebalanced away). Trades execute at the SAME close as the signal (optimistic: no overnight or next-day slippage; noted as a caveat).
3. Cash: ^IRX (yfinance, annual percent), forward-filled on non-quote days, accrued each day as (prior-day ^IRX / 100) x (calendar days elapsed / 365). If ^IRX is unavailable, report that and run cash at 0% and at 2%. As a cross-check only (not the gate), FRED DTB3 via the public CSV endpoint, if reachable.
4. Costs, PRIMARY (the one the gate uses): 10 bp of traded notional on every ETF buy or sell, i.e. charged on the sum of absolute dollar changes in the five ETF sleeves at each rebalance. This includes the small monthly drift-rebalance trades, not only on/off switches, because the gate says "per one-way trade". The cash/T-bill side is treated as free. SENSITIVITIES (reported, not gated): (a) "switch-only" cost, 10 bp charged only on sleeves that changed ON/OFF state; (b) 0 bp; (c) 20 bp.
5. Window definitions: the equity curve starts at the 2006-12-29 close (equity 1.0, initial positions set by the latest decision on or before that date, established at that close with the 10 bp entry cost). "2007-01 to latest" = from the 2006-12-29 close to the last completed session's close. "2013-01 to latest" = from the 2012-12-31 close of the SAME continuous run (a sub-window, not a restart) to the last completed session's close. CAGR = (end/start)^(365.25/calendar days) - 1.
6. "Latest close" = the last COMPLETED regular session. If yfinance returns a partial bar for today (US market is open at the time of writing), that bar is dropped. The position held through the final days is the one set by the last completed month-end signal (30 Sep 2026); the October signal is not yet due.
7. Max drawdown: peak-to-trough of the DAILY-close equity curve, full period 2006-12-29 to latest, with dates. Also reported: worst single day, worst calendar month, calendar-year table, annualised vol (std of daily returns x sqrt(252)), switches per year (count of sleeve ON/OFF state changes), exposure fraction (time-weighted share of equity in the five ETFs).
8. Calendar-2022 return: equity at last 2022 close / equity at last 2021 close - 1.
9. Rebalance-day sweep (d = 1, 10, 20). Decision date for each month = the first trading day on or after calendar day d of that month, decided and traded at that day's close. PRIMARY definition (the one the gate spread uses, literal reading of the brief): asset ON if close on the decision day > mean of the 10 most recent COMPLETED month-end adjusted closes (prior month-end and the 9 before it). SECONDARY definition (reported for robustness): asset ON if mean of {decision-day close + the 9 most recent completed month-end closes} < decision-day close, i.e. the analogue of "current point included in the 10". Spread = max minus min CAGR across d = 1, 10, 20, over 2007-01 to latest (and also reported for 2013-01 to latest). The month-end variant is shown alongside but is NOT part of the gated spread. Each variant starts at the 2006-12-29 close in the state set by its latest decision on or before that date, with the same entry cost.
10. Context strategies: 60/40 = 60% SPY / 40% AGG, rebalanced to target at each month-end close, 10 bp on traded notional (entry included); SPY buy-and-hold, 10 bp entry cost only. Same windows, same dates.
11. Anything unverifiable (single-vendor price source, no second independent price feed, vendor adjustment drift, etc.) will be listed in the caveats section. If a criterion cannot be computed it counts as not met.
12. Verdict rule: PASS only if all five gate lines hold under the PRIMARY definitions in 0.3. Sensitivities cannot rescue a FAIL or break a PASS, but will be shown so the reader can see how fragile the verdict is.

---

(Results follow below the line, appended after the computation.)

## RESULTS

Data fetched 2026-10-30 12:09 ET (market still open, so the 30 Oct bar was partial and dropped). Last completed session used: **2026-10-29**. The October month-end signal was not yet due, exactly as anticipated in 0.3 item 6. Nothing in the method or the gate was changed after results were seen: the first complete run and the final run give identical gate numbers (5.23% / 5.91% / -14.68% / -4.72% / 0.22 points). Later edits only ADDED diagnostics (execution lag, raw-price signal, integrity checks, T-bill baseline, tax estimate).

### R0. Bottom line

**GATE VERDICT: PASS, 5 of 5 lines, under the pre-registered definitions.** The margins on two lines are thin once realistic frictions are added (see R0.2), so treat it as a narrow pass.

| Gate line | Threshold | Measured | Margin | Result |
|---|---|---|---|---|
| CAGR 2007-01 to latest | >= 4.5% | 5.23% | +0.73 pt | PASS |
| CAGR 2013-01 to latest | >= 4.0% | 5.91% | +1.91 pt | PASS |
| Max daily-close drawdown | >= -18% | -14.68% (2008-05-21 to 2008-10-27) | 3.32 pt | PASS |
| Calendar-2022 return | >= -12% | -4.72% | 7.28 pt | PASS |
| CAGR spread, rebalance days 1/10/20 | <= 2.0 pt | 0.22 pt (secondary definition: 0.53 pt) | 1.78 pt | PASS |

Context over the same windows (10 bp costs, same dates): SPY buy-and-hold 10.90% / 14.90% CAGR with a -55.19% max drawdown; 60/40 SPY/AGG 7.92% / 9.64% with -35.80%; T-bills alone 1.57% / 1.81%.

#### R0.1 The researcher's claims against the replay

| Claim | Researcher | Replay (pre-registered treatment) | Reading |
|---|---|---|---|
| CAGR 2007-2026 | ~4.8% | 5.23% | Not matched; **4.77% when cash earns 0%**, which matches. |
| CAGR post-2013 | ~5.4% | 5.91% | Not matched; **5.34% when cash earns 0%**, which matches. |
| Max drawdown | -16.1% (Oct 2008) | -14.68% (27 Oct 2008) | Not reproduced. Closest variants: -14.96% (cash 0%), -15.44% (next-close execution), -15.72% (both). |
| 2022 | no worse than -12% | -4.72% return, -6.18% max drawdown inside 2022 | Holds with a wide margin. |
| Decisions per year | 12 | 12 month-end decisions; on average 8.6 sleeve ON/OFF switches per year (range 4 to 17), plus small drift-rebalance trades every month | Holds; "silent" is roughly right, but expect a trade ticket in most months. |

The CAGR match with cash at 0% suggests the researcher accrued nothing on the cash sleeves. That is an inference from the numbers, not something I can verify. The -16.1% drawdown is not reproduced by any variant I ran, so I cannot say where it came from.

#### R0.2 Where the pass is fragile (all from sections 4, 6, 8, 9 below)

- **CAGR margin is smaller than the noise.** 0.73 points of margin against a standard error of about 1.8 points on 19.8 years of data. The thresholds are inside sampling error.
- **Execution timing matters.** The replay trades at the same close as the signal. Signal at the close and trade at the NEXT close gives 4.66% (still above 4.5%). Next close plus 20 bp gives **4.47% (below 4.5%)**. Next close with cash at 0% gives 4.20%.
- **Drawdown depends on which day you rebalance.** The -14.68% is the month-end timing. Rebalancing on the 1st gives -18.19%, on the 20th -21.54% (primary definition) and -22.63% (secondary). Across 7 timing variants the max drawdown ranges from -14.17% to -22.63% and 4 of 7 breach -18%. The gate line is judged on the month-end strategy as written, which passes, but if it were read as applying to every rebalance day the drawdown line would fail. The 2020 whipsaw (held through the February-March crash, sold near the low) is what hurts the 1st and 20th variants.
- **Risk-adjusted, it does not beat the simple benchmarks.** Sharpe against T-bills: GTAA-5 0.48, SPY 0.55, 60/40 0.58. It is a drawdown limiter that gave up most of the equity return, not a better risk-return trade.
- **Signal inputs must be dividend-adjusted.** With raw (unadjusted) closes the signal differs in 7.5% of asset-month flags (AGG 43 of 238).

#### R0.3 What it means for a $10,000 account (historical, computed from the replay, not a forecast)

Over 2007-2026 the replay compounded at 5.23% a year, which on $10,000 is about **$44 a month on average** (mean monthly return of 0.44%; median month +$59; 35% of months lose money). Roughly $13 of that is just the T-bill interest earned by the cash sleeves and about $31 is the edge over bills; parking the same $10,000 in bills at today's ~4.0% pays about $33 a month, so what the strategy buys is a bounded allocation rather than a large earner, and the edge carries about +/- $15 a month of sampling error. The **worst calendar year was 2008 at -$595** (-5.95%; SPY lost $3,679 that year), the **worst month was -$675** (May 2010), the worst rolling 12 months was -$1,150 (to May 2012), and the **worst peak-to-trough was -$1,468** (21 May to 27 Oct 2008, recovered 14 Oct 2009). The longest stretch below a prior high was 2.06 years (Apr 2011 to May 2013). That bound is timing-dependent: other rebalance days would have drawn down $1,800 to $2,300 on $10,000. Practically, SPY is about $769 a share and one 20% sleeve is $2,000 (2.6 shares), so the account needs fractional shares or the SPY sleeve is misstated by up to 19%. Doubling costs (-0.19 pt), next-close execution (-0.57 pt) or dividend withholding (up to about -0.5 pt) each take a few tenths of a point off the CAGR, which is the same size as the 0.73-point margin over the gate.

---

### Full output of `scripts/gtaa5_replay.py`

## 1. Data audit

- Source: yfinance 1.3.0, `auto_adjust=True`, fetched 2026-10-30 12:09 ET. Window requested from 2005-01-01.

| Series | First | Last | Rows | Internal NaN |
|---|---|---|---|---|
| SPY | 2005-01-03 | 2026-10-29 | 5491 | 0 |
| EFA | 2005-01-03 | 2026-10-29 | 5491 | 0 |
| AGG | 2005-01-03 | 2026-10-29 | 5491 | 0 |
| VNQ | 2005-01-03 | 2026-10-29 | 5491 | 0 |
| DBC | 2006-02-06 | 2026-10-29 | 5216 | 0 |
| ^IRX | 2005-01-03 | 2026-10-29 | 5486 | 5 |

- Latest completed session used: **2026-10-29**. Common calendar from 2006-12-29: 4989 sessions; sessions where some ETF is missing after the base date: 0.
- Largest gap between consecutive sessions after base date: 5 calendar days (ending 2007-01-03).
- ^IRX: min -0.105, max 5.348, mean 1.769 (annual %); days with exactly 0.000: 0; negative prints: 7 (first 2020-03-19, last 2020-03-27, used as-is); NaN (bond-market holidays, forward-filled): 5.
- DBC first bar in yfinance: 2006-02-06. First month-end with a full 10-month DBC window: 2006-11 (so the 2006-12-29 base date is feasible without any pre-inception data).
- Data fingerprint (sha256 of adjusted closes 2006-12-29..2026-10-29, rounded 5dp, first 16 hex): `48d050eefaf58dd7`. yfinance back-adjusts history at download time, so this fingerprint can drift on later runs.
- Frames loaded from cache /private/tmp/claude-501/-Users-xynkro/849132b0-9b52-44c9-a3c5-c4675e03482d/scratchpad/gtaa5/frames.pkl (originally fetched 2026-10-30 12:09 ET).
- Dropped partial in-session bar dated 2026-10-30 (US market still open at fetch time 12:09 ET).
- Final month 2026-10 incomplete (last completed session 2026-10-29); its month-end signal is not yet due and is excluded.

- First month-end with a valid 10-month SMA for all five assets: 2006-11-30. Month-end decisions available: 239; those after the base date: 237.

## 2. Primary result: GTAA-5, month-end signal, 10 bp on all ETF trades, cash = ^IRX

| Strategy | CAGR 2007-01..latest | CAGR 2013-01..latest | Ann. vol | Max DD (daily close) | DD peak to trough | Worst day | Worst month |
|---|---|---|---|---|---|---|---|
| GTAA-5 (primary) | 5.23% | 5.91% | 8.09% | -14.68% | 2008-05-21 to 2008-10-27 | -5.13% (2011-08-08) | -6.75% (2010-05) |
| SPY buy-hold | 10.90% | 14.90% | 19.52% | -55.19% | 2007-10-09 to 2009-03-09 | -10.94% (2020-03-16) | -16.52% (2008-10) |
| 60/40 SPY/AGG | 7.92% | 9.64% | 11.73% | -35.80% | 2007-10-09 to 2009-03-09 | -7.25% (2020-03-12) | -10.83% (2008-10) |
| T-bills only (^IRX accrual) | 1.57% | 1.81% | 0.15% | -0.00% | 2020-03-19 to 2020-03-30 | -0.00% (2020-03-30) | 0.00% (2021-05) |

- Excess over T-bills: GTAA-5 CAGR minus T-bill CAGR = 3.67 points (2007+), 4.10 points (2013+). Sharpe vs T-bills (daily excess, annualised): GTAA-5 0.48; SPY 0.55; 60/40 0.58. Last ^IRX print (2026-10-29): 3.998%.
- Window 2007: 2006-12-29 to 2026-10-29 = 19.83 years. Window 2013: 2012-12-31 to 2026-10-29 = 13.83 years. Terminal value of 1.00: GTAA-5 2.747, SPY 7.779, 60/40 4.527.
- Max-DD recovery date (GTAA-5): 2009-10-14. Max DD of 2013-onward window: -8.46% (2020-01-17 to 2020-03-18).
- Longest time below a prior equity high: 2.06 years (2011-04-29 to 2013-05-20).
- Sampling noise: with 19.8 years and 8.09% vol, the standard error of the mean annual return is about 1.82 points, so a CAGR gap of under ~1.8 points vs any threshold is statistically indistinguishable.
- Turnover/cost (primary, 10 bp on all ETF trades): one-way traded notional averages 183% of equity per year (includes monthly drift rebalancing of unchanged sleeves), a cost drag of about 0.18 points/yr (check: 0 bp vs 10 bp CAGR gap is in section 4).

### 2.1 Checks against the researcher's specific numbers

- Drawdown at end Oct 2008: -12.12%; min drawdown inside Oct 2008: -14.68% (2008-10-27); min drawdown inside 2008: -14.68% (2008-10-27). Researcher claimed -16.1% (Oct 2008).
- 2022: calendar return -4.72%; max drawdown vs all-time high during 2022 -6.18% (2022-12-28); max drawdown measured only inside 2022 -6.18% (2022-12-28).
- Decisions per year: 12 (one per month-end); number of month-end decisions after base: 237.

## 3. Calendar-year returns, switches, exposure

| Year | GTAA-5 | SPY | 60/40 | ON/OFF switches (sleeve changes) | Avg ETF exposure |
|---|---|---|---|---|---|
| 2007 | 8.48% | 5.15% | 5.85% | 5 | 88.4% |
| 2008 | -5.95% | -36.79% | -21.15% | 7 | 36.3% |
| 2009 | 17.05% | 26.35% | 16.93% | 4 | 58.9% |
| 2010 | 3.85% | 15.06% | 12.02% | 12 | 83.3% |
| 2011 | -1.27% | 1.89% | 4.48% | 9 | 75.0% |
| 2012 | 1.55% | 15.99% | 11.09% | 9 | 80.0% |
| 2013 | 8.20% | 32.31% | 17.51% | 5 | 66.5% |
| 2014 | 7.14% | 13.46% | 10.49% | 5 | 80.0% |
| 2015 | -2.32% | 1.23% | 1.15% | 9 | 51.3% |
| 2016 | 3.18% | 12.00% | 8.21% | 11 | 72.2% |
| 2017 | 9.46% | 21.71% | 14.13% | 6 | 77.0% |
| 2018 | -2.01% | -4.57% | -2.35% | 14 | 63.2% |
| 2019 | 8.26% | 31.22% | 21.90% | 8 | 67.2% |
| 2020 | 3.00% | 18.33% | 14.62% | 10 | 58.5% |
| 2021 | 21.76% | 28.73% | 15.75% | 6 | 88.3% |
| 2022 | -4.72% | -18.18% | -15.85% | 12 | 30.9% |
| 2023 | 5.00% | 26.18% | 17.64% | 17 | 59.9% |
| 2024 | 6.65% | 24.89% | 14.98% | 7 | 81.6% |
| 2025 | 9.32% | 17.72% | 13.54% | 8 | 81.8% |
| 2026 | 11.45% | 13.70% | 7.86% | 6 | 88.0% |

(2026 is year-to-date through 2026-10-29. 2007 is measured from the 2006-12-29 close.)

- Switches per year (sleeve ON/OFF changes), complete years 2007-2025: mean 8.63, median 8.0, max 17 (2023), min 4. Total over run: 170.
- Exposure fraction (time-weighted share of equity held in the five ETFs, positions carried into each day): 69.2%. Share of days each sleeve is ON: SPY 79.4%, EFA 68.6%, AGG 75.9%, VNQ 66.6%, DBC 55.6%.
- Worst calendar year: 2008 at -5.95%. Best: 2021 at 21.76%. Negative years: 5 of 20.

## 4. Sensitivities on the month-end strategy (NOT used for the verdict)

| Variant | CAGR 2007+ | CAGR 2013+ | Max DD | 2022 | Vol |
|---|---|---|---|---|---|
| PRIMARY: 10 bp all ETF trades, ^IRX | 5.23% | 5.91% | -14.68% | -4.72% | 8.09% |
| 10 bp on ON/OFF switches only, ^IRX | 5.25% | 5.92% | -14.67% | -4.71% | 8.09% |
| 0 bp costs, ^IRX | 5.42% | 6.11% | -14.60% | -4.49% | 8.09% |
| 20 bp all ETF trades, ^IRX | 5.04% | 5.71% | -14.75% | -4.95% | 8.09% |
| cash 0% | 4.77% | 5.34% | -14.96% | -6.22% | 8.09% |
| cash 2% flat | 5.41% | 6.00% | -14.54% | -4.93% | 8.09% |
| cash = FRED DTB3 (cross-check) | 5.24% | 5.92% | -14.67% | -4.69% | 8.09% |

- FRED DTB3 fetched: 1954-01-04 .. 2026-10-28, 18197 obs.

## 5. Adjusted vs unadjusted prices for the signal (live-implementation risk)

- Signal built on raw `Close` (split-adjusted, NOT dividend-adjusted) vs adjusted close: 89 differing asset-month flags out of 1190 (7.5%). Per asset: SPY 2, EFA 14, AGG 43, VNQ 22, DBC 8.
- Strategy with raw-close signals (returns still adjusted): CAGR 2007+ 5.38%, 2013+ 5.45%, max DD -12.93%, 2022 -3.58%. A live bot must feed total-return (dividend-adjusted) prices to the SMA or it will deviate, mostly on AGG/VNQ.

## 6. Rebalance-day sweep

Definitions (pre-registered): decision on the first trading day on or after calendar day d, decided and traded at that day's close. PRIMARY: ON if that day's close > mean of the 10 most recent completed month-end adjusted closes (prior month-end and the 9 before). SECONDARY: ON if close > mean of the 9 most recent completed month-ends (algebraically identical to 'mean of {today's close + 9 prior month-ends} < today's close'). All variants start at the 2006-12-29 close in the state set by their latest decision on or before that date, 10 bp on all ETF trades, cash = ^IRX.

**PRIMARY definition**

| Rebalance day | CAGR 2007+ | CAGR 2013+ | Max DD | DD peak to trough | 2022 | Switches/yr | # decisions |
|---|---|---|---|---|---|---|---|
| day 1 | 4.89% | 4.91% | -18.19% | 2020-01-17 to 2020-03-23 | -5.26% | 8.67 | 238 |
| day 10 | 4.82% | 5.47% | -14.17% | 2008-07-02 to 2009-08-17 | -3.19% | 8.77 | 238 |
| day 20 | 4.67% | 4.53% | -21.54% | 2020-01-17 to 2020-10-30 | -2.13% | 8.72 | 238 |
| month-end (ref, not gated) | 5.23% | 5.91% | -14.68% | 2008-05-21 to 2008-10-27 | -4.72% | 8.57 | 237 |

- Spread across d = 1, 10, 20 (max - min): 2007+ **0.22 points**; 2013+ 0.94 points. Including month-end as a fourth point: 0.57 points (2007+).

**SECONDARY definition**

| Rebalance day | CAGR 2007+ | CAGR 2013+ | Max DD | DD peak to trough | 2022 | Switches/yr | # decisions |
|---|---|---|---|---|---|---|---|
| day 1 | 5.05% | 4.84% | -18.19% | 2020-01-17 to 2020-03-23 | -4.67% | 8.87 | 238 |
| day 10 | 5.27% | 5.74% | -15.42% | 2008-07-02 to 2009-07-10 | -2.00% | 9.68 | 238 |
| day 20 | 4.74% | 4.44% | -22.63% | 2020-01-17 to 2020-10-30 | -1.21% | 9.38 | 238 |
| month-end (ref, not gated) | 5.23% | 5.91% | -14.68% | 2008-05-21 to 2008-10-27 | -4.72% | 8.57 | 237 |

- Spread across d = 1, 10, 20 (max - min): 2007+ **0.53 points**; 2013+ 1.30 points. Including month-end as a fourth point: 0.53 points (2007+).

- Drawdown-timing sensitivity: across the 7 timing variants above (month-end, plus days 1/10/20 under both definitions) the 2007+ max daily-close drawdown ranges from -14.17% to -22.63%; 4 of 7 breach the -18% gate level. The gate line is evaluated on the month-end strategy as specified, but this shows the -18% margin is inside the rebalance-timing luck range.

## 7. Gate evaluation (exactly as pre-registered)

| Gate line | Threshold | Measured | Result |
|---|---|---|---|
| CAGR 2007-01 to latest | >= 4.5% | 5.23% | PASS |
| CAGR 2013-01 to latest | >= 4.0% | 5.91% | PASS |
| Max daily-close drawdown (full run) | >= -18% | -14.68% | PASS |
| Calendar-2022 return | >= -12% | -4.72% | PASS |
| CAGR spread over rebalance days 1/10/20 (primary def., 2007+) | <= 2.0 points | 0.22 points | PASS |

**VERDICT: PASS** (5 of 5 lines met). Secondary-definition spread, for information: 0.53 points (within the 2.0 limit).

## 8. Execution-lag sensitivity (signal at the close, trade at the NEXT session's close)

| Variant | Execution | CAGR 2007+ | CAGR 2013+ | Max DD | DD peak to trough | 2022 |
|---|---|---|---|---|---|---|
| month-end | same close | 5.23% | 5.91% | -14.68% | 2008-05-21 to 2008-10-27 | -4.72% |
| month-end | next close | 4.66% | 5.44% | -15.44% | 2008-05-21 to 2008-10-27 | -5.53% |
| day 1 (primary def.) | same close | 4.89% | 4.91% | -18.19% | 2020-01-17 to 2020-03-23 | -5.26% |
| day 1 (primary def.) | next close | 4.82% | 5.07% | -18.33% | 2020-01-17 to 2020-03-23 | -3.60% |
| day 10 (primary def.) | same close | 4.82% | 5.47% | -14.17% | 2008-07-02 to 2009-08-17 | -3.19% |
| day 10 (primary def.) | next close | 5.10% | 5.24% | -13.61% | 2011-04-29 to 2011-08-08 | -3.72% |
| day 20 (primary def.) | same close | 4.67% | 4.53% | -21.54% | 2020-01-17 to 2020-10-30 | -2.13% |
| day 20 (primary def.) | next close | 4.80% | 4.60% | -22.24% | 2020-01-17 to 2020-05-11 | -3.13% |
| month-end, next close, 20 bp | - | 4.47% | 5.24% | -15.58% | 2011-04-29 to 2012-06-04 | -5.76% |
| month-end, next close, cash 0% | - | 4.20% | 4.88% | -15.72% | 2008-05-21 to 2008-10-27 | -7.02% |
| month-end, same close, cash 0% (closest to the researcher's likely setup) | - | 4.77% | 5.34% | -14.96% | 2008-05-21 to 2008-10-27 | -6.22% |

## 9. Integrity checks on this script and the data

- Engine vs independent segment-compounding re-implementation (0 bp, ^IRX cash, month-end signal): max |equity difference| = 2.13e-14 over 4989 days (end values 2.851275 vs 2.851275).
- Month-end flags re-computed with plain-Python loops: 1195 asset-month flags checked, 0 mismatches.
- Total-return reconstruction (raw Close + cash dividends, reinvested at the close) vs yfinance adjusted close, 2006-12-29..latest (same vendor, so this tests the adjustment arithmetic, NOT the price source):

| Asset | Max abs deviation | Deviation at end | # dividend events |
|---|---|---|---|
| SPY | 0.066% | 0.060% | 79 |
| EFA | 0.094% | 0.020% | 39 |
| AGG | 0.015% | 0.003% | 237 |
| VNQ | 0.249% | -0.212% | 79 |
| DBC | 0.072% | -0.006% | 8 |

- Conditional tax caveat (account-holder status NOT known to this task): if 30% US withholding were applied to ALL dividends (an upper bound; interest-type AGG dividends are largely exempt for non-US persons), the simple annual drag on this exposure pattern would be about 0.52 points/yr (SPY 0.08, EFA 0.10, AGG 0.14, VNQ 0.15, DBC 0.03). Not included in any CAGR above. US taxpayers would instead face short-term-gain treatment on most switches.
- Second independent price vendor: NOT obtained. Stooq's CSV endpoint returns a JavaScript bot-challenge page, which I did not try to bypass; FRED carries no ETF prices. All ETF prices therefore rest on one vendor (Yahoo via yfinance). FRED DTB3 was used only for the cash cross-check.

## 10. Current state of the signal (informational, no action)

| Month-end | SPY | EFA | AGG | VNQ | DBC |
|---|---|---|---|---|---|
| 2026-06-30 | ON  (+7.7%) | ON  (+6.6%) | ON  (+0.5%) | ON  (+6.1%) | ON  (+4.8%) |
| 2026-07-31 | ON  (+6.5%) | ON  (+6.7%) | off (-0.9%) | ON  (+7.7%) | ON  (+12.4%) |
| 2026-08-31 | ON  (+7.9%) | ON  (+6.8%) | off (-0.5%) | ON  (+3.9%) | ON  (+15.4%) |
| 2026-09-30 | ON  (+6.3%) | ON  (+2.1%) | off (-2.8%) | off (-2.8%) | ON  (+15.0%) |

Last close vs mean of the last 9 completed month-ends (preview of the next month-end test, which also includes that day's own close): SPY +6.5%, EFA +1.7%, AGG -0.8%, VNQ +1.0%, DBC +17.3%.

## 11. What this means in dollars on $10,000 (computed, not estimated)

| Strategy ($10K at 2006-12-29) | Mean $/month (compounding path) | Median $/month | Months down | Worst month $ | Worst calendar year $ | Worst rolling 12m $ | Max peak-to-trough $ (on $10K at the peak) | Value today of $10K |
|---|---|---|---|---|---|---|---|---|
| GTAA-5 | $44 | $59 | 35% | $-675 | $-595 (2008) | $-1150 (to 2012-05) | $-1468 | $27,471 |
| 60/40 | $67 | $103 | 34% | $-1083 | $-2115 (2008) | $-2768 (to 2009-02) | $-3580 | $45,266 |
| SPY | $96 | $151 | 34% | $-1652 | $-3679 (2008) | $-4342 (to 2009-02) | $-5519 | $77,793 |

- Starting a fresh $10,000 at the 2012-12-31 close would be worth $22,117 at 2026-10-29, an average of $73 per month of simple gain.
- Practical: SPY adjusted close on 2026-10-29 is $769.36; one 20% sleeve of a $10,000 account is $2,000, i.e. 2.60 SPY shares. Whole-share rounding on SPY alone can misstate that sleeve by up to 19% unless the broker supports fractional shares.


---

## 12. Data gaps, adjustment caveats, and what could not be verified

1. **One price vendor only.** All ETF prices are Yahoo via yfinance 1.3.0. A second independent feed was not obtained: Stooq's CSV endpoint returned a JavaScript bot-challenge page and I did not attempt to bypass it; FRED has no ETF prices. The price source itself is therefore unverified. What was checked: no internal gaps in any ETF series after inception, and total return rebuilt from raw close plus cash dividends matches yfinance's adjusted series within 0.25% worst case (VNQ), 0.015% for AGG. Prices for 2025-2026 are taken on the vendor's word.
2. **Adjustment drift.** `auto_adjust=True` back-adjusts all history at download time, so the numbers can shift slightly on a later run (fingerprint in section 1). Using the adjusted series in the SMA creates no look-ahead: later dividends rescale every earlier price by the same factor, so the price-versus-SMA comparison at any date is unchanged. A live system must use a dividend-adjusted (total-return) series; raw closes change 7.5% of the flags (section 5).
3. **Partial bar and incomplete month.** The 2026-10-30 bar was in-session (fetched 12:09 ET) and was dropped. October's month-end signal is not yet due, so the final position in the curve is the one set at the 30 Sep 2026 signal. The informational preview in section 10 is not a signal.
4. **DBC history.** yfinance's first DBC bar is 2006-02-06. The first complete 10-month SMA for all five assets is the 30 Nov 2006 month-end, so the 2006-12-29 base date needs no pre-inception data. DBC is a futures-based commodity pool with different tax reporting from the other four.
5. **Proxy mapping.** The ETFs stand in for the paper's index series (S&P 500, MSCI EAFE, 10-year Treasury, NAREIT, GSCI). AGG is an aggregate bond fund, not 10-year Treasuries. I implemented the rule as given in the brief and did not fetch SSRN 962461 to check it against the paper's text or its published results.
6. **Same-close execution** is the pre-registered (optimistic) assumption. The next-close results in section 8 are the more realistic bound. No intraday slippage, bid-ask spread beyond the flat 10 bp, or market impact is modelled.
7. **Cash.** ^IRX is a discount yield accrued on a 365-day basis with the prior day's print and forward-filled over 5 bond-market holidays; 7 slightly negative prints in March 2020 were used as-is. FRED DTB3 gives a CAGR within 0.01 point (section 4). A real cash vehicle (a T-bill ETF or broker sweep) has its own fees that are not modelled.
8. **Taxes and withholding are not modelled.** Section 9 gives a conditional upper-bound estimate of about 0.5 points a year if 30% US withholding hit all dividends; the account holder's status was not part of this task.
9. **Sample size.** One 19.8-year path through a falling-rate period (average ^IRX 1.77%, now ~4.0%). Standard error of the mean return is about 1.8 points, so the gate thresholds and the researcher's 4.8% versus this 5.2% are not statistically distinguishable.
10. **Not reproduced:** the researcher's -16.1% drawdown. Not checked: the researcher's data source, rebalance convention, or cash treatment (the 0% inference is from matching numbers only).
11. This is research output, not investment advice, and nothing was traded. No keys or .env files were read. The only files written are `scripts/gtaa5_replay.py` and this document; nothing was committed. Downloaded data was cached outside the repo.

## 13. Reproduce

`cd /Users/xynkro/Trading/ZeroDTE-Wave && .venv/bin/python scripts/gtaa5_replay.py --out /tmp/gtaa5_results.md` (needs network for yfinance and FRED; `--asof YYYY-MM-DD` truncates the data for a sensitivity run; `GTAA5_CACHE_DIR` caches the download outside the repo). Re-running after the close or after new dividends will move the fourth decimal and the last data date. A truncated run at 2026-09-30 also returns PASS 5 of 5.
