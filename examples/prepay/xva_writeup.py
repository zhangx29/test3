"""Gas prepay: turn eqxva results into the tables of the Claude Doc "Gas Prepay Swaps - XVA Pricing".

    .venv\\Scripts\\python examples\\prepay\\xva_writeup.py RESULTS.xlsx [MORE_RESULTS.xlsx ...]
        [--case "To reset (W7)=W7_FE_NOCSRA:W7_BE_IMVM:W7_BE_IM" ...]
        [--capital "Two-month proxy, M to Dec 2033 (W7)=W7_FE_NOCSRA:W7_BE_IM" ...]
        [--stress "Full strip to Dec 2033 (F7)=F7_BE_NOIM" ...]
        [--out BASE] [credit / spread options, see --help]

Writes BASE_report.docx (the full write-up in Word, see xva_report.py; needs pip install -e .[report]),
BASE_writeup.md (one Markdown table per doc section, in doc order), BASE_writeup.xlsx (the same
tables plus the full numbers) and BASE_pfe_annual.csv (chart data: highest monthly gross PFE each year).
BASE defaults to the first results file without .xlsx.

Deal setup (the defaults):
- Front-end vs the Issuer: unmargined, no CSRA  -> *_FE_NOCSRA sets.
- Back-end vs the SPV: one-way CSA, BMO posts VM at zero threshold, SPV posts static IM ->
  exposure, CVA and FCA from *_BE_IMVM sets (run_prepay_scenarios.py --vm-posting);
  SA-CCR from *_BE_IM sets (a CSA where only the bank posts counts as unmargined, CAR ch.7 para 94/107).
- System import: pass e.g. --case "System, to reset=S84_FE_NOCSRA:S84_BE_IM:S84_BE_IM" (no IMVM import set;
  FCA then falls back to -NEE_uncoll, the VM BMO would post at zero threshold without MPOR lag).

A results file can come from run_prepay_scenarios.py or run_system_export.py; several files are merged,
so the VM run and the main run can be passed together. Sets that are missing are skipped with a note.
CVA, lost spread, FCA and RWA are computed here from the EE_NEE / Summary / SACCR_Proxy sheets; the
engine itself stops at exposure and EAD. Credit inputs default to ILLUSTRATIVE placeholders.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ND = NormalDist()
DEF_CASES = ["To reset (W7)=W7_FE_NOCSRA:W7_BE_IMVM:W7_BE_IM",
             "To Dec 2057 (W30)=W30_FE_NOCSRA:W30_BE_IMVM:W30_BE_IM"]
DEF_CAPITAL = ["Two-month proxy, M to Dec 2033 (W7)=W7_FE_NOCSRA:W7_BE_IM",
               "Two-month proxy, M to Dec 2057 (W30)=W30_FE_NOCSRA:W30_BE_IM",
               "Full notional, hard stop Dec 2033 (F7)=F7_FE_NOCSRA:F7_BE_IM",
               "Full notional, amortization-weighted, Dec 2057 (F30A)=F30A_FE_NOCSRA:F30A_BE_IM",
               "Literal: full notional to Dec 2057 (F30)=F30_FE_NOCSRA:F30_BE_IM"]
DEF_STRESS = ["Full strip to Dec 2033 (F7)=F7_BE_NOIM", "Full strip to Dec 2057 (F30)=F30_BE_NOIM"]


def parse_pairs(items, n):
    out = []
    for s in items:
        label, sets = s.split("=", 1)
        parts = sets.split(":")
        parts += [parts[-1]] * (n - len(parts))
        out.append((label.strip(), *[p.strip() for p in parts[:n]]))
    return out


IN_SHEETS = ("in_Trades", "in_NettingSets", "in_Contracts", "in_Config", "in_Curve")


def load(files):
    tabs = {k: [] for k in ("Summary", "EE_NEE", "SACCR_Proxy") + IN_SHEETS}
    for f in files:
        x = pd.read_excel(f, sheet_name=None)
        for k in tabs:
            if k in x:
                tabs[k].append(x[k])
    S = pd.concat(tabs["Summary"]).drop_duplicates("netting_set", keep="last").set_index("netting_set")
    P = pd.concat(tabs["EE_NEE"]).drop_duplicates(["netting_set", "t"], keep="last").sort_values(["netting_set", "t"])
    PX = pd.concat(tabs["SACCR_Proxy"]) if tabs["SACCR_Proxy"] else pd.DataFrame(columns=["netting_set", "M_trade"])
    keys = {"in_Trades": ["netting_set", "trade_id", "leg_id"], "in_NettingSets": ["netting_set"],
            "in_Contracts": ["contract"], "in_Config": ["key"], "in_Curve": ["tenor"]}
    inputs = {}
    for k in IN_SHEETS:
        if tabs[k]:
            df = pd.concat(tabs[k])
            cols = [c for c in keys[k] if c in df.columns]
            inputs[k] = df.drop_duplicates(cols, keep="last") if cols else df
    return S, P, PX, inputs


def _price_pieces(legs):
    """'$5.035 Jan27-Dec33, $5.00 Jan34-Dec57' from a strip's legs."""
    legs = legs.sort_values("fixing_date")
    out, start, prev, k = [], None, None, None
    for d, p in zip(legs.fixing_date, legs.fixed_price.round(4)):
        if k is None or p != k:
            if k is not None:
                out.append(f"${k:.3f}".rstrip("0").rstrip(".") + f" {start:%b%y}–{prev:%b%y}")
            start, k = d, p
        prev = d
    if k is not None:
        out.append(f"${k:.3f}".rstrip("0").rstrip(".") + f" {start:%b%y}–{prev:%b%y}")
    return ", ".join(x.replace("$5 ", "$5.00 ") for x in out)


