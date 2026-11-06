"""N-factor commodity futures model, seasonal (maturity-dependent volatility) form.

    dF^T(t) / F^T(t) = sum_i q_i(T) exp(-beta_i (T - t)) dW_i(t),   <dW_i, dW_j> = rho_ij dt
    Y_i(t) = int_0^t exp(-beta_i (t - s)) dW_i(s)
    F^T(t) = F^T(0) exp( sum_i q_i(T) exp(-beta_i (T - t)) Y_i(t) - 1/2 Var[log F^T(t)] )

N = 2 for agriculture: a short factor (beta > 0) and a long factor (beta = 0). q_i(T) = ratio_i *
q_ref(T), so one number per contract is fitted to that contract's option. Y is stepped exactly
(Gaussian), so the grid adds no discretisation error. Contracts stop moving at their expiry.
"""
from __future__ import annotations

import numpy as np

from .market import CommodityModel, FuturesContract


def _g(s, t: float):
    """int_0^t exp(s u) du, elementwise in s."""
    s = np.asarray(s, dtype=float)
    small = np.abs(s) < 1e-10
    safe = np.where(small, 1.0, s)
    return np.where(small, t, np.expm1(safe * t) / safe)


def _load(model: CommodityModel, c: FuturesContract):
    idx = model.index[c.product]
    return idx, model.ratio[idx] * c.q_ref * np.exp(-model.beta[idx] * c.expiry_t)


def covariance(model: CommodityModel, a: FuturesContract, ta: float, b: FuturesContract, tb: float) -> float:
    """Cov[log F_a(ta), log F_b(tb)]; a contract's price is frozen after its expiry."""
    t = max(min(ta, a.expiry_t, tb, b.expiry_t), 0.0)
    ia, la = _load(model, a)
    ib, lb = _load(model, b)
    s = model.beta[ia][:, None] + model.beta[ib][None, :]
    return float(np.sum(np.outer(la, lb) * model.corr[np.ix_(ia, ib)] * _g(s, t)))


def calibrate(model: CommodityModel, c: FuturesContract) -> None:
    """Set q_ref so the model variance to the option expiry equals the implied variance."""
    c.q_ref = 1.0
    unit = covariance(model, c, c.option_expiry_t, c, c.option_expiry_t)
    if unit <= 0:
        raise ValueError(f"Contract {c.name}: model variance is not positive; check the factor inputs")
    c.q_ref = c.implied_vol * np.sqrt(c.option_expiry_t / unit)


def _step_root(model: CommodityModel, idx: np.ndarray, h: float) -> np.ndarray:
    """Matrix root of the covariance of the Y increment over a step of length h."""
    b = model.beta[idx]
    s = b[:, None] + b[None, :]
    cov = model.corr[np.ix_(idx, idx)] * _g(-s, h)
    w, v = np.linalg.eigh(cov)
    return v * np.sqrt(np.maximum(w, 0.0))


def simulate_futures(mkt, names: list[str], grid: np.ndarray, n_paths: int, seed: int = 42,
                     antithetic: bool = True) -> dict[str, np.ndarray]:
    """Return {contract: array (n_paths, len(grid))}; grid[0] must be 0."""
    assert abs(grid[0]) < 1e-12, "grid must start at 0"
    model: CommodityModel = mkt.commodity
    rng = np.random.default_rng([seed, 1])       # its own stream: equity paths never change
    cs = {n: mkt.underlyings[n] for n in names}
    used = sorted({int(i) for c in cs.values() for i in model.index[c.product]})
    idx = np.array(used)
    col = {f: k for k, f in enumerate(used)}
    half = (n_paths + 1) // 2 if antithetic else n_paths
    Y = np.zeros((n_paths, len(idx)))
    out = {n: np.empty((n_paths, len(grid))) for n in names}
    for n, c in cs.items():
        out[n][:, 0] = c.spot
    roots: dict[int, np.ndarray] = {}
    beta = model.beta[idx]
    for k in range(1, len(grid)):
        h = float(grid[k] - grid[k - 1])
        key = round(h * 1e9)
        if key not in roots:
            roots[key] = _step_root(model, idx, h)
        Z = rng.standard_normal((half, len(idx))) @ roots[key].T
        if antithetic:
            Z = np.concatenate([Z, -Z], axis=0)[:n_paths]
        Y = Y * np.exp(-beta * h) + Z
        t = float(grid[k])
        for n, c in cs.items():
            if t > c.expiry_t + 1e-9:
                out[n][:, k] = out[n][:, k - 1]
                continue
            i = model.index[c.product]
            w = model.ratio[i] * c.q_ref * np.exp(-model.beta[i] * (c.expiry_t - t))
            cols = [col[int(f)] for f in i]
            out[n][:, k] = c.spot * np.exp(Y[:, cols] @ w - 0.5 * covariance(model, c, t, c, t))
    return out
