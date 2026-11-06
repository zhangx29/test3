"""Run the three crush maturities standalone from the current input file, without the netted strip.
Reads examples/soybean_crush_inputs.xlsx and leaves it unchanged.

Run:  .venv\\Scripts\\python examples\\run_soybean_crush_3_maturities.py
"""
from eqxva.analytic import run_analytic
from eqxva.exposure import run_monte_carlo
from eqxva.io_excel import parse_inputs, read_workbook, write_results

INPUTS = "examples/soybean_crush_inputs.xlsx"
RESULTS = "examples/soybean_crush_3_maturities_results.xlsx"
N_PATHS = 100_000

sheets = read_workbook(INPUTS)
sheets["Trades"] = sheets["Trades"][sheets["Trades"]["netting_set"] != "NS_STRIP"]
sheets["NettingSets"] = sheets["NettingSets"][sheets["NettingSets"]["netting_set"] != "NS_STRIP"]
mkt, nss, cfg, warnings = parse_inputs(sheets)
cfg.n_paths = N_PATHS
res = run_monte_carlo(nss, mkt, cfg, warnings)
res.analytic = run_analytic(nss, mkt, cfg, res)
write_results(res, RESULTS, sheets)
print(f"Results written to {RESULTS}")
