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
from .commodity.market import is_future
from .commodity.nfactor import simulate_factors
from .commodity.swap import CommoditySwapLeg, swap_value
from .market import MarketData
from .model import NettingSet, OptionLeg, RunConfig, TRSLeg
from .products import option_value, trs_value
from .saccr import commodity_sf, maturity_factor, saccr
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
            elif isinstance(leg, CommoditySwapLeg):
                ev.append(leg.fixing_t)
                ev += [x for x in (leg.active_from, leg.active_to) if np.isfinite(x)]
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


EVENT_BUCKET_MODES = ("NONE", "MATURITY", "ALL")


def ns_event_times(ns: NettingSet, mode: str) -> list[float]:
    """Event times of one netting set that become extra output buckets (see RunConfig.event_buckets)."""
    mode = mode.upper()
    if mode not in EVENT_BUCKET_MODES:
        raise ValueError(f"event_buckets must be one of {', '.join(EVENT_BUCKET_MODES)} (got '{mode}')")
    if mode == "NONE":
        return []
    ev = [leg.expiry for leg in ns.legs]
    if mode == "ALL":
        ev += [p.end for leg in ns.legs if isinstance(leg, TRSLeg) for p in leg.periods]
        ev += [leg.fixing_t for leg in ns.legs if isinstance(leg, CommoditySwapLeg)]
    return sorted({e for e in ev if e > 0})


def netting_set_buckets(ns: NettingSet, cfg: RunConfig):
    """Output buckets for one netting set: the standard buckets plus, per cfg.event_buckets, the
    netting set's own event dates up to the last standard bucket.

    AVERAGE mode: an event bucket ends on the event date, so the bucket before it holds only live
    exposure; the area under the profile (sum of value x bucket width) is unchanged.
    POINT mode: the bucket is placed one day before the event, i.e. the last pre-settlement value
    (on the event date itself the cash flow has settled).
    Returns (bucket_dates, bucket_times, bucket_start_dates, bucket_types)."""
    std_t = cfg.bucket_times()
    ev = np.array(ns_event_times(ns, cfg.event_buckets))
    if ev.size and cfg.output_mode.upper() == "POINT":
        ev = ev - ONE_DAY
    ev = ev[(ev > 1e-9) & (ev <= std_t[-1] + 1e-9)] if ev.size else ev
    t = dt.merge_times(std_t, ev)          # a 0D standard bucket (t = 0) is kept: zero width, t0 value
    val = cfg.valuation_date
    std_dates = {round(x * 365): d for x, d in zip(std_t, cfg.buckets)}
    dates, types = [], []
    for x in t:
        n = round(x * 365)
        is_std = n in std_dates
        dates.append(std_dates[n] if is_std else val + dt.timedelta(days=n))
        types.append("STANDARD" if is_std else "EVENT")
    starts = [val] + dates[:-1]
    return dates, t, starts, types


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
    if isinstance(leg, CommoditySwapLeg):
        if not leg.active(t):
            return np.zeros(St.shape[0])
        fixed = leg.fixing_t - 1e-12 <= t < leg.payment_t - 1e-12
        return swap_value(leg, t, St, mkt, S[leg.underlying][:, _idx(grid, leg.fixing_t)] if fixed else None)
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
        idx = range(len(grid))
        if isinstance(leg, CommoditySwapLeg):    # only where the leg is live: zero elsewhere
            a = int(np.searchsorted(grid, leg.active_from - 1e-12))
            b = int(np.searchsorted(grid, min(leg.active_to, leg.payment_t) - 1e-12))
            idx = range(a, b)
        for i in idx:
            V[:, i] += x[i] * leg_value(leg, grid[i], i, grid, S, mkt)
    return V


def leg_mtm0(leg, mkt: MarketData) -> float:
    S0 = np.array([mkt.underlyings[leg.underlying].spot])
    if isinstance(leg, OptionLeg):
        return float(option_value(leg, 0.0, S0, mkt)[0])
    if isinstance(leg, CommoditySwapLeg):
        return float(swap_value(leg, 0.0, S0, mkt)[0])
    return float(trs_value(leg, 0.0, S0, mkt, S0)[0])


