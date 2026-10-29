"""Example inputs covering every supported structure (used by the template command, the Excel
builder and the tests)."""
from __future__ import annotations

from datetime import date

import pandas as pd

VAL_DATE = date(2026, 10, 9)

TRADE_COLUMNS = ["trade_id", "netting_set", "leg_id", "leg_type", "underlying", "direction", "shares",
                 "start_date", "maturity", "equity_reset_freq", "funding_reset_freq", "funding_spread",
                 "notional_basis", "avg_price", "initial_mtm", "div_passthrough",
                 "option_type", "strike", "option_maturity", "vol_override", "ovme_price", "comment"]

NS_COLUMNS = ["netting_set", "counterparty", "collateralized", "threshold_cp", "threshold_bank", "mta",
              "ia_received", "ia_posted", "ia_posted_segregated", "mpor_days", "rounding", "csa_ccy"]


def _trs(tid, ns, leg, und, direction, shares, maturity, reset, spread, basis="CURRENT", avg=None,
         im=None, comment=""):
    return dict(trade_id=tid, netting_set=ns, leg_id=leg, leg_type="TRS", underlying=und, direction=direction,
                shares=shares, maturity=maturity, equity_reset_freq=reset, funding_reset_freq="3M",
                funding_spread=spread, notional_basis=basis, avg_price=avg, initial_mtm=im, div_passthrough=1.0,
                comment=comment)


def _opt(tid, ns, leg, und, direction, otype, shares, strike, expiry, comment=""):
    return dict(trade_id=tid, netting_set=ns, leg_id=leg, leg_type="OPTION", underlying=und,
                direction=direction, shares=shares, option_type=otype, strike=strike, option_maturity=expiry,
                comment=comment)


