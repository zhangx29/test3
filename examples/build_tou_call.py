"""Input workbook for: bank buys 1mm TOU CN Equity calls, K=C$75 (11.21% OTM, spot C$67.44),
expiry 2027-02-27, under a CAD CSA. Exposure outputs reported in USD.

Run:  .venv\\Scripts\\python examples\\build_tou_call.py
"""
import pandas as pd

from eqxva.cli import run_file
from eqxva.examples import NS_COLUMNS, TRADE_COLUMNS

USDCAD = 1.40   # CAD per USD (placeholder - replace with the USDCAD Curncy fixing)
INPUTS = "examples/tou_call_inputs.xlsx"
RESULTS = "examples/tou_call_inputs_results.xlsx"

sheets = dict(
    Config=pd.DataFrame([
        ("valuation_date", "2026-10-23"),
        ("buckets", "1D,1W,2W,1M,2M,3M,4M,6M"),
        ("n_paths", 100000), ("seed", 42), ("pfe_quantile", 0.95), ("fine_step", "1D"),
        ("output_mode", "AVERAGE"), ("antithetic", "Y"), ("epe_horizon", "1Y"), ("analytic_nodes", 201),
        ("reporting_ccy", "USD"),
    ], columns=["key", "value"]),
    # div yield: base dividend C$0.50/qtr = C$2.00/yr on C$67.44 (special dividends excluded)
    Market=pd.DataFrame([dict(underlying="TOU", spot=67.44, div_yield=2.00 / 67.44, is_index="N",
                              flat_vol=0.25757, bbg_ticker="TOU CN Equity", ccy="CAD")]),
    # CAD CORRA OIS and USD SOFR OIS, continuously compounded (placeholders - replace with live marks)
    Curve=pd.DataFrame(dict(tenor=["1M", "3M", "6M", "1Y"] * 2,
                            zero_rate=[0.0225, 0.0225, 0.0225, 0.0228, 0.0410, 0.0405, 0.0395, 0.0380],
                            ccy=["CAD"] * 4 + ["USD"] * 4)),
    FX=pd.DataFrame(dict(pair=["USDCAD"], spot=[USDCAD])),
    # CSA in CAD: CP threshold C$8mm, bank threshold infinite (1e15: bank never posts), MTA C$400k
    NettingSets=pd.DataFrame([dict(netting_set="NS_TOU", counterparty="CP", collateralized="Y",
                                   threshold_cp=8_000_000, threshold_bank=1e15, mta=400_000, ia_received=0,
                                   ia_posted=0, ia_posted_segregated="Y", mpor_days=10, rounding=0,
                                   csa_ccy="CAD")], columns=NS_COLUMNS),
    # ovme_price: Bloomberg 2.09% of spot = C$1.4095 per share
    Trades=pd.DataFrame([dict(trade_id="TOU_C75", netting_set="NS_TOU", leg_id="L1", leg_type="OPTION",
                              underlying="TOU", direction="BUY", shares=1_000_000, option_type="CALL",
                              strike=75.0, option_maturity="2027-02-27", ovme_price=0.0209 * 67.44,
                              comment="bank buys 1mm TOU CN Feb-27 C75 (11.21% OTM)")], columns=TRADE_COLUMNS),
)
with pd.ExcelWriter(INPUTS, engine="openpyxl") as xw:
    for n, df in sheets.items():
        df.to_excel(xw, sheet_name=n, index=False)

res = run_file(INPUTS, RESULTS)
print(res.summary.to_string(index=False))
for w in res.warnings:
    print("WARNING:", w)
print(f"\nResults written to {RESULTS}")
