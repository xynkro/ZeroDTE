"""
quant_utils.py — vetted quantitative primitives.

Provenance: harvested and corrected from an external "OpenClaw" codebase audit
(2026-06-15). Only the modules that were independently verified mathematically
correct were lifted; each was rewritten from the original `evaluate(state)` gate
form into a plain, testable function with real inputs. The original's broken
discrete gambler's-ruin gate was DISCARDED and replaced here with the correct
continuous first-passage (barrier-touch) formula.

Dependencies: numpy + stdlib only (deliberately dependency-light).

Contents
--------
- yang_zhang_volatility   : drift/gap-invariant realized vol from OHLC (Yang-Zhang 2000)
- expected_shortfall      : historical CVaR (mean of the worst tail) + VaR
- BetaBinomialWinRate      : posterior win-rate tracker with forgetting factor + lower bound
- shrink_to_prior          : one-shot James-Stein-style shrinkage of a win rate
- correlation_clusters     : single-linkage clustering of redundant (correlated) assets
- touch_probability        : P(barrier touched before expiry) under GBM (first passage)

Run `python quant_utils.py` to execute the self-test.
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 1. Yang-Zhang realized volatility (gap- and drift-invariant)
# ---------------------------------------------------------------------------
def yang_zhang_volatility(
    open_: Sequence[float],
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    periods_per_year: int = 252,
) -> float:
    """Annualized Yang-Zhang volatility from OHLC bars.

    Pairs naturally with an implied-vol read (e.g. IV rank / VIX): IV / YZ-RV is a
    clean variance-risk-premium gauge for deciding when premium selling is rich.

    Returns annualized volatility (e.g. 0.18 == 18%). Needs >= 10 bars.
    """
    o = np.asarray(open_, dtype=float)
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    n = len(o)
    if not (len(h) == len(l) == len(c) == n):
        raise ValueError("OHLC arrays must be equal length")
    if n < 10:
        raise ValueError(f"need >= 10 bars for Yang-Zhang, got {n}")

    c_prev = np.roll(c, 1)
    c_prev[0] = c[0]  # avoid NaN on first bar

    u = np.log(h / c_prev)   # high vs prev close
    d = np.log(l / c_prev)   # low vs prev close
    cc = np.log(c / c_prev)  # close vs prev close
    oo = np.log(o / c_prev)  # open vs prev close (overnight)

    vo = np.sum(oo ** 2) / (n - 1)                       # overnight variance
    vc = np.sum((cc - cc.mean()) ** 2) / (n - 1)          # close-to-close variance
    vrs = np.sum(u * (u - cc) + d * (d - cc)) / n         # Rogers-Satchell (drift-free)

    k = 0.34 / (1.34 + (n + 1) / (n - 1))                 # YZ minimum-variance weight
    vz = vo + k * vc + (1 - k) * vrs
    vz = max(0.0, float(vz))
    return float(math.sqrt(vz * periods_per_year))


# ---------------------------------------------------------------------------
# 2. Expected Shortfall (CVaR) — score the TAIL, not the average
# ---------------------------------------------------------------------------
def expected_shortfall(
    returns: Sequence[float], confidence: float = 0.95
) -> Tuple[float, float]:
    """Historical Expected Shortfall (CVaR) and VaR of a return series.

    ES = mean of the worst (1 - confidence) tail. For a negative-skew book
    (e.g. 0DTE premium selling) this is far more honest than win rate or mean P&L.

    Both values are signed (losses negative). Returns (es, var). Needs >= 20 points.
    """
    r = np.asarray(returns, dtype=float)
    if len(r) < 20:
        raise ValueError(f"need >= 20 observations for ES, got {len(r)}")
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be in (0, 1)")

    sorted_r = np.sort(r)
    # Number of observations in the worst (1-confidence) tail; floor guarantees at
    # least 0, but we clamp to 1 so the tail is never empty (mean of 0 values).
    n_tail = max(1, int(np.floor((1 - confidence) * len(sorted_r))))
    var = float(sorted_r[n_tail - 1])
    tail = sorted_r[:n_tail]
    es = float(np.mean(tail))
    return es, var


# ---------------------------------------------------------------------------
# 2b. Newey-West (HAC) t-statistic for the mean of a return series
# ---------------------------------------------------------------------------
def newey_west_tstat(returns: Sequence[float], lags: int | None = None) -> Dict[str, float]:
    """HAC (Newey-West) t-statistic for the mean of a return series.

    The naive t = mean / (s/√n) assumes i.i.d. observations. Trading P&L — and a
    0DTE ladder whose rungs share one day's tape especially — is SERIALLY
    CORRELATED, so the naive standard error is too small and the t too large: it
    waves through changes that aren't real edge. Newey-West corrects the variance
    of the mean for autocorrelation up to `lags` using the Bartlett kernel, which
    guarantees a non-negative long-run-variance estimate.

    lags=None → Newey-West (1994) plug-in bandwidth floor(4·(n/100)^(2/9)), min 1.
    Returns {t, naive_t, se, naive_se, mean, n, lags}. t/naive_t are 0.0 when the
    series is constant or n<2 (no dispersion to test)."""
    r = [float(x) for x in returns if x is not None]
    n = len(r)
    if n < 2:
        return {"t": 0.0, "naive_t": 0.0, "se": 0.0, "naive_se": 0.0,
                "mean": (r[0] if r else 0.0), "n": n, "lags": 0}
    mean = sum(r) / n
    dev = [x - mean for x in r]
    ss = sum(d * d for d in dev)
    gamma0 = ss / n                       # population autocovariance at lag 0
    if gamma0 <= 0:                       # constant series — no dispersion
        return {"t": 0.0, "naive_t": 0.0, "se": 0.0, "naive_se": 0.0,
                "mean": mean, "n": n, "lags": 0}
    if lags is None:
        lags = max(1, int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0))))
    lags = min(lags, n - 1)
    lrv = gamma0                          # long-run variance (Bartlett-weighted)
    for k in range(1, lags + 1):
        w = 1.0 - k / (lags + 1.0)        # Bartlett kernel weight
        cov_k = sum(dev[t] * dev[t - k] for t in range(k, n)) / n
        lrv += 2.0 * w * cov_k
    lrv = max(lrv, 1e-12)                 # Bartlett guarantees ≥0; clamp fp noise
    se = math.sqrt(lrv / n)
    s = math.sqrt(ss / (n - 1))          # sample stdev for the naive comparison
    naive_se = s / math.sqrt(n)
    return {
        "t": round(mean / se, 3) if se > 0 else 0.0,
        "naive_t": round(mean / naive_se, 3) if naive_se > 0 else 0.0,
        "se": se, "naive_se": naive_se, "mean": mean, "n": n, "lags": lags,
    }


def daily_edge_summary(series: Sequence[float], gate_n: int = 20) -> Dict[str, float]:
    """Edge diagnostics for a daily P&L series: HAC t-stat + outlier (jackknife)
    dependence — the honest "is there a signal worth scaling, or N lucky days?" read.

    Computes the Newey-West t-stat over the series AND re-computes it with the single
    best day removed (`t_drop_best`). A result that collapses when its best day is
    dropped is outlier-carried, not an edge. `has_edge` requires enough sample
    (n≥gate_n), HAC t≥2, AND survival of the drop-best jackknife (t≥1).

    Returns a dict; daily_edge_line() renders the one-liner. {ok: False} if n<2."""
    s = [float(x) for x in series if x is not None]
    n = len(s)
    if n < 2:
        return {"ok": False, "n": n}
    total = sum(s)
    nw = newey_west_tstat(s)
    bi = max(range(n), key=lambda i: s[i])          # index of the single best day
    best = s[bi]
    drop_best = s[:bi] + s[bi + 1:]
    nw_db = newey_west_tstat(drop_best)
    best_pct = (best / total * 100.0) if total else float("inf")
    return {
        "ok": True, "n": n, "total": round(total, 2), "mean": round(total / n, 2),
        "green": sum(1 for v in s if v > 0),
        "hac_t": nw["t"], "naive_t": nw["naive_t"], "lags": nw["lags"],
        "best": round(best, 2), "best_pct": round(best_pct, 1),
        "t_drop_best": nw_db["t"], "total_drop_best": round(sum(drop_best), 2),
        "gate_n": gate_n, "gate_eligible": n >= gate_n,
        "has_edge": bool(n >= gate_n and nw["t"] >= 2.0 and nw_db["t"] >= 1.0),
    }


def daily_edge_line(d: Dict[str, float]) -> str:
    """One-line render of daily_edge_summary() for the nightly debrief / console."""
    if not d.get("ok"):
        return "📐 edge: <2 days — nothing to test yet."
    gate = "" if d["gate_eligible"] else f", <{d['gate_n']} gate"
    if d["has_edge"]:
        tail = "✅ distinguishable edge — survives the drop-best jackknife"
    elif not d["gate_eligible"]:
        tail = "TRACKING — too few days to judge"
    elif d["hac_t"] < 2.0:
        tail = "NO edge yet (t<2): indistinguishable from zero"
    else:
        tail = "FRAGILE — fails the drop-best jackknife (outlier-carried)"
    return (f"📐 edge: HAC t={d['hac_t']:+.2f} (n={d['n']}{gate}) · "
            f"best day {d['best_pct']:.0f}% of total · drop it → t={d['t_drop_best']:+.2f} · {tail}")


# ---------------------------------------------------------------------------
# 3. Bayesian win-rate tracker — don't trust a small-sample hot streak
# ---------------------------------------------------------------------------
class BetaBinomialWinRate:
    """Beta-Binomial conjugate win-rate tracker with an optional forgetting factor.

    The point of this class: in shadow/paper validation a 7-1 start is NOT an 88%
    edge. `mean` gives the posterior estimate; `lower_bound()` gives a conservative
    floor you can gate on ("only trust setups whose lower-bound win rate clears
    breakeven"). `n_effective` tells you how much evidence you actually have.

    forgetting < 1.0 decays old evidence so recent performance dominates (regime
    adaptivity); 1.0 = pure accumulation. prior alpha=beta=1 is uniform/uninformative.
    """

    def __init__(
        self, forgetting: float = 1.0, prior_alpha: float = 1.0, prior_beta: float = 1.0
    ):
        if not (0.0 < forgetting <= 1.0):
            raise ValueError("forgetting must be in (0, 1]")
        self.forgetting = forgetting
        self.alpha = float(prior_alpha)
        self.beta = float(prior_beta)

    def update(self, win: bool) -> "BetaBinomialWinRate":
        self.alpha *= self.forgetting
        self.beta *= self.forgetting
        if win:
            self.alpha += 1.0
        else:
            self.beta += 1.0
        return self

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def n_effective(self) -> float:
        # subtract the uniform prior's 2 pseudo-counts so this reads as "real" evidence
        return max(0.0, self.alpha + self.beta - 2.0)

    @property
    def variance(self) -> float:
        a, b = self.alpha, self.beta
        return (a * b) / (((a + b) ** 2) * (a + b + 1.0))

    def lower_bound(self, z: float = 1.64) -> float:
        """Conservative lower bound on the win rate (normal approx to the Beta).

        z=1.64 ~ 95% one-sided. Use this, not `mean`, to decide whether an edge is real.
        """
        return max(0.0, self.mean - z * math.sqrt(self.variance))


# ---------------------------------------------------------------------------
# 4. One-shot shrinkage of a win rate toward a prior
# ---------------------------------------------------------------------------
def shrink_to_prior(
    wins: int, losses: int, prior_mean: float = 0.55, prior_strength: float = 10.0
) -> float:
    """Shrink an empirical win rate toward a prior, weighted by sample size.

    weight_empirical = N / (N + k); the rest goes to the prior. Equivalent in spirit
    to BetaBinomialWinRate but stateless/one-shot. Use when you just need a number.
    """
    n = wins + losses
    if n <= 0:
        return float(prior_mean)
    emp = wins / n
    w = n / (n + prior_strength)
    return float(w * emp + (1 - w) * prior_mean)


# ---------------------------------------------------------------------------
# 5. Correlation clustering — find positions that are secretly the same bet
# ---------------------------------------------------------------------------
def correlation_clusters(
    corr_matrix: Sequence[Sequence[float]],
    labels: Sequence[str],
    threshold: float = 0.85,
) -> Dict[str, object]:
    """Single-linkage clustering of assets whose |correlation| exceeds `threshold`.

    Answers "are my 10 positions really 10 independent bets, or 3 correlated clusters?"
    Returns {clusters, representatives, n_independent, redundant}. `representatives` is
    one name per cluster (the first); `redundant` is everyone else — candidates to trim
    for genuine diversification.
    """
    m = np.asarray(corr_matrix, dtype=float)
    n = len(labels)
    if m.shape != (n, n):
        raise ValueError("corr_matrix must be square and match labels length")

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    for i in range(n):
        for j in range(i + 1, n):
            if abs(m[i, j]) > threshold:
                union(i, j)

    groups: Dict[int, List[str]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(labels[i])

    clusters = list(groups.values())
    representatives = [g[0] for g in clusters]
    redundant = [name for g in clusters for name in g[1:]]
    return {
        "clusters": clusters,
        "representatives": representatives,
        "n_independent": len(clusters),
        "redundant": redundant,
    }


# ---------------------------------------------------------------------------
# 6. Barrier-touch probability (first passage) — the CORRECT replacement for
#    the original's broken discrete gambler's-ruin gate.
# ---------------------------------------------------------------------------
def _phi(x: float) -> float:
    """Standard normal CDF via erf (stdlib, no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def touch_probability(
    spot: float,
    barrier: float,
    sigma_annual: float,
    t_years: float,
    risk_free: float = 0.0,
) -> float:
    """P(price touches `barrier` at any time before `t_years`) under GBM.

    This is the right gate for a credit spread: feed it the short strike to get the
    probability it gets breached intraday. Far better than reading delta off a chain,
    and the correct continuous model (the audited source used a discrete random walk
    whose formula was additionally inverted).

    Uses the reflection-principle first-passage law. The zero-log-drift case
    (risk_free = sigma**2/2) reduces to the classic 2*Phi(-|ln(B/S)| / (sigma*sqrt(T))).
    """
    if min(spot, barrier, sigma_annual, t_years) <= 0:
        raise ValueError("spot, barrier, sigma_annual, t_years must all be > 0")

    mu = risk_free - 0.5 * sigma_annual ** 2          # log-drift
    s = sigma_annual * math.sqrt(t_years)             # log-std over horizon

    if barrier < spot:  # down-barrier: P(min log-return <= -m)
        m = math.log(spot / barrier)
        p = _phi((-m - mu * t_years) / s) + math.exp(-2.0 * mu * m / sigma_annual ** 2) * _phi(
            (-m + mu * t_years) / s
        )
    else:               # up-barrier: P(max log-return >= m)
        m = math.log(barrier / spot)
        p = _phi((-m + mu * t_years) / s) + math.exp(2.0 * mu * m / sigma_annual ** 2) * _phi(
            (-m - mu * t_years) / s
        )
    return float(min(1.0, max(0.0, p)))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
def _self_test() -> None:
    rng = np.random.default_rng(7)  # seeded; no Date/random-state surprises

    # --- Yang-Zhang: a synthetic 16%-vol series should estimate near 16% ---
    n = 252
    true_vol = 0.16
    daily = true_vol / math.sqrt(252)
    close = 100 * np.exp(np.cumsum(rng.normal(0, daily, n)))
    open_ = close * np.exp(rng.normal(0, daily / 2, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, daily / 2, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, daily / 2, n)))
    yz = yang_zhang_volatility(open_, high, low, close)
    assert 0.08 < yz < 0.30, yz

    # --- Expected Shortfall: ES must be <= VaR <= 0 on a loss-skewed series ---
    returns = rng.normal(0.001, 0.01, 500)
    returns[:10] = -0.05  # inject a fat left tail
    es, var = expected_shortfall(returns, confidence=0.95)
    assert es <= var <= 0.0, (es, var)

    # --- Newey-West: on POSITIVELY autocorrelated data the HAC t must be SMALLER
    #     (more conservative) than the naive t; on i.i.d. data they're close ---
    base = rng.normal(0.0, 1.0, 400)
    ac = [base[0]]
    for i in range(1, len(base)):
        ac.append(0.6 * ac[-1] + base[i])            # AR(1), strong positive autocorr
    ac = [x + 0.5 for x in ac]                        # shift to a positive mean
    nw = newey_west_tstat(ac)
    assert abs(nw["t"]) < abs(nw["naive_t"]), (nw["t"], nw["naive_t"])
    iid = newey_west_tstat([0.5 + x for x in rng.normal(0.0, 1.0, 400)])
    assert abs(iid["t"] - iid["naive_t"]) < 0.6, (iid["t"], iid["naive_t"])

    # --- Beta-Binomial: 7 wins, 1 loss -> mean ~0.8, lower bound strictly below ---
    wr = BetaBinomialWinRate()
    for w in [True, True, True, True, True, True, True, False]:
        wr.update(w)
    assert 0.7 < wr.mean < 0.85, wr.mean
    assert wr.lower_bound() < wr.mean, (wr.lower_bound(), wr.mean)
    assert abs(wr.n_effective - 8.0) < 1e-9, wr.n_effective

    # --- Shrinkage: 7-1 must land between empirical (0.875) and prior (0.55) ---
    s = shrink_to_prior(7, 1, prior_mean=0.55, prior_strength=10.0)
    assert 0.55 < s < 0.875, s

    # --- Correlation clusters: two obvious pairs collapse to 2 independent bets ---
    labels = ["SPY", "VOO", "GLD", "GDX"]
    corr = [
        [1.00, 0.98, 0.10, 0.12],
        [0.98, 1.00, 0.08, 0.11],
        [0.10, 0.08, 1.00, 0.90],
        [0.12, 0.11, 0.90, 1.00],
    ]
    cl = correlation_clusters(corr, labels, threshold=0.85)
    assert cl["n_independent"] == 2, cl
    assert sorted(cl["redundant"]) == ["GDX", "VOO"], cl

    # --- Touch probability: validate against a Monte-Carlo GBM simulation ---
    spot, barrier, sig, T = 100.0, 99.0, 0.16, 1.0 / 252.0  # ~1% down-barrier, 1 day
    analytic = touch_probability(spot, barrier, sig, T)
    steps, paths = 300, 40000
    dt = T / steps
    incr = rng.normal((-0.5 * sig ** 2) * dt, sig * math.sqrt(dt), (paths, steps))
    logpath = np.cumsum(incr, axis=1)
    touched = (spot * np.exp(logpath)).min(axis=1) <= barrier
    mc = touched.mean()
    assert abs(analytic - mc) < 0.05, (analytic, mc)
    # zero-log-drift sanity (risk_free = sigma^2/2 => mu=0): equals 2*Phi(-m/s)
    m = math.log(spot / barrier)
    zero_drift_rf = 0.5 * sig ** 2
    assert abs(
        touch_probability(spot, barrier, sig, T, risk_free=zero_drift_rf)
        - 2 * _phi(-m / (sig * math.sqrt(T)))
    ) < 1e-9

    print("quant_utils self-test: ALL PASS")
    print(f"  yang_zhang_volatility   -> {yz:.3f} (target ~0.16)")
    print(f"  expected_shortfall      -> ES={es:.4f}  VaR={var:.4f}")
    print(f"  BetaBinomialWinRate     -> mean={wr.mean:.3f}  lower={wr.lower_bound():.3f}  n={wr.n_effective:.0f}")
    print(f"  shrink_to_prior(7,1)    -> {s:.3f}")
    print(f"  correlation_clusters    -> {cl['n_independent']} bets, trim {cl['redundant']}")
    print(f"  touch_probability       -> analytic={analytic:.3f}  montecarlo={mc:.3f}")


if __name__ == "__main__":
    _self_test()
