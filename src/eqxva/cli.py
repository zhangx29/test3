"""Command line: python -m eqxva run inputs.xlsx --out results.xlsx [--analytic] [--paths N]
                 python -m eqxva template example_inputs.xlsx"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from .analytic import run_analytic
from .exposure import run_monte_carlo
from .io_excel import parse_inputs, read_workbook, write_results


def run_file(inp: str | Path, out: str | Path | None = None, analytic: bool = True,
             n_paths: int | None = None, seed: int | None = None):
    sheets = read_workbook(inp)
    mkt, nss, cfg, warnings = parse_inputs(sheets)
    if n_paths:
        cfg.n_paths = n_paths
    if seed is not None:
        cfg.seed = seed
    res = run_monte_carlo(nss, mkt, cfg, warnings)
    if analytic:
        res.analytic = run_analytic(nss, mkt, cfg, res)
    if out:
        write_results(res, out, sheets)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="eqxva")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run exposure calculation on an input workbook")
    r.add_argument("inputs")
    r.add_argument("--out", default=None)
    r.add_argument("--no-analytic", action="store_true")
    r.add_argument("--paths", type=int, default=None)
    r.add_argument("--seed", type=int, default=None)
    t = sub.add_parser("template", help="write an example input workbook")
    t.add_argument("path")
    a = ap.parse_args(argv)
    if a.cmd == "template":
        from .examples import write_example_inputs
        print(write_example_inputs(a.path))
        return
    out = a.out or str(Path(a.inputs).with_name(Path(a.inputs).stem + "_results.xlsx"))
    t0 = time.time()
    res = run_file(a.inputs, out, not a.no_analytic, a.paths, a.seed)
    print(res.summary.to_string(index=False))
    for w in res.warnings:
        print("WARNING:", w)
    print(f"\nResults written to {out} ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
