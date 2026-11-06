"""Soybean crush example inputs (input sheets for the .xlsx and the workbook).

Prices, vols, factor parameters and correlations are PLACEHOLDERS, not market data. Replace them
before using any output. Trade terms follow the prospect request: BMO long the crush on 5,000,000
bushels, May 2027 / Dec 2027 / May 2028 bullets. The oil quantity is the index formula's 55,000,000
lb (11 lb per bushel), not 900 lots (54,000,000 lb): enter lots instead if the lots are contractual.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

VAL_DATE = date(2026, 11, 5)
BEANS, MEAL, OIL = "SOYBEANS", "SOYBEAN_MEAL", "SOYBEAN_OIL"
PRICE = {BEANS: 1181.0, MEAL: 317.40, OIL: 62.22}       # c/bu, $/ton, c/lb: placeholders
VOL = {BEANS: 0.20, MEAL: 0.28, OIL: 0.30}              # placeholders
CROSS_RHO = {(BEANS, MEAL): 0.85, (BEANS, OIL): 0.60, (MEAL, OIL): 0.30}   # placeholders
BETA, SHORT_RATIO = 1.5, 0.5                            # placeholders

QTY = {BEANS: 5_000_000, MEAL: 110_000, OIL: 55_000_000}
SIDE = {BEANS: "SELL", MEAL: "BUY", OIL: "BUY"}
BBG = {BEANS: "S {} Comdty", MEAL: "SM{} Comdty", OIL: "BO{} Comdty"}
CODE = {BEANS: "ZS", MEAL: "ZM", OIL: "ZL"}

# contract month -> (futures expiry, option expiry); option expiries are approximate placeholders
MONTHS = {"K7": (date(2027, 5, 14), date(2027, 4, 23)), "X7": (date(2027, 11, 12), date(2027, 10, 22)),
          "Z7": (date(2027, 12, 14), date(2027, 11, 19)), "K8": (date(2028, 5, 12), date(2028, 4, 21))}
# tenor -> (contract month by product, fixing date). The CME crush ends at the earliest leg expiry,
# so the Dec 2027 bullet fixes all three legs on the X7 beans expiry.
TENORS = {"MAY27": ({BEANS: "K7", MEAL: "K7", OIL: "K7"}, date(2027, 5, 14)),
          "DEC27": ({BEANS: "X7", MEAL: "Z7", OIL: "Z7"}, date(2027, 11, 12)),
          "MAY28": ({BEANS: "K8", MEAL: "K8", OIL: "K8"}, date(2028, 5, 12))}

NS_COLUMNS = ["netting_set", "counterparty", "collateralized", "threshold_cp", "threshold_bank", "mta",
              "ia_received", "ia_posted", "ia_posted_segregated", "mpor_days", "rounding", "csa_ccy", "sa_alpha"]
TRADE_COLUMNS = ["trade_id", "netting_set", "leg_id", "leg_type", "underlying", "direction", "lots", "quantity",
                 "fixed_price", "fixing_date", "payment_date", "comment"]


def _contracts() -> pd.DataFrame:
    seen, rows = set(), []
    for months, _ in TENORS.values():
        for p, m in months.items():
            if (p, m) in seen:
                continue
            seen.add((p, m))
            rows.append(dict(contract=f"{CODE[p]}{m}", product=p, expiry=MONTHS[m][0], option_expiry=MONTHS[m][1],
                             price=PRICE[p], implied_vol=VOL[p], bbg_ticker=BBG[p].format(m)))
    return pd.DataFrame(rows)


def _correlation() -> pd.DataFrame:
    rows = [dict(factor1=f"{p}.SHORT", factor2=f"{p}.LONG", rho=0.0) for p in (BEANS, MEAL, OIL)]
    for (a, b), rho in CROSS_RHO.items():
        rows += [dict(factor1=f"{a}.LONG", factor2=f"{b}.LONG", rho=rho),
                 dict(factor1=f"{a}.SHORT", factor2=f"{b}.SHORT", rho=rho),
                 dict(factor1=f"{a}.SHORT", factor2=f"{b}.LONG", rho=0.0),
                 dict(factor1=f"{a}.LONG", factor2=f"{b}.SHORT", rho=0.0)]
    return pd.DataFrame(rows)


def _trades() -> pd.DataFrame:
    rows = []
    for ns_prefix, strip in (("", False), ("STRIP", True)):
        for tenor, (months, fix) in TENORS.items():
            ns = "NS_STRIP" if strip else f"NS_{tenor}"
            tid = f"{ns_prefix or 'CRUSH'}_{tenor}"
            for p, m in months.items():
                rows.append(dict(trade_id=tid, netting_set=ns, leg_id=p, leg_type="COMSWAP",
                                 underlying=f"{CODE[p]}{m}", direction=SIDE[p], quantity=QTY[p],
                                 fixing_date=fix, comment="At market: blank fixed_price"))
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def example_sheets() -> dict[str, pd.DataFrame]:
    config = pd.DataFrame([
        ("valuation_date", VAL_DATE), ("buckets", "EVERY:1M"), ("n_paths", 10000), ("seed", 42),
        ("pfe_quantile", 0.95), ("fine_step", "1W"), ("output_mode", "AVERAGE"), ("event_buckets", "MATURITY"),
        ("epe_horizon", "1Y"), ("reporting_ccy", "USD")], columns=["key", "value"])
    curve = pd.DataFrame([(t, 0.0375, "USD") for t in ("1M", "6M", "1Y", "2Y", "3Y")],
                         columns=["tenor", "zero_rate", "ccy"])
    products = pd.DataFrame([
        (BEANS, "AGRICULTURAL", BEANS, 5000, "BU", "C/BU", "USD"),
        (MEAL, "AGRICULTURAL", MEAL, 100, "TON", "$/TON", "USD"),
        (OIL, "AGRICULTURAL", OIL, 60000, "LB", "C/LB", "USD")],
        columns=["product", "hedging_set", "commodity_type", "lot_size", "quantity_unit", "price_unit", "ccy"])
    factors = pd.DataFrame([r for p in (BEANS, MEAL, OIL)
                            for r in ((p, "SHORT", BETA, SHORT_RATIO), (p, "LONG", 0.0, 1.0))],
                           columns=["product", "factor", "beta", "vol_ratio"])
    ns = [dict(netting_set=n, counterparty="PROSPECT", collateralized="N", mpor_days=10, csa_ccy="USD", sa_alpha=1.4)
          for n in ("NS_MAY27", "NS_DEC27", "NS_MAY28", "NS_STRIP")]
    return {"Config": config, "Curve": curve, "NettingSets": pd.DataFrame(ns, columns=NS_COLUMNS),
            "Trades": _trades(), "CommodityProducts": products, "CommodityFactors": factors,
            "FactorCorrelation": _correlation(), "Contracts": _contracts()}


def write_example_inputs(path) -> str:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in example_sheets().items():
            df.to_excel(xw, sheet_name=name, index=False)
    return str(path)
