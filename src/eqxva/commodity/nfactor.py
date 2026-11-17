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


class LazyContractPaths:
    """Price paths of one futures contract, computed on demand from the shared factor states.

    Behaves like the (n_paths, len(grid)) array it stands for under ``[rows, cols]`` indexing,
    ``.shape`` and ``np.asarray``. Only the factor states are stored, so a long monthly strip (hundreds
    of contracts) costs the memory of the factors, not of every contract. After its expiry the
    contract holds its value at the last grid point on or before the expiry."""

    def __init__(self, contract: FuturesContract, model: CommodityModel, grid: np.ndarray,
                 Y: np.ndarray, col: dict[int, int]):
        self.c, self.model, self.grid, self.Y = contract, model, grid, Y
        self.i = model.index[contract.product]
        self.cols = [col[int(f)] for f in self.i]
        self.n_paths = Y.shape[1]
        last = int(np.searchsorted(grid, contract.expiry_t + 1e-9, side="right")) - 1
        self.last = max(last, 0)
        # loadings w(t) and 1/2 Var[log F(t)] on every grid point, computed once (vectorised covariance)
        ratio, beta = model.ratio[self.i], model.beta[self.i]
        g = np.asarray(grid, dtype=float)
        self.W = ratio[None, :] * contract.q_ref * np.exp(-beta[None, :] * (contract.expiry_t - g[:, None]))
        la = ratio * contract.q_ref * np.exp(-beta * contract.expiry_t)
        s = beta[:, None] + beta[None, :]
        tt = np.clip(np.minimum(g, contract.expiry_t), 0.0, None)
        A = np.outer(la, la) * model.corr[np.ix_(self.i, self.i)]
        self.halfvar = 0.5 * np.einsum("ij,ijk->k", A, _g(s[:, :, None], tt[None, None, :]))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.n_paths, len(self.grid))

    def _column(self, k: int, rows=slice(None)) -> np.ndarray:
        k = min(k, self.last)
        c = self.c
        if k == 0:
            return np.full(self.Y[0][rows].shape[0], c.spot)
        full = isinstance(rows, slice) and rows == slice(None)
        Yk = self.Y[k] if full else self.Y[k][rows]
        return c.spot * np.exp(Yk[:, self.cols] @ self.W[k] - self.halfvar[k])

    def __getitem__(self, key):
        rows, cols = key if isinstance(key, tuple) else (key, slice(None))
        if isinstance(cols, (int, np.integer)):
            k = int(cols)
            return self._column(k + len(self.grid) if k < 0 else k, rows)
        ks = np.arange(len(self.grid))[cols]
        return np.column_stack([self._column(int(k), rows) for k in ks])

    def __array__(self, dtype=None, copy=None):
        a = self[:, :]
        return a if dtype is None else a.astype(dtype)


def simulate_factors(mkt, names: list[str], grid: np.ndarray, n_paths: int, seed: int = 42,
                     antithetic: bool = True) -> dict[str, LazyContractPaths]:
    """Simulate the factor states on the grid; return {contract: LazyContractPaths}. grid[0] must be 0."""
    assert abs(grid[0]) < 1e-12, "grid must start at 0"
    model: CommodityModel = mkt.commodity
    rng = np.random.default_rng([seed, 1])       # its own stream: equity paths never change
    cs = {n: mkt.underlyings[n] for n in names}
    used = sorted({int(i) for c in cs.values() for i in model.index[c.product]})
    idx = np.array(used)
    col = {f: k for k, f in enumerate(used)}
    half = (n_paths + 1) // 2 if antithetic else n_paths
    Yh = np.zeros((len(grid), n_paths, len(idx)))   # factor state at each grid point
    Y = Yh[0]
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
        Yh[k] = Y
    return {n: LazyContractPaths(c, model, grid, Yh, col) for n, c in cs.items()}


def simulate_futures(mkt, names: list[str], grid: np.ndarray, n_paths: int, seed: int = 42,
                     antithetic: bool = True) -> dict[str, np.ndarray]:
    """Return {contract: array (n_paths, len(grid))}; grid[0] must be 0."""
    lazy = simulate_factors(mkt, names, grid, n_paths, seed, antithetic)
    return {n: np.asarray(p) for n, p in lazy.items()}
