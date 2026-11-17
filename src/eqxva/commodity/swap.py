"""Bullet commodity swap leg, one leg per referenced futures contract (bank view).

    before fixing     V(t) = omega * Q * (F(t) - K) * P(t, pay)
    fixed, unpaid     V(t) = omega * Q * (F(fix) - K) * P(t, pay)
    from payment      V(t) = 0   (cash flows on a grid date are treated as settled)

omega = +1 when the bank buys (pays fixed, receives floating). A spread such as a soybean crush is
several legs sharing a trade_id. ``expiry`` is the swap's payment date, not the futures expiry: it
drives the netting-set maturity, event buckets and SA-CCR liveness.

Exposure window (gas prepay, cross-extinguishing swaps): a leg can be restricted to an active
interval [active_from, active_to). Outside it the leg counts zero in exposure and SA-CCR, though its
booked value (LegMTM) is unchanged. Set per leg (``active_from`` / ``active_to``, e.g. a booking of
rolling two-month trades where trade k is live in month k only) or per trade with ``window_months``
on a strip, from the strip's own fixing dates f_i:
    FWD  (months {k, k+1} at a date in month k): leg i active on [f_{i-W+1}, f_{i+1})
    BWD  (months {k-1, k}):                      leg i active on [f_i, f_{i+W})
both cut at the leg's payment; the first legs start at the valuation date. A netting-set sum of such
legs is the window exposure, so overlapping windows are never double counted.

SA-CCR fields: ``sa_maturity_t`` overrides M (= sa_maturity_t - t, CAR ch.7 para 124(a)); with
``sa_keep_fixed`` a fixed but unpaid leg keeps its notional (at the fixing price) while active, so a
window carries its full notional; ``sa_notional_basis`` AMORT weights each leg's notional by
(fixing - t) / (M_trade - t), the time-average of the notional outstanding over the trade's life.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from .. import dates as dt
from .market import FuturesContract

INF = float("inf")
WINDOW_CONVENTIONS = ("FWD", "BWD")


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
    sa_notional_basis: str = "MARKET"     # MARKET or AMORT
    kind: str = "COMSWAP"
    active_from: float = -INF
    active_to: float = INF
    sa_maturity_t: float | None = None    # M override (years from valuation); None = time to payment
    sa_keep_fixed: bool = False
    sa_amort_T: float | None = None       # trade SA maturity used by AMORT
    # raw trade-level inputs, resolved by finalize_trade
    window_months: int = 0
    window_convention: str = "FWD"
    sa_maturity_raw: str = ""
    sa_fixed_raw: str = ""

    @property
    def expiry(self) -> float:
        return self.payment_t

    @property
    def shares(self) -> float:
        return self.quantity

    @property
    def windowed(self) -> bool:
        return self.active_from > -INF or self.active_to < INF

    def active(self, t: float) -> bool:
        return self.active_from - 1e-12 <= t < self.active_to - 1e-12

    def live(self, t: float) -> bool:
        """Active and not yet paid: the leg can contribute to exposure at t."""
        return self.active(t) and t < self.payment_t - 1e-12

    def sa_M(self, t: float) -> float:
        return (self.sa_maturity_t if self.sa_maturity_t is not None else self.payment_t) - t

    def sa_interval(self) -> tuple[float, float]:
        """[a, b) where the leg carries SA-CCR notional (from t = 0)."""
        end = self.payment_t if self.sa_keep_fixed else self.fixing_t
        return max(self.active_from, 0.0), min(self.active_to, end)


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
    """None, NaN, NaT (a blank date cell read from Excel) or an empty string."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return True
    if not isinstance(x, str) and x != x:      # NaT
        return True
    return str(x).strip() == ""


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
    sab = "MARKET" if _blank(r.get("sa_notional_basis")) else str(r["sa_notional_basis"]).strip().upper()
    if sab not in ("MARKET", "AMORT"):
        raise ValueError(f"{tag}: COMSWAP sa_notional_basis must be MARKET or AMORT (got '{sab}')")
    leg = CommoditySwapLeg(r["trade_id"], r["leg_id"], c.name, 1.0 if direction == "BUY" else -1.0, qty, K,
                           dt.yf(val, fix), dt.yf(val, pay), fix, pay, sa_notional_basis=sab)
    if not _blank(r.get("active_from")):
        leg.active_from = dt.yf(val, dt.to_date(r["active_from"]))
    if not _blank(r.get("active_to")):
        leg.active_to = dt.yf(val, dt.to_date(r["active_to"]))
    if leg.active_to <= leg.active_from:
        raise ValueError(f"{tag}: active_to must be after active_from")
    if not _blank(r.get("window_months")):
        wm = float(r["window_months"])
        if wm < 1 or wm != int(wm):
            raise ValueError(f"{tag}: window_months must be a whole number of months >= 1")
        leg.window_months = int(wm)
    conv = "FWD" if _blank(r.get("window_convention")) else str(r["window_convention"]).strip().upper()
    if conv not in WINDOW_CONVENTIONS:
        raise ValueError(f"{tag}: window_convention must be FWD or BWD (got '{conv}')")
    leg.window_convention = conv
    leg.sa_maturity_raw = "" if _blank(r.get("sa_maturity")) else str(r["sa_maturity"]).strip()
    if hasattr(r.get("sa_maturity"), "year"):
        leg.sa_maturity_raw = dt.to_date(r["sa_maturity"]).isoformat()
    leg.sa_fixed_raw = "" if _blank(r.get("sa_fixed_notional")) else str(r["sa_fixed_notional"]).strip().upper()
    return leg


