"""Set the daily volume of the prepay swap legs in an existing input workbook (keeps your other edits).

    .venv\\Scripts\\python examples\\prepay\\set_volume.py WORKBOOK.xlsx --volume 25000
        [--side FE|BE] [--sets W7,F7,...] [--im USD] [--csra USD] [--no-collateral-scaling] [--out NEW.xlsx]

Each COMSWAP leg's Trades `quantity` becomes days-in-delivery-month x volume (the month is taken from the
leg's fixing date, so Feb = 28 or 29 days). NettingSets `ia_received` on *_FE_CSRA and *_BE_IM / *_BE_IMVM sets
is rescaled pro rata to the new volume (IM sized as two months of volume), unless --im / --csra give the
amounts or --no-collateral-scaling keeps them. Sets with ia_received 0 (NOCSRA / NOIM) stay 0.
Netting sets with value_source IMPORT (S84_*) are skipped: their PVs come from the system at the booked
volume, so rebook those in the system instead.
Writes to --out (default: overwrite WORKBOOK). Then run run_prepay_scenarios.py --name <base> WITHOUT --rebuild.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook")
    ap.add_argument("--volume", required=True, type=float, help="MMBtu per day")
    ap.add_argument("--side", choices=("FE", "BE"), default=None, help="only this side (default both)")
    ap.add_argument("--sets", default=None, help="comma list of scenario prefixes, e.g. W7,W30")
    ap.add_argument("--im", type=float, default=None, help="SPV IM in USD (overrides pro rata scaling)")
    ap.add_argument("--csra", type=float, default=None, help="CSRA in USD (overrides pro rata scaling)")
    ap.add_argument("--no-collateral-scaling", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    sh = pd.read_excel(a.workbook, sheet_name=None)
    t, ns = sh["Trades"], sh["NettingSets"]
    src = ns.set_index("netting_set")["value_source"] if "value_source" in ns.columns else pd.Series(dtype=str)
    imported = {n for n, v in src.items() if str(v).strip().upper() == "IMPORT"}

    m = (t.leg_type.astype(str).str.upper() == "COMSWAP") & ~t.netting_set.isin(imported)
    if a.side:
        m &= t.netting_set.astype(str).str.contains(f"_{a.side}_", regex=False)
    if a.sets:
        m &= t.netting_set.astype(str).str.startswith(tuple(f"{s.strip()}_" for s in a.sets.split(",")))
    if not m.any():
        raise SystemExit("No legs matched: check --side / --sets")

    fix = pd.to_datetime(t.loc[m, "fixing_date"])
    days = fix.dt.days_in_month
    old_vol = (t.loc[m, "quantity"] / days).round(0)
    t.loc[m, "quantity"] = days * a.volume
    sets = sorted(set(t.loc[m, "netting_set"]))

    if not a.no_collateral_scaling:
        for i, r in ns.iterrows():
            n = str(r.netting_set)
            if n not in sets or not (pd.notna(r.ia_received) and r.ia_received > 0):
                continue
            ov = old_vol[t.loc[m, "netting_set"] == n].median()
            if n.endswith("_CSRA"):
                ns.at[i, "ia_received"] = a.csra if a.csra is not None else r.ia_received * a.volume / ov
            elif n.endswith("_IM") or n.endswith("_IMVM"):
                ns.at[i, "ia_received"] = a.im if a.im is not None else r.ia_received * a.volume / ov

    out = Path(a.out or a.workbook)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sh.items():
            df.to_excel(xw, sheet_name=name, index=False)
    print(f"{int(m.sum())} legs in {len(sets)} netting sets set to {a.volume:,.0f} MMBtu/d "
          f"(was {', '.join(f'{v:,.0f}' for v in sorted(old_vol.unique()))})")
    coll = ns[ns.netting_set.isin(sets) & (ns.ia_received.fillna(0) > 0)][["netting_set", "ia_received"]]
    if len(coll):
        print("Collateral now:\n" + coll.to_string(index=False))
    skipped = sorted(imported & set(t.netting_set))
    if skipped:
        print(f"Skipped IMPORT sets (rebook in the system): {', '.join(skipped)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
