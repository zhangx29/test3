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

from .commodity import analytic as com
from .commodity.swap import CommoditySwapLeg
from .exposure import Results, build_grid, bucketise, netting_set_buckets
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
    v = float(u.atm_vol(max(t, 1e-8))) ** 2 * t if t > 0 else 0.0
    m = np.log(float(u.forward(t, mkt.curve_for(u.ccy)))) - 0.5 * v
    return m, v


def _value(legs, t, St, Sfix, mkt):
    """Sum of leg values in the reporting currency."""
    out = np.zeros_like(St)
    for leg in legs:
        x = float(mkt.fx(mkt.underlyings[leg.underlying].ccy, mkt.reporting_ccy, t))
        if isinstance(leg, OptionLeg):
            out = out + x * option_value(leg, t, St, mkt)
        else:
            out = out + x * trs_value(leg, t, St, mkt, Sfix if leg.fixing_time(t) is not None else None)
    return out


def ns_ee_nee(ns: NettingSet, mkt: MarketData, t: float, n: int = 201):
    legs = ns.legs
    if any(isinstance(l, CommoditySwapLeg) for l in legs):
        return com.ee_nee(legs, t, mkt) if com.is_commodity_set(legs) else (np.nan, np.nan)
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
    mode = cfg.output_mode.upper()
    rows = []
    for ns in netting_sets:
        bdates, bt, _, btype = netting_set_buckets(ns, cfg)
        g = grid[grid <= ns.maturity + 1e-9]
        if mode == "AVERAGE":
            prof = np.array([ns_ee_nee(ns, mkt, t, cfg.analytic_nodes) for t in g])
            ee = bucketise(g, prof[:, 0], bt, mode)
            nee = bucketise(g, prof[:, 1], bt, mode)
        else:
            res = [ns_ee_nee(ns, mkt, t, cfg.analytic_nodes) if t <= ns.maturity + 1e-9 else (0.0, 0.0)
                   for t in bt]
            ee, nee = np.array([r[0] for r in res]), np.array([r[1] for r in res])
        approx = None
        if com.is_commodity_set(ns.legs):      # exact moments + normal approximation, same bucketing
            if mode == "AVERAGE":
                a = np.array([com.conservative_profile(ns, t, mkt) for t in g])
                approx = [bucketise(g, a[:, j], bt, mode) for j in range(6)]
            else:
                a = np.array([com.conservative_profile(ns, t, mkt) if t <= ns.maturity + 1e-9 else (0.0,) * 6
                              for t in bt])
                approx = [a[:, j] for j in range(6)]
        for k, (d, t) in enumerate(zip(bdates, bt)):
            rows.append(dict(netting_set=ns.netting_set, bucket_date=d, bucket_type=btype[k], t=t,
                             EE_analytic=ee[k], NEE_analytic=nee[k]))
            if approx:
                rows[-1].update(Sigma=approx[0][k], EE_normal=approx[1][k], NEE_normal=approx[2][k],
                                PFE_normal=approx[3][k], AddOn_fwd=approx[4][k], SACCR_EAD_cons=approx[5][k])
    out = pd.DataFrame(rows)
    if mc is not None and len(out):
        m = mc.ns_profile[["netting_set", "t", "EE_uncoll", "NEE_uncoll", "EE_stderr"]]
        out = out.merge(m, on=["netting_set", "t"], how="left")
        out = out.rename(columns={"EE_uncoll": "EE_MC_uncoll", "NEE_uncoll": "NEE_MC_uncoll"})
        out["EE_diff"] = out["EE_MC_uncoll"] - out["EE_analytic"]
        out["NEE_diff"] = out["NEE_MC_uncoll"] - out["NEE_analytic"]
        mc.exposure_compare = exposure_compare(mc.ns_profile, out)
    return out


def exposure_compare(profile: pd.DataFrame, analytic: pd.DataFrame) -> pd.DataFrame | None:
    """Full simulation and analytic conservative EE / NEE / SA-CCR EAD, side by side per bucket."""
    if "SACCR_EAD_cons" not in analytic:
        return None
    a = analytic[analytic["SACCR_EAD_cons"].notna()]
    a = a[["netting_set", "t", "EE_analytic", "NEE_analytic", "EE_normal", "NEE_normal", "SACCR_EAD_cons"]]
    m = profile[["netting_set", "ccy", "bucket_start", "bucket_date", "bucket_type", "t", "EE", "NEE",
                 "SACCR_EAD_avg"]].merge(a, on=["netting_set", "t"])
    m = m.rename(columns={"EE": "EE_sim", "NEE": "NEE_sim", "SACCR_EAD_avg": "SACCR_EAD_sim",
                          "EE_analytic": "EE_exact", "NEE_analytic": "NEE_exact",
                          "EE_normal": "EE_cons", "NEE_normal": "NEE_cons"})
    for c, s, r in (("EE_cons", "EE_sim", "EE_cons_over_sim"), ("NEE_cons", "NEE_sim", "NEE_cons_over_sim"),
                    ("SACCR_EAD_cons", "SACCR_EAD_sim", "EAD_cons_over_sim")):
        m[r] = (m[c] / m[s].where(m[s] != 0)).astype(float)
    return m[["netting_set", "ccy", "bucket_start", "bucket_date", "bucket_type", "t",
              "EE_sim", "EE_exact", "EE_cons", "NEE_sim", "NEE_exact", "NEE_cons",
              "SACCR_EAD_sim", "SACCR_EAD_cons", "EE_cons_over_sim", "NEE_cons_over_sim", "EAD_cons_over_sim"]]
