"""SA-CCR (BCBS CRE52) for the equity and commodity asset classes.

EAD = alpha * (RC + PFE), alpha = 1.4
PFE = multiplier * AddOn,  multiplier = min(1, 0.05 + 0.95 exp((V - C) / (2 * 0.95 * AddOn)))
AddOn_equity = sqrt((sum_k rho_k A_k)^2 + sum_k (1 - rho_k^2) A_k^2),  A_k = SF_k * sum_i delta_i d_i MF_i
  SF 32% single name / 20% index, rho 50% / 80%, supervisory option vol 120% / 75%
  d_i = shares * current price (MARKET), or for a TRS with sa_notional_basis STRIKE (default for
  AVERAGE / blend & extend) shares * reference price of the period in force (the previous strike,
  then the reset price once the notional re-fixes on the path)
MF unmargined = sqrt(min(max(M, 10bd), 1y) / 1y); margined = 1.5 sqrt(MPOR / 1y) (250 business days)
For transactions that settle outstanding exposure and reset to zero fair value on set dates
(reset TRS), M is the time to the next reset date (CRE52.37(5)); MF floor of 10 business days
per CRE52.48.
AddOn_commodity = sum over hedging sets (no offset between them) of
  sqrt((rho sum_k A_k)^2 + (1 - rho^2) sum_k A_k^2),  A_k = SF_k * sum_i delta_i d_i MF_i per commodity
  type, rho 40%, SF 18% (40% electricity). Long and short positions in one commodity type offset
  fully. Asset-class add-ons are summed. A leg that has fixed but not paid has no price risk: it
  stays in RC with no add-on. Commodity type is an input (a policy choice, see CRE52.71).
RC unmargined = max(V - C, 0); margined = max(V - C, TH + MTA - NICA, 0); margined EAD capped at
the unmargined EAD (reported components are those of the binding branch). Zero once matured.
"""
from __future__ import annotations

import numpy as np

from .collateral import vm_balance
from .commodity.swap import CommoditySwapLeg
from .market import MarketData
from .model import CSA, OptionLeg, TRSLeg
from .products.option import supervisory_delta

ALPHA = 1.4
SF = {False: 0.32, True: 0.20}
RHO = {False: 0.50, True: 0.80}
SUP_VOL = {False: 1.20, True: 0.75}
FLOOR_M = 10.0 / 250.0
COM_RHO = 0.40


def commodity_sf(commodity_type: str) -> float:
    return 0.40 if commodity_type == "ELECTRICITY" else 0.18


def maturity_factor(M: float, csa: CSA, margined: bool) -> float:
    if margined:
        return 1.5 * np.sqrt(max(csa.mpor_days, 10) / 250.0)
    return float(np.sqrt(min(max(M, FLOOR_M), 1.0)))


def sa_price(leg, t: float, S_t, fix=None):
    """Price used in the adjusted notional d = shares * price. ``fix(underlying, t_fix)`` returns S at
    a past path fixing (needed for STRIKE basis after a reset)."""
    S_t = np.asarray(S_t, dtype=float)
    if isinstance(leg, TRSLeg) and leg.sa_notional_basis == "STRIKE":
        if leg.sa_ref_price is not None:          # blend & extend with resets: previous strike for life
            return np.full_like(S_t, leg.sa_ref_price)
        ref, tf = leg.strike_ref(t)
        if ref is not None:
            return np.full_like(S_t, ref)
        if tf is not None and fix is not None:
            return np.asarray(fix(leg.underlying, tf), dtype=float) * np.ones_like(S_t)
    return S_t


def leg_terms(leg, t: float, S_t, mkt: MarketData, ccy: str = "", fix=None):
    """Return (entity, is_index, d, delta, M) for a live leg at t, else None; d in ``ccy``."""
    if leg.expiry <= t + 1e-12:
        return None
    und = mkt.underlyings[leg.underlying]
    S_t = np.asarray(S_t, dtype=float)
    d = leg.shares * sa_price(leg, t, S_t, fix) * float(mkt.fx(und.ccy, ccy, t))
    if isinstance(leg, CommoditySwapLeg):
        p = mkt.commodity.products[und.product]
        d = d if t < leg.fixing_t - 1e-12 else np.zeros_like(S_t)
        return (p.hedging_set, p.commodity_type), False, d, np.full_like(S_t, leg.omega), leg.expiry - t
    if isinstance(leg, TRSLeg):
        M = (leg.next_reset_after(t) if leg.has_reset else leg.maturity) - t
        delta = np.full_like(S_t, leg.omega)
    else:
        M = leg.expiry - t
        delta = supervisory_delta(leg.sign, leg.is_call, S_t, leg.strike, M, SUP_VOL[und.is_index])
    return und.root, und.is_index, d, delta, M


