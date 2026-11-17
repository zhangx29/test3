"""Gas prepay example inputs: front-end swap vs the Issuer, back-end swap vs the SPV.

ALL MARKET INPUTS ARE ILLUSTRATIVE PLACEHOLDERS (flat $5.00 curve, 45% short vol decaying at kappa 1
plus 15% long vol, 4% rates, CSRA / IM sizing at 20,000 MMBtu/d). Replace them with BMO data and the
engine's calibration before using any number. See docs/prepay/context.md.

Deal (13 Nov proposal, 20,000 MMBtu/d):
  front-end (Issuer): BMO pays fixed $5.00, receives index      -> COMSWAP BUY
  back-end  (SPV):    BMO receives $5.035 to Dec33, $5.00 after, pays index -> COMSWAP SELL
Monthly index fixes at the start of its delivery month (FOM) and is paid at the start of the next.

Scenarios (one netting set per scenario x side x collateral variant):
  W7    rolling 2-month window, strip Jan27-Dec33, SA-CCR M to Dec 2033
  W30   rolling 2-month window, strip Jan27-Dec57, SA-CCR M to Dec 2057
  F7    full 7-year swap Jan27-Dec33, M = trade maturity, MARKET notional   (F7A: AMORT notional)
  F30   full 30-year swap Jan27-Dec57, M = trade maturity, MARKET notional  (F30A: AMORT notional)
  B84   the system booking: 84 two-month trades (k covers months k, k+1), trade k live in month k only
  N84   the same 84 trades summed without active dates (shows the double count)
Collateral: front-end with / without the CSRA, back-end with / without the SPV IM (both static,
entered as ia_received on an uncollateralised netting set). Optional back-end variant with BMO
posting VM to the SPV (vm_posting=True).
"""
from __future__ import annotations

import math
from datetime import date

import pandas as pd

from .. import dates as dt

VAL_DATE = date(2026, 11, 13)
FIRST = date(2027, 1, 1)
N_MONTHS = 372                       # Jan27 .. Dec57
RESET = date(2034, 1, 1)             # first month after the Dec 2033 tender / reset
VOL_D = 20_000                       # MMBtu per day
F0 = 5.00                            # flat curve, $/MMBtu (placeholder)
SIG_S, KAPPA, SIG_L = 0.45, 1.0, 0.15   # placeholders
RATE = 0.04
K_FRONT, K_BACK_7Y, K_BACK_AFTER = 5.00, 5.035, 5.00
CSRA, IM_SPV = 8.0e6, 6.24e6         # assumed pro rata sizing at 20k/d
PRODUCT = "NATGAS"
SCENARIOS = ("W7", "W30", "F7", "F7A", "F30", "F30A", "B84", "N84")
M7, M30 = date(2034, 1, 1), date(2058, 1, 1)   # SA-CCR M: last payment of Dec33 / Dec57

NS_COLUMNS = ["netting_set", "counterparty", "collateralized", "threshold_cp", "threshold_bank", "mta",
              "ia_received", "ia_posted", "ia_posted_segregated", "mpor_days", "rounding", "csa_ccy", "sa_alpha"]
TRADE_COLUMNS = ["trade_id", "netting_set", "leg_id", "leg_type", "underlying", "direction", "quantity",
                 "fixed_price", "fixing_date", "payment_date", "active_from", "active_to", "window_months",
                 "window_convention", "sa_maturity", "sa_notional_basis", "sa_fixed_notional", "comment"]
_CODE = "FGHJKMNQUVXZ"


def month_start(i: int) -> date:
    return dt.add_months(FIRST, i)


def contract_name(i: int) -> str:
    d = month_start(i)
    return f"NG{_CODE[d.month - 1]}{d.year % 100:02d}"


def quantity(i: int, vol_d: float = VOL_D) -> float:
    return (month_start(i + 1) - month_start(i)).days * vol_d


def implied_vol(T: float, sig_s: float = SIG_S, kappa: float = KAPPA, sig_l: float = SIG_L) -> float:
    """Vol of the contract's option to its expiry under the reference 2-factor model, so the engine
    calibrates q_ref = sig_l and reproduces it: var = sig_s^2 (1 - e^{-2 kappa T}) / (2 kappa) + sig_l^2 T."""
    var = sig_s ** 2 * (1 - math.exp(-2 * kappa * T)) / (2 * kappa) + sig_l ** 2 * T
    return math.sqrt(var / T)


def load_curve(path) -> dict[tuple[int, int], float]:
    """Forward curve CSV with columns month (YYYY-MM) and price ($/MMBtu) -> {(year, month): price}."""
    df = pd.read_csv(path, dtype={"month": str})
    out = {}
    for m, p in zip(df["month"], df["price"]):
        y, mo = (int(x) for x in m.strip().split("-")[:2])
        out[(y, mo)] = float(p)
    return out