def deal_info(inputs, a, fe_sets, be_sets, im_set):
    """Deal terms read from the copied input sheets; falls back to the CLI options where a sheet is missing."""
    d = dict(val_date=a.valuation_date)
    cfg = inputs.get("in_Config")
    if cfg is not None:
        kv = dict(zip(cfg.key, cfg.value))
        d.update(val_date=str(kv.get("valuation_date", a.valuation_date))[:10], n_paths=kv.get("n_paths"), seed=kv.get("seed"))
    ns = inputs.get("in_NettingSets")
    if ns is not None and im_set in set(ns.netting_set):
        d["im"] = float(ns.set_index("netting_set").ia_received.get(im_set, 0) or 0) / 1e6
    tr = inputs.get("in_Trades")
    if tr is not None:
        tr = tr.copy()
        tr["fixing_date"] = pd.to_datetime(tr.fixing_date)
        tr["payment_date"] = pd.to_datetime(tr.payment_date)
        # longest strips available for the description and the spread
        fe = max((tr[tr.netting_set == s] for s in fe_sets), key=len, default=pd.DataFrame())
        be = max((tr[tr.netting_set == s] for s in be_sets), key=len, default=pd.DataFrame())
        if len(fe) and len(be):
            fe, be = fe.drop_duplicates("fixing_date"), be.drop_duplicates("fixing_date")
            d["volume"] = float((fe.quantity / fe.fixing_date.dt.days_in_month).median())
            d["k_front_txt"], d["k_back_txt"] = _price_pieces(fe), _price_pieces(be)
            m = be.merge(fe[["fixing_date", "fixed_price"]], on="fixing_date", suffixes=("", "_fe"))
            m["spread"] = m.fixed_price - m.fixed_price_fe
            live = m[m.spread > 1e-9]
            if len(live):
                d["spread_usd"] = float(live.spread.median())
                d["reset_year"] = int(live.fixing_date.max().year)
                d["spread_legs"] = live[["payment_date", "quantity", "spread"]]
                yrs = max((live.fixing_date.max() - live.fixing_date.min()).days / 365.25 + 1 / 12, 1 / 12)
                d["spread_yr"] = float((live.spread * live.quantity).sum() / 1e6 / yrs)
            d["window_max"] = float(2 * 31 * d["volume"] * be.fixed_price.max() / 1e6)
    ct = inputs.get("in_Contracts")
    if ct is not None:
        ct = ct.copy()
        ct["expiry"] = pd.to_datetime(ct.expiry)
        txt = f"${ct.price.min():.2f}–{ct.price.max():.2f} across {len(ct)} contracts"
        if d.get("reset_year"):
            pre = ct[ct.expiry.dt.year <= d["reset_year"]]
            txt += f" (${pre.price.min():.2f}–{pre.price.max():.2f} to Dec {d['reset_year']})"
        d["curve_txt"] = txt
    cv = inputs.get("in_Curve")
    if cv is not None and "zero_rate" in cv:
        d["rate"] = float(cv.zero_rate.mean())
    return d