def leg_exposure0(leg, mkt: MarketData) -> float:
    """Value at t = 0 counted in exposure: the booked MTM, or 0 for a commodity leg outside its window."""
    if isinstance(leg, CommoditySwapLeg) and not leg.active(0.0):
        return 0.0
    return leg_mtm0(leg, mkt)


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
    saccr_proxy: pd.DataFrame | None = None
    exposure_compare: pd.DataFrame | None = None
    system_saccr: pd.DataFrame | None = None

    def tables(self) -> dict[str, pd.DataFrame]:
        out = {"Summary": self.summary, "EE_NEE": self.ns_profile, "TradeExposure": self.trade_profile,
               "LegMTM": self.leg_mtm, "SACCR_Legs": self.saccr_legs, "SACCR_Entities": self.saccr_entities,
               "GridProfile": self.grid_profile}
        if self.saccr_proxy is not None and len(self.saccr_proxy):
            out["SACCR_Proxy"] = self.saccr_proxy
        if self.system_saccr is not None and len(self.system_saccr):
            out["SystemSACCR"] = self.system_saccr
        if self.exposure_compare is not None and len(self.exposure_compare):
            out["ExposureCompare"] = self.exposure_compare
        if self.analytic is not None:
            out["AnalyticCheck"] = self.analytic
        out["Warnings"] = pd.DataFrame({"warning": self.warnings or ["(none)"]})
        return out


# ----------------------------------------------------------------------------- engine
def simulate_all(mkt: MarketData, names: list[str], grid, cfg: RunConfig) -> dict[str, np.ndarray]:
    """Paths for every underlying: equities by GBM, futures contracts by the N-factor model."""
    fut = [n for n in names if is_future(mkt, n)]
    eq = [n for n in names if n not in fut]
    S = simulate(mkt, eq, grid, cfg.n_paths, cfg.seed, cfg.antithetic) if eq else {}
    if fut:
        S.update(simulate_factors(mkt, fut, grid, cfg.n_paths, cfg.seed, cfg.antithetic))
    return S


def sa_proxy_rows(ns: NettingSet, mkt: MarketData, x0: float) -> list[dict]:
    """Single-line equivalent of each commodity trade at t = 0: the notional that gives the trade's
    standalone add-on as one position, SA_NOTIONAL = AddOn / (SF * MF). Exact at t = 0 for a
    standalone trade whose legs share a hedging set, supervisory factor and maturity factor."""
    rows = []
    margined = ns.csa.collateralized
    for tr in ns.trades:
        if not all(isinstance(l, CommoditySwapLeg) for l in tr.legs):
            continue
        S0 = {l.underlying: np.array([mkt.underlyings[l.underlying].spot]) for l in tr.legs}
        r = saccr(tr.legs, 0.0, S0, np.zeros(1), ns.csa, mkt)
        live = [l for l in tr.legs if l.active(0.0) and l.expiry > 1e-12] or tr.legs
        ps = [mkt.commodity.products[mkt.underlyings[l.underlying].product] for l in live]
        sfs = {commodity_sf(p.commodity_type) for p in ps}
        mfs = {round(maturity_factor(l.sa_M(0.0), ns.csa, margined), 12) for l in live}
        exact = len(sfs) == 1 and len(mfs) == 1 and len({p.hedging_set for p in ps}) == 1
        sf, mf = max(sfs), max(mfs)
        addon = float(r["margined" if margined else "unmargined"]["AddOn"][0]) * x0
        # notional per leg in the reporting currency at today's prices
        n0 = {id(l): l.quantity * mkt.underlyings[l.underlying].spot
              * float(mkt.fx(mkt.underlyings[l.underlying].ccy, mkt.reporting_ccy, 0.0)) for l in tr.legs}
        M = max(l.sa_amort_T if l.sa_amort_T is not None else l.payment_t for l in tr.legs)
        total = sum(n0[id(l)] for l in tr.legs if l.sa_interval()[0] <= 1e-12 < l.sa_interval()[1])
        amort = sum(n0[id(l)] * max(min(l.sa_interval()[1], M) - l.sa_interval()[0], 0.0)
                    for l in tr.legs) / M if M > 0 else 0.0
        rows.append(dict(netting_set=ns.netting_set, trade_id=tr.trade_id, SF=sf, MF=mf, AddOn0=addon,
                         SA_NOTIONAL=addon / (sf * mf) if exact else np.nan,
                         SA_NOTIONAL_TOTAL=total, SA_NOTIONAL_AMORT=amort, M_trade=M,
                         sa_notional_basis=tr.legs[0].sa_notional_basis,
                         windowed=any(l.windowed for l in tr.legs),
                         note="" if exact else "legs differ in hedging set, SF or MF: keep the legs"))
    return rows