def curve_prices(curve: dict[tuple[int, int], float]) -> list[float]:
    """Price for each strip month 0..N_MONTHS. Months past the curve's last point repeat the last 12
    quoted months (seasonal shape held flat); months before its first point take the first price."""
    keys = sorted(curve)
    last12 = [curve[k] for k in keys[-12:]]
    first_y, first_m = keys[-12]
    out = []
    for i in range(N_MONTHS + 1):
        d = month_start(i)
        k = (d.year, d.month)
        if k in curve:
            out.append(curve[k])
        elif k < keys[0]:
            out.append(curve[keys[0]])
        else:
            out.append(last12[((d.year - first_y) * 12 + d.month - first_m) % 12])
    return out


def _contracts(price, sig_s: float, kappa: float, sig_l: float) -> pd.DataFrame:
    """``price``: a flat level, or a list of N_MONTHS + 1 prices (one per strip month)."""
    prices = [float(price)] * (N_MONTHS + 1) if isinstance(price, (int, float)) else list(price)
    rows = []
    for i in range(N_MONTHS + 1):          # one spare month so any strip can reference it
        d = month_start(i)
        T = dt.yf(VAL_DATE, d)
        rows.append(dict(contract=contract_name(i), product=PRODUCT, expiry=d, option_expiry=d, price=prices[i],
                         implied_vol=implied_vol(T, sig_s, kappa, sig_l), bbg_ticker=""))
    return pd.DataFrame(rows)


def k_back(i: int) -> float:
    return K_BACK_7Y if month_start(i) < RESET else K_BACK_AFTER


def _leg(tid, ns, i, side, vol_d, **extra) -> dict:
    buy = side == "FE"
    return dict(trade_id=tid, netting_set=ns, leg_id=f"M{i:03d}", leg_type="COMSWAP",
                underlying=contract_name(i), direction="BUY" if buy else "SELL",
                quantity=quantity(i, vol_d), fixed_price=K_FRONT if buy else k_back(i),
                fixing_date=month_start(i), payment_date=month_start(i + 1), **extra)


def strip_rows(tid, ns, side, i0, i1, vol_d=VOL_D, **extra) -> list[dict]:
    """One trade with a leg per month i0..i1 (inclusive)."""
    return [_leg(tid, ns, i, side, vol_d, **extra) for i in range(i0, i1 + 1)]


def booked_84_rows(prefix, ns, side, vol_d=VOL_D, active=True, n=84) -> list[dict]:
    """The system booking: trade k covers months k and k+1. With ``active`` (window treatment) trade k
    is live in month k only and SA-CCR M runs to the Dec 2033 reset; without, each trade is as booked
    (M = its own two-month maturity) and the netting-set sum double counts."""
    rows = []
    for k in range(n):
        a = (dict(active_from=None if k == 0 else month_start(k), active_to=month_start(k + 1),
                  sa_maturity=max(M7, month_start(k + 2))) if active else {})
        rows += [_leg(f"{prefix}_{k:03d}", ns, i, side, vol_d, **a) for i in (k, k + 1)]
    return rows


def _last_month(d: date) -> int:
    return (d.year - FIRST.year) * 12 + d.month - FIRST.month


def scenario_rows(scen: str, ns: str, side: str, vol_d: float = VOL_D) -> list[dict]:
    i7, i30 = _last_month(date(2033, 12, 1)), N_MONTHS - 1
    if scen == "W7":
        return strip_rows(ns, ns, side, 0, i7, vol_d, window_months=2, window_convention="FWD", sa_maturity=M7)
    if scen == "W30":
        return strip_rows(ns, ns, side, 0, i30, vol_d, window_months=2, window_convention="FWD", sa_maturity=M30)
    if scen in ("F7", "F7A", "F30", "F30A"):
        last = i7 if scen.startswith("F7") else i30
        return strip_rows(ns, ns, side, 0, last, vol_d, sa_maturity="TRADE",
                          sa_notional_basis="AMORT" if scen.endswith("A") else "MARKET")
    if scen in ("B84", "N84"):
        return booked_84_rows(ns, ns, side, vol_d, active=scen == "B84")
    raise ValueError(f"unknown scenario {scen}")


