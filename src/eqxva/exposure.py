"""Monte Carlo exposure engine: EE / NEE / PFE / EPE / EEPE / funding exposure / SA-CCR profiles.

Currency: each leg is valued in its underlying's currency and converted to the netting set's CSA
currency at the FX forward, where collateral and SA-CCR are computed. Netting-set and trade outputs
are then converted to the reporting currency (exact, since every statistic scales with a positive
deterministic FX factor). LegMTM keeps the leg currency for reconciliation against OVME.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import dates as dt
from .collateral import collateralised, vm_balance
from .market import MarketData
from .model import NettingSet, OptionLeg, RunConfig, TRSLeg
from .products import option_value, trs_value
from .saccr import saccr
from .simulation import forward_path, simulate

ONE_DAY = 1.0 / 365.0


# ----------------------------------------------------------------------------- grid
def event_times(netting_sets: list[NettingSet]) -> list[float]:
    ev = []
    for ns in netting_sets:
        for leg in ns.legs:
            ev.append(leg.expiry)
            if isinstance(leg, TRSLeg):
                for p in leg.periods:
                    ev += [p.start, p.end]
    return [e for e in ev if e > 0]


def build_grid(netting_sets: list[NettingSet], cfg: RunConfig) -> np.ndarray:
    horizon = max(ns.maturity for ns in netting_sets)
    val = cfg.valuation_date
    end_date = val + dt.timedelta(days=int(np.ceil(horizon * 365)) + 1)
    fine = [dt.yf(val, d) for d in dt.schedule(val, end_date, cfg.fine_step)]
    ev = event_times(netting_sets)
    pre = [e - ONE_DAY for e in ev]                    # capture the value just before settlement
    epe_h = [dt.tenor_years(cfg.epe_horizon)]
    base = dt.merge_times([0.0], cfg.bucket_times(), fine, ev, pre, epe_h)
    base = base[base <= horizon + 1e-9]
    lags = []
    for m in {ns.csa.mpor_years for ns in netting_sets if ns.csa.collateralized}:
        lags.append(base - m)
    grid = dt.merge_times(base, *lags) if lags else base
    return grid


def _idx(grid: np.ndarray, t: float) -> int:
    i = int(np.searchsorted(grid, t - 1e-9))
    if i >= len(grid) or abs(grid[i] - t) > 1e-6:
        raise KeyError(f"time {t} not on simulation grid")
    return i


def bucketise(grid: np.ndarray, prof: np.ndarray, bt: np.ndarray, mode: str) -> np.ndarray:
    """Map a grid profile to bucket times: point value (POINT) or time-average over the bucket
    (AVERAGE). The profile is 0 beyond the grid horizon (everything has matured), so a bucket that
    straddles the horizon averages the live part over the full bucket width."""
    out = np.zeros(len(bt))
    for k, t in enumerate(bt):
        if mode == "AVERAGE":
            t0 = bt[k - 1] if k > 0 else 0.0
            if t0 > grid[-1] + 1e-9:
                continue
            m = (grid >= t0 - 1e-9) & (grid <= t + 1e-9)
            g, p = grid[m], prof[m]
            out[k] = np.trapezoid(p, g) / (t - t0) if len(g) > 1 and t > t0 else p[-1]
        elif t <= grid[-1] + 1e-9:
            out[k] = np.interp(t, grid, prof)
    return out


def time_avg(grid, prof, h):
    m = grid <= h + 1e-9
    g, p = grid[m], prof[m]
    return float(np.trapezoid(p, g) / g[-1]) if len(g) > 1 and g[-1] > 0 else float(p[0])


# ----------------------------------------------------------------------------- valuation
def leg_value(leg, t: float, i: int, grid, S: dict, mkt: MarketData):
    St = S[leg.underlying][:, i]
    if isinstance(leg, OptionLeg):
        return option_value(leg, t, St, mkt)
    tf = leg.fixing_time(t)
    sfix = S[leg.underlying][:, _idx(grid, tf)] if tf is not None else None
    return trs_value(leg, t, St, mkt, sfix)


def leg_ccy(leg, mkt: MarketData) -> str:
    return mkt.underlyings[leg.underlying].ccy


def value_cube(legs, grid, S, mkt, ccy: str = "") -> np.ndarray:
    """Sum of the leg values on the grid, converted to ``ccy`` at the FX forward."""
    n = next(iter(S.values())).shape[0]
    V = np.zeros((n, len(grid)))
    for leg in legs:
        x = mkt.fx(leg_ccy(leg, mkt), ccy, grid)
        for i, t in enumerate(grid):
            V[:, i] += x[i] * leg_value(leg, t, i, grid, S, mkt)
    return V


def leg_mtm0(leg, mkt: MarketData) -> float:
    S0 = np.array([mkt.underlyings[leg.underlying].spot])
    if isinstance(leg, OptionLeg):
        return float(option_value(leg, 0.0, S0, mkt)[0])
    return float(trs_value(leg, 0.0, S0, mkt, S0)[0])


# ----------------------------------------------------------------------------- statistics
def stats(X: np.ndarray, q: float):
    pos, neg = np.maximum(X, 0.0), np.minimum(X, 0.0)
    n = X.shape[0]
    return dict(EE=pos.mean(0), NEE=neg.mean(0), PFE=np.quantile(X, q, axis=0),
                NPFE=np.quantile(X, 1.0 - q, axis=0), MTM=X.mean(0),
                EE_se=pos.std(0, ddof=1) / np.sqrt(n), NEE_se=neg.std(0, ddof=1) / np.sqrt(n))


@dataclass
class Results:
    ns_profile: pd.DataFrame
    trade_profile: pd.DataFrame
    summary: pd.DataFrame
    leg_mtm: pd.DataFrame
    saccr_legs: pd.DataFrame
    saccr_entities: pd.DataFrame
    grid_profile: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    analytic: pd.DataFrame | None = None

    def tables(self) -> dict[str, pd.DataFrame]:
        out = {"Summary": self.summary, "EE_NEE": self.ns_profile, "TradeExposure": self.trade_profile,
               "LegMTM": self.leg_mtm, "SACCR_Legs": self.saccr_legs, "SACCR_Entities": self.saccr_entities,
               "GridProfile": self.grid_profile}
        if self.analytic is not None:
            out["AnalyticCheck"] = self.analytic
        out["Warnings"] = pd.DataFrame({"warning": self.warnings or ["(none)"]})
        return out


# ----------------------------------------------------------------------------- engine
def run_monte_carlo(netting_sets: list[NettingSet], mkt: MarketData, cfg: RunConfig,
                    warnings: list[str] | None = None, saccr_paths: int = 2000) -> Results:
    warnings = list(warnings or [])
    grid = build_grid(netting_sets, cfg)
    names = sorted({l.underlying for ns in netting_sets for l in ns.legs})
    S = simulate(mkt, names, grid, cfg.n_paths, cfg.seed, cfg.antithetic)
    Sfwd = forward_path(mkt, names, grid)
    bt = cfg.bucket_times()
    bdates = cfg.buckets
    mode = cfg.output_mode.upper()
    q = cfg.pfe_quantile
    df_b = mkt.curve.df(bt)
    h_epe = dt.tenor_years(cfg.epe_horizon)
    n_sa = min(saccr_paths, cfg.n_paths)

    ns_rows, tr_rows, sum_rows, mtm_rows, sl_rows, se_rows, grid_rows = [], [], [], [], [], [], []
    rep = mkt.reporting_ccy
    for ns in netting_sets:
        csa = ns.csa
        ccy = csa.ccy
        xg = mkt.fx(ccy, rep, grid)                      # CSA currency -> reporting, along the grid
        x0 = float(mkt.fx(ccy, rep, 0.0))

        def to_rep(st):
            return {k: v * xg for k, v in st.items()}

        V = np.zeros((cfg.n_paths, len(grid)))
        for tr in ns.trades:
            Vt = value_cube(tr.legs, grid, S, mkt, ccy)
            V += Vt
            st = to_rep(stats(Vt, q))
            b = {k: bucketise(grid, v, bt, mode) for k, v in st.items()}
            for k, (d, t) in enumerate(zip(bdates, bt)):
                tr_rows.append(dict(netting_set=ns.netting_set, trade_id=tr.trade_id, ccy=rep, bucket_date=d, t=t,
                                    MTM=b["MTM"][k], EE=b["EE"][k], NEE=b["NEE"][k], PFE=b["PFE"][k],
                                    EE_disc=b["EE"][k] * df_b[k], NEE_disc=b["NEE"][k] * df_b[k]))
            for leg in tr.legs:
                m0 = leg_mtm0(leg, mkt)
                row = dict(netting_set=ns.netting_set, trade_id=tr.trade_id, leg_id=leg.leg_id,
                           leg_type=leg.kind, underlying=leg.underlying, mtm0=m0)
                if isinstance(leg, OptionLeg):
                    px = m0 / (leg.sign * leg.shares)
                    row.update(strike=leg.strike, expiry=leg.expiry_date, engine_px_per_share=px,
                               ovme_px_per_share=leg.ovme_price,
                               diff_vs_ovme=None if leg.ovme_price is None else px - leg.ovme_price)
                else:
                    row.update(maturity=leg.maturity_date, reset=leg.reset_freq or "NONE",
                               notional0=leg.shares * (leg.periods[0].ref_price or 0.0))
                lc = leg_ccy(leg, mkt)
                row.update(leg_ccy=lc or rep, mtm0_reporting=m0 * float(mkt.fx(lc, rep, 0.0)))
                mtm_rows.append(row)

        E, _ = collateralised(V, grid, csa, ns.maturity)
        st_c = to_rep(stats(E, q))
        st_u = to_rep(stats(V, q))
        # funding exposure (no default): margin actually held today, i.e. CSA(V(t)) without the MPOR lag
        live = (grid < ns.maturity - 1e-9)[None, :]
        F = np.where(live, V - vm_balance(V, csa), 0.0) if csa.collateralized else V
        fca, fba = np.maximum(F, 0).mean(0) * xg, np.minimum(F, 0).mean(0) * xg
        eee = np.maximum.accumulate(st_c["EE"])
        h = min(h_epe, ns.maturity)

        # SA-CCR through time: path-wise mean (subset of paths) and deterministic forward path
        legs = ns.legs
        Vf = value_cube(legs, grid, Sfwd, mkt, ccy)
        sa_mean = {k: np.zeros(len(bt)) for k in ("EAD", "RC", "PFE", "AddOn")}
        sa_fwd = np.zeros(len(bt))
        for k, t in enumerate(bt):
            if t > grid[-1] + 1e-9:
                continue
            i = _idx(grid, t)
            r = saccr(legs, t, {n: S[n][:n_sa, i] for n in names}, V[:n_sa, i], csa, mkt)
            for key in sa_mean:
                sa_mean[key][k] = float(np.mean(r[key])) * xg[i]
            r_f = saccr(legs, t, {n: Sfwd[n][:, i] for n in names}, Vf[:, i], csa, mkt)
            sa_fwd[k] = float(r_f["EAD"][0]) * xg[i]
        S0 = {n: np.array([mkt.underlyings[n].spot]) for n in names}
        V0 = np.array([sum(leg_mtm0(l, mkt) * float(mkt.fx(leg_ccy(l, mkt), ccy, 0.0)) for l in legs)])
        r0 = saccr(legs, 0.0, S0, V0, csa, mkt, breakdown=True)
        for row in r0["legs"]:
            row.update(adjusted_notional=row["adjusted_notional"] * x0,
                       effective_notional=row["effective_notional"] * x0)
            sl_rows.append(dict(netting_set=ns.netting_set, ccy=rep, **row))
        for row in r0["entities"]:
            row.update(entity_addon=row["entity_addon"] * x0)
            se_rows.append(dict(netting_set=ns.netting_set, ccy=rep, **row))

        bc = {k: bucketise(grid, v, bt, mode) for k, v in st_c.items()}
        bu = {k: bucketise(grid, v, bt, mode) for k, v in st_u.items()}
        bfca, bfba = bucketise(grid, fca, bt, mode), bucketise(grid, fba, bt, mode)
        for k, (d, t) in enumerate(zip(bdates, bt)):
            ns_rows.append(dict(
                netting_set=ns.netting_set, counterparty=ns.counterparty, ccy=rep, bucket_date=d, t=t, df=df_b[k],
                MTM=bu["MTM"][k], EE=bc["EE"][k], NEE=bc["NEE"][k], PFE=bc["PFE"][k], NPFE=bc["NPFE"][k],
                EE_disc=bc["EE"][k] * df_b[k], NEE_disc=bc["NEE"][k] * df_b[k],
                EE_uncoll=bu["EE"][k], NEE_uncoll=bu["NEE"][k], PFE_uncoll=bu["PFE"][k],
                FCA_exposure=bfca[k], FBA_exposure=bfba[k],
                EE_stderr=bc["EE_se"][k], NEE_stderr=bc["NEE_se"][k],
                SACCR_EAD_mean=sa_mean["EAD"][k], SACCR_RC_mean=sa_mean["RC"][k],
                SACCR_PFE_mean=sa_mean["PFE"][k], SACCR_AddOn_mean=sa_mean["AddOn"][k],
                SACCR_EAD_fwd=sa_fwd[k]))
        for i, t in enumerate(grid):
            grid_rows.append(dict(netting_set=ns.netting_set, ccy=rep, t=t, EE=st_c["EE"][i], NEE=st_c["NEE"][i],
                                  PFE=st_c["PFE"][i], EE_uncoll=st_u["EE"][i], NEE_uncoll=st_u["NEE"][i]))
        sum_rows.append(dict(
            netting_set=ns.netting_set, counterparty=ns.counterparty, ccy=rep, csa_ccy=ccy or rep,
            collateralized=csa.collateralized, n_trades=len(ns.trades), maturity_yrs=ns.maturity,
            MTM0=float(V0[0]) * x0,
            EPE=time_avg(grid, st_c["EE"], h), ENE=time_avg(grid, st_c["NEE"], h),
            EEPE=time_avg(grid, eee, h), peak_EE=float(st_c["EE"].max()), peak_PFE=float(st_c["PFE"].max()),
            EPE_uncoll=time_avg(grid, st_u["EE"], h),
            SACCR_EAD0=float(r0["EAD"][0]) * x0, SACCR_RC0=float(r0["RC"][0]) * x0,
            SACCR_AddOn0=float(r0["AddOn"][0]) * x0, SACCR_multiplier0=float(r0["multiplier"][0]),
            n_paths=cfg.n_paths, seed=cfg.seed))

    return Results(pd.DataFrame(ns_rows), pd.DataFrame(tr_rows), pd.DataFrame(sum_rows),
                   pd.DataFrame(mtm_rows), pd.DataFrame(sl_rows), pd.DataFrame(se_rows),
                   pd.DataFrame(grid_rows), warnings)
