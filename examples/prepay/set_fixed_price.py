"""Set the fixed price of the front-end or back-end prepay swap legs in an input workbook.

    .venv\\Scripts\\python examples\\prepay\\set_fixed_price.py WORKBOOK.xlsx --side FE|BE --price 5.50
        [--from 2027-01-01] [--until 2034-01-01] [--sets W7,F7,...] [--out NEW.xlsx]

Edits Trades fixed_price ($/MMBtu) on the legs of netting sets named *_<side>_* whose fixing_date is in
[--from, --until). --sets limits it to netting sets starting with those scenario prefixes. Netting
sets with value_source IMPORT (S84_*) are skipped: their PVs come from the system at the booked price,
so changing the engine's trades would leave exposure unchanged and make the implied SA-CCR prices
inconsistent. Rebook those in the system instead.
Writes to --out (default: overwrite WORKBOOK). Then run without --rebuild.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workbook")
    ap.add_argument("--side", required=True, choices=("FE", "BE"))
    ap.add_argument("--price", required=True, type=float, help="new fixed price, $/MMBtu")
    ap.add_argument("--from", dest="start", default=None, help="first fixing date to change (inclusive)")
    ap.add_argument("--until", default=None, help="fixing dates before this date only (exclusive)")
    ap.add_argument("--sets", default=None, help="comma list of scenario prefixes, e.g. W7,F7")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    sh = pd.read_excel(a.workbook, sheet_name=None)
    t, ns = sh["Trades"], sh["NettingSets"]
    src = ns.set_index("netting_set")["value_source"] if "value_source" in ns.columns else pd.Series(dtype=str)
    imported = {n for n, v in src.items() if str(v).strip().upper() == "IMPORT"}

    m = t.netting_set.astype(str).str.contains(f"_{a.side}_", regex=False)
    if a.sets:
        m &= t.netting_set.astype(str).str.startswith(tuple(f"{s.strip()}_" for s in a.sets.split(",")))
    fix = pd.to_datetime(t.fixing_date)
    if a.start:
        m &= fix >= pd.Timestamp(a.start)
    if a.until:
        m &= fix < pd.Timestamp(a.until)
    skipped = sorted(set(t.loc[m & t.netting_set.isin(imported), "netting_set"]))
    m &= ~t.netting_set.isin(imported)
    if not m.any():
        raise SystemExit("No legs matched: check --side / --sets / dates")

    old = t.loc[m, "fixed_price"].round(4).value_counts().to_dict()
    t.loc[m, "fixed_price"] = a.price
    out = Path(a.out or a.workbook)
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        for name, df in sh.items():
            df.to_excel(xw, sheet_name=name, index=False)
    sets = sorted(set(t.loc[m, "netting_set"]))
    print(f"{int(m.sum())} {a.side} legs in {len(sets)} netting sets set to {a.price} (was {old})")
    print("  " + ", ".join(sets))
    if skipped:
        print(f"Skipped IMPORT sets (rebook in the system): {', '.join(skipped)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
