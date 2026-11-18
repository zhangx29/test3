"""Gas prepay: exposure and SA-CCR EAD from the bank system's 2,000-path export of the 84 booked strips.

    .venv\\Scripts\\python examples\\prepay\\run_system_export.py --requests REQ.csv [--side FE|BE]
        [--root \\\\server\\...\\intraday] [--no-snapshot] [--check-only] [--curve ng_curve.csv]
        [--name prepay_system] [--with-model] [--paths N] [--rebuild]

REQ.csv lists the 84 request ids per side. Columns: request_id (required), side (FE / BE, else --side),
strip (0..83, else file order = strip order: strip k covers delivery months k and k+1, Jan27 = 0).
Each request's files sit in <root>\\<request_id>\\ (RiskProfile_<id>.Test DiscPosNet.csv, ...Test
DiscNegNet.csv, optional Base files, RiskProfile_<id>_SACCR_PathEADs_TT_Test.csv).

Steps: (1) preflight every request (files, dates, scenario count, Base empty, t0 PV vs the engine's
booked MTM, strip order) -> <name>_preflight.csv; (2) copy the files to
examples\\prepay\\system_export\\<name>\\<request_id>\\ (skip with --no-snapshot); (3) write <name>.xlsx with
IMPORT netting sets S84_<side>_<CSRA|NOCSRA|IM|NOIM> (+ B84 model sets with --with-model) and run it ->
<name>_results.xlsx (+ PrepaySystem sheet).

Market inputs other than --curve (vols, factors, discount rate, CSRA / IM) are the illustrative
placeholders of eqxva.commodity.prepay_examples; edit <name>.xlsx and rerun without --rebuild.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from eqxva import pathimport as pi
from eqxva.cli import run_file
from eqxva.commodity import prepay_examples as pe
from eqxva.exposure import leg_mtm0
from eqxva.io_excel import parse_inputs

HERE = Path(__file__).resolve().parent
ROOT = r"\\cmcscpdsfil01\LITES_ARCHIVE\Intraday\Tertiary\output\reports\intraday"
KINDS = ("testdiscposnet", "testdiscnegnet", "basediscposnet", "basediscnegnet", "saccr")


def read_requests(path: str, side: str | None) -> dict[str, list[str]]:
    p = Path(path)
    df = pd.read_excel(p) if p.suffix.lower() in (".xlsx", ".xls") else pd.read_csv(p, dtype=str)
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "request_id" not in df.columns:
        raise SystemExit(f"{p.name}: needs a request_id column (got {list(df.columns)})")
    df = df.dropna(subset=["request_id"]).copy()
    df["request_id"] = [str(x).strip().removesuffix(".0") for x in df["request_id"]]
    if "side" not in df.columns:
        if not side:
            raise SystemExit("No side column in the request list: pass --side FE or --side BE")
        df["side"] = side
    df["side"] = df["side"].str.strip().str.upper()
    out = {}
    for s, g in df.groupby("side", sort=False):
        if s not in ("FE", "BE"):
            raise SystemExit(f"side must be FE or BE (got {s})")
        if "strip" in g.columns:
            g = g.assign(strip=g["strip"].astype(int)).sort_values("strip")
            if list(g["strip"]) != list(range(len(g))):
                raise SystemExit(f"{s}: strip must run 0..{len(g) - 1} with no gaps or repeats")
        ids = list(g["request_id"])
        if len(ids) != 84 or len(set(ids)) != 84:
            raise SystemExit(f"{s}: expected 84 distinct request ids, got {len(ids)} ({len(set(ids))} distinct)")
        out[s] = ids
    return out


def locate(root: Path, rid: str) -> dict[str, Path | None]:
    folder = root / rid
    if not folder.is_dir():
        raise FileNotFoundError(f"request {rid}: folder not found: {folder}")
    files = {pi._norm(p.name): p for p in folder.iterdir() if p.is_file()}
    return {k: pi._find(files, rid, k) for k in KINDS}


def with_val_date(sheets, val) -> dict:
    s = dict(sheets)
    cfg = s["Config"].set_index("key")["value"].to_dict()
    cfg["valuation_date"] = val
    s["Config"] = pd.DataFrame(list(cfg.items()), columns=["key", "value"])
    return s


def model_mtm0(sheets, side: str) -> list[float]:
    """The engine's booked t0 MTM of each strip (both months, ignoring the window) on the run's curve."""
    s = pe.add_system_import(sheets, {side: [str(i) for i in range(84)]}, collateral=(False,))
    mkt, nss, _, _ = parse_inputs(s)
    ns = next(n for n in nss if n.netting_set.startswith(f"S84_{side}"))
    return [sum(leg_mtm0(l, mkt) for l in tr.legs) for tr in ns.trades]


def preflight(reqs, root: Path, base_sheets) -> tuple[pd.DataFrame, list, int]:
    rows, ref_dates, n_scen = [], None, None
    for side, ids in reqs.items():
        mtm = None
        for k, rid in enumerate(ids):
            f = locate(root, rid)
            r = dict(side=side, strip=k, request_id=rid, delivery=f"{pe.contract_name(k)}+{pe.contract_name(k + 1)}",
                     **{f"has_{k_}": f[k_] is not None for k_ in KINDS})
            pos, neg = pi._wide(f["testdiscposnet"]), pi._wide(f["testdiscnegnet"])
            if pos is None or neg is None:
                r["problem"] = "missing or empty Test DiscPosNet / DiscNegNet"
                rows.append(r)
                continue
            V = pos.to_numpy() + neg.to_numpy()
            base_nonzero = 0.0
            for b in ("basediscposnet", "basediscnegnet"):
                bb = pi._wide(f[b])
                if bb is not None:
                    base_nonzero = max(base_nonzero, float(np.abs(bb.to_numpy()).max()))
                    V = V - bb.to_numpy()
            dates = list(pos.columns)
            ref_dates = ref_dates or dates
            n_scen = n_scen or V.shape[0]
            if mtm is None:
                mtm = model_mtm0(with_val_date(base_sheets, ref_dates[0]), side)
            nz = np.where(np.abs(V).max(axis=0) > 1e-6)[0]
            sa = pi._saccr_paths(f["saccr"])
            r.update(n_scen=V.shape[0], n_dates=len(dates), first_date=dates[0], last_date=dates[-1],
                     same_grid=dates == ref_dates and V.shape[0] == n_scen, base_max_abs=base_nonzero,
                     sys_pv0_mean=float(V[:, 0].mean()), sys_pv0_std=float(V[:, 0].std()),
                     model_mtm0=mtm[k], last_nonzero_date=dates[nz[-1]] if len(nz) else None,
                     saccr_dates=0 if sa is None else len(sa), saccr_paths=0 if sa is None else int(sa.n_paths.max()),
                     saccr_ead0=np.nan if sa is None else float(sa.EAD.iloc[0]))
            r["pv0_diff"] = r["sys_pv0_mean"] - r["model_mtm0"]
            probs = []
            if not r["same_grid"]:
                probs.append("dates / scenario count differ from the first request")
            if base_nonzero > 0:
                probs.append("Base file not empty (PV = Test - Base is used)")
            if r["sys_pv0_std"] > 1e-6 * max(1.0, abs(r["sys_pv0_mean"])):
                probs.append("first-date PV varies by scenario (first column is not t0?)")
            if sa is None:
                probs.append("no SACCR_PathEADs file")
            r["problem"] = "; ".join(probs)
            rows.append(r)
    df = pd.DataFrame(rows)
    for side in reqs:                                     # strip k's PVs should stop after strip k-1's
        d = df[(df.side == side) & df.last_nonzero_date.notna()]
        if len(d) and not d.sort_values("strip").last_nonzero_date.is_monotonic_increasing:
            df.loc[df.side == side, "problem"] += "; last non-zero date not increasing with strip: check order"
    return df, ref_dates, n_scen


def snapshot(reqs, root: Path, dest: Path) -> None:
    for ids in reqs.values():
        for rid in ids:
            (dest / rid).mkdir(parents=True, exist_ok=True)
            for p in locate(root, rid).values():
                if p is not None:
                    shutil.copy2(p, dest / rid / p.name)


def build(reqs, folder_of, dates, base_sheets) -> dict:
    s = pe.add_system_import(base_sheets, reqs)
    s["SystemRequests"]["folder"] = [folder_of(r) for r in s["SystemRequests"]["request_id"]]
    cfg = s["Config"].set_index("key")["value"].to_dict()
    cfg.update(valuation_date=dates[0], buckets=",".join(d.isoformat() for d in dates[1:] if d > dates[0]),
               output_mode="POINT", event_buckets="NONE")
    s["Config"] = pd.DataFrame(list(cfg.items()), columns=["key", "value"])
    return s


def write(sheets, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)


def system_table(res) -> pd.DataFrame:
    s = res.summary.copy()
    p = res.ns_profile
    agg = p.groupby("netting_set").agg(SACCR_EAD_mean_peak=("SACCR_EAD_mean", "max"),
                                      SACCR_EAD_system_peak=("SACCR_EAD_system", "max"))
    cols = [c for c in ("netting_set", "value_source", "MTM0", "peak_EE", "EPE", "EEPE", "peak_PFE",
                        "SACCR_EAD0", "SACCR_EAD0_system", "n_paths") if c in s.columns]
    return s[cols].join(agg, on="netting_set")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requests", required=True, help="CSV / XLSX: request_id [, side] [, strip]")
    ap.add_argument("--side", choices=("FE", "BE"), default=None, help="side when the list has no side column")
    ap.add_argument("--root", default=ROOT, help="folder holding one sub-folder per request id")
    ap.add_argument("--name", default="prepay_system", help="base name of the workbook / results / snapshot")
    ap.add_argument("--no-snapshot", action="store_true", help="read the share directly (no local copy)")
    ap.add_argument("--check-only", action="store_true", help="preflight only, no run")
    ap.add_argument("--curve", default=None, help="forward curve CSV (month,price); match the system's curve")
    ap.add_argument("--with-model", action="store_true", help="also run the engine's own B84 sets side by side")
    ap.add_argument("--paths", type=int, default=2000, help="paths for the --with-model sets")
    ap.add_argument("--rebuild", action="store_true", help="rewrite <name>.xlsx even if it exists")
    ap.add_argument("--no-analytic", action="store_true")
    a = ap.parse_args(argv)

    reqs = read_requests(a.requests, a.side)
    root = Path(a.root)
    kw = dict(price=pe.curve_prices(pe.load_curve(a.curve))) if a.curve else {}
    base = pe.example_sheets(scenarios=("B84",) if a.with_model else (), sides=tuple(reqs), n_paths=a.paths, **kw)

    print(f"Preflight: {sum(map(len, reqs.values()))} requests under {root}")
    try:
        chk, dates, n_scen = preflight(reqs, root, base)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"Preflight failed: {e}")
    chk_path = HERE / f"{a.name}_preflight.csv"
    chk.to_csv(chk_path, index=False)
    bad = chk[chk.problem.fillna("") != ""]
    print(f"  {n_scen} scenarios, {len(dates or [])} dates {dates[0] if dates else ''} .. {dates[-1] if dates else ''}")
    print(f"  max |system PV0 - engine MTM0| = {chk.pv0_diff.abs().max():,.0f} USD; details in {chk_path}")
    for _, r in bad.iterrows():
        print(f"  {r.side} strip {r.strip:2d} request {r.request_id}: {r.problem}")
    fatal = chk.problem.fillna("").str.contains("missing|differ")
    if fatal.any():
        sys.exit("Preflight failed: fix the requests above before running")
    if a.check_only:
        return

    if a.no_snapshot:
        folder_of = lambda rid: str(root / rid)
    else:
        dest = HERE / "system_export" / a.name
        print(f"Copying files to {dest}")
        snapshot(reqs, root, dest)
        folder_of = lambda rid: str(Path("system_export") / a.name / rid)

    inp = HERE / f"{a.name}.xlsx"
    if a.rebuild or not inp.exists():
        write(build(reqs, folder_of, dates, base), inp)
        print(f"wrote {inp}")
    else:
        print(f"using existing {inp} (pass --rebuild to regenerate)")
    out = HERE / f"{a.name}_results.xlsx"
    t0 = time.time()
    res = run_file(inp, out, analytic=not a.no_analytic)
    tbl = system_table(res)
    with pd.ExcelWriter(out, engine="openpyxl", mode="a", if_sheet_exists="replace") as xw:
        tbl.to_excel(xw, sheet_name="PrepaySystem", index=False)
        chk.to_excel(xw, sheet_name="Preflight", index=False)
    pd.set_option("display.width", 220)
    show = tbl.copy()
    num = show.select_dtypes("number").columns.drop("n_paths", errors="ignore")
    show[num] = show[num] / 1e6
    print("USD mm (illustrative market inputs apart from the imported PVs)")
    print(show.round(3).to_string(index=False))
    for w in res.warnings:
        print("WARNING:", w)
    print(f"\nResults written to {out} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
