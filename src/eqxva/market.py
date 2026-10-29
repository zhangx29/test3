"""Market data: discount curves per currency, FX spots, dividend yield, implied-vol surface, correlations.

FX is deterministic: the rate at t is the forward implied by the two discount curves,
X(t) = X(0) * P_from(t) / P_to(t), so there is no FX volatility and no equity/FX correlation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np


@dataclass
class ZeroCurve:
    """Continuously-compounded zero curve, linear in zero rate, flat extrapolation."""
    times: np.ndarray
    rates: np.ndarray

    @classmethod
    def flat(cls, r: float) -> "ZeroCurve":
        return cls(np.array([1.0]), np.array([r]))

    def zero(self, t):
        return np.interp(t, self.times, self.rates)

    def df(self, t):
        t = np.asarray(t, dtype=float)
        return np.exp(-self.zero(t) * t)

    def df_fwd(self, t0, t1):
        """P(t0, t1) implied by today's curve (deterministic rates)."""
        return self.df(t1) / self.df(t0)


@dataclass
class VolSurface:
    """Black vol by (tenor in years, moneyness K/S0). Linear in total variance across tenor,
    linear across moneyness, flat extrapolation on both axes (sticky strike)."""
    tenors: np.ndarray
    moneyness: np.ndarray
    vols: np.ndarray  # shape (len(tenors), len(moneyness))

    @classmethod
    def flat(cls, v: float) -> "VolSurface":
        return cls(np.array([1.0]), np.array([1.0]), np.array([[v]]))

    def _smile_at(self, i: int, m):
        return np.interp(m, self.moneyness, self.vols[i])

    def vol(self, T, m):
        """T: time to expiry (yrs, scalar or array); m: moneyness K/S0 (scalar)."""
        T = np.maximum(np.asarray(T, dtype=float), 1e-8)
        ts = self.tenors
        if ts.size == 1:
            return np.broadcast_to(self._smile_at(0, m), T.shape).astype(float) + 0.0
        smile = np.array([self._smile_at(i, m) for i in range(ts.size)])
        w_nodes = smile ** 2 * ts
        Tc = np.clip(T, ts[0], ts[-1])
        w = np.interp(Tc, ts, w_nodes)
        v = np.sqrt(w / Tc)
        return v

    def atm(self, T):
        return self.vol(T, 1.0)


@dataclass
class Underlying:
    name: str
    spot: float
    div_yield: float = 0.0
    is_index: bool = False
    vol: VolSurface = field(default_factory=lambda: VolSurface.flat(0.25))
    bbg_ticker: str = ""
    ccy: str = ""                  # blank = reporting currency

    def forward(self, t, curve: ZeroCurve):
        return self.spot * np.exp(-self.div_yield * np.asarray(t, dtype=float)) / curve.df(t)


@dataclass
class MarketData:
    valuation_date: date
    curve: ZeroCurve
    underlyings: dict[str, Underlying]
    correlation: dict[tuple[str, str], float] = field(default_factory=dict)
    reporting_ccy: str = ""        # ``curve`` is this currency's curve
    curves: dict[str, ZeroCurve] = field(default_factory=dict)    # other currencies
    fx_spot: dict[str, float] = field(default_factory=dict)       # reporting-ccy units per 1 unit of ccy

    def curve_for(self, ccy: str) -> ZeroCurve:
        if not ccy or ccy == self.reporting_ccy:
            return self.curve
        if ccy not in self.curves:
            raise ValueError(f"No discount curve for {ccy}: add Curve rows with ccy = {ccy}")
        return self.curves[ccy]

    def und_curve(self, name: str) -> ZeroCurve:
        return self.curve_for(self.underlyings[name].ccy)

    def _spot(self, ccy: str) -> float:
        if not ccy or ccy == self.reporting_ccy:
            return 1.0
        if ccy not in self.fx_spot:
            raise ValueError(f"No FX rate between {ccy} and {self.reporting_ccy}: add it to the FX sheet")
        return self.fx_spot[ccy]

    def fx(self, frm: str, to: str, t=0.0):
        """Units of ``to`` per 1 unit of ``frm`` at time t (deterministic FX forward)."""
        t = np.asarray(t, dtype=float)
        if (frm or self.reporting_ccy) == (to or self.reporting_ccy):
            return np.ones_like(t)
        return (self._spot(frm) / self._spot(to)
                * self.curve_for(frm).df(t) / self.curve_for(to).df(t))

    def rho(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return self.correlation.get((a, b), self.correlation.get((b, a), 0.0))

    def corr_matrix(self, names: list[str]) -> np.ndarray:
        n = len(names)
        c = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                c[i, j] = c[j, i] = self.rho(names[i], names[j])
        return c