class Calc:
    def __init__(self, a, S, P, PX):
        self.a, self.S, self.P = a, S, P
        self.M = PX.groupby("netting_set").M_trade.max() if len(PX) else pd.Series(dtype=float)
        self.spread = {"ISSUER": a.spread_issuer / 1e4, "SPV": a.spread_spv / 1e4}
        self.notes = []

    def has(self, ns):
        ok = ns in self.S.index and (self.P.netting_set == ns).any()
        if not ok:
            self.notes.append(f"netting set {ns} not in the results - skipped")
        return ok

    def surv(self, c, t):
        return np.exp(-self.spread[c] / self.a.lgd * np.asarray(t, float))

    def g(self, ns):
        return self.P[self.P.netting_set == ns]

    def cva(self, ns, c, col="EE"):
        g = self.g(ns)
        t = g.t.values
        tp = np.r_[0.0, t[:-1]]
        return self.a.lgd * float((g.df.values * g[col].values / 1e6 * (self.surv(c, tp) - self.surv(c, t))).sum())

    def vm_posted(self, ns):
        g = self.g(ns)
        if "E_VM_posted" in g and g.E_VM_posted.abs().sum() > 0:
            return g.E_VM_posted.values / 1e6, "E_VM_posted"
        return -g.NEE_uncoll.values / 1e6, "-NEE_uncoll (no CSA in this set)"

    def fca(self, ns):
        g = self.g(ns)
        t = g.t.values
        vm, src = self.vm_posted(ns)
        joint = self.surv("ISSUER", t) * self.surv("SPV", t)
        return self.a.funding / 1e4 * float((g.df.values * vm * np.diff(np.r_[0.0, t]) * joint).sum()), float(vm.max()), src

    def exposure(self, ns, c):
        r, g = self.S.loc[ns], self.g(ns)
        pk, pp = g.loc[g.EE.idxmax()], g.loc[g.PFE.idxmax()]
        # peak values from Summary (fine grid incl. the day before each settlement); month from the buckets
        return dict(netting_set=ns, MTM0=r.MTM0 / 1e6, peak_EE=r.peak_EE / 1e6,
                    peak_EE_date=str(pk.bucket_date)[:7] if g.EE.max() > 5e3 else "",
                    peak_PFE=max(r.peak_PFE, 0) / 1e6,
                    peak_PFE_date=str(pp.bucket_date)[:7] if g.PFE.max() > 5e3 else "",
                    EPE_1y=r.EPE / 1e6, peak_EE_gross=g.EE_uncoll.max() / 1e6,
                    peak_PFE_gross=g.PFE_uncoll.max() / 1e6, CVA=self.cva(ns, c),
                    EAD0=r.SACCR_EAD0 / 1e6,
                    EAD0_system=(r.SACCR_EAD0_system / 1e6 if "SACCR_EAD0_system" in r and pd.notna(r.get("SACCR_EAD0_system")) else np.nan))

    def irb_k(self, pd_, M):
        pd_ = max(pd_, 0.0005)
        w = (1 - math.exp(-50 * pd_)) / (1 - math.exp(-50))
        R = 0.12 * w + 0.24 * (1 - w)
        b = (0.11852 - 0.05478 * math.log(pd_)) ** 2
        M = min(max(M, 1.0), 5.0)
        k = self.a.lgd_irb * ND.cdf((ND.inv_cdf(pd_) + math.sqrt(R) * ND.inv_cdf(0.999)) / math.sqrt(1 - R)) - pd_ * self.a.lgd_irb
        return k * (1 + (M - 2.5) * b) / (1 - 1.5 * b)

    def scva(self, rw, ead, M):
        M = max(M, 1.0)
        return rw * M * ead * (1 - math.exp(-0.05 * M)) / (0.05 * M) / 1.4

    def capital(self, fe, be):
        a = self.a
        e_fe, e_be = self.S.loc[fe].SACCR_EAD0 / 1e6, self.S.loc[be].SACCR_EAD0 / 1e6
        m_fe, m_be = self.M.get(fe, self.S.loc[fe].maturity_yrs), self.M.get(be, self.S.loc[be].maturity_yrs)
        ccr = 12.5 * (self.irb_k(a.pd_issuer / 100, m_fe) * e_fe + self.irb_k(a.pd_spv / 100, m_be) * e_be)
        sv = [self.scva(a.rw_issuer / 100, e_fe, m_fe), self.scva(a.rw_spv / 100, e_be, m_be)]
        cva = 12.5 * 0.65 * math.sqrt((0.5 * sum(sv)) ** 2 + 0.75 * sum(v * v for v in sv))
        tot = ccr + cva
        return dict(EAD_Issuer=e_fe, EAD_SPV=e_be, M=m_fe, CCR_RWA=ccr, BA_CVA_RWA=cva, Total_RWA=tot,
                    capital_cost_yr=tot * a.cap_ratio / 100 * a.hurdle / 100)

    def lost_spread(self, deal=None):
        """Spread PV and the part lost at either default. From the booked legs (back-end minus front-end
        fixed price, back-end quantity, paid on the payment date) when the input sheets are in the results;
        otherwise from --volume / --spread-usd / --spread-start / --spread-end."""
        a = self.a
        val = pd.Timestamp(deal.get("val_date", a.valuation_date) if deal else a.valuation_date)
        rate = deal.get("rate", a.rate / 100) if deal else a.rate / 100
        if deal and "spread_legs" in deal:
            lg = deal["spread_legs"]
            tp = ((lg.payment_date - val).dt.days / 365.25).values
            pv = (lg.spread * lg.quantity / 1e6).values * np.exp(-rate * tp)
        else:
            months = pd.date_range(a.spread_start, a.spread_end, freq="MS")
            q = np.array([((m + pd.offsets.MonthBegin(1)) - m).days * a.volume / 1e6 for m in months])
            tp = np.array([((m + pd.offsets.MonthBegin(1)) - val).days / 365.25 for m in months])
            pv = a.spread_usd * q * np.exp(-rate * tp)
        return float(pv.sum()), float((pv * (1 - self.surv("ISSUER", tp) * self.surv("SPV", tp))).sum())


