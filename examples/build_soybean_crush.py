"""Soybean crush example: BMO long the crush on 5,000,000 bushels, May 2027 / Dec 2027 / May 2028 bullets.
Each tenor runs standalone, and NS_STRIP nets all three. Market inputs are placeholders.
The input file is created only if it does not exist, so your updated inputs are kept.

Run:  .venv\\Scripts\\python examples\\build_soybean_crush.py
"""
from pathlib import Path

from eqxva.cli import run_file
from eqxva.commodity.examples import write_example_inputs

INPUTS = "examples/soybean_crush_inputs.xlsx"
RESULTS = "examples/soybean_crush_inputs_results.xlsx"

if not Path(INPUTS).exists():
    write_example_inputs(INPUTS)
res = run_file(INPUTS, RESULTS)
print(res.summary[["netting_set", "peak_EE", "peak_PFE", "SACCR_EAD0"]].to_string(index=False))
print(res.saccr_proxy[["trade_id", "MF", "AddOn0", "SA_NOTIONAL"]].to_string(index=False))
