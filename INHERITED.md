# Inherited from MEIC — 2026-09-22

MEIC was retired on the pre-registered gate (32 nights, −$67, HAC t −0.38, no
edge). The strategy died; the infrastructure it paid for did not. This is what
moved across, why it exists, and what was deliberately NOT moved.

## Ported and live

| What | Why it exists (all learned the expensive way on MEIC) |
|---|---|
| **Feed self-heal** — hard exit at 2× the stale alarm, launchd KeepAlive relaunches | MEIC ran ~20 min with unmanaged open positions because a human had to notice and restart. KeepAlive is verified `true` for `com.caspar.wavezero-backend`, so the exit is a restart, not a kill. 15-min uptime guard prevents a boot-loop when the provider itself is down. |
| **Feed promotion** — yfinance → Alpaca during RTH | A boot while the market was closed fell back to YFinanceFeed and **camped there for three trading days** on degraded bars. Now it restarts into Alpaca after 10 min on the fallback. |
| **Ghost sanitizer** — prior-day open trades/condors stamped closed at restore | A 0DTE position cannot outlive its session. On MEIC a restored ghost got marked to a phantom price, fired a stop, and 422'd buying back legs we no longer held. **On Wave the risk is worse and quieter:** `MAX_CONCURRENT_POSITIONS` counts open trades, so one ghost silently throttles new entries. |
| **Connect-error order retries** — `ConnectError`/`ConnectTimeout` now raise | Connection-never-established means the order was **not delivered**, so a retry is safe; read-timeouts stay swallowed because the order may have landed and a retry could double it. MEIC lost live orders to 2am-SGT DNS blips. |
| **`debrief.annotate_log_row`** | The broker number is only knowable *after* a fetch that runs later than the nightly row write, so the row otherwise carries MODEL P&L forever. This is the shape of Wave's known integrity gap (`project_zerodte_wave_pnl`) — the tool to close it is now here. |

## Deliberately NOT ported

**The FOMC stand-aside filter, and every other entry filter.** Wave is mid-trial
under pre-registered gates (`docs/TRIAL_GATES.md`, Config F, 2/25). Adding a
filter now would change what Wave trades mid-sample and contaminate the very
result the trial exists to measure — the exact sin the gates were written to
prevent. The FOMC evidence is real and sits in `../ZeroDTE/DECISION.md` (34
events, mean −$2,082/day at SPX scale, 12 of the 20 worst days): it is a
candidate for Wave's **next** pre-registration, not a mid-flight patch.

Everything ported above is ops, safety, or reporting — none of it changes an
entry or exit decision, so the trial stays clean.

## The discipline itself

The most valuable inheritance is not code. It is that MEIC was retired by a rule
written in July, on evidence, without negotiation — `DECISION.md` R1. Wave has
the same structure in `docs/TRIAL_GATES.md`. Honour it the same way.
