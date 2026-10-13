"""Analytic (quadrature) exposure engine - uncollateralised EE / NEE per netting set.

On a single underlying the netting-set value at t is a deterministic function of S_t and, for
reset TRS legs whose notional fixed on the path, of S at the start of the period in force.
EE(t) = E[max(V,0)] is integrated over the joint lognormal law with Gauss-Legendre quadrature on
standard normal factors (1-D, or 2-D when a path fixing is in force). Netting sets on several
underlyings, or with more than one distinct path-fixing date in force, are reported as NaN.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .exposure import Results, build_grid, bucketise
from .market import MarketData
from .model import NettingSet, OptionLeg, RunConfig
from .products import option_value, trs_value


def _nodes(n: int, width: float = 8.0):
    x, w = np.polynomial.legendre.leggauss(n)
    z = x * width
    w = w * width * np.exp(-0.5 * z * z) / np.sqrt(2.0 * np.pi)
    return z, w / w.sum()


def _logS_params(mkt: MarketData, name: str, t: float):
    """Mean and variance of ln S_t."""
    u = mkt.underlyings[name]
    v = float(u.vol.atm(max(t, 1e-8))) ** 2 * t if t > 0 else 0.0
    m = np.log(float(u.forward(t, mkt.curve))) - 0.5 * v
    return m, v


def _value(legs, t, St, Sfix, mkt):
    out = np.zeros_like(St)
    for leg in legs:
        if isinstance(leg, OptionLeg):
            out = out + option_value(leg, t, St, mkt)
        else:
            out = out + trs_value(leg, t, St, mkt, Sfix if leg.fixing_time(t) is not None else None)
    return out


def ns_ee_nee(ns: NettingSet, mkt: MarketData, t: float, n: int = 201):
    legs = ns.legs
    names = {l.underlying for l in legs}
    if len(names) != 1:
        return np.nan, np.nan
    name = names.pop()
    if t <= 0:
        v = _value(legs, 0.0, np.array([mkt.underlyings[name].spot]), np.array([np.nan]), mkt)[0]
        return max(v, 0.0), min(v, 0.0)
    fix = {f for f in (l.fixing_time(t) for l in legs if not isinstance(l, OptionLeg)) if f is not None}
    if len(fix) > 1:
        return np.nan, np.nan
    z, w = _nodes(n)
    mt, vt = _logS_params(mkt, name, t)
    if not fix:
        St = np.exp(mt + np.sqrt(vt) * z)
        V = _value(legs, t, St, None, mkt)
        return float(np.sum(w * np.maximum(V, 0))), float(np.sum(w * np.minimum(V, 0)))
    a = fix.pop()
    ma, va = _logS_params(mkt, name, a)
    Sa = np.exp(ma + np.sqrt(va) * z)[:, None]                         # outer factor
    dm, dv = mt - ma, max(vt - va, 0.0)
    St = Sa * np.exp(dm + np.sqrt(dv) * z)[None, :]                    # inner factor
    V = _value(legs, t, St, np.broadcast_to(Sa, St.shape), mkt)
    W = w[:, None] * w[None, :]
    return float(np.sum(W * np.maximum(V, 0))), float(np.sum(W * np.minimum(V, 0)))


def run_analytic(netting_sets: list[NettingSet], mkt: MarketData, cfg: RunConfig,
                 mc: Results | None = None) -> pd.DataFrame:
    """Uncollateralised EE/NEE by bucket; if MC results are given, adds the comparison columns."""
    grid = build_grid(netting_sets, cfg)
    bt = cfg.bucket_times()
    mode = cfg.output_mode.upper()
    rows = []
    for ns in netting_sets:
        g = grid[grid <= ns.maturity + 1e-9]
        if mode == "AVERAGE":
            prof = np.array([ns_ee_nee(ns, mkt, t, cfg.analytic_nodes) for t in g])
            ee = bucketise(g, prof[:, 0], bt, mode)
            nee = bucketise(g, prof[:, 1], bt, mode)
        else:
            res = [ns_ee_nee(ns, mkt, t, cfg.analytic_nodes) if t <= ns.maturity + 1e-9 else (0.0, 0.0)
                   for t in bt]
            ee, nee = np.array([r[0] for r in res]), np.array([r[1] for r in res])
        for k, (d, t) in enumerate(zip(cfg.buckets, bt)):
            rows.append(dict(netting_set=ns.netting_set, bucket_date=d, t=t,
                             EE_analytic=ee[k], NEE_analytic=nee[k]))
    out = pd.DataFrame(rows)
    if mc is not None and len(out):
        m = mc.ns_profile[["netting_set", "t", "EE_uncoll", "NEE_uncoll", "EE_stderr"]]
        out = out.merge(m, on=["netting_set", "t"], how="left")
        out = out.rename(columns={"EE_uncoll": "EE_MC_uncoll", "NEE_uncoll": "NEE_MC_uncoll"})
        out["EE_diff"] = out["EE_MC_uncoll"] - out["EE_analytic"]
        out["NEE_diff"] = out["NEE_MC_uncoll"] - out["NEE_analytic"]
    return out
