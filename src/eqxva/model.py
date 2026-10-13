"""Trade / netting-set / run-configuration objects.

All legs are expressed in model time (years ACT/365F from the valuation date) once built.
Sign convention: bank view, positive value = counterparty owes the bank.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import dates as dt
from .market import MarketData

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- legs
@dataclass
class FundingPeriod:
    end: float        # payment time (yrs)
    tau: float        # ACT/360 accrual
    fwd: float        # simple forward rate fixed for the period (deterministic rates)


@dataclass
class EquityPeriod:
    start: float
    end: float
    ref_price: float | None        # fixed reference price; None -> fixes to S(start) on the path
    funding: list[FundingPeriod]


@dataclass
class TRSLeg:
    trade_id: str
    leg_id: str
    underlying: str
    omega: float                   # +1 bank receives equity return, -1 bank pays it
    shares: float
    maturity: float
    periods: list[EquityPeriod]
    spread: float
    div_passthrough: float = 1.0
    initial_mtm: float = 0.0       # PV at valuation date, bank view
    im_settle: float = 0.0         # time the initial MTM is settled (first reset or maturity)
    has_reset: bool = False
    maturity_date: date | None = None
    reset_freq: str | None = None
    kind: str = "TRS"

    @property
    def expiry(self) -> float:
        return self.maturity

    def next_reset_after(self, t: float) -> float:
        """End of the equity period in force at t (the next settlement / reset date)."""
        for p in self.periods:
            if p.end > t + 1e-12:
                return p.end
        return self.maturity

    def fixing_time(self, t: float) -> float | None:
        """Start time of the period in force at t if its notional fixes on the path (start > 0)."""
        for p in self.periods:
            if p.start <= t + 1e-12 and t < p.end - 1e-12:
                return p.start if p.ref_price is None else None
        return None


@dataclass
class OptionLeg:
    trade_id: str
    leg_id: str
    underlying: str
    sign: float                    # +1 bank bought, -1 bank sold
    is_call: bool
    shares: float
    strike: float
    expiry_t: float
    expiry_date: date | None = None
    vol_override: float | None = None
    ovme_price: float | None = None   # per-share price pasted from Bloomberg OVME (reconciliation only)
    kind: str = "OPTION"

    @property
    def expiry(self) -> float:
        return self.expiry_t


Leg = TRSLeg | OptionLeg


@dataclass
class Trade:
    trade_id: str
    netting_set: str
    legs: list = field(default_factory=list)

    @property
    def maturity(self) -> float:
        return max(l.expiry for l in self.legs)


@dataclass
class CSA:
    collateralized: bool = False
    threshold_cp: float = 0.0      # CP may owe up to this before posting VM
    threshold_bank: float = 0.0    # bank may owe up to this before posting VM
    mta: float = 0.0
    ia_received: float = 0.0       # independent amount / IM received by the bank
    ia_posted: float = 0.0         # independent amount / IM posted by the bank
    ia_posted_segregated: bool = True
    mpor_days: int = 10            # business days
    rounding: float = 0.0

    @property
    def mpor_years(self) -> float:
        return self.mpor_days / 252.0

    @property
    def nica(self) -> float:
        """Net independent collateral amount (SA-CCR): received minus unsegregated posted."""
        return self.ia_received - (0.0 if self.ia_posted_segregated else self.ia_posted)


@dataclass
class NettingSet:
    netting_set: str
    counterparty: str = ""
    csa: CSA = field(default_factory=CSA)
    trades: list[Trade] = field(default_factory=list)

    @property
    def legs(self):
        return [l for t in self.trades for l in t.legs]

    @property
    def maturity(self) -> float:
        return max(t.maturity for t in self.trades)


@dataclass
class RunConfig:
    valuation_date: date
    buckets: list[date]
    n_paths: int = 10000
    seed: int = 42
    pfe_quantile: float = 0.95
    fine_step: str = "1W"
    output_mode: str = "AVERAGE"   # AVERAGE (time-average over each bucket) or POINT
    antithetic: bool = True
    epe_horizon: str = "1Y"
    analytic_nodes: int = 201

    def bucket_times(self) -> np.ndarray:
        return np.array([dt.yf(self.valuation_date, d) for d in self.buckets])


# ----------------------------------------------------------------------------- builders
def _is_blank(x) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and np.isnan(x):
        return True
    return isinstance(x, str) and x.strip() == ""


def _f(x, default=0.0) -> float:
    return default if _is_blank(x) else float(x)


def build_trs(row: dict, mkt: MarketData, warnings: list[str]) -> TRSLeg:
    val = mkt.valuation_date
    und = mkt.underlyings[row["underlying"]]
    start = val if _is_blank(row.get("start_date")) else dt.date_or_tenor(row["start_date"], val)
    mat = dt.date_or_tenor(row["maturity"], val)
    if mat <= val:
        raise ValueError(f"{row['trade_id']}: TRS maturity {mat} is not after valuation date {val}")
    reset = None if _is_blank(row.get("equity_reset_freq")) else str(row["equity_reset_freq"]).strip().upper()
    if reset in ("NONE", "0", "NAN"):
        reset = None
    ffreq = "3M" if _is_blank(row.get("funding_reset_freq")) else str(row["funding_reset_freq"]).strip().upper()
    basis = str(row.get("notional_basis") or "CURRENT").strip().upper()
    if basis not in ("CURRENT", "AVERAGE"):
        raise ValueError(f"{row['trade_id']}: notional_basis must be CURRENT or AVERAGE")
    if basis == "AVERAGE" and _is_blank(row.get("avg_price")):
        raise ValueError(f"{row['trade_id']}: avg_price is required when notional_basis = AVERAGE")
    shares = float(row["shares"])
    direction = str(row["direction"]).strip().upper()
    omega = {"RECEIVE_EQUITY": 1.0, "LONG": 1.0, "PAY_EQUITY": -1.0, "SHORT": -1.0}[direction]
    spread = _f(row.get("funding_spread"))
    dp = _f(row.get("div_passthrough"), 1.0)
    im = _f(row.get("initial_mtm"))

    ref0 = und.spot if basis == "CURRENT" else float(row["avg_price"])
    if basis == "AVERAGE" and im != 0.0:
        warnings.append(
            f"{row['trade_id']}: initial_mtm={im:,.2f} ignored - with notional_basis=AVERAGE the "
            f"carried MTM is already in the strike (shares*(S0-avg_price)="
            f"{omega * shares * (und.spot - ref0):,.2f}).")
        im = 0.0

    curve = mkt.curve
    eq_dates = dt.schedule(start, mat, reset)
    periods: list[EquityPeriod] = []
    for k in range(len(eq_dates) - 1):
        a_d, b_d = eq_dates[k], eq_dates[k + 1]
        if b_d <= val:
            continue
        a, b = dt.yf(val, a_d), dt.yf(val, b_d)
        in_force = a_d <= val
        if in_force or not periods:
            # first live period: notional from the notional basis (current spot or average price);
            # a forward-starting CURRENT trade fixes on the path at its start date
            ref = ref0 if (in_force or basis == "AVERAGE") else None
        else:
            ref = None
        fund = []
        f_dates = dt.schedule(a_d, b_d, ffreq)
        for j in range(len(f_dates) - 1):
            s_d, e_d = f_dates[j], f_dates[j + 1]
            if e_d <= val:
                continue
            tau = dt.act360(s_d, e_d)
            s, e = dt.yf(val, s_d), dt.yf(val, e_d)
            fwd = (float(curve.df(s)) / float(curve.df(e)) - 1.0) / tau
            fund.append(FundingPeriod(e, tau, fwd))
        periods.append(EquityPeriod(a, b, ref, fund))

    return TRSLeg(
        trade_id=str(row["trade_id"]), leg_id=str(row.get("leg_id", "")), underlying=und.name,
        omega=omega, shares=shares, maturity=dt.yf(val, mat), periods=periods, spread=spread,
        div_passthrough=dp, initial_mtm=im, im_settle=periods[0].end, has_reset=reset is not None,
        maturity_date=mat, reset_freq=reset)


def build_option(row: dict, mkt: MarketData) -> OptionLeg:
    val = mkt.valuation_date
    und = mkt.underlyings[row["underlying"]]
    exp_field = row.get("option_maturity")
    if _is_blank(exp_field):
        exp_field = row.get("maturity")
    exp = dt.date_or_tenor(exp_field, val)
    if exp <= val:
        raise ValueError(f"{row['trade_id']}: option maturity {exp} is not after valuation date")
    k = row["strike"]
    if isinstance(k, str) and k.strip().endswith("%"):
        strike = und.spot * float(k.strip()[:-1]) / 100.0
    else:
        strike = float(k)
    direction = str(row["direction"]).strip().upper()
    sign = {"BUY": 1.0, "LONG": 1.0, "SELL": -1.0, "SHORT": -1.0}[direction]
    otype = str(row["option_type"]).strip().upper()
    if otype not in ("CALL", "PUT", "C", "P"):
        raise ValueError(f"{row['trade_id']}: option_type must be CALL or PUT")
    vo = None if _is_blank(row.get("vol_override")) else float(row["vol_override"])
    ovme = None if _is_blank(row.get("ovme_price")) else float(row["ovme_price"])
    return OptionLeg(
        trade_id=str(row["trade_id"]), leg_id=str(row.get("leg_id", "")), underlying=und.name,
        sign=sign, is_call=otype in ("CALL", "C"), shares=float(row["shares"]), strike=strike,
        expiry_t=dt.yf(val, exp), expiry_date=exp, vol_override=vo, ovme_price=ovme)


def build_netting_sets(trade_rows: list[dict], ns_rows: list[dict], mkt: MarketData,
                       warnings: list[str]) -> list[NettingSet]:
    sets: dict[str, NettingSet] = {}
    for r in ns_rows:
        ns_id = str(r["netting_set"]).strip()
        coll = str(r.get("collateralized") or "N").strip().upper() in ("Y", "YES", "TRUE", "1")
        csa = CSA(collateralized=coll,
                  threshold_cp=_f(r.get("threshold_cp")), threshold_bank=_f(r.get("threshold_bank")),
                  mta=_f(r.get("mta")), ia_received=_f(r.get("ia_received")),
                  ia_posted=_f(r.get("ia_posted")),
                  ia_posted_segregated=str(r.get("ia_posted_segregated") or "Y").strip().upper()
                  in ("Y", "YES", "TRUE", "1"),
                  mpor_days=int(_f(r.get("mpor_days"), 10)), rounding=_f(r.get("rounding")))
        sets[ns_id] = NettingSet(ns_id, str(r.get("counterparty") or ""), csa)

    trades: dict[str, Trade] = {}
    for i, r in enumerate(trade_rows):
        if _is_blank(r.get("trade_id")):
            continue
        r = dict(r)
        r["trade_id"] = str(r["trade_id"]).strip()
        r.setdefault("leg_id", f"L{i + 1}")
        if _is_blank(r.get("leg_id")):
            r["leg_id"] = f"L{i + 1}"
        ns_id = str(r.get("netting_set") or r["trade_id"]).strip()
        if r["underlying"] not in mkt.underlyings:
            raise ValueError(f"{r['trade_id']}: underlying '{r['underlying']}' not in market data")
        lt = str(r["leg_type"]).strip().upper()
        leg = build_trs(r, mkt, warnings) if lt == "TRS" else build_option(r, mkt) if lt == "OPTION" else None
        if leg is None:
            raise ValueError(f"{r['trade_id']}: leg_type must be TRS or OPTION")
        tr = trades.get(r["trade_id"])
        if tr is None:
            tr = trades[r["trade_id"]] = Trade(r["trade_id"], ns_id)
            if ns_id not in sets:
                warnings.append(f"Netting set '{ns_id}' not defined - treated as uncollateralised.")
                sets[ns_id] = NettingSet(ns_id)
            sets[ns_id].trades.append(tr)
        elif tr.netting_set != ns_id:
            raise ValueError(f"{r['trade_id']}: legs assigned to different netting sets")
        tr.legs.append(leg)
    return [s for s in sets.values() if s.trades]
