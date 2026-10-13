"""CSA variation margin with margin-period-of-risk lag (bank view)."""
from __future__ import annotations

import numpy as np

from .model import CSA


def vm_balance(V, csa: CSA):
    """Variation margin held by the bank (+) / posted by the bank (-) for portfolio value V.
    Stateless approximation: thresholds applied to V, transfers below MTA are not called."""
    V = np.asarray(V, dtype=float)
    C = np.where(V > csa.threshold_cp, V - csa.threshold_cp,
                 np.where(V < -csa.threshold_bank, V + csa.threshold_bank, 0.0))
    if csa.mta > 0:
        C = np.where(np.abs(C) < csa.mta, 0.0, C)
    if csa.rounding > 0:
        C = np.round(C / csa.rounding) * csa.rounding
    return C


def lag_indices(grid: np.ndarray, mpor: float) -> np.ndarray:
    """Index of the grid point t - MPOR (floored at 0) for every grid point (the grid contains them)."""
    target = np.maximum(grid - mpor, 0.0)
    idx = np.searchsorted(grid, target - 1e-9)
    return np.clip(idx, 0, len(grid) - 1)


def collateralised(V: np.ndarray, grid: np.ndarray, csa: CSA, maturity: float = np.inf):
    """Return (exposure E = V - VM(t-MPOR) - IA_net, VM) for V of shape (paths, times).
    Exposure and collateral are zero once every trade in the netting set has matured."""
    ia_net = csa.ia_received - (0.0 if csa.ia_posted_segregated else csa.ia_posted)
    live = (grid < maturity - 1e-9)[None, :]
    if not csa.collateralized:
        return np.where(live, V - ia_net, 0.0), np.zeros_like(V)
    vm = np.where(live, vm_balance(V[:, lag_indices(grid, csa.mpor_years)], csa), 0.0)
    return np.where(live, V - vm - ia_net, 0.0), vm
