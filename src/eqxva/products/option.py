"""European equity option valuation (Black-Scholes with continuous dividend yield)."""
from __future__ import annotations

import numpy as np
from scipy.special import ndtr

from ..market import MarketData


def bs_price(S, K, T, df, q, vol, is_call: bool):
    """Black-Scholes price per share. df = P(t,T); T = time to expiry. Vectorised over S."""
    S = np.asarray(S, dtype=float)
    if T <= 0:
        return np.maximum(S - K, 0.0) if is_call else np.maximum(K - S, 0.0)
    fwd = S * np.exp(-q * T) / df
    sd = np.maximum(vol * np.sqrt(T), 1e-12)
    with np.errstate(divide="ignore"):
        d1 = (np.log(np.maximum(fwd, 1e-300) / K) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if is_call:
        return df * (fwd * ndtr(d1) - K * ndtr(d2))
    return df * (K * ndtr(-d2) - fwd * ndtr(-d1))


def option_vol(leg, mkt: MarketData, tau):
    if leg.vol_override is not None:
        return np.full(np.shape(tau), leg.vol_override, dtype=float) + 0.0
    und = mkt.underlyings[leg.underlying]
    return und.vol.vol(tau, leg.strike / und.spot)


def option_value(leg, t: float, S_t, mkt: MarketData):
    """Bank-view value of an option leg at time t given S_t (post-settlement: 0 at/after expiry)."""
    S_t = np.asarray(S_t, dtype=float)
    tau = leg.expiry_t - t
    if tau <= 1e-12:
        return np.zeros_like(S_t)
    und = mkt.underlyings[leg.underlying]
    df = float(mkt.curve.df_fwd(t, leg.expiry_t))
    vol = float(option_vol(leg, mkt, tau))
    return leg.sign * leg.shares * bs_price(S_t, leg.strike, tau, df, und.div_yield, vol, leg.is_call)


def supervisory_delta(sign: float, is_call: bool, S, K: float, T: float, sigma: float):
    """SA-CCR supervisory delta (CRE52.40): bought call +N(d1), sold call -N(d1),
    bought put -N(-d1), sold put +N(-d1); d1 = (ln(P/K) + 0.5 sigma^2 T)/(sigma sqrt T)."""
    S = np.asarray(S, dtype=float)
    T = max(T, 1e-8)
    d1 = (np.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * np.sqrt(T))
    return sign * ndtr(d1) if is_call else -sign * ndtr(-d1)
