# GEX/OI Dealer-Positioning — First Look

**Date:** 2026-07-24 (scheduled checkpoint, ~20 session-days after the forward logger went live 2026-06-26)
**Type:** READ-ONLY exploratory analysis. No trade-path change, no flag flipped.
**Verdict:** **(b) Nothing yet — keep logging silently. Re-check at ~40 session-days.** But read the caveat: the binding constraint is **loss-day count**, not GEX-day count, and a benign tape won't fix that.

---

## TL;DR

The one genuinely-untested edge (dealer positioning: GEX walls, gamma pins, max-pain, OI-anchored strikes) **could not be tested for winner/loser separation on this sample — because WAVE barely lost.** The validated config went **7-0** (zero losers, zero breaches). The un-gated baseline went **19-1**, and **not one of the 20 days had price come within its short strike** (`worst_through = 0` on all but the single marginal breach). There is essentially **no adverse-outcome variance** for *any* predictor — dealer or oscillator — to explain over this window. This is neither promising nor a dud; it's "insufficient signal," and the reason is the sample, not the hypothesis.

---

## Data accrued

- `gex_history.jsonl`: 590 snapshots, **22 distinct days carry a non-empty `oi` block** (07-12 is a weekend artifact with 2 snapshots → **21 real trading session-days**, 2026-06-26 → 2026-07-24). Clears the ~10-day floor; at the ~20-day target.
- Each trading day has intraday snapshots ~07:00–16:30 ET (~30-min cadence), so a **~10:00 ET snapshot exists for every day** — features taken from the snapshot nearest 10:00 ET.

## WAVE win/loss — how it was obtained

- **No live broker-truth for the validated WAVE config exists.** `debrief_log.jsonl` is 100% MEIC (`ic_*`); the validated band-anchored config is **not wired live** (HANDOFF "NEXT BUILD" is still queued). Fell back to the documented method: pulled **fresh SPY→SPX 5m bars (2022 → 2026-07-24, 88.7k bars)** into scratchpad (the live `SPX_5m_3y.json` ends 2026-05-14 and was **not** touched), and ran the **exact** `scripts/wave_band_backtest.py:main` logic on the extended series via an `_load` monkeypatch — zero re-implementation.
- **Validated config** (`regime="released"`, BB 14/2.5): traded **7 of 21** days, **7 win / 0 loss**, all TP, total +$495 (in-model $, overstates).
- **Un-gated baseline** (`regime="all"`, BB 14/2.5): traded **20 of 21** days, **19 win / 1 loss**, total +$1,250. Used **only** to manufacture loss variance so the methodology could run at all.

## Dealer features derived (nearest-10:00-ET snapshot)

Distance spot→max-pain, spot→nearest hi-OI call wall (above), spot→nearest hi-OI put wall (below), gamma regime (±), net GEX ($B), net ratio; plus whether the WAVE short sat inside vs beyond the same-side OI wall and beyond max-pain.

## Result — separation test

- **Validated config: UNDEFINED.** 0 losers → no loss variance → point-biserial is not computable. The whole premise ("do dealer levels separate winners from losers?") has nothing to separate.
- **Un-gated baseline: n_loss = 1 (2026-07-13, a marginal put-spread breach, −$80).** Every point-biserial "correlation" is therefore driven entirely by that single day's feature values and is **statistically meaningless**. For the record, the largest |r| were net_gex_b (+0.24), gamma_pos (+0.23), net_ratio (+0.21) — i.e. the lone loss happened to fall on a high-positive-gamma day. With n_loss=1 this is pure noise, not signal, and the direction (a breach *in* a pinning positive-gamma regime) is counter-intuitive anyway.
- **Filter test:** gating on the "best" feature (net_gex_b) never isolates the −$80 day until the kept set collapses to 4 — no clean cut. Nothing survives.
- **The oscillators didn't "fail" here either** — there was simply nothing for them (or GEX) to predict. In the original 3y study the oscillators failed *against a real population of losers*; this window has none.

## Notable (weak, n=1) observation

The single baseline loser (07-13) was **gated out by the validated vol-released regime** — but by the *volatility* gate, independent of any GEX feature. No evidence dealer positioning added anything on top.

## Data-quality flags for the logger (fix before the next look)

These will blunt any feature test even at 40+ days:
1. **`hi_oi_puts` is dominated by deep crash-hedge tail strikes** (e.g. 6000/5200-strike puts vs 7400 spot → `d_putwall` of +17–19% on several days). "Nearest put wall" from the raw top-3 OI is **not** a near-money support level. Consider filtering OI walls to a ±X% band around spot, or weighting by gamma not raw OI.
2. **`pos_gamma_strikes` occasionally degenerate** (e.g. 2600/2800/3000 vs 7400 spot in the last snapshot) — looks like a computation/filter bug on some days.
3. **`gamma_flip` is frequently `null`**, and top-level `call_wall`/`put_wall` sometimes collapse to the same strike (7000/7000). Low usable coverage.

## Recommendation

**(b) Nothing yet — keep logging silently.** The dealer angle is neither confirmed nor refuted; the sample gave it nothing to work with.

Two concrete asks for the next checkpoint:
- **Re-check at ~40 session-days**, but recognise the real gate is **≥ ~8–10 WAVE loss/adverse days**, which a calm tape (like this July) will not supply on schedule. Consider triggering the next look on *loss-day count*, not calendar days.
- **Clean the OI feature extraction** (flags above) so a longer sample yields testable near-money walls rather than tail-hedge noise.

---

*Method mirrors `scripts/wave_failure_analysis.py` (point-biserial + IS/OOS-style filter test). Analysis script: `scripts/gex_oi_first_look.py`. In-model $ overstate; the point here is variance structure, not absolute P&L. ~21-day exploratory sample — no edge is declared or denied on 21 points.*