def f(x, d=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return f"{x:,.{d}f}"


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def with_date(v, d, dec=2):
    return f(v, dec) + (f" ({d})" if d else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", nargs="+")
    ap.add_argument("--case", action="append", help="label=FE_SET:BE_EXPOSURE_SET[:BE_SACCR_SET]")
    ap.add_argument("--capital", action="append", help="label=FE_SET:BE_SET")
    ap.add_argument("--stress", action="append", help="label=BE_SET valued without extinguishment")
    ap.add_argument("--out", default=None)
    ap.add_argument("--spread-issuer", type=float, default=50, help="bp (illustrative)")
    ap.add_argument("--spread-spv", type=float, default=100, help="bp (illustrative)")
    ap.add_argument("--lgd", type=float, default=0.60, help="CVA LGD")
    ap.add_argument("--funding", type=float, default=50, help="funding spread on VM posted, bp")
    ap.add_argument("--pd-issuer", type=float, default=0.10, help="IRB PD, %%")
    ap.add_argument("--pd-spv", type=float, default=0.30, help="IRB PD, %%")
    ap.add_argument("--lgd-irb", type=float, default=0.45)
    ap.add_argument("--rw-issuer", type=float, default=1.0, help="BA-CVA risk weight, %%")
    ap.add_argument("--rw-spv", type=float, default=5.0, help="BA-CVA risk weight, %%")
    ap.add_argument("--cap-ratio", type=float, default=11.5, help="capital / RWA, %%")
    ap.add_argument("--hurdle", type=float, default=10.0, help="cost of capital, %%")
    ap.add_argument("--status", default="Illustrative: vols, factor parameters, the curve beyond listed contracts, "
                    "the IM size and the credit inputs are placeholders.",
                    help="one sentence on input status, printed in the report's summary and caveats")
    ap.add_argument("--no-docx", action="store_true", help="skip the Word report")
    g = ap.add_argument_group("fallbacks, used only when the results lack the in_* input sheets")
    g.add_argument("--valuation-date", default="2026-11-13")
    g.add_argument("--volume", type=float, default=20000, help="MMBtu/d")
    g.add_argument("--spread-usd", type=float, default=0.035, help="BMO spread $/MMBtu")
    g.add_argument("--spread-start", default="2027-01-01")
    g.add_argument("--spread-end", default="2033-12-01", help="last month that earns the spread")
    g.add_argument("--rate", type=float, default=4.0, help="flat discount rate for the spread PV, %%")
    a = ap.parse_args(argv)

    S, P, PX, inputs = load(a.results)
    c = Calc(a, S, P, PX)
    cases = [x for x in parse_pairs(a.case or DEF_CASES, 3) if all(c.has(s) for s in x[1:])]
    caps = [x for x in parse_pairs(a.capital or DEF_CAPITAL, 2) if all(c.has(s) for s in x[1:])]
    stress = [x for x in parse_pairs(a.stress or DEF_STRESS, 1) if c.has(x[1])]
    base = Path(a.out) if a.out else Path(a.results[0]).with_suffix("")
    deal = deal_info(inputs, a, [x[1] for x in cases] + [x[1] for x in caps],
                     [x[2] for x in cases] + [x[2] for x in caps], cases[-1][2] if cases else "")
    if not inputs:
        c.notes.append("no in_* input sheets in the results: deal terms and spread from the fallback options")
    spread_pv, lost = c.lost_spread(deal)

    md, xl = [f"# XVA write-up tables\n\nSource: {', '.join(Path(r).name for r in a.results)}. USD mm. "
              f"Credit inputs: Issuer {a.spread_issuer:g}bp, SPV {a.spread_spv:g}bp, LGD {a.lgd:.0%}, funding "
              f"{a.funding:g}bp; IRB PD {a.pd_issuer:g}% / {a.pd_spv:g}%, LGD {a.lgd_irb:.0%}; BA-CVA RW "
              f"{a.rw_issuer:g}% / {a.rw_spv:g}%; capital {a.cap_ratio:g}% x hurdle {a.hurdle:g}%."], {}

    # exposure per case
    fe_rows, be_rows, full = [], [], []
    for label, fe, be, be_sa in cases:
        e = c.exposure(fe, "ISSUER")
        b = c.exposure(be, "SPV")
        b_sa = c.exposure(be_sa, "SPV") if be_sa != be else b
        fca, vm_pk, src = c.fca(be)
        fe_rows.append((label, e))
        be_rows.append((label, b, b_sa, fca, vm_pk, src))
        full += [dict(case=label, side="Issuer", **e), dict(case=label, side="SPV", **b, FCA=fca, peak_VM_posted=vm_pk,
                                                             EAD0_saccr_set=b_sa["EAD0"], vm_source=src)]
        if not src.startswith("E_VM"):
            c.notes.append(f"{be}: FCA from {src}")
    hdr = ["USD mm"] + [l for l, _ in fe_rows]
    sys_fe = any(pd.notna(e["EAD0_system"]) for _, e in fe_rows)
    rows = [["Window MTM at t0"] + [f(e["MTM0"]) for _, e in fe_rows],
            ["Peak EE (date)"] + [with_date(e["peak_EE"], e["peak_EE_date"]) for _, e in fe_rows],
            ["Peak PFE 97.5% (date)"] + [with_date(e["peak_PFE"], e["peak_PFE_date"]) for _, e in fe_rows],
            ["EPE, first year"] + [f(e["EPE_1y"], 3) for _, e in fe_rows],
            ["SA-CCR EAD t0"] + [f(e["EAD0"]) for _, e in fe_rows]]
    if sys_fe:
        rows.append(["SA-CCR EAD t0, system"] + [f(e["EAD0_system"]) for _, e in fe_rows])
    rows.append([f"CVA (Issuer {a.spread_issuer:g}bp, LGD {a.lgd:.0%})"] + [f(e["CVA"], 4) for _, e in fe_rows])
    md.append("## Results facing the Municipal Issuer (front-end)\n\n" + md_table(hdr, rows))
    rows = [["Window MTM at t0"] + [f(b["MTM0"]) for _, b, *_ in be_rows],
            ["Peak EE (date)"] + [with_date(b["peak_EE"], b["peak_EE_date"]) for _, b, *_ in be_rows],
            ["Peak PFE 97.5% (date)"] + [with_date(b["peak_PFE"], b["peak_PFE_date"]) for _, b, *_ in be_rows],
            ["Peak EE / PFE before IM and VM"] + [f"{f(b['peak_EE_gross'])} / {f(b['peak_PFE_gross'])}" for _, b, *_ in be_rows],
            ["EPE, first year"] + [f(b["EPE_1y"], 3) for _, b, *_ in be_rows],
            ["SA-CCR EAD t0 (unmargined, IM)"] + [f(bs["EAD0"]) for _, _, bs, *_ in be_rows]]
    if any(pd.notna(bs["EAD0_system"]) for _, _, bs, *_ in be_rows):
        rows.append(["SA-CCR EAD t0, system"] + [f(bs["EAD0_system"]) for _, _, bs, *_ in be_rows])
    rows += [[f"CVA (SPV {a.spread_spv:g}bp, LGD {a.lgd:.0%})"] + [f(b["CVA"], 4) for _, b, *_ in be_rows],
             ["Peak expected VM posted by BMO"] + [f(r[4]) for r in be_rows],
             [f"FCA on VM posted ({a.funding:g}bp)"] + [f(r[3], 4) for r in be_rows]]
    md.append("## Results facing the Gas Supplier SPV (back-end)\n\n" + md_table(hdr, rows))

    # XVA summary
    rows = [["Issuer CVA, unmargined"] + [f(e["CVA"], 4) for _, e in fe_rows],
            ["SPV CVA, net of IM and VM"] + [f(b["CVA"], 4) for _, b, *_ in be_rows],
            ["Lost spread"] + [f(lost, 4) for _ in fe_rows],
            ["FCA on VM posted to SPV"] + [f(r[3], 4) for r in be_rows],
            ["Total"] + [f(e["CVA"] + r[1]["CVA"] + lost + r[3], 3) for (_, e), r in zip(fe_rows, be_rows)]]
    md.append(f"## XVA terms (spread PV {f(spread_pv)})\n\n" + md_table(["XVA term, USD mm PV"] + hdr[1:], rows))
    xl["XVA"] = pd.DataFrame([dict(case=l, issuer_cva=e["CVA"], spv_cva=r[1]["CVA"], lost_spread=lost, fca=r[3],
                                   total=e["CVA"] + r[1]["CVA"] + lost + r[3], spread_pv=spread_pv)
                              for (l, e), r in zip(fe_rows, be_rows)])

    # capital
    cap_rows = []
    for label, fe, be in caps:
        k = c.capital(fe, be)
        cap_rows.append(dict(treatment=label, fe_set=fe, be_set=be, **k))
    if cap_rows:
        md.append("## SA-CCR EAD and capital\n\n" + md_table(
            ["USD mm, Issuer unmargined, SPV IM", "EAD Issuer", "EAD SPV", "CCR RWA", "BA-CVA RWA", "Total RWA",
             "Capital cost a year, indicative"],
            [[r["treatment"], f(r["EAD_Issuer"]), f(r["EAD_SPV"]), f(r["CCR_RWA"]), f(r["BA_CVA_RWA"]),
              f(r["Total_RWA"]), f(r["capital_cost_yr"], 3)] for r in cap_rows]))
        xl["Capital"] = pd.DataFrame(cap_rows)

    # stress: extinguishment fails, SPV full strip
    if stress:
        st = [dict(stress=l, set=ns, **c.exposure(ns, "SPV")) for l, ns in stress]
        md.append("## Stress: cross-extinguishment fails (SPV full strip, before IM)\n\n" + md_table(
            ["Case", "MTM t0", "Peak EE", "CVA"], [[s["stress"], f(s["MTM0"], 1), f(s["peak_EE"], 1), f(s["CVA"], 2)] for s in st]))
        xl["Stress"] = pd.DataFrame(st)

    # chart data: highest monthly gross PFE per year, last case
    ann = None
    if cases:
        label, fe, be, _ = cases[-1]
        pr = P[P.netting_set.isin([fe, be])].copy()
        pr["year"] = pd.to_datetime(pr.bucket_date).dt.year
        pr["side"] = np.where(pr.netting_set == fe, "Issuer", "SPV")
        ann = (pr.groupby(["side", "year"]).agg(pfe=("PFE_uncoll", "max"), ee=("EE_uncoll", "max")) / 1e6).reset_index()
        ann = ann[ann.pfe > 0]
        ann.round(2).to_csv(f"{base}_pfe_annual.csv", index=False)
        md.append(f"## Chart data ({label}: highest monthly gross PFE 97.5% each year)\n\n```csv\n"
                  + ann.round(2).to_csv(index=False) + "```")
    if c.notes:
        md.append("## Notes\n\n" + "\n".join(f"- {n}" for n in dict.fromkeys(c.notes)))

    Path(f"{base}_writeup.md").write_text("\n\n".join(md) + "\n", encoding="utf-8")
    with pd.ExcelWriter(f"{base}_writeup.xlsx", engine="openpyxl") as xw:
        pd.DataFrame(full).to_excel(xw, sheet_name="Exposure", index=False)
        for k, v in xl.items():
            v.to_excel(xw, sheet_name=k, index=False)
    print(Path(f"{base}_writeup.md").read_text(encoding="utf-8"))
    print(f"wrote {base}_writeup.md, {base}_writeup.xlsx" + (f", {base}_pfe_annual.csv" if cases else ""))

    if a.no_docx or not cases:
        return
    try:
        import xva_report
    except ImportError as e:
        print(f"Word report skipped ({e}). Install with: .venv\\Scripts\\python -m pip install -e .[report]")
        return
    obs_84 = None
    if all(n in S.index for n in ("N84_BE_IM", "F7_BE_IM", "B84_FE_NOCSRA", "W7_FE_NOCSRA")):
        r = S.loc["N84_BE_IM"].peak_EE / S.loc["F7_BE_IM"].peak_EE
        obs_84 = (f"The summed booking gives about {r:.1f}× the strip (SPV peak EE "
                  f"{S.loc['N84_BE_IM'].peak_EE / 1e6:,.0f} against {S.loc['F7_BE_IM'].peak_EE / 1e6:,.0f}). The windowed "
                  f"booking reproduces the window: Issuer peak EE {S.loc['B84_FE_NOCSRA'].peak_EE / 1e6:.2f} against "
                  f"{S.loc['W7_FE_NOCSRA'].peak_EE / 1e6:.2f}.")
    R = dict(args=a, deal=deal, sources=[Path(r).name for r in a.results], spread_pv=spread_pv, lost=lost,
             stress=st if stress else [], caps=cap_rows, annual=ann, notes=list(dict.fromkeys(c.notes)), obs_84=obs_84,
             cases=[dict(label=l, fe=e, be=r[1], be_sa=r[2], fca=r[3], vm_peak=r[4],
                         total_xva=e["CVA"] + r[1]["CVA"] + lost + r[3]) for (l, e), r in zip(fe_rows, be_rows)])
    png = Path(f"{base}_pfe_chart.png")
    try:
        xva_report.chart_png(R, png)
        R["chart"] = png
    except Exception as e:  # report without the chart rather than nothing
        print(f"chart skipped: {e}")
    xva_report.build(R, f"{base}_report.docx")
    print(f"wrote {base}_report.docx")


if __name__ == "__main__":
    main()
