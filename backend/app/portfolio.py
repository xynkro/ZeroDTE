"""Portfolio bridge — reads FinancePWA's snapshot/positions sheets and formats
a Telegram-friendly summary. Imported by telegram_bot.py /portfolio handler.

We re-use FinancePWA's `src/portfolio_summary.py` (project-agnostic) and
`src/sheets.py` (OAuth + gspread wrapper) by adding the FinancePWA repo to
sys.path lazily — only when /portfolio is invoked. Keeps the import cost
out of the main backend boot.

User → account mapping is read from env:
  TELEGRAM_USER_CASPAR=922547929
  TELEGRAM_USER_SARAH=<unknown until first /portfolio from her>

Sheet IDs read from FinancePWA env automatically since we share the project root.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx


log = logging.getLogger(__name__)


# Cached USDSGD rate (Yahoo Finance free quote). Cached for 1h so we don't
# hit Yahoo on every /portfolio call.
_USDSGD_RATE: float | None = None
_USDSGD_FETCHED_AT: float = 0.0


def _usdsgd() -> float | None:
    """Spot USDSGD rate via Yahoo Finance. Cached 1h. Returns None on failure
    so callers can degrade gracefully (USD-only display)."""
    global _USDSGD_RATE, _USDSGD_FETCHED_AT
    if _USDSGD_RATE is not None and (time.time() - _USDSGD_FETCHED_AT) < 3600:
        return _USDSGD_RATE
    try:
        r = httpx.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/USDSGD=X",
            params={"interval": "1d", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5.0,
        )
        r.raise_for_status()
        data = r.json()
        rate = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        _USDSGD_RATE = float(rate)
        _USDSGD_FETCHED_AT = time.time()
        log.info("USDSGD rate refreshed: %.4f", _USDSGD_RATE)
        return _USDSGD_RATE
    except Exception as e:
        log.warning("USDSGD rate fetch failed: %s", e)
        return _USDSGD_RATE  # may be None if never fetched

# Hard-coded handoff per FinancePWA brief — Portfolio Ping topic
PORTFOLIO_PING_TOPIC_ID = 31

# FinancePWA project root — needed to import sheets/portfolio_summary modules
# (those modules reference paths relative to FinancePWA's own root).
_FINANCEPWA_ROOT = Path("/Users/xynkro/Documents/Trading/FinancePWA")


def _ensure_financepwa_imports() -> tuple:
    """Lazy-add FinancePWA repo to sys.path and import the bridge modules.

    Returns (sheets_module, portfolio_summary_module). Raises RuntimeError if
    FinancePWA is not present at the expected path.
    """
    if not _FINANCEPWA_ROOT.exists():
        raise RuntimeError(
            f"FinancePWA repo not found at {_FINANCEPWA_ROOT} — /portfolio disabled. "
            "Either clone FinancePWA there, or update _FINANCEPWA_ROOT in portfolio.py."
        )
    if str(_FINANCEPWA_ROOT) not in sys.path:
        sys.path.insert(0, str(_FINANCEPWA_ROOT))

    # Make sure FinancePWA's env (SHEET_ID, OAUTH paths) is loaded
    fp_env = _FINANCEPWA_ROOT / ".env"
    if fp_env.exists():
        # Manually parse — we don't want to overwrite ZeroDTE env vars by accident.
        for line in fp_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # Only set if not already in env (ZeroDTE's env wins for shared keys)
            if k not in os.environ:
                os.environ[k] = v

    from src import sheets as sh                  # type: ignore
    from src import portfolio_summary as psum     # type: ignore
    return sh, psum


def user_account_map() -> dict[int, str]:
    """user_id → 'caspar' | 'sarah' from env."""
    out: dict[int, str] = {}
    cas = os.environ.get("TELEGRAM_USER_CASPAR", "")
    sar = os.environ.get("TELEGRAM_USER_SARAH", "")
    if cas and cas.isdigit():
        out[int(cas)] = "caspar"
    if sar and sar.isdigit():
        out[int(sar)] = "sarah"
    return out


def fetch_summary(account: str) -> str:
    """Build the portfolio summary string for `account` ('caspar' | 'sarah').

    Reads the latest BATCH from snapshot_<account>, positions_<account>, and
    the options tab (filtered by account). Latest batch = max audit-suffix
    timestamp (full timestamp match, not date-prefix — yahoo-grab writes 6×
    duplicates within a day at different times).
    """
    sh, psum = _ensure_financepwa_imports()
    name_lc = account.lower()

    ss = sh._open_sheet(sh.authenticate())

    # ── snapshot_<account> ────────────────────────────────────────────────
    snap_tab = "snapshot_caspar" if name_lc == "caspar" else "snapshot_sarah"
    ws = ss.worksheet(snap_tab)
    snap_rows = ws.get_all_values()
    snapshot: dict | None = None
    if len(snap_rows) > 1:
        hdr = snap_rows[0]
        latest = max(snap_rows[1:], key=lambda r: r[0] if r else "")
        rec = {hdr[i]: (latest[i] if i < len(latest) else "") for i in range(len(hdr))}
        snapshot = {
            "date": rec.get("date", ""),
            "net_liq": rec.get("net_liq")
                       or rec.get("net_liq_usd")
                       or rec.get("net_liq_sgd")
                       or "",
            "cash": rec.get("cash") or rec.get("cash_sgd") or "",
            "upl": rec.get("upl") or rec.get("upl_sgd") or "",
            "upl_pct": rec.get("upl_pct") or "",
        }

    # ── positions_<account>: latest batch only (exact timestamp match) ────
    pos_tab = "positions_caspar" if name_lc == "caspar" else "positions_sarah"
    ws = ss.worksheet(pos_tab)
    pos_rows = ws.get_all_values()
    positions: list[dict] = []
    if len(pos_rows) > 1:
        hdr = pos_rows[0]
        latest_ts = max((r[0] for r in pos_rows[1:] if r), default="")
        for r in pos_rows[1:]:
            if not r or r[0] != latest_ts:
                continue
            rec = {hdr[i]: (r[i] if i < len(r) else "") for i in range(len(hdr))}
            positions.append(rec)

    # ── options count for this account (latest batch only) ────────────────
    options_count = 0
    try:
        ws = ss.worksheet("options")
        opt_rows = ws.get_all_values()
        if len(opt_rows) > 1:
            hdr = opt_rows[0]
            account_col = hdr.index("account") if "account" in hdr else -1
            date_col = hdr.index("date") if "date" in hdr else 0
            latest_ts = max(
                (r[date_col] for r in opt_rows[1:] if r and len(r) > date_col),
                default="",
            )
            for r in opt_rows[1:]:
                if not r or len(r) <= max(date_col, account_col):
                    continue
                if r[date_col] != latest_ts:
                    continue
                if account_col >= 0 and r[account_col].lower() == name_lc:
                    options_count += 1
    except Exception as e:
        log.warning("options count fetch failed for %s: %s", name_lc, e)

    return _format_summary(account, snapshot, positions, options_count=options_count)


# ─────────────────────────────────────────────────────────────────────────
# Custom formatter — supersedes FinancePWA's build_portfolio_summary so we
# can show ALL positions and add SGD-equivalent NLV for USD accounts.
# ─────────────────────────────────────────────────────────────────────────

def _f(v, ndp: int = 0) -> str:
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{n:,.{ndp}f}"


def _abs_with_sign(v, prefix: str = "$") -> str:
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n >= 0 else "−"
    return f"{sign}{prefix}{abs(n):,.0f}"


def _pct_from_fraction(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    pct = n * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def _format_summary(
    account: str,
    snapshot: Optional[dict],
    positions: list[dict],
    options_count: int = 0,
) -> str:
    """Compose the full portfolio reply. Shows ALL positions (no +N more cap)
    and adds an SGD-equivalent NLV line for USD accounts using the cached
    Yahoo USDSGD rate.
    """
    name = account.capitalize()
    ccy = "SGD" if account.lower() == "sarah" else "USD"

    if not snapshot:
        return f"👤 {name} — no snapshot yet"

    date = (snapshot.get("date") or "")[:10] or "—"
    net_liq_raw = snapshot.get("net_liq")
    cash_raw = snapshot.get("cash")
    upl_abs = _abs_with_sign(snapshot.get("upl"), "$")
    upl_pct = _pct_from_fraction(snapshot.get("upl_pct"))

    try:
        c_pct = (float(cash_raw or 0) / float(net_liq_raw or 1)) * 100
        cash_pct_str = f" ({c_pct:.0f}%)"
    except (TypeError, ValueError, ZeroDivisionError):
        cash_pct_str = ""

    lines: list[str] = []
    lines.append(f"👤 {name} · {date}")
    lines.append(f"NLV {ccy} ${_f(net_liq_raw, 0)} · UPL {upl_abs} ({upl_pct})")

    # SGD equivalent for USD accounts (Caspar's case)
    if ccy == "USD":
        rate = _usdsgd()
        try:
            usd_val = float(net_liq_raw or 0)
            if rate and usd_val:
                sgd_val = usd_val * rate
                lines.append(f"NLV SGD ~S${_f(sgd_val, 0)} (rate {rate:.4f})")
        except (TypeError, ValueError):
            pass

    lines.append(f"Cash ${_f(cash_raw, 0)}{cash_pct_str}")

    # ALL positions (no +N more cap)
    valid = [p for p in positions if p.get("ticker") and p.get("mkt_val")]
    valid.sort(key=lambda p: float(p.get("mkt_val") or 0), reverse=True)
    if valid:
        lines.append("")
        lines.append(f"All holdings ({len(valid)}):")
        for p in valid:
            ticker = (p.get("ticker") or "")[:6]
            try:
                weight = float(p.get("weight") or 0) * 100
                weight_s = f"{weight:.1f}%"
            except (TypeError, ValueError):
                weight_s = "—"
            try:
                mv = float(p.get("mkt_val") or 0)
                mv_s = f"${mv:,.0f}"
            except (TypeError, ValueError):
                mv_s = "—"
            try:
                upl_p = float(p.get("upl") or 0)
                upl_emoji = "🟢" if upl_p >= 0 else "🔴"
            except (TypeError, ValueError):
                upl_emoji = "·"
            lines.append(f"  {upl_emoji} {ticker:<6} {weight_s:>6} · {mv_s}")

    if options_count:
        lines.append(f"\n📑 {options_count} option contract(s)")

    return "\n".join(lines)


def unknown_user_reply(user_name: str, user_id: int) -> str:
    return (
        f"👋 Hi {user_name}! I don't have your account on file yet.\n\n"
        f"Ask the admin to add\n"
        f"  TELEGRAM_USER_<NAME>={user_id}\n"
        f"to ZeroDTE/.env (and the same in FinancePWA's secrets) so I can look up your portfolio."
    )
