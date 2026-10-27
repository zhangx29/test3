"""Input workbook for: bank buys 1mm single-stock calls, K=C$75, expiry 2027-02-28, under a CSA.
Runs the engine and adds a CAD/USD exposure profile sheet to the results."""
import pandas as pd
from openpyxl import load_workbook

from eqxva.cli import run_file
from eqxva.examples import NS_COLUMNS, TRADE_COLUMNS

USDCAD = 1.40   # CAD per USD at the valuation date (placeholder - replace with the actual fixing)
INPUTS = "examples/call_option_inputs.xlsx"
RESULTS = "examples/call_option_inputs_results.xlsx"

sheets = dict(
    Config=pd.DataFrame([
        ("valuation_date", "2026-10-23"),
        ("buckets", "2026-10-23,2026-10-24,2026-10-30,2026-11-06,2026-11-23,2026-12-23,2027-01-23,"
                    "2027-02-23,2027-02-28,2027-03-01"),
        ("n_paths", 100000), ("seed", 42), ("pfe_quantile", 0.95), ("fine_step", "1D"),
        ("output_mode", "POINT"), ("antithetic", "Y"), ("epe_horizon", "1Y"), ("analytic_nodes", 201),
    ], columns=["key", "value"]),
    # div yield: base dividend C$0.50/qtr = C$2.00/yr on C$67.44 (special dividends excluded)
    Market=pd.DataFrame([dict(underlying="STOCK", spot=67.44, div_yield=2.00 / 67.44, is_index="N",
                              flat_vol=0.25757, bbg_ticker="")]),
    # CSA: CP threshold C$8mm, bank threshold infinite (1e15: bank never posts), MTA C$400k both ways
    # CAD CORRA OIS, continuously compounded (assumed ~2.25% policy rate; replace with live CDOR/CORRA marks)
    Curve=pd.DataFrame(dict(tenor=["1M", "3M", "6M", "1Y"], zero_rate=[0.0225, 0.0225, 0.0225, 0.0228])),
    NettingSets=pd.DataFrame([dict(netting_set="NS_CP", counterparty="CP", collateralized="Y",
                                   threshold_cp=8_000_000, threshold_bank=1e15, mta=400_000, ia_received=0, ia_posted=0,
                                   ia_posted_segregated="Y", mpor_days=10, rounding=0)], columns=NS_COLUMNS),
    Trades=pd.DataFrame([dict(trade_id="CALL_C75", netting_set="NS_CP", leg_id="L1", leg_type="OPTION",
                              underlying="STOCK", direction="BUY", shares=1_000_000, option_type="CALL",
                              strike=75.0, option_maturity="2027-02-28", ovme_price=1.4989,
                              comment="bank buys 1mm Feb-27 C75 (11.21% OTM)")], columns=TRADE_COLUMNS),
)
with pd.ExcelWriter(INPUTS, engine="openpyxl") as xw:
    for n, df in sheets.items():
        df.to_excel(xw, sheet_name=n, index=False)

# CAD profile from the engine; USD = CAD / USDCAD spot (deterministic FX, no equity/FX correlation)
res = run_file(INPUTS, RESULTS)
cols = ["EE", "EE_disc", "NEE", "NEE_disc", "PFE", "FCA_exposure", "FBA_exposure", "EE_uncoll", "PFE_uncoll"]
prof = res.ns_profile[["bucket_date", "t", "df", "MTM"] + cols].rename(columns={c: c + "_CAD" for c in ["MTM"] + cols})
for c in ["MTM"] + cols:
    prof[c + "_USD"] = prof[c + "_CAD"] / USDCAD
prof["USDCAD"] = USDCAD
with pd.ExcelWriter(RESULTS, engine="openpyxl", mode="a", if_sheet_exists="replace") as xw:
    prof.to_excel(xw, sheet_name="Profile_CAD_USD", index=False)
wb = load_workbook(RESULTS)
wb.move_sheet("Profile_CAD_USD", offset=-(len(wb.sheetnames) - 1))
wb.save(RESULTS)
