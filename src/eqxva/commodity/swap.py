"""Bullet commodity swap leg, one leg per referenced futures contract (bank view).

    before fixing     V(t) = omega * Q * (F(t) - K) * P(t, pay)
    fixed, unpaid     V(t) = omega * Q * (F(fix) - K) * P(t, pay)
    from payment      V(t) = 0   (cash flows on a grid date are treated as settled)

omega = +1 when the bank buys (pays fixed, receives floating). A spread such as a soybean crush is
several legs sharing a trade_id. ``expiry`` is the swap's payment date, not the futures expiry: it
drives the netting-set maturity, event buckets and SA-CCR liveness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from .. import dates as dt
from .market import FuturesContract


@dataclass
class CommoditySwapLeg:
    trade_id: str
    leg_id: str
    underlying: str                # futures contract
    omega: float
    quantity: float                # in the product's quantity unit
    fixed_price: float             # currency per quantity unit
    fixing_t: float
    payment_t: float
    fixing_date: date | None = None
    payment_date: date | None = None
    sa_notional_basis: str = "MARKET"
    kind: str = "COMSWAP"

    @property
    def expiry(self) -> float:
        return self.payment_t

    @property
    def shares(self) -> float:
        return self.quantity


def swap_value(leg: CommoditySwapLeg, t: float, F_t, mkt, F_fix=None):
    """Bank-view value at t. ``F_fix`` is the price at the fixing date, needed once t >= fixing."""
    F_t = np.asarray(F_t, dtype=float)
    if t >= leg.payment_t - 1e-12:
        return np.zeros_like(F_t)
    F = F_t
    if t >= leg.fixing_t - 1e-12:
        if F_fix is None:
            raise ValueError("F_fix required once a swap leg has fixed")
        F = np.asarray(F_fix, dtype=float)
    df = float(mkt.und_curve(leg.underlying).df_fwd(t, leg.payment_t))
    return leg.omega * leg.quantity * (F - leg.fixed_price) * df


def _blank(x) -> bool:
    return x is None or (isinstance(x, float) and np.isnan(x)) or str(x).strip() == ""


def build_commodity_swap(r: dict, mkt, warnings: list[str]) -> CommoditySwapLeg:
    tag = f"{r['trade_id']} {r['leg_id']}"
    c = mkt.underlyings[r["underlying"]]
    if not isinstance(c, FuturesContract):
        raise ValueError(f"{tag}: '{r['underlying']}' is not a futures contract on the Contracts sheet")
    direction = str(r.get("direction") or "").strip().upper()
    if direction not in ("BUY", "SELL"):
        raise ValueError(f"{tag}: COMSWAP direction must be BUY or SELL (got '{r.get('direction')}')")
    lot = mkt.commodity.products[c.product].lot_size
    if not _blank(r.get("quantity")):
        qty = float(r["quantity"])
        if not _blank(r.get("lots")) and abs(float(r["lots"]) * lot - qty) > 1e-6:
            warnings.append(f"{tag}: quantity {qty:,.0f} differs from lots x lot size "
                            f"{float(r['lots']) * lot:,.0f}; the quantity is used.")
        elif lot > 0 and abs(qty / lot - round(qty / lot)) > 1e-9:
            warnings.append(f"{tag}: quantity {qty:,.0f} is {qty / lot:,.2f} lots, not a whole number "
                            f"(a hedge in lots leaves a residual).")
    elif not _blank(r.get("lots")):
        qty = float(r["lots"]) * lot
    else:
        raise ValueError(f"{tag}: enter quantity or lots")
    if qty <= 0:
        raise ValueError(f"{tag}: quantity must be positive; use direction for the sign")

    val = mkt.valuation_date
    fix = c.expiry_date if _blank(r.get("fixing_date")) else dt.to_date(r["fixing_date"])
    pay = fix if _blank(r.get("payment_date")) else dt.to_date(r["payment_date"])
    if fix <= val:
        raise ValueError(f"{tag}: fixing date {fix} is not after the valuation date")
    if fix > c.expiry_date:
        raise ValueError(f"{tag}: fixing date {fix} is after the expiry of {c.name} ({c.expiry_date})")
    if pay < fix:
        raise ValueError(f"{tag}: payment date {pay} is before the fixing date {fix}")
    K = c.spot if _blank(r.get("fixed_price")) else float(r["fixed_price"]) * c.price_scale
    return CommoditySwapLeg(r["trade_id"], r["leg_id"], c.name, 1.0 if direction == "BUY" else -1.0, qty, K,
                            dt.yf(val, fix), dt.yf(val, pay), fix, pay)
