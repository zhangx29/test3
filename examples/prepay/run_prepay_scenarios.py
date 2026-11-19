"""Gas prepay: exposure and SA-CCR EAD for every scenario x side x collateral variant, side by side.

    .venv\\Scripts\\python examples\\prepay\\run_prepay_scenarios.py [--paths N] [--scenarios W7,F7,...]
                                                              [--vm-posting] [--no-analytic]
                                                              [--volume MMBtu/d] [--im USD] [--csra USD]

Writes examples\\prepay\\prepay_inputs.xlsx (if missing, or with --rebuild) and
examples\\prepay\\prepay_inputs_results.xlsx with the usual sheets plus PrepayScenarios.

ALL MARKET INPUTS ARE ILLUSTRATIVE PLACEHOLDERS (see eqxva/commodity/prepay_examples.py). Exposure and
EAD only: CVA / lost spread / KVA / RWA are computed downstream from these profiles.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

from eqxva.cli import run_file
from eqxva.commodity import prepay_examples as pe

HERE = Path(__file__).resolve().parent
LABEL = {"W7": "2-month window, M to Dec33", "W30": "2-month window, M to Dec57",
         "F7": "full 7y swap, MARKET notional", "F7A": "full 7y swap, AMORT notional",
         "F30": "full 30y swap, MARKET notional", "F30A": "full 30y swap, AMORT notional",
         "B84": "84-trade booking, windowed (SELECT)", "N84": "84-trade booking, summed (double count)"}


def scenario_table(res) -> pd.DataFrame:
    s = res.summary.copy()
    parts = s.netting_set.str.split("_", n=2, expand=True)
    s["scenario"], s["side"], s["collateral"] = parts[0], parts[1], parts[2]
    s["description"] = s.scenario.map(LABEL)
    px = res.saccr_proxy.groupby("netting_set").agg(SA_NOTIONAL_TOTAL=("SA_NOTIONAL_TOTAL", "sum"),
                                                    SA_NOTIONAL_AMORT=("SA_NOTIONAL_AMORT", "sum"),
                                                    M_trade=("M_trade", "max"))
    p = res.ns_profile
    mat = s.set_index("netting_set").maturity_yrs

    def life_avg(col):          # time average over each netting set's own life (bucket widths, POINT values)
        return p.groupby("netting_set").apply(
            lambda g: (g[col] * g.t.diff().fillna(g.t)).sum() / mat[g.name], include_groups=False).rename(col + "_life_avg")
    s = s.join(px, on="netting_set")
    for c in ("EE", "NEE_uncoll", "SACCR_EAD_mean"):
        s = s.join(life_avg(c), on="netting_set")
    s = s.join(p.groupby("netting_set").SACCR_EAD_mean.max().rename("SACCR_EAD_peak"), on="netting_set")
    cols = ["scenario", "description", "side", "counterparty", "collateral", "netting_set", "value_source",
            "MTM0", "peak_EE", "EPE", "EE_life_avg", "NEE_uncoll_life_avg", "peak_PFE", "SACCR_EAD0",
            "SACCR_EAD_mean_life_avg", "SACCR_EAD_peak", "SACCR_AddOn0", "SACCR_multiplier0",
            "SA_NOTIONAL_TOTAL", "SA_NOTIONAL_AMORT", "M_trade", "maturity_yrs", "n_paths"]
    return s[cols]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=None)
    ap.add_argument("--scenarios", default=",".join(pe.SCENARIOS))
    ap.add_argument("--vm-posting", action="store_true")
    ap.add_argument("--no-analytic", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--curve", default=None, help="forward curve CSV (month,price); default flat F0")
    ap.add_argument("--name", default="prepay_inputs", help="input workbook base name")
    ap.add_argument("--volume", type=float, default=None,
                    help="MMBtu/day for every leg (default 20,000); applied only on (re)build")
    ap.add_argument("--im", type=float, default=None,
                    help="SPV IM, USD (default: 6.24mm scaled pro rata to --volume)")
    ap.add_argument("--csra", type=float, default=None,
                    help="CSRA, USD (default: 8mm scaled pro rata to --volume)")
    a = ap.parse_args(argv)
    inp = HERE / f"{a.name}.xlsx"
    if a.rebuild or not inp.exists():
        kw = dict(price=pe.curve_prices(pe.load_curve(a.curve))) if a.curve else {}
        scale = (a.volume / pe.VOL_D) if a.volume else 1.0
        if a.volume:
            kw["vol_d"] = a.volume
        kw["im"] = a.im if a.im is not None else pe.IM_SPV * scale
        kw["csra"] = a.csra if a.csra is not None else pe.CSRA * scale
        pe.write_example_inputs(inp, scenarios=tuple(a.scenarios.split(",")), vm_posting=a.vm_posting, **kw)
        print(f"wrote {inp} (volume {kw.get('vol_d', pe.VOL_D):,.0f} MMBtu/d, IM {kw['im']:,.0f}, CSRA {kw['csra']:,.0f})")
    out = HERE / f"{a.name}_results.xlsx"
    t0 = time.time()
    res = run_file(inp, out, analytic=not a.no_analytic, n_paths=a.paths)
    tbl = scenario_table(res)
    with pd.ExcelWriter(out, engine="openpyxl", mode="a", if_sheet_exists="replace") as xw:
        tbl.to_excel(xw, sheet_name="PrepayScenarios", index=False)
    pd.set_option("display.width", 220)
    show = tbl[["netting_set", "peak_EE", "EPE", "peak_PFE", "SACCR_EAD0", "SA_NOTIONAL_TOTAL",
                "SA_NOTIONAL_AMORT", "M_trade"]].copy()
    for c in show.columns[1:-1]:
        show[c] = show[c] / 1e6
    print("USD mm (illustrative inputs)")
    print(show.round(3).to_string(index=False))
    for w in res.warnings:
        print("WARNING:", w)
    print(f"\nResults written to {out} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
