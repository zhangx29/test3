"""Closed-form exposure for netting sets of commodity swaps (uncollateralised, no simulation).

At time t the netting-set value is a constant plus a weighted sum of jointly lognormal futures
prices, so its mean and variance are exact. EE / NEE are integrated exactly when at most
MAX_DIMS independent risk drivers are live, otherwise the normal approximation is used.

The normal (Bachelier) approximation uses the exact mean m and standard deviation Sigma:
    EE = m Phi(m / Sigma) + Sigma phi(m / Sigma),  PFE = m + PFE_SIGMA_MULT * Sigma
A spread of lognormals is right-skewed, so 1.645 Sigma understates a 95% PFE; 1.9 covered the
tested range of crush inputs. Check it against the Monte Carlo PFE when the inputs change.

Conservative SA-CCR EAD: alpha * (EE + AddOn at forward prices), with the multiplier set to 1 and no
collateral (the unmargined EAD caps the margined EAD). It is an estimate, not a proven upper bound.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm

from ..saccr import saccr
from .nfactor import covariance
from .swap import CommoditySwapLeg

MAX_DIMS = 3
PFE_SIGMA_MULT = 1.9
_NODES = 60


def is_commodity_set(legs) -> bool:
    return all(isinstance(l, CommoditySwapLeg) for l in legs)


def _drivers(legs, t: float, mkt):
    """Value at t in the reporting currency = const + sum_k w_k F_k: returns const, w, F0, cov."""
    terms: dict[tuple, float] = {}
    const = 0.0
    for l in legs:
        if t >= l.payment_t - 1e-12:
            continue
        c = mkt.underlyings[l.underlying]
        a = (l.omega * l.quantity * float(mkt.und_curve(l.underlying).df_fwd(t, l.payment_t))
             * float(mkt.fx(c.ccy, mkt.reporting_ccy, t)))
        key = (l.underlying, min(t, l.fixing_t))
        terms[key] = terms.get(key, 0.0) + a
        const -= a * l.fixed_price
    keys = list(terms)
    w = np.array([terms[k] for k in keys])
    f0 = np.array([mkt.underlyings[k[0]].spot for k in keys])
    cs = [mkt.underlyings[k[0]] for k in keys]
    n = len(keys)
    cov = np.array([[covariance(mkt.commodity, cs[i], keys[i][1], cs[j], keys[j][1]) for j in range(n)]
                    for i in range(n)]).reshape(n, n)
    return const, w, f0, cov


def moments(legs, t: float, mkt) -> tuple[float, float]:
    """Exact mean and standard deviation of the netting-set value at t."""
    const, w, f0, cov = _drivers(legs, t, mkt)
    a = w * f0
    return const + float(a.sum()), float(np.sqrt(max(a @ np.expm1(cov) @ a, 0.0)))


def normal_approx(legs, t: float, mkt) -> tuple[float, float, float, float]:
    """(Sigma, EE, NEE, PFE) from the exact moments and the normal approximation."""
    m, s = moments(legs, t, mkt)
    if s <= 0:
        return 0.0, max(m, 0.0), min(m, 0.0), m
    d = m / s
    ee = m * norm.cdf(d) + s * norm.pdf(d)
    return s, float(ee), float(m - ee), m + PFE_SIGMA_MULT * s


def conservative_profile(ns, t: float, mkt) -> tuple:
    """(Sigma, EE, NEE, PFE, forward AddOn, conservative EAD) at t, in the reporting currency."""
    s, ee, nee, pfe = normal_approx(ns.legs, t, mkt)
    if t >= ns.maturity - 1e-9:
        return s, ee, nee, pfe, 0.0, 0.0
    csa = ns.csa
    x = float(mkt.fx(csa.ccy, mkt.reporting_ccy, t))
    F = {l.underlying: np.array([mkt.underlyings[l.underlying].spot]) for l in ns.legs}
    addon = float(saccr(ns.legs, t, F, np.zeros(1), csa, mkt)["unmargined"]["AddOn"][0]) * x
    return s, ee, nee, pfe, addon, csa.sa_alpha * (ee + addon + max(-csa.nica, 0.0) * x)


def ee_nee(legs, t: float, mkt) -> tuple[float, float]:
    """Uncollateralised EE and NEE at t."""
    const, w, f0, cov = _drivers(legs, t, mkt)
    if len(w) == 0:
        return 0.0, 0.0
    lam, vec = np.linalg.eigh(cov)
    keep = lam > 1e-12 * max(lam.max(), 1e-300)
    d = int(keep.sum())
    if d == 0:
        v = const + float((w * f0).sum())
        return max(v, 0.0), min(v, 0.0)
    if d > MAX_DIMS:
        return normal_approx(legs, t, mkt)[1:3]
    x, q = np.polynomial.legendre.leggauss(_NODES)
    z = x * 7.0
    p = q * np.exp(-0.5 * z * z)
    p = p / p.sum()
    Z = np.stack([g.ravel() for g in np.meshgrid(*([z] * d), indexing="ij")], axis=1)
    wts = np.prod(np.stack([g.ravel() for g in np.meshgrid(*([p] * d), indexing="ij")], axis=1), axis=1)
    X = Z @ (vec[:, keep] * np.sqrt(lam[keep])).T - 0.5 * np.diag(cov)
    V = const + np.exp(X) @ (w * f0)
    return float(wts @ np.maximum(V, 0.0)), float(wts @ np.minimum(V, 0.0))
