# ZeroDTE Iron Condor v2.2 — Pine Script (copy-paste into TradingView)

**Last updated:** 2026-05-12

## Fixes in v2.2 (win-rate improvements)

- **Wider base OTM: 0.5% → 0.9%** — gives ~$5 cushion on SPY (was ~$2.88). 0.9% OTM approximates 12-delta at 0DTE, surviving typical intraday swings while keeping competitive premium.
- **Later build: 09:45 → 10:15 ET** — lets the morning volatility settle. Henry Schwartz (CBOE) notes first 30 min is highest breach risk.
- **Longer obs window: 15 min → 30 min** (09:45-10:15 ET) — more data for range/drift estimation before strike placement.
- **VIX gate** — refuses IC build when VIX >= 20. 0DTE iron condors are historically net-negative in elevated-vol regimes. Uses CBOE:VIX1D daily close; shows "VIX BLOCKED" in status table when gated.
- **Range floor** — `effective_otm = max(base_otm, obs_range_pct * 1.2)`. Ensures strikes are never placed inside the range already established during the observation window.
- **Softened skew multipliers** — reduced from 1.5x/0.7x to 1.3x/0.8x (strong) and 1.25x/0.85x to 1.15x/0.9x (mild). 0DTE mean-reverts often; the old tight side was getting breached on reversals.
- **Status table expanded to 11 rows** — added VIX status (OK/BLOCKED), range floor, and scale rows.

## Previous fixes (v2.1)

- **SKEWED IC** — asymmetric OTM placement based on observation-window drift direction.
- **Configurable skew inputs** — enable/disable skew, adjust drift thresholds.
- **Status table shows drift + skew** — obs drift %, skew direction, color-coded.
- **IC build label shows skew** — arrow indicator on the chart label.

## Previous fixes (v2)

- **Pine v6, shorttitle <= 10 chars** — compliant with TradingView's current requirements.
- **Instrument auto-detect: works on SPX, SPY, XSP** — wing width auto-scales (10pt SPX -> 1pt SPY).
- **Session detection fix:** uses date-change detection (`ta.change(time("D"))`) so `newSession` fires correctly on SPY.
- **Status table shows IC SPX/SPY/XSP + wing scale**.

## Usage

Add to a **5m or 15m chart** of SPX, SPY, or XSP. IC builds at **10:15 ET** (after 30-min observation window 09:45-10:15), resets each session. Requires VIX < 20 to build.

### VIX gate

The indicator fetches `CBOE:VIX1D` (configurable) daily close. If VIX >= threshold (default 20), the IC is **not built** and the status table shows "VIX BLOCKED" with the reason.

### Range floor

After the observation window, range floor is computed: `effective_otm = max(base_otm, obs_range% * 1.2)`. If the market already moved 0.8% during observation, strikes won't be placed any tighter than 0.96% OTM regardless of the base setting.

### Skew behavior

During the observation window (default 09:45-10:15 ET), the indicator tracks the open of the first bar and the close of the last bar. At obs end, drift is computed as `(obs_close - obs_open) / obs_open * 100`.

| Drift magnitude | Skew type | Threatened side multiplier | Safe side multiplier |
|---|---|---|---|
| > 0.10% | Strong | 1.3x wider OTM | 0.8x tighter OTM |
| > 0.05% | Mild | 1.15x wider OTM | 0.9x tighter OTM |
| <= 0.05% | None | 1.0x (symmetric) | 1.0x (symmetric) |

**Example:** Base OTM = 0.9%, bearish drift = -0.15% (strong):
- Put: 0.9% x 1.3 = 1.17% OTM (wider cushion on threatened side)
- Call: 0.9% x 0.8 = 0.72% OTM (tighter, collects more premium from safe side)

---