def _netting_set_results(ns: NettingSet, grid: np.ndarray, trade_value, S_sa: dict, Sfwd: dict,
                         mkt: MarketData, cfg: RunConfig, n_sa: int, out: dict, source: str = "MODEL",
                         fwd_grid: np.ndarray | None = None) -> None:
    """Exposure, collateral, funding and SA-CCR outputs of one netting set, appended to ``out``.

    trade_value(trade) -> (paths x grid) value cube of the trade in the CSA currency (simulated, or
    imported path-by-path PVs). S_sa: {underlying: (paths x grid) prices} for the SA-CCR adjusted
    notional (simulated paths, or the forward broadcast when only PVs are imported). Sfwd lives on
    ``fwd_grid`` (default ``grid``), which must hold every bucket and event date. With an imported
    grid, a bucket or fixing that is not an import date uses the last import date before it."""
    strict = fwd_grid is None
    gF = grid if strict else fwd_grid

    def col(t: float) -> int:
        if strict:
            return _idx(grid, t)
        return max(int(np.searchsorted(grid, t + 1e-9)) - 1, 0)
    mode = cfg.output_mode.upper()
    q = cfg.pfe_quantile
    h_epe = dt.tenor_years(cfg.epe_horizon)
    rep = mkt.reporting_ccy
    csa = ns.csa
    ccy = csa.ccy
    xg = mkt.fx(ccy, rep, grid)                      # CSA currency -> reporting, along the grid
    x0 = float(mkt.fx(ccy, rep, 0.0))
    bdates, bt, bstart, btype = netting_set_buckets(ns, cfg)
    df_b = mkt.curve.df(bt)

    def to_rep(st):
        return {k: v * xg for k, v in st.items()}

    V = None
    for tr in ns.trades:
        Vt = trade_value(tr)
        V = Vt.copy() if V is None else V + Vt
        st = to_rep(stats(Vt, q))
        b = {k: bucketise(grid, v, bt, mode) for k, v in st.items()}
        for k, (d, t) in enumerate(zip(bdates, bt)):
            out["tr"].append(dict(netting_set=ns.netting_set, trade_id=tr.trade_id, ccy=rep, bucket_date=d,
                                  bucket_start=bstart[k], bucket_type=btype[k], t=t,
                                  MTM=b["MTM"][k], EE=b["EE"][k], NEE=b["NEE"][k], PFE=b["PFE"][k],
                                  EE_disc=b["EE"][k] * df_b[k], NEE_disc=b["NEE"][k] * df_b[k]))
        for leg in tr.legs:
            m0 = leg_mtm0(leg, mkt)
            und = mkt.underlyings[leg.underlying]
            row = dict(netting_set=ns.netting_set, trade_id=tr.trade_id, leg_id=leg.leg_id,
                       leg_type=leg.kind, underlying=leg.underlying, mtm0=m0,
                       spot=und.spot, trade_spot=und.trade_spot, spot_shock=und.shock)
            if isinstance(leg, OptionLeg):
                px = m0 / (leg.sign * leg.shares)
                row.update(strike=leg.strike, expiry=leg.expiry_date, engine_px_per_share=px,
                           ovme_px_per_share=leg.ovme_price,
                           diff_vs_ovme=None if leg.ovme_price is None else px - leg.ovme_price)
            elif isinstance(leg, CommoditySwapLeg):
                row.update(maturity=leg.payment_date, fixing_date=leg.fixing_date,
                           quantity=leg.quantity, fixed_price=leg.fixed_price,
                           notional0=leg.quantity * und.spot)
            else:
                row.update(maturity=leg.maturity_date, reset=leg.reset_freq or "NONE",
                           notional_basis=leg.notional_basis,
                           notional0=leg.shares * (leg.periods[0].ref_price or 0.0),
                           carried_mtm=leg.initial_mtm,
                           carried_mtm_settle=(mkt.valuation_date + dt.timedelta(days=round(leg.im_settle * 365))
                                               if leg.initial_mtm else None),
                           sa_notional_basis=leg.sa_notional_basis)
            lc = leg_ccy(leg, mkt)
            row.update(leg_ccy=lc or rep, mtm0_reporting=m0 * float(mkt.fx(lc, rep, 0.0)))
            out["mtm"].append(row)
    n_paths = V.shape[0]

    E, _ = collateralised(V, grid, csa, ns.maturity)
    st_c = to_rep(stats(E, q))
    st_u = to_rep(stats(V, q))
    # funding exposure (no default): margin actually held today, i.e. CSA(V(t)) without the MPOR lag
    live = (grid < ns.maturity - 1e-9)[None, :]
    if csa.collateralized:
        vm_now = np.where(live, vm_balance(V, csa), 0.0)
        F = np.where(live, V - vm_balance(V, csa), 0.0)
    else:
        vm_now, F = np.zeros_like(V), V
    fca, fba = np.maximum(F, 0).mean(0) * xg, np.minimum(F, 0).mean(0) * xg
    vm_rec, vm_post = np.maximum(vm_now, 0).mean(0) * xg, np.maximum(-vm_now, 0).mean(0) * xg
    eee = np.maximum.accumulate(st_c["EE"])
    h = min(h_epe, ns.maturity)

    # SA-CCR through time: path-wise mean (subset of paths) and deterministic forward path
    legs = ns.legs
    names = sorted({l.underlying for l in legs})
    n_sa = min(n_sa, n_paths)
    Vf = value_cube(legs, gF, Sfwd, mkt, ccy)
    xF = mkt.fx(ccy, rep, gF)
    sa_mean = {k: np.zeros(len(bt)) for k in ("EAD", "RC", "PFE", "AddOn", "multiplier", "cap")}
    sa_fwd = np.zeros(len(bt))

    def fix_mc(u, tf):
        return S_sa[u][:n_sa, col(tf)]

    def fix_fwd(u, tf):
        return Sfwd[u][:, _idx(gF, tf)]
    for k, t in enumerate(bt):
        if t <= gF[-1] + 1e-9:
            j = _idx(gF, t)
            r_f = saccr(legs, t, {n: Sfwd[n][:, j] for n in names}, Vf[:, j], csa, mkt, fix=fix_fwd)
            sa_fwd[k] = float(r_f["EAD"][0]) * xF[j]
        if t > grid[-1] + 1e-9:
            continue
        i = col(t)
        r = saccr(legs, t, {n: S_sa[n][:n_sa, i] for n in names}, V[:n_sa, i], csa, mkt, fix=fix_mc)
        for key in sa_mean:
            fx = 1.0 if key in ("multiplier", "cap") else xg[i]
            sa_mean[key][k] = float(np.mean(r[key])) * fx
    # time-averaged EAD over each bucket (same convention as EE in AVERAGE mode): mean EAD on
    # every grid point while the netting set is live (0 afterwards), then bucket-averaged
    if mode == "AVERAGE":
        ead_g = np.zeros(len(grid))
        for i, t in enumerate(grid):
            if t >= ns.maturity - 1e-9:
                break
            r = saccr(legs, t, {n: S_sa[n][:n_sa, i] for n in names}, V[:n_sa, i], csa, mkt, fix=fix_mc)
            ead_g[i] = float(np.mean(r["EAD"])) * xg[i]
        sa_avg = bucketise(grid, ead_g, bt, mode)
    else:
        sa_avg = sa_mean["EAD"].copy()
    S0 = {n: np.array([mkt.underlyings[n].spot]) for n in names}
    V0 = np.array([sum(leg_exposure0(l, mkt) * float(mkt.fx(leg_ccy(l, mkt), ccy, 0.0)) for l in legs)])
    r0 = saccr(legs, 0.0, S0, V0, csa, mkt, breakdown=True)
    for row in r0["legs"]:
        row.update(adjusted_notional=row["adjusted_notional"] * x0,
                   effective_notional=row["effective_notional"] * x0)
        out["sl"].append(dict(netting_set=ns.netting_set, ccy=rep, **row))
    for row in r0["entities"]:
        row.update(entity_addon=row["entity_addon"] * x0)
        out["se"].append(dict(netting_set=ns.netting_set, ccy=rep, **row))

    out["px"] += sa_proxy_rows(ns, mkt, x0)

    bc = {k: bucketise(grid, v, bt, mode) for k, v in st_c.items()}
    bu = {k: bucketise(grid, v, bt, mode) for k, v in st_u.items()}
    bfca, bfba = bucketise(grid, fca, bt, mode), bucketise(grid, fba, bt, mode)
    bvr, bvp = bucketise(grid, vm_rec, bt, mode), bucketise(grid, vm_post, bt, mode)
    for k, (d, t) in enumerate(zip(bdates, bt)):
        out["ns"].append(dict(
            netting_set=ns.netting_set, counterparty=ns.counterparty, ccy=rep, bucket_date=d,
            bucket_start=bstart[k], bucket_type=btype[k], t=t, df=df_b[k],
            MTM=bu["MTM"][k], EE=bc["EE"][k], NEE=bc["NEE"][k], PFE=bc["PFE"][k], NPFE=bc["NPFE"][k],
            EE_disc=bc["EE"][k] * df_b[k], NEE_disc=bc["NEE"][k] * df_b[k],
            EE_uncoll=bu["EE"][k], NEE_uncoll=bu["NEE"][k], PFE_uncoll=bu["PFE"][k],
            FCA_exposure=bfca[k], FBA_exposure=bfba[k], E_VM_received=bvr[k], E_VM_posted=bvp[k],
            EE_stderr=bc["EE_se"][k], NEE_stderr=bc["NEE_se"][k],
            SACCR_EAD_mean=sa_mean["EAD"][k], SACCR_RC_mean=sa_mean["RC"][k],
            SACCR_PFE_mean=sa_mean["PFE"][k], SACCR_AddOn_mean=sa_mean["AddOn"][k],
            SACCR_multiplier_mean=sa_mean["multiplier"][k], SACCR_cap_share=sa_mean["cap"][k],
            SACCR_EAD_fwd=sa_fwd[k], SACCR_EAD_avg=sa_avg[k]))
    for i, t in enumerate(grid):
        out["grid"].append(dict(netting_set=ns.netting_set, ccy=rep, t=t, EE=st_c["EE"][i], NEE=st_c["NEE"][i],
                                PFE=st_c["PFE"][i], EE_uncoll=st_u["EE"][i], NEE_uncoll=st_u["NEE"][i]))
    out["sum"].append(dict(
        netting_set=ns.netting_set, counterparty=ns.counterparty, ccy=rep, csa_ccy=ccy or rep,
        collateralized=csa.collateralized, n_trades=len(ns.trades), maturity_yrs=ns.maturity,
        MTM0=float(V0[0]) * x0,
        EPE=time_avg(grid, st_c["EE"], h), ENE=time_avg(grid, st_c["NEE"], h),
        EEPE=time_avg(grid, eee, h), peak_EE=float(st_c["EE"].max()), peak_PFE=float(st_c["PFE"].max()),
        EPE_uncoll=time_avg(grid, st_u["EE"], h),
        SACCR_EAD0=float(r0["EAD"][0]) * x0, SACCR_RC0=float(r0["RC"][0]) * x0,
        SACCR_AddOn0=float(r0["AddOn"][0]) * x0, SACCR_multiplier0=float(r0["multiplier"][0]),
        SACCR_PFE0=float(r0["PFE"][0]) * x0,
        SACCR_basis0=("UNMARGINED_CAP" if bool(r0["cap"][0]) else "MARGINED") if csa.collateralized
        else "UNMARGINED",
        n_paths=n_paths, seed=cfg.seed, value_source=source))