def example_sheets(scenarios=SCENARIOS, sides=("FE", "BE"), collateral=(True, False), vol_d: float = VOL_D,
                   csra: float = CSRA, im: float = IM_SPV, vm_posting: bool = False, n_paths: int = 10000,
                   price: float | list[float] = F0, sig_s: float = SIG_S, kappa: float = KAPPA, sig_l: float = SIG_L,
                   output_mode: str = "POINT", buckets: str = "EVERY:1M") -> dict[str, pd.DataFrame]:
    """Input sheets for the selected scenarios / sides / collateral variants (True = with CSRA or IM).
    Netting set ids: <scenario>_<FE|BE>_<CSRA|NOCSRA|IM|NOIM|IMVM>."""
    ns_rows, trades = [], []
    for scen in scenarios:
        for side in sides:
            for coll in collateral:
                tag = ("CSRA" if coll else "NOCSRA") if side == "FE" else ("IM" if coll else "NOIM")
                ns = f"{scen}_{side}_{tag}"
                amount = (csra if side == "FE" else im) if coll else 0.0
                ns_rows.append(dict(netting_set=ns, counterparty="ISSUER" if side == "FE" else "SPV",
                                    collateralized="N", ia_received=amount, mpor_days=10, csa_ccy="USD", sa_alpha=1.4))
                trades += scenario_rows(scen, ns, side, vol_d)
            if vm_posting and side == "BE":
                ns = f"{scen}_BE_IMVM"        # SPV never posts VM (it has IM); BMO posts VM on the window value
                ns_rows.append(dict(netting_set=ns, counterparty="SPV", collateralized="Y", threshold_cp=1e15,
                                    threshold_bank=0.0, mta=0.0, ia_received=im, mpor_days=10, csa_ccy="USD",
                                    sa_alpha=1.4))
                trades += scenario_rows(scen, ns, side, vol_d)
    config = pd.DataFrame([
        ("valuation_date", VAL_DATE), ("buckets", buckets), ("n_paths", n_paths), ("seed", 42),
        ("pfe_quantile", 0.975), ("fine_step", "1M"), ("output_mode", output_mode), ("event_buckets", "NONE"),
        ("epe_horizon", "1Y"), ("reporting_ccy", "USD")], columns=["key", "value"])
    curve = pd.DataFrame([(t, RATE, "USD") for t in ("1M", "1Y", "5Y", "10Y", "20Y", "40Y")],
                         columns=["tenor", "zero_rate", "ccy"])
    products = pd.DataFrame([(PRODUCT, "ENERGY", "NATURAL_GAS", 10000, "MMBTU", "$/MMBTU", "USD")],
                            columns=["product", "hedging_set", "commodity_type", "lot_size", "quantity_unit",
                                     "price_unit", "ccy"])
    factors = pd.DataFrame([(PRODUCT, "SHORT", kappa, sig_s / sig_l), (PRODUCT, "LONG", 0.0, 1.0)],
                           columns=["product", "factor", "beta", "vol_ratio"])
    corr = pd.DataFrame([(f"{PRODUCT}.SHORT", f"{PRODUCT}.LONG", 0.0)], columns=["factor1", "factor2", "rho"])
    return {"Config": config, "Curve": curve, "NettingSets": pd.DataFrame(ns_rows, columns=NS_COLUMNS),
            "Trades": pd.DataFrame(trades, columns=TRADE_COLUMNS), "CommodityProducts": products,
            "CommodityFactors": factors, "FactorCorrelation": corr,
            "Contracts": _contracts(price, sig_s, kappa, sig_l)}


def add_system_import(sheets: dict[str, pd.DataFrame], request_ids: dict[str, list], folder: str = "",
                      collateral=(True, False), csra: float = CSRA, im: float = IM_SPV,
                      vol_d: float = VOL_D, undiscount: str = "Y") -> dict[str, pd.DataFrame]:
    """Add IMPORT netting sets S84_<FE|BE>_<collateral> for the system's 84-strip booking.

    request_ids: {"FE": [84 request ids in strip order], "BE": [...]} - strip k covers months k, k+1 and
    is live in month k only. Each strip's PVs come from that request's RiskProfile files in ``folder``
    (relative to the input workbook). The same request feeds the with- and without-collateral sets.
    Set Config valuation_date to the export's first date."""
    s = dict(sheets)
    ns_rows, trades, req = [], [], []
    for side, ids in request_ids.items():
        if len(ids) != 84:
            raise ValueError(f"{side}: expected 84 request ids, got {len(ids)}")
        for coll in collateral:
            tag = ("CSRA" if coll else "NOCSRA") if side == "FE" else ("IM" if coll else "NOIM")
            ns = f"S84_{side}_{tag}"
            ns_rows.append(dict(netting_set=ns, counterparty="ISSUER" if side == "FE" else "SPV", collateralized="N",
                                ia_received=((csra if side == "FE" else im) if coll else 0.0), mpor_days=10,
                                csa_ccy="USD", sa_alpha=1.4, value_source="IMPORT"))
            trades += booked_84_rows(ns, ns, side, vol_d)
            req += [dict(trade_id=f"{ns}_{k:03d}", request_id=str(r), folder=folder, undiscount=undiscount)
                    for k, r in enumerate(ids)]
    s["NettingSets"] = pd.concat([s["NettingSets"], pd.DataFrame(ns_rows)], ignore_index=True)
    s["Trades"] = pd.concat([s["Trades"], pd.DataFrame(trades, columns=TRADE_COLUMNS)], ignore_index=True)
    s["SystemRequests"] = pd.DataFrame(req, columns=["trade_id", "request_id", "folder", "undiscount"])
    return s


def write_example_inputs(path, **kw) -> str:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in example_sheets(**kw).items():
            df.to_excel(xw, sheet_name=name, index=False)
    return str(path)