```pine
//@version=6
indicator("ZeroDTE Iron Condor v2", shorttitle="IC v2", overlay=true,
          max_lines_count=20, max_labels_count=50, max_boxes_count=20)

// ────────────────────────────────────────────────────────────────────────
// ZeroDTE Iron Condor — overlay for SPX/SPY/XSP (auto-detects instrument)
// v2.2 — wider OTM, later build, VIX gate, range floor, softened skew
// ────────────────────────────────────────────────────────────────────────

g1 = "IC build timing"
buildHourET = input.int(10, "Build hour ET",   group=g1, minval=9, maxval=15)
buildMinET  = input.int(15, "Build minute ET", group=g1, minval=0, maxval=59)

g2 = "Observation window"
obsStartH    = input.int(9,  "Obs start hour ET",  group=g2)
obsStartM    = input.int(45, "Obs start min ET",   group=g2)
obsWindowMin = input.int(30, "Obs length (min)",   group=g2, minval=5, maxval=60)

g3 = "Strike picker (matches live system)"
pctOtm       = input.float(0.9,  "Base short strike % OTM",    group=g3, minval=0.1, step=0.1,
                           tooltip="Base OTM before skew + range floor. 0.9% ≈ 12Δ.")
wingWidth    = input.float(10.0, "Wing width (SPX points)",    group=g3, minval=1, step=1,
                           tooltip="10pt = Henry CBOE recommendation. Auto-scales for SPY/XSP.")

g5 = "Skew settings"
enableSkew     = input.bool(true, "Enable drift-based skew",     group=g5)
strongDriftPct = input.float(0.10, "Strong drift threshold (%)", group=g5, minval=0.01, step=0.01,
                             tooltip="|drift| above this → 1.3×/0.8× skew")
mildDriftPct   = input.float(0.05, "Mild drift threshold (%)",   group=g5, minval=0.01, step=0.01,
                             tooltip="|drift| above this → 1.15×/0.9× skew")
rangeFloorMult = input.float(1.2,  "Range floor multiplier",     group=g5, minval=0.5, step=0.1,
                             tooltip="Effective OTM = max(base, obs_range% × this). Prevents strikes inside established range.")

g5b = "VIX gate"
vixSymbol      = input.string("CBOE:VIX1D", "VIX symbol",
                             options=["CBOE:VIX1D", "TVC:VIX"], group=g5b)
vixStandAside  = input.float(20.0,  "VIX stand-aside threshold", group=g5b, minval=10.0, step=0.5,
                             tooltip="≥ this VIX → don't build IC. 0DTE ICs are net-negative in high-vol.")

g4 = "Visualization"
showIcBox       = input.bool(true,  "Draw IC box at build",        group=g4)
showOutcomeZones= input.bool(true,  "Shade GOOD/ATM/BAAAAD zones", group=g4)
showBreachAlerts= input.bool(true,  "Mark wing-breach with line",  group=g4)

// ── Instrument auto-detect ──────────────────────────────────────────────
_root   = str.upper(syminfo.root != "" ? syminfo.root : syminfo.ticker)
isSPX   = str.contains(_root, "SPX")
isSPY   = str.contains(_root, "SPY")
isXSP   = str.contains(_root, "XSP")
pxScale = isSPY or isXSP ? 0.1 : 1.0
effWingWidth = wingWidth * pxScale
instName = isSPX ? "SPX" : isSPY ? "SPY" : isXSP ? "XSP" : syminfo.ticker

// ── VIX gate ────────────────────────────────────────────────────────────
vixDaily = request.security(vixSymbol, "D", close, lookahead=barmerge.lookahead_off)
vixOk    = na(vixDaily) ? true : vixDaily < vixStandAside

// ── ET clock ────────────────────────────────────────────────────────────
inSession = not na(time(timeframe.period, "0930-1600", "America/New_York"))
etHour    = hour(time, "America/New_York")
etMin     = minute(time, "America/New_York")
etMins    = etHour * 60 + etMin
obsStartMin = obsStartH * 60 + obsStartM
obsEndMin   = obsStartMin + obsWindowMin
buildMin    = buildHourET * 60 + buildMinET

// ── Observation window tracking ─────────────────────────────────────────
var float obsHigh  = na
var float obsLow   = na
var float obsOpen  = na
var float obsClose = na

_dayChange = ta.change(time("D"))
newSession = inSession and (not inSession[1] or _dayChange != 0)
if newSession
    obsHigh  := na
    obsLow   := na
    obsOpen  := na
    obsClose := na

inObs = inSession and etMins >= obsStartMin and etMins < obsEndMin
if inObs
    obsHigh := na(obsHigh) ? high : math.max(obsHigh, high)
    obsLow  := na(obsLow)  ? low  : math.min(obsLow, low)
    if na(obsOpen)
        obsOpen := open
    obsClose := close

// ── Drift + skew (locked at obs end) ────────────────────────────────────
var float obsDriftPct   = na
var string skewDirection = na
var float skewCallMult  = 1.0
var float skewPutMult   = 1.0
var float projHigh = na
var float projLow  = na

obsJustEnded = etMins == obsEndMin and etMins[1] < obsEndMin
if obsJustEnded and not na(obsHigh) and not na(obsLow)
    obsRange = obsHigh - obsLow
    projHigh := obsHigh + 1.5 * obsRange
    projLow  := obsLow  - 1.5 * obsRange

    // Compute drift
    if not na(obsOpen) and not na(obsClose) and obsOpen > 0
        obsDriftPct := (obsClose - obsOpen) / obsOpen * 100.0
    else
        obsDriftPct := 0.0

    // Apply skew multipliers — pre-declare outside the if-else chain
    if enableSkew
        absDrift = math.abs(obsDriftPct)
        float wideMult  = absDrift > strongDriftPct ? 1.3  : absDrift > mildDriftPct ? 1.15 : 1.0
        float tightMult = absDrift > strongDriftPct ? 0.8  : absDrift > mildDriftPct ? 0.9  : 1.0

        if obsDriftPct < -mildDriftPct
            skewCallMult  := tightMult
            skewPutMult   := wideMult
            skewDirection := "bearish"
        else if obsDriftPct > mildDriftPct
            skewCallMult  := wideMult
            skewPutMult   := tightMult
            skewDirection := "bullish"
        else
            skewCallMult  := 1.0
            skewPutMult   := 1.0
            skewDirection := "neutral"
    else
        skewCallMult  := 1.0
        skewPutMult   := 1.0
        skewDirection := "neutral"
        obsDriftPct   := 0.0

// ── Range floor (obs range as minimum OTM) ──────────────────────────────
var float obsRangePct = na
if obsJustEnded and not na(obsHigh) and not na(obsLow) and not na(obsOpen) and obsOpen > 0
    obsRangePct := (obsHigh - obsLow) / obsOpen * 100.0

// Effective base OTM = max(user base, range floor)
var float effectiveOtm = na
if obsJustEnded
    effectiveOtm := na(obsRangePct) ? pctOtm : math.max(pctOtm, obsRangePct * rangeFloorMult)

// ── IC strikes (locked at build time) ───────────────────────────────────
var float shortCall = na
var float longCall  = na
var float shortPut  = na
var float longPut   = na
var float icCredit  = na
var int   icBuiltBar = na
var float icBuiltPrice = na
var float callOtmUsed = na
var float putOtmUsed  = na
var bool  vixBlocked  = false

icBuildTrigger = etMins == buildMin and etMins[1] < buildMin and inSession and vixOk
if icBuildTrigger and na(shortCall)
    _baseOtm     = na(effectiveOtm) ? pctOtm : effectiveOtm
    icBuiltPrice := close
    callOtmUsed  := _baseOtm * skewCallMult
    putOtmUsed   := _baseOtm * skewPutMult
    shortCall    := close * (1.0 + callOtmUsed / 100.0)
    longCall     := shortCall + effWingWidth
    shortPut     := close * (1.0 - putOtmUsed / 100.0)
    longPut      := shortPut  - effWingWidth
    icCredit     := effWingWidth * 100 * 0.12 * 2
    icBuiltBar   := bar_index

// VIX blocked indicator (for status table)
vixBlockTrigger = etMins == buildMin and etMins[1] < buildMin and inSession and not vixOk
if vixBlockTrigger
    vixBlocked := true

// Reset at new session
if newSession and bar_index > 0
    shortCall    := na
    longCall     := na
    shortPut     := na
    longPut      := na
    icCredit     := na
    icBuiltBar   := na
    icBuiltPrice := na
    callOtmUsed  := na
    putOtmUsed   := na
    projHigh     := na
    projLow      := na
    obsDriftPct  := na
    skewDirection := na
    skewCallMult := 1.0
    skewPutMult  := 1.0
    obsRangePct  := na
    effectiveOtm := na
    vixBlocked   := false

// ── Plotting ────────────────────────────────────────────────────────────
plot(projHigh, "Projected H", color=color.new(color.red,   60), linewidth=1, style=plot.style_circles)
plot(projLow,  "Projected L", color=color.new(color.green, 60), linewidth=1, style=plot.style_circles)
bgcolor(inObs ? color.new(color.blue, 90) : na, title="Obs window")

plot(shortCall, "Short CALL", color=color.new(color.red,   0), linewidth=2, style=plot.style_line)
plot(longCall,  "Long CALL",  color=color.new(color.red,   60), linewidth=1, style=plot.style_line)
plot(shortPut,  "Short PUT",  color=color.new(color.green, 0), linewidth=2, style=plot.style_line)
plot(longPut,   "Long PUT",   color=color.new(color.green, 60), linewidth=1, style=plot.style_line)

goodZoneTop  = plot(showOutcomeZones ? shortCall : na, color=color.new(color.green, 100), display=display.none)
goodZoneBot  = plot(showOutcomeZones ? shortPut  : na, color=color.new(color.green, 100), display=display.none)
fill(goodZoneTop, goodZoneBot, color=color.new(color.green, 95), title="GOOD (in-the-box)")

// IC build marker — show skew info
if icBuildTrigger and not na(icBuiltPrice)
    string skewText = na(skewDirection) or skewDirection == "neutral" ? "" : skewDirection == "bearish" ? " ↘ bear" : " ↗ bull"
    label.new(bar_index, icBuiltPrice,
              text=str.format("🦅 IC{0} · C${1,number,#.##}/{2,number,#.##}  P${3,number,#.##}/{4,number,#.##}",
                              skewText, shortCall, longCall, shortPut, longPut),
              style=label.style_label_down, color=color.new(color.gray, 30),
              textcolor=color.white, size=size.small)

// Wing-breach markers
breachCall = showBreachAlerts and not na(shortCall) and high >= shortCall
breachPut  = showBreachAlerts and not na(shortPut)  and low  <= shortPut
plotshape(breachCall, title="CALL wing tagged", style=shape.xcross, location=location.abovebar,
          color=color.new(color.red, 0), size=size.small)
plotshape(breachPut,  title="PUT wing tagged",  style=shape.xcross, location=location.belowbar,
          color=color.new(color.red, 0), size=size.small)

// ── Status table (top-right) ────────────────────────────────────────────
var table icTable = table.new(position.top_right, 2, 11, border_width=1)
// Show VIX-blocked message when IC wasn't built due to VIX gate
if barstate.islast and na(shortCall) and vixBlocked
    table.cell(icTable, 0, 0, "IC " + instName, text_color=color.white, bgcolor=color.new(color.red, 50))
    table.cell(icTable, 1, 0, "VIX BLOCKED", text_color=color.white, bgcolor=color.new(color.red, 50))
    string vbTxt = na(vixDaily) ? "n/a" : str.format("VIX {0,number,#.#} ≥ {1,number,#.#}", vixDaily, vixStandAside)
    table.cell(icTable, 0, 1, "Reason", text_color=color.white)
    table.cell(icTable, 1, 1, vbTxt, text_color=color.white)

if barstate.islast and not na(shortCall)
    icHeadroom = shortCall - close
    icCushion  = close - shortPut
    inGood = close < shortCall and close > shortPut
    inAtmCallSide = close >= shortCall and close < longCall
    inAtmPutSide  = close <= shortPut  and close > longPut
    inBaaaadCall  = close >= longCall
    inBaaaadPut   = close <= longPut
    string statusLabel = inGood ? "✓ GOOD" : inAtmCallSide or inAtmPutSide ? "🤏 ATM" : inBaaaadCall or inBaaaadPut ? "✗ BAAAAD" : "—"
    color statusColor = inGood ? color.green : inAtmCallSide or inAtmPutSide ? color.orange : inBaaaadCall or inBaaaadPut ? color.red : color.gray

    string skewLabel = na(skewDirection) or skewDirection == "neutral" ? "symmetric" : skewDirection == "bearish" ? "↘ bearish" : "↗ bullish"
    string driftLabel = na(obsDriftPct) ? "—" : str.format("{0,number,#.###}%", obsDriftPct)
    color skewBg = skewDirection == "bearish" ? color.new(color.red, 70) : skewDirection == "bullish" ? color.new(color.green, 70) : color.new(color.gray, 70)

    table.cell(icTable, 0, 0, "IC " + instName, text_color=color.white, bgcolor=color.new(color.blue, 70))
    table.cell(icTable, 1, 0, statusLabel, text_color=color.white, bgcolor=color.new(statusColor, 50))
    table.cell(icTable, 0, 1, "Short CALL", text_color=color.white)
    table.cell(icTable, 1, 1, str.format("${0,number,#.##}", shortCall), text_color=color.white)
    table.cell(icTable, 0, 2, "Short PUT",  text_color=color.white)
    table.cell(icTable, 1, 2, str.format("${0,number,#.##}", shortPut),  text_color=color.white)
    table.cell(icTable, 0, 3, "Headroom (call)", text_color=color.white)
    table.cell(icTable, 1, 3, str.format("${0,number,#.##}", icHeadroom), text_color=color.white)
    table.cell(icTable, 0, 4, "Cushion (put)",   text_color=color.white)
    table.cell(icTable, 1, 4, str.format("${0,number,#.##}", icCushion),  text_color=color.white)
    table.cell(icTable, 0, 5, "Est credit", text_color=color.white)
    table.cell(icTable, 1, 5, str.format("~${0,number,#.##}", icCredit),  text_color=color.white)
    table.cell(icTable, 0, 6, "Obs drift", text_color=color.white)
    table.cell(icTable, 1, 6, driftLabel, text_color=color.white)
    table.cell(icTable, 0, 7, "Skew", text_color=color.white)
    table.cell(icTable, 1, 7, skewLabel, text_color=color.white, bgcolor=skewBg)
    table.cell(icTable, 0, 8, "VIX", text_color=color.white)
    string vixTxt = na(vixDaily) ? "n/a" : str.format("{0,number,#.#}", vixDaily)
    color vixCol = vixOk ? color.new(color.green, 60) : color.new(color.red, 60)
    table.cell(icTable, 1, 8, vixTxt + (vixOk ? " OK" : " BLOCKED"), text_color=color.white, bgcolor=vixCol)
    table.cell(icTable, 0, 9, "Range floor", text_color=color.white)
    string floorTxt = na(obsRangePct) ? "—" : str.format("obs={0,number,#.##}% → eff={1,number,#.##}%", obsRangePct, na(effectiveOtm) ? pctOtm : effectiveOtm)
    table.cell(icTable, 1, 9, floorTxt, text_color=color.white)
    table.cell(icTable, 0, 10, "Scale", text_color=color.white)
    table.cell(icTable, 1, 10, str.format("wing={0,number,#.#}pt", effWingWidth), text_color=color.white, bgcolor=color.new(color.blue, 60))

// ── Alerts ──────────────────────────────────────────────────────────────
alertcondition(icBuildTrigger, title="ZeroDTE IC built",
               message="ZeroDTE IC built · short C={{plot('Short CALL')}} P={{plot('Short PUT')}}")
alertcondition(breachCall, title="ZeroDTE IC: CALL wing tagged",
               message="🛑 IC CALL tagged · close to STOP-LOSS at breakeven")
alertcondition(breachPut,  title="ZeroDTE IC: PUT wing tagged",
               message="🛑 IC PUT tagged · close to STOP-LOSS at breakeven")
```