def _new_rows() -> dict[str, list]:
    return {k: [] for k in ("ns", "tr", "sum", "mtm", "sl", "se", "grid", "px", "sys")}


def _results(out: dict, warnings: list[str]) -> Results:
    return Results(pd.DataFrame(out["ns"]), pd.DataFrame(out["tr"]), pd.DataFrame(out["sum"]),
                   pd.DataFrame(out["mtm"]), pd.DataFrame(out["sl"]), pd.DataFrame(out["se"]),
                   pd.DataFrame(out["grid"]), warnings, saccr_proxy=pd.DataFrame(out["px"]),
                   system_saccr=pd.DataFrame(out["sys"]))


def trade_live(tr, t: float) -> bool:
    """False once every leg is settled, or when every commodity leg is outside its exposure window."""
    if t >= tr.maturity - 1e-12:
        return False
    return any(l.live(t) if isinstance(l, CommoditySwapLeg) else True for l in tr.legs)


def _imported_value(tr, ns: NettingSet, cube, grid: np.ndarray, mkt: MarketData) -> np.ndarray:
    """Imported PVs of one trade on ``grid`` (import dates plus t = 0), zero where the booked trade is
    not live (outside its window or settled). t = 0 uses the booked MTM if the export has no
    valuation-date column."""
    if tr.trade_id not in cube.values:
        raise ValueError(f"Netting set {ns.netting_set} is IMPORT but trade {tr.trade_id} is not on PathImport")
    X = cube.values[tr.trade_id]
    V = np.zeros((cube.n_scen, len(grid)))
    pos = {round(t, 9): j for j, t in enumerate(cube.times)}
    for i, t in enumerate(grid):
        if not trade_live(tr, t):
            continue
        j = pos.get(round(t, 9))
        if j is not None:
            V[:, i] = X[:, j]
        else:                                   # t = 0 not in the export
            V[:, i] = sum(leg_exposure0(l, mkt) * float(mkt.fx(leg_ccy(l, mkt), ns.csa.ccy, 0.0)) for l in tr.legs)
    return V