def _same(legs, attr: str, tid: str):
    vals = {getattr(l, attr) for l in legs}
    if len(vals) > 1:
        raise ValueError(f"{tid}: all COMSWAP legs of a trade need the same {attr}")
    return vals.pop()


def finalize_trade(legs: list[CommoditySwapLeg], val, warnings: list[str]) -> None:
    """Resolve the trade-level window and SA-CCR settings of a trade's COMSWAP legs."""
    if not legs:
        return
    tid = legs[0].trade_id
    W = _same(legs, "window_months", tid)
    conv = _same(legs, "window_convention", tid)
    raw_m = _same(legs, "sa_maturity_raw", tid)
    raw_fix = _same(legs, "sa_fixed_raw", tid)
    explicit = {id(l) for l in legs if l.windowed}
    if W:
        f = sorted({round(l.fixing_t, 9) for l in legs})
        pos = {x: k for k, x in enumerate(f)}
        cut = 0
        for l in legs:
            if id(l) in explicit:
                continue                         # explicit active_from / active_to win
            i = pos[round(l.fixing_t, 9)]
            if conv == "FWD":
                j0, j1 = i - W + 1, i + 1
            else:
                j0, j1 = i, i + W
            l.active_from = -INF if j0 <= 0 else f[j0]
            end = f[j1] if j1 < len(f) else INF
            cut += end > l.payment_t + 1e-9 and j1 < len(f)
            l.active_to = min(end, l.payment_t)
        if conv == "BWD" and cut:
            warnings.append(
                f"{tid}: window_convention BWD - {cut} legs are paid before the end of their window, and a leg "
                f"counts zero from its payment date (a missed settlement that stays owed is not modelled), so "
                f"the window is shorter than {W} months. Use FWD, or a later payment_date.")
    windowed = any(l.windowed for l in legs)
    trade_end = max(l.payment_t for l in legs)
    if raw_m:
        if raw_m.upper() == "TRADE":
            sa_T = trade_end
        else:
            sa_T = dt.yf(val, dt.date_or_tenor(raw_m, val))
            if sa_T < trade_end - 1e-9:
                warnings.append(f"{tid}: sa_maturity {raw_m} is before the last payment of the trade; "
                                f"M is floored at each leg's own payment.")
    else:
        sa_T = None
    if raw_fix not in ("", "Y", "YES", "TRUE", "1", "N", "NO", "FALSE", "0"):
        raise ValueError(f"{tid}: sa_fixed_notional must be Y or N")
    keep = (raw_fix in ("Y", "YES", "TRUE", "1")) if raw_fix else windowed
    amort = any(l.sa_notional_basis == "AMORT" for l in legs)
    if amort and windowed:
        warnings.append(f"{tid}: sa_notional_basis AMORT ignored on a windowed trade - the window "
                        f"notional is already level over the life; MARKET used.")
    for l in legs:
        l.sa_maturity_t = None if sa_T is None else max(sa_T, l.payment_t)
        l.sa_keep_fixed = keep
        if amort and windowed:
            l.sa_notional_basis = "MARKET"
        l.sa_amort_T = sa_T if sa_T is not None else trade_end


def overlap_warning(ns_name: str, legs: list[CommoditySwapLeg]) -> str | None:
    """Warn when legs of different trades reference the same contract in the same direction over
    overlapping live intervals: a netting-set sum then counts those months more than once."""
    groups: dict[tuple, list] = {}
    for l in legs:
        a, b = max(l.active_from, 0.0), min(l.active_to, l.payment_t)
        if b > a:
            groups.setdefault((l.underlying, l.omega), []).append((a, b, l.trade_id))
    n = 0
    for iv in groups.values():
        iv.sort()
        for (a1, b1, t1), (a2, b2, t2) in zip(iv, iv[1:]):
            if t1 != t2 and a2 < b1 - 1e-9:
                n += 1
    if n:
        return (f"Netting set '{ns_name}': {n} overlapping commodity legs from different trades on the same "
                f"contract and direction - the netting-set sum double counts those months (e.g. rolling "
                f"two-month trades booked without active_from/active_to). Use active dates or window_months "
                f"for window exposure.")
    return None