def saccr(legs, t: float, S: dict, V, csa: CSA, mkt: MarketData, breakdown: bool = False, fix=None):
    """SA-CCR at time t, in the CSA currency. S: {underlying: S_t array}; V: netting-set value
    array in the CSA currency (same shape).

    Returns dict of arrays RC, AddOn, multiplier, PFE, EAD and cap (bool). For a margined netting
    set the EAD is min(margined, unmargined) path by path, and RC / AddOn / multiplier / PFE are
    those of the branch that gives the EAD, so EAD = 1.4 (RC + PFE) always holds; cap is True where
    the unmargined EAD is the binding one. The raw components of each branch are under "margined"
    and "unmargined". Once every leg has expired (netting set matured) everything is 0, including
    the margined RC floor TH + MTA - NICA. With breakdown=True, per-leg and per-entity rows of the
    binding branch (by the first path) are added."""
    V = np.asarray(V, dtype=float)
    margined = csa.collateralized
    entities: dict[str, list] = {}
    rows = []
    en_u: dict[str, np.ndarray] = {}
    en_m: dict[str, np.ndarray] = {}
    for leg in legs:
        terms = leg_terms(leg, t, S[leg.underlying], mkt, csa.ccy, fix)
        if terms is None:
            continue
        ent, is_idx, d, delta, M = terms
        mf_u = maturity_factor(M, csa, False)
        mf_m = maturity_factor(M, csa, True)
        entities[ent] = [is_idx]
        en_u[ent] = en_u.get(ent, 0.0) + delta * d * mf_u
        en_m[ent] = en_m.get(ent, 0.0) + delta * d * mf_m
        if breakdown:
            com = isinstance(ent, tuple)
            rows.append(dict(trade_id=leg.trade_id, leg_id=leg.leg_id, leg_type=leg.kind,
                             underlying=leg.underlying if com else ent, sa_notional_basis=getattr(leg, "sa_notional_basis", "MARKET"),
                             adjusted_notional=float(np.mean(d)),
                             supervisory_delta=float(np.mean(delta)), maturity_M=M,
                             _mf=(mf_u, mf_m), _en=(float(np.mean(delta * d * mf_u)),
                                                    float(np.mean(delta * d * mf_m))),
                             **(dict(hedging_set=ent[0], commodity_type=ent[1]) if com else {})))

    zero = np.zeros_like(V)
    if not entities:              # netting set matured: no exposure, no RC floor
        comp = dict(RC=zero, AddOn=zero, multiplier=np.ones_like(V), PFE=zero, EAD=zero)
        res = dict(comp, cap=np.zeros(V.shape, dtype=bool), unmargined=comp, margined=comp)
        if breakdown:
            res["legs"], res["entities"] = [], []
        return res

    def addon(en):
        sys_ = 0.0
        idio = 0.0
        sets: dict[str, list] = {}
        for ent, (is_idx,) in entities.items():
            if isinstance(ent, tuple):
                a = commodity_sf(ent[1]) * en[ent]
                s = sets.setdefault(ent[0], [0.0, 0.0])
                s[0], s[1] = s[0] + a, s[1] + a * a
                continue
            a = SF[is_idx] * en[ent]
            sys_ = sys_ + RHO[is_idx] * a
            idio = idio + (1.0 - RHO[is_idx] ** 2) * a * a
        out = np.sqrt(sys_ * sys_ + idio)
        for net, sq in sets.values():
            out = out + np.sqrt((COM_RHO * net) ** 2 + (1.0 - COM_RHO ** 2) * sq)
        return out * np.ones_like(V)

    def mult(vc, ao):
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            m = 0.05 + 0.95 * np.exp(vc / (2.0 * 0.95 * ao))
        return np.where(ao > 0, np.minimum(1.0, m), 1.0)

    def branch(rc, ao, vc):
        m = mult(vc, ao)
        return dict(RC=rc, AddOn=ao, multiplier=m, PFE=m * ao, EAD=csa.sa_alpha * (rc + m * ao))

    nica = csa.nica
    vc_u = V - nica
    unm = branch(np.maximum(vc_u, 0.0), addon(en_u), vc_u)
    if margined:
        vm = vm_balance(V, csa)
        vc = V - vm - nica
        mar = branch(np.maximum(np.maximum(vc, csa.threshold_cp + csa.mta - nica), 0.0), addon(en_m), vc)
        cap = unm["EAD"] < mar["EAD"]
        res = {k: np.where(cap, unm[k], mar[k]) for k in unm}
    else:
        mar, cap = unm, np.zeros(V.shape, dtype=bool)
        res = dict(unm)
    res.update(cap=cap, unmargined=unm, margined=mar)
    if breakdown:
        use_u = (not margined) or bool(cap.flat[0])
        j = 0 if use_u else 1
        for r in rows:
            r["MF"] = r.pop("_mf")[j]
            r["effective_notional"] = r.pop("_en")[j]
        en = en_u if use_u else en_m
        res["legs"] = rows
        res["entities"] = [
            dict(underlying=e[1], is_index=False, SF=commodity_sf(e[1]), rho=COM_RHO, hedging_set=e[0],
                 commodity_type=e[1], entity_addon=float(np.mean(commodity_sf(e[1]) * en[e])))
            if isinstance(e, tuple) else
            dict(underlying=e, is_index=v[0], SF=SF[v[0]], rho=RHO[v[0]],
                 entity_addon=float(np.mean(SF[v[0]] * en[e])))
            for e, v in entities.items()]
    return res
