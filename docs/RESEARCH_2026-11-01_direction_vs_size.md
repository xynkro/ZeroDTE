# Direction is a coin flip, size is forecastable — SPY, 2020-07-27 → 2026-10-30
Written 2026-11-01 (Sat 22:45 ET / Sun 10:45 SGT) to answer Caspar's question: "if it's a bad day we
don't trade, simple? or if it's a bad day we sell calls because we know it's going down? we can tell
direction before the open, right?" Scripts: `scripts/gap_vs_intraday.py` (daily bars, 1,575 days) and
`scripts/morning_vs_afternoon.py` (30-min bars, 1,561 sessions). Data: Alpaca stock bars, `iex` feed
(paper keys; the `sip` feed is not licensed on this account). Re-run anytime; no model, no fitting.

## 1. Pre-market gap → the day after the open
| gap (open vs prev close) | days | day continues in gap direction | day reverses past the open | gap-day closes beyond prev close | high ran >0.30% above open (kills a call spread sold at the open) | low ran >0.30% below open (kills a put spread) |
|---|---|---|---|---|---|---|
| down > 1.0% | 91 | 45.1% | 54.9% | 89.0% | **79.1%** | 71.4% |
| down 0.5–1.0% | 140 | 48.6% | 51.4% | 82.9% | 67.9% | 66.4% |
| down 0.2–0.5% | 210 | 45.7% | 54.3% | 66.7% | 62.9% | 60.5% |
| flat ±0.2% | 535 | – | – | – | 53.8% | 54.2% |
| up 0.2–0.5% | 332 | 50.6% | 49.4% | 71.1% | 54.5% | 60.5% |
| up 0.5–1.0% | 198 | 56.6% | 43.4% | 82.8% | 62.1% | 62.6% |
| up > 1.0% | 69 | 66.7% | 33.3% | 88.4% | 84.1% | 62.3% |

corr(gap, open→close move) = **−0.03**. corr(|gap|, day range) = **+0.27**. Median day range 0.94% on all
days, 1.50% after a ≥1% gap.

Reading: the pre-market move is already in the price at 09:30. What happens AFTER the open is a coin
flip (45–57% continuation; gap-downs lean to reversal). "We know it is going down so sell calls" fails
on the number that matters: on the 91 worst gap-down days the intraday high still ran more than 0.30%
above the open 79% of the time, so a call spread sold at the open at 0.30% was tested four days in five.
What the gap DOES tell you is how big the day will be.

## 2. The 13:00 mech arm: does the morning predict the afternoon?
corr(morning move 09:30→13:00, afternoon move 13:00→close) = **+0.03** (direction: nothing).
corr(morning range, afternoon range) = **+0.50** (size: strong).

| morning range 09:30–13:00 | sessions | afternoon touches +0.30% | touches −0.30% | touches EITHER | touches BOTH |
|---|---|---|---|---|---|
| quietest 25% (< 0.51%) | 390 | 16.9% | 26.4% | **42.1%** | 1.3% |
| 25–50% (0.51–0.74%) | 390 | 32.1% | 31.3% | 59.0% | 4.4% |
| 50–75% (0.74–1.06%) | 390 | 45.6% | 40.0% | 76.2% | 9.5% |
| 75–90% (1.06–1.49%) | 234 | 50.9% | 54.3% | 88.0% | 17.1% |
| loudest 10% (≥ 1.49%) | 157 | 68.2% | 65.6% | **98.1%** | 35.7% |

Morning down >0.30% by 13:00 (n=384): afternoon continues down 49.7%, reverses 50.0%, touches +0.30%
anyway 50.3%. Morning up >0.30% (n=461): continues 56.2%, reverses 43.8%, touches −0.30% anyway 36.2%.

## Decision (pre-registered amendment, see TRIAL_GATES.md 2026-11-01)
The mech arm sells both sides 0.30% from spot with a stop near 2× credit (≈ −$30 against ≈ +$12 credit
on XSP), so a side needs roughly a 71% win rate to break even. On loud mornings (range ≥ ~1.0%) each
side is touched 46–68% of the time: that cell is negative by construction and is now SKIPPED
(`CALL_MECH_MAX_AM_RANGE_PCT`, default 1.0; 0 disables). Direction is NOT used to pick a side: the
arm stays two-sided. Caspar's own /read trades are not gated. The NBBO surface sampler keeps recording
skipped sessions, so the gate can be checked against real credits after the two-week trial.
Caveat: credits are larger on loud days; the surface rows (not these bars) will settle whether the
extra credit ever pays for the touch rate.