def example_sheets() -> dict[str, pd.DataFrame]:
    config = pd.DataFrame([
        ("valuation_date", VAL_DATE.isoformat()),
        ("buckets", "1M,3M,6M,9M,1Y,18M,2Y,3Y,4Y,5Y"),
        ("n_paths", 10000),
        ("seed", 42),
        ("pfe_quantile", 0.95),
        ("fine_step", "1W"),
        ("output_mode", "AVERAGE"),
        ("antithetic", "Y"),
        ("epe_horizon", "1Y"),
        ("analytic_nodes", 201),
        ("reporting_ccy", "USD"),
    ], columns=["key", "value"])
    market = pd.DataFrame([
        dict(underlying="STOCK_A", spot=100.0, div_yield=0.015, is_index="N", flat_vol=0.28,
             bbg_ticker="AAPL US Equity", ccy="USD"),
        dict(underlying="INDEX_X", spot=5000.0, div_yield=0.018, is_index="Y", flat_vol=0.18,
             bbg_ticker="SPX Index", ccy="USD"),
        dict(underlying="STOCK_CA", spot=67.44, div_yield=0.0297, is_index="N", flat_vol=0.2576,
             bbg_ticker="TOU CN Equity", ccy="CAD"),
    ])
    # USD SOFR OIS and CAD CORRA OIS zero rates (example values - replace with live marks)
    tenors = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "10Y"]
    curve = pd.DataFrame(dict(
        tenor=tenors * 2,
        zero_rate=[0.0410, 0.0405, 0.0395, 0.0380, 0.0365, 0.0360, 0.0362, 0.0375,
                   0.0225, 0.0225, 0.0225, 0.0228, 0.0240, 0.0252, 0.0270, 0.0300],
        ccy=["USD"] * 8 + ["CAD"] * 8))
    fx = pd.DataFrame([dict(pair="USDCAD", bbg_ticker="USDCAD Curncy", spot=1.40)])
    vrows = []
    smile = {0.8: 0.06, 0.9: 0.03, 1.0: 0.0, 1.1: -0.015, 1.2: -0.02}
    term = {"1M": 0.30, "3M": 0.29, "6M": 0.285, "1Y": 0.28, "2Y": 0.275, "5Y": 0.27}
    for ten, atm in term.items():
        for m, sk in smile.items():
            vrows.append(dict(underlying="STOCK_A", tenor=ten, moneyness=m, vol=round(atm + sk, 4)))
    vol = pd.DataFrame(vrows)
    corr = pd.DataFrame([dict(underlying1="STOCK_A", underlying2="INDEX_X", rho=0.6)])
    ns = pd.DataFrame([
        dict(netting_set="NS_ALPHA", counterparty="Alpha Fund", collateralized="N", mpor_days=10),
        dict(netting_set="NS_BETA", counterparty="Beta Capital", collateralized="Y", threshold_cp=0,
             threshold_bank=0, mta=50000, ia_received=0, ia_posted=0, ia_posted_segregated="Y", mpor_days=10),
        dict(netting_set="NS_GAMMA", counterparty="Gamma Partners", collateralized="N", mpor_days=10),
        dict(netting_set="NS_DELTA", counterparty="Delta Pension", collateralized="Y", threshold_cp=1000000,
             threshold_bank=1000000, mta=100000, ia_received=500000, ia_posted=0, ia_posted_segregated="Y",
             mpor_days=10),
        dict(netting_set="NS_EPSILON", counterparty="Epsilon Asset Mgmt", collateralized="Y",
             threshold_cp=8000000, threshold_bank=1e15, mta=400000, ia_received=0, ia_posted=0,
             ia_posted_segregated="Y", mpor_days=10, csa_ccy="CAD"),
    ], columns=NS_COLUMNS)
    trades = pd.DataFrame([
        _trs("T001", "NS_ALPHA", "L1", "STOCK_A", "RECEIVE_EQUITY", 100000, "3Y", "3M", 0.0050,
             comment="TRS, quarterly equity reset"),
        _trs("T002", "NS_ALPHA", "L1", "STOCK_A", "PAY_EQUITY", 50000, "2Y", "NONE", 0.0040,
             comment="TRS, no reset (bank pays equity)"),
        _trs("T003", "NS_BETA", "L1", "STOCK_A", "RECEIVE_EQUITY", 80000, "1Y", "1M", 0.0045, "AVERAGE", 92.0,
             comment="Extension: notional on previous average price 92 (MTM embedded in strike)"),
        _trs("T004", "NS_BETA", "L1", "STOCK_A", "RECEIVE_EQUITY", 60000, "18M", "3M", 0.0050, "CURRENT",
             im=250000, comment="Extension re-struck at spot with carried initial MTM 250k"),
        _trs("T005", "NS_GAMMA", "L1", "STOCK_A", "RECEIVE_EQUITY", 100000, "2Y", "NONE", 0.0050,
             comment="TRS + protective put"),
        _opt("T005", "NS_GAMMA", "L2", "STOCK_A", "SELL", "PUT", 100000, "90%", "1Y",
             comment="bank sells put to the client alongside TRS"),
        _opt("T006", "NS_GAMMA", "L1", "STOCK_A", "BUY", "PUT", 100000, "90%", "1Y", comment="collar: long put"),
        _opt("T006", "NS_GAMMA", "L2", "STOCK_A", "SELL", "CALL", 100000, "115%", "1Y", comment="collar: short call"),
        _opt("T007", "NS_DELTA", "L1", "INDEX_X", "SELL", "PUT", 2000, "95%", "6M", comment="short index put"),
        _opt("T008", "NS_DELTA", "L1", "INDEX_X", "BUY", "CALL", 2000, 5200, "2Y", comment="long index call"),
        _trs("T009", "NS_DELTA", "L1", "INDEX_X", "RECEIVE_EQUITY", 1000, "5Y", "6M", 0.0030,
             comment="index TRS, semi-annual reset"),
        _opt("T010", "NS_EPSILON", "L1", "STOCK_CA", "BUY", "CALL", 1000000, 75.0, "2027-02-27",
             comment="CAD stock call under a CAD CSA (C$ thresholds), reported in USD"),
    ], columns=TRADE_COLUMNS)
    return dict(Config=config, Market=market, Curve=curve, FX=fx, VolSurface=vol, Correlation=corr,
                NettingSets=ns, Trades=trades)


def write_example_inputs(path) -> str:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in example_sheets().items():
            df.to_excel(xw, sheet_name=name, index=False)
    return str(path)