def implied_prices(ns: NettingSet, cubes: dict, grid: np.ndarray, base: dict, mkt: MarketData) -> dict:
    """SA-CCR price paths implied from imported trade PVs (only PVs are exported).

    For each live trade, PV = sum omega q DF (X - K) over its live legs; the uniform price move r with
    X_i = r F0_i that reproduces the PV is r = (PV + sum omega q K DF) / sum omega q DF F0, and each of
    the trade's contracts gets the price r F0 on that path (the first live trade wins if two share a
    contract). Exact for a trade on one price; a fixed but unpaid month enters through the PV.
    Contracts not in any live trade keep the forward ``base``."""
    out = {u: np.array(base[u], dtype=float) for u in base}
    for i, t in enumerate(grid):
        done = set()
        for tr in ns.trades:
            legs = [l for l in tr.legs if isinstance(l, CommoditySwapLeg) and l.live(t)]
            if not legs or not trade_live(tr, t):
                continue
            num, den = cubes[tr.trade_id][:, i].copy(), 0.0
            for l in legs:
                c = mkt.underlyings[l.underlying]
                a = l.omega * l.quantity * float(mkt.und_curve(l.underlying).df_fwd(t, l.payment_t)) \
                    * float(mkt.fx(c.ccy, ns.csa.ccy, t))
                num += a * l.fixed_price
                den += a * float(base[l.underlying][0, i])
            if abs(den) < 1e-12:
                continue
            r = np.maximum(num / den, 0.0)
            for l in legs:
                if l.underlying not in done:
                    out[l.underlying][:, i] = r * base[l.underlying][0, i]
                    done.add(l.underlying)
    return out


