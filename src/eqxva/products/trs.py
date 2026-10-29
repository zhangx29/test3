"""Equity TRS valuation (bank receives total return if omega=+1, pays funding = floating + spread).

For an equity period [a, b) with notional N = shares * ref (ref fixed, or S(a) fixed on the path):
  equity leg   pays shares*S(b) - N at b, dividends passed through (fraction dp) when paid
  funding leg  pays N * (L_j + s) * tau_j at each funding date inside the period
Valued at t with deterministic rates and continuous dividend yield q:
  current period (a <= t < b):
      shares*S_t*A(t,b) - N * [P(t,b) + sum_j (L_j + s) tau_j P(t,e_j)]
  future period (a > t):
      shares*S_t*e^{-q(a-t)}*A(a,b) - N_eff * [P(t,b) + sum_j (L_j + s) tau_j P(t,e_j)]
      N_eff = shares*ref (fixed) or shares*S_t*e^{-q(a-t)}/P(t,a) (fixes at a)
  A(x,b) = e^{-q(b-x)} + dp*(1 - e^{-q(b-x)})
plus the carried initial MTM, which accretes until it is settled at the first reset.
Cash flows falling exactly on t are treated as settled (post-settlement convention).
"""
from __future__ import annotations

import numpy as np

from ..market import MarketData


def _A(q: float, dp: float, x: float, b: float) -> float:
    e = np.exp(-q * (b - x))
    return e + dp * (1.0 - e)


def trs_value(leg, t: float, S_t, mkt: MarketData, S_fix=None):
    """Bank-view TRS value at t. ``S_fix`` is S at the start of the period in force when that
    period's notional fixes on the path (see ``leg.fixing_time``)."""
    S_t = np.asarray(S_t, dtype=float)
    q = mkt.underlyings[leg.underlying].div_yield
    P = mkt.und_curve(leg.underlying).df_fwd
    out = np.zeros_like(S_t)
    eps = 1e-12
    for p in leg.periods:
        if p.end <= t + eps:
            continue
        flt = float(P(t, p.end)) + sum((f.fwd + leg.spread) * f.tau * float(P(t, f.end))
                                       for f in p.funding if f.end > t + eps)
        if p.start <= t + eps:  # period in force
            eq = leg.shares * S_t * _A(q, leg.div_passthrough, t, p.end)
            if p.ref_price is not None:
                N = leg.shares * p.ref_price
            else:
                if S_fix is None:
                    raise ValueError("S_fix required for a period whose notional fixes on the path")
                N = leg.shares * np.asarray(S_fix, dtype=float)
        else:
            g = np.exp(-q * (p.start - t))
            eq = leg.shares * S_t * g * _A(q, leg.div_passthrough, p.start, p.end)
            N = leg.shares * p.ref_price if p.ref_price is not None else \
                leg.shares * S_t * g / float(P(t, p.start))
        out = out + leg.omega * (eq - N * flt)
    if leg.initial_mtm != 0.0 and t < leg.im_settle - eps:
        out = out + leg.initial_mtm * float(P(t, leg.im_settle) / P(0.0, leg.im_settle))
    return out
