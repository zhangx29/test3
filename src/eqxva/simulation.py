"""Risk-neutral GBM simulation of correlated equity underlyings on a time grid.

Exact log-normal stepping with piecewise-constant forward rate, dividend yield and forward
ATM variance (from the ATM term structure), so the marginals match the analytic engine.
"""
from __future__ import annotations

import numpy as np

from .market import MarketData


def forward_variance(und, grid: np.ndarray) -> np.ndarray:
    """Variance increments sigma^2 dt on each grid step from ATM total variance."""
    tt = np.maximum(grid, 1e-8)
    w = und.vol.atm(tt) ** 2 * tt
    w[grid <= 0] = 0.0
    dw = np.diff(w)
    return np.maximum(dw, 0.0)


def simulate(mkt: MarketData, names: list[str], grid: np.ndarray, n_paths: int, seed: int = 42,
             antithetic: bool = True) -> dict[str, np.ndarray]:
    """Return {underlying: array (n_paths, len(grid))}; grid[0] must be 0."""
    assert abs(grid[0]) < 1e-12, "grid must start at 0"
    rng = np.random.default_rng(seed)
    n_steps = len(grid) - 1
    m = len(names)
    half = (n_paths + 1) // 2 if antithetic else n_paths
    corr = mkt.corr_matrix(names)
    L = np.linalg.cholesky(corr + 1e-12 * np.eye(m))

    out = {}
    Z = rng.standard_normal((n_steps, half, m)) @ L.T  # correlated normals
    if antithetic:
        Z = np.concatenate([Z, -Z], axis=1)[:, :n_paths, :]
    dt = np.diff(grid)
    for i, name in enumerate(names):
        und = mkt.underlyings[name]
        dfs = mkt.curve_for(und.ccy).df(grid)
        log_df_step = np.log(dfs[1:] / dfs[:-1])      # = -r_fwd * dt in the underlying's currency
        dv = forward_variance(und, grid)
        drift = -log_df_step - und.div_yield * dt - 0.5 * dv     # (n_steps,)
        inc = drift[:, None] + np.sqrt(dv)[:, None] * Z[:, :, i]  # (n_steps, n_paths)
        logS = np.concatenate([np.zeros((1, n_paths)), np.cumsum(inc, axis=0)], axis=0)
        out[name] = (und.spot * np.exp(logS)).T
    return out


def forward_path(mkt: MarketData, names: list[str], grid: np.ndarray) -> dict[str, np.ndarray]:
    """Deterministic forward path (1 path) used for the 'expected path' SA-CCR projection."""
    return {n: mkt.underlyings[n].forward(grid, mkt.und_curve(n))[None, :] for n in names}