def run_monte_carlo(netting_sets: list[NettingSet], mkt: MarketData, cfg: RunConfig,
                    warnings: list[str] | None = None, saccr_paths: int = 2000, imported=None) -> Results:
    """``imported``: a pathimport.PathCube for netting sets with value_source IMPORT."""
    warnings = list(warnings or [])
    out = _new_rows()
    model_sets = [ns for ns in netting_sets if ns.value_source != "IMPORT"]
    imp_sets = [ns for ns in netting_sets if ns.value_source == "IMPORT"]
    if model_sets:
        grid = build_grid(model_sets, cfg)
        names = sorted({l.underlying for ns in model_sets for l in ns.legs})
        S = simulate_all(mkt, names, grid, cfg)
        Sfwd = forward_path(mkt, names, grid)
    if imp_sets:
        if imported is None:
            raise ValueError(f"Netting sets {', '.join(n.netting_set for n in imp_sets)} have value_source "
                             f"IMPORT but no PathImport data was loaded")
        g_imp = dt.merge_times([0.0], imported.times)
        g_fwd = build_grid(imp_sets, cfg)       # forward-path SA-CCR projection on the usual grid
        inames = sorted({l.underlying for ns in imp_sets for l in ns.legs})
        Sf_imp = forward_path(mkt, inames, g_fwd)
        S_imp = {n: np.broadcast_to(mkt.underlyings[n].forward(g_imp, mkt.und_curve(n))[None, :],
                                    (imported.n_scen, len(g_imp))) for n in inames}
        bt_all = np.unique(np.concatenate([netting_set_buckets(ns, cfg)[1] for ns in imp_sets]))
        off = [t for t in bt_all if t <= g_imp[-1] + 1e-9 and np.min(np.abs(g_imp - t)) > 1e-6]
        for basis in ("IMPLIED", "FORWARD"):
            sets_ = [n.netting_set for n in imp_sets if n.import_sa_price == basis]
            if not sets_:
                continue
            how = ("prices implied from each trade's PV (uniform price move per trade)" if basis == "IMPLIED"
                   else "today's forward on every scenario, which ignores the co-movement of notional and MTM")
            warnings.append(f"IMPORT netting sets ({', '.join(sets_)}): only PVs imported, so the SA-CCR "
                            f"adjusted notional uses {how}; RC and the multiplier are path-wise from the "
                            f"imported PVs ({imported.n_scen} scenarios).")
        if off:
            warnings.append(f"IMPORT: {len(off)} bucket date(s) are not export dates; their SA-CCR uses the "
                            f"last export date before the bucket and EE/PFE are interpolated.")
        coll = [n.netting_set for n in imp_sets if n.csa.collateralized]
        if coll:
            warnings.append(f"IMPORT: {', '.join(coll)} collateralised - the MPOR lag is taken on the export "
                            f"dates (nearest date at or after t - MPOR), so it is only as fine as the export.")
        if 0.0 not in np.round(imported.times, 9):
            warnings.append("IMPORT: no valuation-date PVs in the export; t = 0 uses the booked trade MTM.")
    prev: dict = {}          # trade cubes of the previous netting set (collateral variants of one booking)
    for ns in netting_sets:
        ccy = ns.csa.ccy
        if ns.value_source == "IMPORT":
            cubes = {tr.trade_id: _imported_value(tr, ns, imported, g_imp, mkt) for tr in ns.trades}
            S_ns = S_imp
            if ns.import_sa_price == "IMPLIED":
                S_ns = implied_prices(ns, cubes, g_imp, {u: S_imp[u] for u in {l.underlying for l in ns.legs}}, mkt)
            _netting_set_results(ns, g_imp, lambda tr, c=cubes: c[tr.trade_id],
                                 S_ns, Sf_imp, mkt, cfg, saccr_paths, out, "IMPORT", fwd_grid=g_fwd)
            _add_system_saccr(ns, imported, cfg, mkt, out)
        else:
            keep = len(ns.trades) <= 4
            cur: dict = {}

            def tv(tr, c=ccy, cur=cur, keep=keep):
                key = (c, _trade_key(tr))
                V = prev.get(key)
                if V is None:
                    V = value_cube(tr.legs, grid, S, mkt, c)
                if keep:
                    cur[key] = V
                return V
            _netting_set_results(ns, grid, tv, S, Sfwd, mkt, cfg, saccr_paths, out)
            prev = cur
    return _results(out, warnings)


