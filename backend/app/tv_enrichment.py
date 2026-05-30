"""TradingView enrichment — external TA data for confluence scoring.

Wraps the same tradingview-ta library that powers the TradingView MCP.
Runs inside the orchestrator as an optional, non-blocking enrichment step:
  - If TV data is available, it adds extra confluence factors
  - If it fails (network, timeout, weekend), the system falls back
    to internal indicator computation (zero degradation)

Tier 1 integrations (from May 2026 MCP review):
  1. tv_analyze()      — full TA snapshot (RSI, MACD, Stoch, ADX, ATR, BBands)
  2. multi_tf_check()   — multi-timeframe alignment (1h vs 5m)
  3. market_snapshot()  — broad market overview (SPY, VIX, DXY, sectors)
  4. financial_news()   — RSS news for macro event detection
  5. yahoo_price()      — fallback SPY price when Alpaca feed is down

All functions are sync (tradingview-ta uses requests internally),
so the orchestrator wraps calls in asyncio.to_thread() to stay non-blocking.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Lazy imports — don't crash if libs missing; degrade gracefully
_ta_available = False

try:
    from tradingview_ta import TA_Handler, Interval as TAInterval
    _ta_available = True
except ImportError:
    log.warning("tradingview-ta not installed — TV enrichment disabled")

# yfinance: LAZY import only — `import yfinance` hangs on macOS when
# launched via launchd (OpenBLAS/BLAS thread-safety issue in pandas→numpy).
# Import on first use instead of module load.  May 2026 fix.
_yf = None        # module ref, set on first call to _get_yf()
_yf_checked = False

def _get_yf():
    """Lazy-load yfinance. Returns module or None."""
    global _yf, _yf_checked
    if _yf_checked:
        return _yf
    _yf_checked = True
    try:
        import yfinance as _yfinance
        _yf = _yfinance
        log.info("yfinance loaded (lazy)")
    except ImportError:
        log.warning("yfinance not installed — yahoo_price fallback disabled")
    return _yf

# ── Interval mapping (matches the MCP server) ──────────────────────────────

TA_INTERVALS = {}
if _ta_available:
    TA_INTERVALS = {
        "1m":  TAInterval.INTERVAL_1_MINUTE,
        "5m":  TAInterval.INTERVAL_5_MINUTES,
        "15m": TAInterval.INTERVAL_15_MINUTES,
        "30m": TAInterval.INTERVAL_30_MINUTES,
        "1h":  TAInterval.INTERVAL_1_HOUR,
        "2h":  TAInterval.INTERVAL_2_HOURS,
        "4h":  TAInterval.INTERVAL_4_HOURS,
        "1d":  TAInterval.INTERVAL_1_DAY,
        "1W":  TAInterval.INTERVAL_1_WEEK,
        "1M":  TAInterval.INTERVAL_1_MONTH,
    }


# ── 1. Full TA snapshot ────────────────────────────────────────────────────

def tv_analyze(
    symbol: str = "SPY",
    exchange: str = "AMEX",
    screener: str = "america",
    interval: str = "5m",
) -> Optional[dict]:
    """Full TradingView TA analysis — summary signal + all indicator values.

    Returns dict with:
      - recommendation: STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
      - oscillators/moving_averages: sub-recommendations
      - indicators: RSI, MACD, Stoch, ADX, ATR, BBands, EMAs, etc.

    Returns None on any failure (network, weekend, bad symbol).
    """
    if not _ta_available:
        return None
    try:
        handler = TA_Handler(
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            screener=screener.lower(),
            interval=TA_INTERVALS.get(interval, TA_INTERVALS.get("5m")),
        )
        analysis = handler.get_analysis()
        ind = analysis.indicators
        return {
            "symbol": symbol.upper(),
            "interval": interval,
            "recommendation": analysis.summary["RECOMMENDATION"],
            "buy": analysis.summary["BUY"],
            "sell": analysis.summary["SELL"],
            "neutral": analysis.summary["NEUTRAL"],
            "oscillators_rec": analysis.oscillators["RECOMMENDATION"],
            "moving_averages_rec": analysis.moving_averages["RECOMMENDATION"],
            # Key indicators for confluence
            "rsi": ind.get("RSI"),
            "macd": ind.get("MACD.macd"),
            "macd_signal": ind.get("MACD.signal"),
            "stoch_k": ind.get("Stoch.K"),
            "stoch_d": ind.get("Stoch.D"),
            "adx": ind.get("ADX"),
            "atr": ind.get("ATR"),
            "ema10": ind.get("EMA10"),
            "ema20": ind.get("EMA20"),
            "ema50": ind.get("EMA50"),
            "ema200": ind.get("EMA200"),
            "bb_upper": ind.get("BB.upper"),
            "bb_lower": ind.get("BB.lower"),
            "close": ind.get("close"),
            "volume": ind.get("volume"),
            "change_pct": ind.get("change"),
        }
    except Exception as e:
        log.debug("tv_analyze(%s) failed: %s", symbol, e)
        return None


# ── 2. Multi-timeframe alignment ───────────────────────────────────────────

def multi_tf_check(
    symbol: str = "SPY",
    exchange: str = "AMEX",
    screener: str = "america",
    intervals: tuple[str, ...] = ("5m", "15m", "1h"),
) -> Optional[dict]:
    """Check TA alignment across multiple timeframes.

    Returns dict with per-timeframe recommendation + alignment score.
    Alignment = fraction of timeframes agreeing with 5m direction.

    Returns None on failure.
    """
    if not _ta_available:
        return None
    try:
        results = {}
        for iv in intervals:
            handler = TA_Handler(
                symbol=symbol.upper(),
                exchange=exchange.upper(),
                screener=screener.lower(),
                interval=TA_INTERVALS.get(iv, TA_INTERVALS.get("5m")),
            )
            analysis = handler.get_analysis()
            rec = analysis.summary["RECOMMENDATION"]
            results[iv] = {
                "recommendation": rec,
                "buy": analysis.summary["BUY"],
                "sell": analysis.summary["SELL"],
                "neutral": analysis.summary["NEUTRAL"],
                "rsi": analysis.indicators.get("RSI"),
            }

        # Compute alignment: how many TFs agree with primary (first interval)
        primary_rec = results[intervals[0]]["recommendation"]
        # Map to direction: BUY/STRONG_BUY → bullish, SELL/STRONG_SELL → bearish
        def direction(rec):
            if rec in ("BUY", "STRONG_BUY"):
                return "bullish"
            elif rec in ("SELL", "STRONG_SELL"):
                return "bearish"
            return "neutral"

        primary_dir = direction(primary_rec)
        agree = sum(1 for iv in intervals if direction(results[iv]["recommendation"]) == primary_dir)
        alignment = agree / len(intervals)

        return {
            "symbol": symbol.upper(),
            "timeframes": results,
            "primary_direction": primary_dir,
            "alignment": alignment,  # 1.0 = all TFs agree, 0.33 = only primary
            "aligned": alignment >= 0.67,  # at least 2 of 3 agree
        }
    except Exception as e:
        log.debug("multi_tf_check(%s) failed: %s", symbol, e)
        return None


# ── 3. Market snapshot ─────────────────────────────────────────────────────

def market_snapshot() -> Optional[dict]:
    """Broad market overview: SPY, VIX, DXY, key sector ETFs.

    Uses yfinance for real-time quotes. Returns None if unavailable.
    """
    yf = _get_yf()
    if yf is None:
        return None
    try:
        tickers = {
            "SPY": "S&P 500 ETF",
            "^VIX": "VIX",
            "DX-Y.NYB": "US Dollar Index",
            "QQQ": "Nasdaq 100 ETF",
            "IWM": "Russell 2000 ETF",
            "TLT": "20+ Year Treasury",
            "XLF": "Financials",
            "XLE": "Energy",
            "XLK": "Technology",
        }
        result = {}
        for sym, label in tickers.items():
            try:
                t = yf.Ticker(sym)
                info = t.fast_info
                result[sym] = {
                    "label": label,
                    "price": getattr(info, "last_price", None),
                    "change_pct": getattr(info, "day_change", None),
                }
                if result[sym]["change_pct"] is not None:
                    result[sym]["change_pct"] = round(result[sym]["change_pct"] * 100, 2)
            except Exception:
                result[sym] = {"label": label, "price": None, "error": True}
        return result
    except Exception as e:
        log.debug("market_snapshot() failed: %s", e)
        return None


# ── 4. Financial news check ────────────────────────────────────────────────

def financial_news_check(symbol: str = "SPY", limit: int = 5) -> Optional[dict]:
    """Check recent financial news for potential market-moving events.

    Uses Yahoo Finance news feed. Returns headline count + any
    keywords that suggest high-impact events (FOMC, CPI, jobs, earnings).

    Returns None if unavailable.
    """
    yf = _get_yf()
    if yf is None:
        return None
    try:
        t = yf.Ticker(symbol)
        news = t.news[:limit] if hasattr(t, "news") and t.news else []

        HIGH_IMPACT_KEYWORDS = {
            "fomc", "federal reserve", "rate decision", "cpi", "inflation",
            "nonfarm", "jobs report", "unemployment", "earnings", "gdp",
            "powell", "fed meeting", "interest rate", "payrolls",
        }

        headlines = []
        high_impact_detected = False
        for item in news:
            title = item.get("title", "")
            headlines.append(title)
            title_lower = title.lower()
            if any(kw in title_lower for kw in HIGH_IMPACT_KEYWORDS):
                high_impact_detected = True

        return {
            "symbol": symbol,
            "headline_count": len(headlines),
            "headlines": headlines,
            "high_impact_detected": high_impact_detected,
        }
    except Exception as e:
        log.debug("financial_news_check(%s) failed: %s", symbol, e)
        return None


# ── 5. Yahoo price fallback ────────────────────────────────────────────────

def yahoo_price(symbol: str = "SPY") -> Optional[float]:
    """Get current price from Yahoo Finance. Fallback when Alpaca feed is down.

    Returns price as float, or None on failure.
    """
    yf = _get_yf()
    if yf is None:
        return None
    try:
        t = yf.Ticker(symbol)
        price = getattr(t.fast_info, "last_price", None)
        return float(price) if price is not None else None
    except Exception as e:
        log.debug("yahoo_price(%s) failed: %s", symbol, e)
        return None


# ── Composite enrichment call ──────────────────────────────────────────────

def enrich_signal(
    side: str,
    spy_price: float,
    interval: str = "5m",
) -> dict:
    """Run all Tier 1 enrichments and return a flat dict of extra confluence factors.

    Called by the orchestrator BEFORE confluence scoring. Each enrichment is
    independent — if one fails, the others still run. Returns a dict like:

      {
        "tv_available": True,
        "tv_recommendation": "SELL",
        "tv_agrees_with_signal": True,       # TV says SELL and we're selling calls
        "tv_rsi": 72.5,
        "tv_adx": 28.1,
        "mtf_aligned": True,                  # multi-TF agreement
        "mtf_alignment": 0.67,
        "news_high_impact": False,
        "market_vix": 14.2,
      }
    """
    result = {"tv_available": False}
    t0 = time.time()

    # 1. TV Analyze (5m)
    ta = tv_analyze("SPY", interval=interval)
    if ta:
        result["tv_available"] = True
        result["tv_recommendation"] = ta["recommendation"]
        result["tv_rsi"] = ta["rsi"]
        result["tv_adx"] = ta["adx"]
        result["tv_atr"] = ta["atr"]
        result["tv_macd"] = ta["macd"]
        result["tv_stoch_k"] = ta["stoch_k"]

        # Does TV agree with our signal direction?
        if side == "sell_call_cs":
            # Selling calls = bearish bet. TV should say SELL/STRONG_SELL
            result["tv_agrees_with_signal"] = ta["recommendation"] in ("SELL", "STRONG_SELL")
        else:
            # Selling puts = bullish bet. TV should say BUY/STRONG_BUY
            result["tv_agrees_with_signal"] = ta["recommendation"] in ("BUY", "STRONG_BUY")

    # 2. Multi-TF alignment
    mtf = multi_tf_check("SPY", intervals=("5m", "15m", "1h"))
    if mtf:
        result["mtf_aligned"] = mtf["aligned"]
        result["mtf_alignment"] = mtf["alignment"]
        result["mtf_direction"] = mtf["primary_direction"]

        # MTF should agree with signal direction
        if side == "sell_call_cs":
            result["mtf_agrees"] = mtf["primary_direction"] == "bearish"
        else:
            result["mtf_agrees"] = mtf["primary_direction"] == "bullish"

    # 3. News check
    news = financial_news_check("SPY", limit=5)
    if news:
        result["news_high_impact"] = news["high_impact_detected"]
        result["news_headline_count"] = news["headline_count"]

    result["enrichment_ms"] = int((time.time() - t0) * 1000)
    return result
