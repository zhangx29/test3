"""SA-CCR (BCBS CRE52) for the equity asset class.

EAD = alpha * (RC + PFE), alpha = 1.4
PFE = multiplier * AddOn,  multiplier = min(1, 0.05 + 0.95 exp((V - C) / (2 * 0.95 * AddOn)))
AddOn_equity = sqrt((sum_k rho_k A_k)^2 + sum_k (1 - rho_k^2) A_k^2),  A_k = SF_k * sum_i delta_i d_i MF_i
  SF 32% single name / 20% index, rho 50% / 80%, supervisory option vol 120% / 75%
  d_i = shares * current price
MF unmargined = sqrt(min(max(M, 10bd), 1y) / 1y); margined = 1.5 sqrt(MPOR / 1y) (250 business days)
For transactions that settle outstanding exposure and reset to zero fair value on set dates
(reset TRS), M is the time to the next reset date (CRE52.37(5)); MF floor of 10 business days
per CRE52.48.
RC unmargined = max(V - C, 0); margined = max(V - C, TH + MTA - NICA, 0); margined EAD capped at
the unmargined EAD.
"""
from __future__ import annotations

import numpy as np

from .collateral import vm_balance
from .market import MarketData
from .model import CSA, OptionLeg, TRSLeg
from .products.option import supervisory_delta

ALPHA = 1.4
SF = {False: 0.32, True: 0.20}
RHO = {False: 0.50, True: 0.80}
SUP_VOL = {False: 1.20, True: 0.75}
FLOOR_M = 10.0 / 250.0


def maturity_factor(M: float, csa: CSA, margined: bool) -> float:
    if margined:
        return 1.5 * np.sqrt(max(csa.mpor_days, 10) / 250.0)
    return float(np.sqrt(min(max(M, FLOOR_M), 1.0)))


def leg_terms(leg, t: float, S_t, mkt: MarketData):
    """Return (entity, is_index, d, delta, M) for a live leg at t, else None."""
    if leg.expiry <= t + 1e-12:
        return None
    und = mkt.underlyings[leg.underlying]
    S_t = np.asarray(S_t, dtype=float)
    d = leg.shares * S_t
    if isinstance(leg, TRSLeg):
        M = (leg.next_reset_after(t) if leg.has_reset else leg.maturity) - t
        delta = np.full_like(S_t, leg.omega)
    else:
        M = leg.expiry - t
        delta = supervisory_delta(leg.sign, leg.is_call, S_t, leg.strike, M, SUP_VOL[und.is_index])
    return leg.underlying, und.is_index, d, delta, M


def saccr(legs, t: float, S: dict, V, csa: CSA, mkt: MarketData, breakdown: bool = False):
    """SA-CCR at time t. S: {underlying: S_t array}; V: netting-set value array (same shape).
    Returns dict of arrays (RC, AddOn, multiplier, PFE, EAD) and optional per-leg breakdown."""
    V = np.asarray(V, dtype=float)
    margined = csa.collateralized
    entities: dict[str, list] = {}
    rows = []
    en_u: dict[str, np.ndarray] = {}
    en_m: dict[str, np.ndarray] = {}
    for leg in legs:
        terms = leg_terms(leg, t, S[leg.underlying], mkt)
        if terms is None:
            continue
        ent, is_idx, d, delta, M = terms
        mf_u = maturity_factor(M, csa, False)
        mf_m = maturity_factor(M, csa, True)
        entities[ent] = [is_idx]
        en_u[ent] = en_u.get(ent, 0.0) + delta * d * mf_u
        en_m[ent] = en_m.get(ent, 0.0) + delta * d * mf_m
        if breakdown:
            rows.append(dict(trade_id=leg.trade_id, leg_id=leg.leg_id, leg_type=leg.kind,
                             underlying=ent, adjusted_notional=float(np.mean(d)),
                             supervisory_delta=float(np.mean(delta)), maturity_M=M,
                             MF=mf_m if margined else mf_u,
                             effective_notional=float(np.mean(delta * d * (mf_m if margined else mf_u)))))

    def addon(en):
        sys_ = 0.0
        idio = 0.0
        for ent, (is_idx,) in entities.items():
            a = SF[is_idx] * en[ent]
            sys_ = sys_ + RHO[is_idx] * a
            idio = idio + (1.0 - RHO[is_idx] ** 2) * a * a
        return np.sqrt(sys_ * sys_ + idio) if entities else np.zeros_like(V)

    def mult(vc, ao):
        ao = np.asarray(ao, dtype=float) * np.ones_like(vc)
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            m = 0.05 + 0.95 * np.exp(vc / (2.0 * 0.95 * ao))
        return np.where(ao > 0, np.minimum(1.0, m), 1.0)

    nica = csa.nica
    # unmargined
    ao_u = addon(en_u) * np.ones_like(V)
    vc_u = V - nica
    rc_u = np.maximum(vc_u, 0.0)
    m_u = mult(vc_u, ao_u)
    ead_u = ALPHA * (rc_u + m_u * ao_u)
    if margined:
        vm = vm_balance(V, csa)
        ao = addon(en_m) * np.ones_like(V)
        vc = V - vm - nica
        rc = np.maximum(np.maximum(vc, csa.threshold_cp + csa.mta - nica), 0.0)
        m = mult(vc, ao)
        ead = np.minimum(ALPHA * (rc + m * ao), ead_u)
    else:
        ao, rc, m, ead = ao_u, rc_u, m_u, ead_u
    res = dict(RC=rc, AddOn=ao, multiplier=m, PFE=m * ao, EAD=ead)
    if breakdown:
        res["legs"] = rows
        res["entities"] = [dict(underlying=e, is_index=v[0], SF=SF[v[0]], rho=RHO[v[0]],
                                entity_addon=float(np.mean(SF[v[0]] * (en_m if margined else en_u)[e])))
                           for e, v in entities.items()]
    return res