def _add_system_saccr(ns: NettingSet, cube, cfg: RunConfig, mkt: MarketData, out: dict) -> None:
    """The exporting system's own path-wise SA-CCR (if exported), next to the engine's: at each bucket
    the path means of the trades live at that date (the strip whose window it is), taken at the last
    export date on or before the bucket. Summed over live trades (one under a window booking)."""
    if not any(tr.trade_id in cube.saccr for tr in ns.trades):
        return
    rep = mkt.reporting_ccy
    x = mkt.fx(ns.csa.ccy, rep, 0.0)

    def at(t: float):
        tot = dict(EAD=0.0, RC=0.0, PFE=0.0)
        n, missing = 0, False
        for tr in ns.trades:
            if not trade_live(tr, t):
                continue
            sa = cube.saccr.get(tr.trade_id)
            m = None if sa is None else sa[sa.t <= t + 1e-9]
            stale = m is not None and not m.empty and bool(
                ((cube.times > m.t.iloc[-1] + 1e-9) & (cube.times <= t + 1e-9)).any())
            if m is None or m.empty or stale:       # no SA-CCR for an export date on or before t
                missing = True
                continue
            row = m.iloc[-1]
            for k in tot:
                tot[k] += float(row[k]) * float(x)
            n += 1
        if missing:
            return dict(EAD=np.nan, RC=np.nan, PFE=np.nan), n
        return tot, n
    _, bt, _, _ = netting_set_buckets(ns, cfg)
    rows = [r for r in out["ns"] if r["netting_set"] == ns.netting_set]
    for r, t in zip(rows[-len(bt):], bt):
        v, n = at(t)
        r.update(SACCR_EAD_system=v["EAD"], SACCR_RC_system=v["RC"], SACCR_PFE_system=v["PFE"], system_live_trades=n)
    v0, _ = at(0.0)
    out["sum"][-1].update(SACCR_EAD0_system=v0["EAD"])
    for tr in ns.trades:
        sa = cube.saccr.get(tr.trade_id)
        if sa is None:
            continue
        for row in sa.itertuples(index=False):
            out["sys"].append(dict(netting_set=ns.netting_set, trade_id=tr.trade_id,
                                   request_id=cube.requests.get(tr.trade_id, ""), date=row.date, t=row.t,
                                   live=trade_live(tr, row.t), RC=row.RC * x, multiplier=row.multiplier,
                                   PFE=row.PFE * x, EAD=row.EAD * x, n_paths=row.n_paths))


def _trade_key(tr) -> tuple | None:
    """Valuation terms of a trade: two trades with the same key have the same value cube."""
    return tuple((type(l).__name__, repr(sorted((k, v) for k, v in vars(l).items()
                                              if k not in ("trade_id", "leg_id")))) for l in tr.legs)
