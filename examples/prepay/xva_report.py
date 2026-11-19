"""Word report for the gas prepay XVA write-up, built from the numbers xva_writeup.py computes.

Called by xva_writeup.py (default on; --no-docx skips it). Needs python-docx and matplotlib:
    .venv\\Scripts\\python -m pip install -e .[report]

The text follows the Claude Doc "Gas Prepay Swaps - XVA Pricing" (18 Nov 2026). Sentences that depend on the
results are computed or switched on the numbers (e.g. whether the IM covers the SPV window), but the prose is a
template: read the generated report before sending it on.
"""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd


def _f(x, d=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:,.{d}f}"


def _mm(x, d=2):
    return f"${_f(x, d)}mm"


def chart_png(R, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ann, deal = R["annual"], R["deal"]
    fig, ax = plt.subplots(figsize=(7.5, 3.8), dpi=200)
    blue, grey = "#2f6fd6", "#9a9a92"
    iss, spv = ann[ann.side == "Issuer"], ann[ann.side == "SPV"]
    ax.plot(spv.year, spv.pfe, color=grey, lw=2, marker="o", ms=3, label="SPV window")
    ax.plot(iss.year, iss.pfe, color=blue, lw=2, marker="o", ms=3, label="Issuer window")
    if deal.get("im"):
        ax.axhline(deal["im"], color="#555555", ls="--", lw=1)
        ax.text(spv.year.max(), deal["im"], f"  SPV IM ${deal['im']:.2f}mm", va="bottom", ha="right", fontsize=8, color="#555555")
    if deal.get("reset_year"):
        ax.axvline(deal["reset_year"] + 0.95, color="#999999", ls=":", lw=1)
        ax.text(deal["reset_year"] + 1.1, ax.get_ylim()[1] * 0.97, f"Reset, Dec {deal['reset_year']}", fontsize=8,
                color="#555555", va="top")
    if len(iss):
        pk = iss.loc[iss.pfe.idxmax()]
        ax.annotate(f"{pk.pfe:.2f} in {int(pk.year)}", (pk.year, pk.pfe), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8)
    ax.set_ylabel("USD mm")
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color="#e6e6e6")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    ax.set_title("Two-month window, PFE 97.5% before collateral, highest month each year", fontsize=9, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def build(R, path):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    a, deal, cases, caps = R["args"], R["deal"], R["cases"], R["caps"]
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = Inches(0.9)

    def para(text="", bold_lead=None, style=None, italic=False):
        p = doc.add_paragraph(style=style)
        if bold_lead:
            p.add_run(bold_lead + " ").bold = True
        r = p.add_run(text)
        r.italic = italic
        return p

    def bullet(lead, text, numbered=False):
        return para(text, lead, "List Number" if numbered else "List Bullet")

    def table(header, rows, widths=None):
        t = doc.add_table(rows=1, cols=len(header))
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(header):
            c = t.rows[0].cells[i]
            c.text = ""
            c.paragraphs[0].add_run(str(h)).bold = True
        for r in rows:
            cells = t.add_row().cells
            for i, v in enumerate(r):
                cells[i].text = str(v)
        for row in t.rows:
            for c in row.cells:
                for p in c.paragraphs:
                    for run in p.runs:
                        run.font.size = Pt(9)
        doc.add_paragraph()
        return t

    labels = [c["label"] for c in cases]
    first, last = cases[0], cases[-1]
    join = lambda vals: " / ".join(vals)
    vol = deal.get("volume")
    im = deal.get("im")
    status = a.status

    # ------------------------------------------------------------------ title
    doc.add_heading("Gas Prepay Swaps — XVA Pricing", 0)
    para(f"As of {deal.get('val_date', a.valuation_date)}. Source: {', '.join(R['sources'])}.", italic=True)

    # ------------------------------------------------------------------ executive summary
    doc.add_heading("Executive summary", 1)
    tot = [c["total_xva"] for c in cases]
    caps_proxy = [c for c in caps if "proxy" in c["treatment"].lower()]
    caps_full = [c for c in caps if "proxy" not in c["treatment"].lower()]
    rng = lambda xs, d=2: (_mm(min(xs), d) if min(xs) == max(xs) else f"${_f(min(xs), d)}–{_f(max(xs), d)}mm")
    lead = (f"On the two-month window basis, the XVA on the prepay swaps is about {rng(tot)} against a "
            f"{_mm(R['spread_pv'])} spread PV.")
    if caps_full:
        lead += f" Capital, not XVA, decides the trade: the full-notional SA-CCR readings cost {rng([c['Total_RWA'] for c in caps_full], 0)} of RWA."
    lead += (" The front-end with the Issuer is unmargined, and BMO does not rely on the CSRA. The back-end with the SPV "
             "has a one-way CSA: the SPV posts static IM and BMO posts VM.")
    if vol:
        lead += f" Volume {vol:,.0f} MMBtu/d."
    para(lead + " " + status)
    hdr = ["USD mm, " + join(labels), "Issuer (front-end, unmargined)", "SPV (back-end, IM + one-way VM)"]
    rows = [["BMO position on the window today",
             f"{first['fe']['MTM0']:+,.2f} ({'out of' if first['fe']['MTM0'] < 0 else 'in'} the money)",
             f"{first['be']['MTM0']:+,.2f} ({'out of' if first['be']['MTM0'] < 0 else 'in'} the money)"],
            ["Peak EE", join(_f(c["fe"]["peak_EE"]) for c in cases), join(_f(c["be"]["peak_EE"]) for c in cases)],
            ["Peak PFE 97.5%", join(_f(c["fe"]["peak_PFE"]) for c in cases), join(_f(c["be"]["peak_PFE"]) for c in cases)],
            ["CVA", join(_f(c["fe"]["CVA"], 3) for c in cases), join(_f(c["be"]["CVA"], 3) for c in cases)],
            ["SA-CCR EAD t0, two-month window", _f(first["fe"]["EAD0"]), _f(first["be_sa"]["EAD0"])]]
    if caps_full:
        lit = caps_full[-1]
        rows.append([f"SA-CCR EAD, {lit['treatment']}", _f(lit["EAD_Issuer"], 1), _f(lit["EAD_SPV"], 1)])
    table(hdr, rows)

    para("", "Highlights")
    terms = {"Lost spread": R["lost"], "Issuer CVA": last["fe"]["CVA"], "FCA on VM posted": last["fca"],
             "SPV CVA": last["be"]["CVA"]}
    top = max(terms, key=terms.get)
    bullet(f"{top} is the main XVA cost: {_mm(terms[top], 3)}.",
           "The spread stops at either party's default with no claim, and no collateral covers it." if top == "Lost spread"
           else f"Lost spread is {_mm(R['lost'], 3)}.")
    bullet("The Issuer is unsecured.",
           f"With no CSRA, the window's peak PFE is {join(_mm(c['fe']['peak_PFE']) for c in cases)} "
           f"({join(labels)}). BMO starts {_mm(abs(first['fe']['MTM0']))} "
           f"{'out of' if first['fe']['MTM0'] < 0 else 'in'} the money.")
    g = last["be"]["peak_PFE_gross"]
    if im and g < im:
        bullet("The SPV is covered by its IM.",
               f"Gross window PFE peaks at {_mm(g)} against {_mm(im)} of IM. The one-way CSA leaves a residual peak PFE of "
               f"{_mm(last['be']['peak_PFE'])} and costs {join(_mm(c['fca'], 3) for c in cases)} of funding.")
    else:
        bullet("The SPV IM does not cover the 97.5% tail.",
               f"Gross window PFE peaks at {_mm(g)} against {_mm(im) if im else 'no'} IM; peak PFE net of IM and VM is "
               f"{_mm(last['be']['peak_PFE'])}.")
    if caps_proxy and caps_full:
        bullet("Capital is the issue.",
               f"The two-month proxy costs {rng([c['Total_RWA'] for c in caps_proxy])} of RWA; full-notional readings cost "
               f"{rng([c['Total_RWA'] for c in caps_full], 0)}.")
    para("", "Caveats")
    if R["stress"]:
        s = R["stress"][-1]
        bullet("Cross-extinguishment.", f"The window result depends on it holding both ways. If it fails, SPV exposure is "
               f"the full strip: peak EE {_mm(s['peak_EE'], 0)} and CVA {_mm(s['CVA'], 1)} ({s['stress']}).")
    bullet("SPV IM.", f"{_mm(im)} decides whether SPV exposure is zero; confirm it against the term sheet." if im else
           "No IM found in the inputs.")
    bullet("Posted VM.", "Treated as not segregated. If the SPV's custodian segregates it, the SPV residual falls to zero.")
    bullet("Regulatory capital.", "The two-month notional needs Reg Cap and OSFI sign-off. Until then the literal "
           "full-notional number is Pillar 1.")
    bullet("Not yet included.", "Remarketing probability weighting, wrong-way risk and roll-gap windows.")

    # ------------------------------------------------------------------ the trade
    doc.add_heading("The trade and where the exposure comes from", 1)
    sp, ry = deal.get("spread_usd"), deal.get("reset_year")
    vol_txt = f" on {vol:,.0f} MMBtu/d" if vol else ""
    sp_txt = f"{sp * 100:.1f} cents" if sp else "the spread"
    ry_txt = f" to Dec {ry}" if ry else ""
    para(f"BMO sits between a Municipal Issuer and a Gas Supplier SPV with two matched commodity swaps{vol_txt}. "
         f"It is flat gas and earns {sp_txt}{ry_txt}: spread PV {_mm(R['spread_pv'])}.")
    table(["Swap", "Counterparty", "BMO position", "Fixed price", "BMO is owed money when", "Collateral"],
          [["Front-end", "Municipal Issuer", "Pays fixed, receives index (long gas)", deal.get("k_front_txt", ""),
            "Index > fixed", "None: unmargined; BMO does not rely on the CSRA"],
           ["Back-end", "Gas Supplier SPV", "Receives fixed, pays index (short gas)", deal.get("k_back_txt", ""),
            "Index < fixed", f"One-way CSA: SPV posts static IM ({_mm(im) if im else 'n/a'}); BMO posts VM"]])
    bullet("Cross-extinguishment.", "Any event of default ends both swaps with no termination payment. Only unpaid "
           "settlements survive: at most the missed month and the month in progress.")
    bullet("Rolling two-month term.", "Each swap has a two-month term that extends a month on each settlement, through "
           "final delivery.")
    bullet("Collateral.", "The front-end is unmargined: the CSRA stays in the bond structure, but BMO does not treat it as "
           "collateral. The back-end has a one-way CSA: the SPV posts static IM, and BMO posts VM on the two-month amount "
           "at zero threshold.")
    para("So the loss on a default is one two-month slice priced at the gas price on the default date, less the IM on "
         "the SPV side; nothing is netted on the Issuer side. It is not the MTM of the remaining strip. The two netting "
         "sets face different counterparties and cannot be netted against each other.")

    # ------------------------------------------------------------------ method
    doc.add_heading("How the XVA is priced", 1)
    para("Exposure at each date is the one two-month window that would be lost on a default then, net of collateral. "
         "For a default at time t in delivery month k, facing the Issuer:")
    p = para("E_I(t) = max( Σ_{i ∈ {k, k+1}} q_i (X_i − K_F) DF(t, T_i), 0 )", italic=True)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    para("The SPV side has the same form with (K_B − X), less the IM, plus any VM BMO posted as of t − MPoR. "
         "Here q is the monthly volume, X is the month's fixing (simulated on the path once fixed) or the simulated "
         "forward, and K the fixed price.")
    table(["Element", "Treatment"],
          [["Price model", "eqxva two-factor NG model on the input forward curve"],
           ["Window", "Strip booked to final delivery, each monthly leg live only inside its two-month window (FWD: months "
                      "k, k+1); the netting-set sum of windowed legs is the SELECT"],
           ["Collateral", "Issuer: none. SPV: static IM plus VM posted by BMO at zero threshold, 10-day MPoR; posted VM "
                          "not segregated"],
           ["Horizon", "One column per case: " + "; ".join(labels)],
           ["SA-CCR EAD", "Two-month notional (proxy) and full booked notional (literal), both unmargined (a CSA where "
                          "only BMO posts counts as unmargined), α = 1.4, IM as collateral on the SPV side"],
           ["CVA", "LGD × Σ DF × EE × PD in the period, on window EE"],
           ["Lost spread", "Σ spread × q × DF × (1 − joint survival): the spread stops at either "
                           "default with no claim"],
           ["FCA", "Funding spread × expected VM posted to the SPV × joint survival"]])
    para(f"The engine produces EE, PFE, EPE and SA-CCR EAD profiles. CVA, lost spread, FCA and RWA are computed from "
         f"those profiles with: Issuer spread {a.spread_issuer:g}bp, SPV {a.spread_spv:g}bp, LGD {a.lgd:.0%}, funding "
         f"spread {a.funding:g}bp.")

    # ------------------------------------------------------------------ Issuer
    doc.add_heading("Results facing the Municipal Issuer (front-end)", 1)
    para(f"The Issuer side is unmargined and unsecured: peak EE {join(_mm(c['fe']['peak_EE']) for c in cases)} and CVA "
         f"{join(_mm(c['fe']['CVA'], 3) for c in cases)} ({join(labels)}). The window starts "
         f"{_mm(abs(first['fe']['MTM0']))} {'out of' if first['fe']['MTM0'] < 0 else 'in'} the money for BMO.")
    rows = [["Window MTM at t0"] + [_f(c["fe"]["MTM0"]) for c in cases],
            ["Peak EE (date)"] + [_f(c["fe"]["peak_EE"]) + (f" ({c['fe']['peak_EE_date']})" if c["fe"]["peak_EE_date"] else "") for c in cases],
            ["Peak PFE 97.5% (date)"] + [_f(c["fe"]["peak_PFE"]) + (f" ({c['fe']['peak_PFE_date']})" if c["fe"]["peak_PFE_date"] else "") for c in cases],
            ["EPE, first year"] + [_f(c["fe"]["EPE_1y"], 3) for c in cases],
            ["SA-CCR EAD t0, two-month notional"] + [_f(c["fe"]["EAD0"]) for c in cases]]
    if any(pd.notna(c["fe"]["EAD0_system"]) for c in cases):
        rows.append(["SA-CCR EAD t0, system"] + [_f(c["fe"]["EAD0_system"]) for c in cases])
    rows.append([f"CVA (Issuer {a.spread_issuer:g}bp, LGD {a.lgd:.0%})"] + [_f(c["fe"]["CVA"], 3) for c in cases])
    table(["USD mm"] + labels, rows)
    ann = R["annual"]
    iss = ann[ann.side == "Issuer"] if ann is not None else None
    if iss is not None and len(iss):
        pk = iss.loc[iss.pfe.idxmax()]
        txt = f"PFE grows with price uncertainty: {_mm(iss.pfe.iloc[0])} in {int(iss.year.iloc[0])}"
        ry = deal.get("reset_year")
        if ry and (iss.year == ry).any():
            txt += f", {_mm(float(iss[iss.year == ry].pfe.iloc[0]))} by the reset"
        txt += f", {_mm(pk.pfe)} by {int(pk.year)}."
        bullet("Nothing caps the tail.", "The window has no collateral and no price ceiling. " + txt)
    if len(cases) > 1:
        bullet("The horizon matters.", f"From “{first['label']}” to “{last['label']}”, CVA rises by "
               f"{_mm(last['fe']['CVA'] - first['fe']['CVA'], 3)} and peak PFE goes from "
               f"{_mm(first['fe']['peak_PFE'])} to {_mm(last['fe']['peak_PFE'])}. Weight post-reset windows by the "
               f"probability of successful remarketing.")

    # ------------------------------------------------------------------ SPV
    doc.add_heading("Results facing the Gas Supplier SPV (back-end)", 1)
    cap_txt = ""
    if deal.get("window_max"):
        cap_txt = (f" Gas cannot go below $0, so the window can never exceed about {_mm(deal['window_max'], 1)}"
                   f"{f' against {_mm(im)} of IM' if im else ''}.")
    para(f"BMO is {'in' if first['be']['MTM0'] > 0 else 'out of'} the money against the SPV: the window is worth "
         f"{'' if first['be']['MTM0'] >= 0 else '−'}{_mm(abs(first['be']['MTM0']))} to BMO today. Peak PFE net of IM and VM is "
         f"{join(_mm(c['be']['peak_PFE']) for c in cases)}." + cap_txt)
    rows = [["Window MTM at t0"] + [_f(c["be"]["MTM0"]) for c in cases],
            ["Peak EE, net of IM and VM"] + [_f(c["be"]["peak_EE"]) for c in cases],
            ["Peak PFE 97.5%, net of IM and VM"] + [_f(c["be"]["peak_PFE"]) for c in cases],
            ["Peak EE / PFE before IM and VM"] + [f"{_f(c['be']['peak_EE_gross'])} / {_f(c['be']['peak_PFE_gross'])}" for c in cases],
            ["SA-CCR EAD t0 (unmargined, IM)"] + [_f(c["be_sa"]["EAD0"]) for c in cases]]
    if any(pd.notna(c["be_sa"]["EAD0_system"]) for c in cases):
        rows.append(["SA-CCR EAD t0, system"] + [_f(c["be_sa"]["EAD0_system"]) for c in cases])
    rows += [[f"CVA (SPV {a.spread_spv:g}bp, LGD {a.lgd:.0%})"] + [_f(c["be"]["CVA"], 3) for c in cases],
             ["Peak expected VM posted by BMO"] + [_f(c["vm_peak"]) for c in cases],
             [f"FCA on VM posted ({a.funding:g}bp)"] + [_f(c["fca"], 3) for c in cases]]
    table(["USD mm"] + labels, rows)
    if im:
        bullet("The IM sizing is the whole result.", f"Peak PFE before collateral is {_mm(g)} against {_mm(im)} of IM, "
               f"a cushion of {_mm(im - g)}. Check the final IM against 2 × 31 days × volume × back-end price.")
    bullet("One-way VM creates a small exposure of its own.", "If prices fall during the MPoR after BMO has posted VM, BMO "
           "is owed back the posted VM plus the window value. With segregated VM it is zero.")
    bullet("The cost facing the SPV is funding, not credit.", f"Expected VM posted peaks at "
           f"{join(_mm(c['vm_peak']) for c in cases)}, for an FCA of {join(_mm(c['fca'], 3) for c in cases)}.")
    if R["stress"]:
        bullet("If extinguishment failed, this side would carry the risk.", "On standard ISDA close-out: " + "; ".join(
            f"{s['stress']}: MTM {_mm(s['MTM0'], 0)}, peak EE {_mm(s['peak_EE'], 0)}, CVA {_mm(s['CVA'], 1)}" for s in R["stress"]) + ".")

    # ------------------------------------------------------------------ chart
    if R.get("chart"):
        doc.add_heading("Exposure profile", 1)
        doc.add_picture(str(R["chart"]), width=Inches(6.6))
        para(f"Source: {last['fe']['netting_set']} and {last['be']['netting_set']}. The Issuer line is the exposure "
             f"itself, since nothing is netted against it; for the SPV, the gap to the IM line is the cushion.", italic=True)

    # ------------------------------------------------------------------ capital
    if caps:
        doc.add_heading("SA-CCR EAD and capital", 1)
        para("Both netting sets are unmargined: the Issuer has no CSA, and a CSA where only BMO posts does not count as "
             "margining (CAR ch. 7 ¶94, ¶107). α = 1.4, 18% gas supervisory factor, SPV IM as collateral, "
             f"no CSRA. CCR RWA uses IRB with PD {a.pd_issuer:g}% (Issuer) and {a.pd_spv:g}% (SPV), LGD {a.lgd_irb:.0%}. "
             f"BA-CVA is the reduced version with RW {a.rw_issuer:g}% / {a.rw_spv:g}%, ρ = 0.5, DS = 0.65. Capital "
             f"cost = total RWA × {a.cap_ratio:g}% × {a.hurdle:g}% hurdle, an order of magnitude only.")
        table(["USD mm", "EAD Issuer", "EAD SPV", "CCR RWA", "BA-CVA RWA", "Total RWA", "Capital cost a year"],
              [[c["treatment"], _f(c["EAD_Issuer"]), _f(c["EAD_SPV"]), _f(c["CCR_RWA"]), _f(c["BA_CVA_RWA"]),
                _f(c["Total_RWA"]), _f(c["capital_cost_yr"], 3)] for c in caps])
        if caps_full:
            lit = caps_full[-1]
            drv = "SPV" if lit["EAD_SPV"] > lit["EAD_Issuer"] else "Issuer"
            bullet(f"The {drv} drives full-notional capital.", f"{lit['treatment']}: EAD {_f(lit['EAD_Issuer'], 1)} "
                   f"(Issuer) against {_f(lit['EAD_SPV'], 1)} (SPV).")
        if caps_proxy:
            bullet("Under the proxy, maturity only moves BA-CVA.", "EAD and CCR RWA do not move with the horizon, because "
                   "the maturity factor stops at one year and IRB M at five.")
        bullet("The literal reading remains the Pillar 1 number.", "Until Reg Cap and OSFI accept the two-month notional, "
               "present both.")

    # ------------------------------------------------------------------ XVA summary / observations
    doc.add_heading("Key observations", 1)
    para(f"On the window basis the XVA is about {rng(tot)}, {rng([t / R['spread_pv'] * 100 for t in tot], 0).replace('$', '').replace('mm', '%')} "
         f"of the {_mm(R['spread_pv'])} spread PV.")
    table(["XVA term, USD mm PV"] + labels,
          [["Issuer CVA, unmargined"] + [_f(c["fe"]["CVA"], 3) for c in cases],
           ["SPV CVA, net of IM and VM"] + [_f(c["be"]["CVA"], 3) for c in cases],
           ["Lost spread"] + [_f(R["lost"], 3) for c in cases],
           ["FCA on VM posted to SPV"] + [_f(c["fca"], 3) for c in cases],
           ["Total"] + [_f(c["total_xva"], 3) for c in cases]])
    bullet("Lost spread is not collateralised.", "The IM secures unpaid settlements, not future spread; it depends on the "
           "joint default probability of the Issuer and SPV.", True)
    bullet("The Issuer is the only open credit line.", f"Size the Issuer limit on the PFE profile (peak "
           f"{_mm(last['fe']['peak_PFE'])}), not on the EE.", True)
    bullet("The SPV side is mainly a funding cost.", f"FCA {join(_mm(c['fca'], 3) for c in cases)}; residual PFE "
           f"{_mm(last['be']['peak_PFE'])} if posted VM is not segregated.", True)
    if R.get("obs_84"):
        bullet("Summing the 84 booked trades double counts.", R["obs_84"], True)
    if caps_full:
        yr = [c["capital_cost_yr"] / deal["spread_yr"] for c in caps_full] if deal.get("spread_yr") else None
        bullet("Capital, not XVA, decides the trade.", f"Full-notional capital costs roughly "
               f"{_f(min(yr), 0)}–{_f(max(yr), 0)}× the spread's yearly income." if yr else "", True)

    # ------------------------------------------------------------------ caveats
    doc.add_heading("Caveats and open items", 1)
    para(status)
    table(["Input or assumption", "Used here"],
          [["Forward curve", deal.get("curve_txt", "see in_Contracts")],
           ["Valuation date / paths / seed", f"{deal.get('val_date', '')} / {deal.get('n_paths', '')} / {deal.get('seed', '')}"],
           ["SPV IM", _mm(im) if im else "n/a"],
           ["Posted VM", "Not segregated, 10-day MPoR, zero threshold"],
           ["Credit inputs", f"Issuer {a.spread_issuer:g}bp, SPV {a.spread_spv:g}bp, LGD {a.lgd:.0%}; IRB PD "
                             f"{a.pd_issuer:g}% / {a.pd_spv:g}%; BA-CVA RW {a.rw_issuer:g}% / {a.rw_spv:g}%"],
           ["Funding", f"{a.funding:g}bp on expected VM posted"],
           ["Window convention", "Forward window (months k, k+1), monthly first-of-month fixing"]])
    for lead, txt in (("Remarketing probability.", "Cases are run as bounds; a probability-weighted case and an SPV "
                       "hazard step-up after the reset are not run."),
                      ("Wrong-way risk.", "A gas spike that strains the Issuer's participants is not stressed."),
                      ("Stress windows.", "A three-month roll gap or a 120-day cure are not run."),
                      ("Legal.", "Cross-extinguishment both ways in Chapter 9 and SPV insolvency; the roll stops on "
                       "non-payment; VM limited to the two-month amount; the IM pledged to BMO and enforceable. If any "
                       "fails, use the full-strip numbers.")):
        bullet(lead, txt)
    if R["notes"]:
        para("", "Run notes")
        for n in R["notes"]:
            doc.add_paragraph(n, style="List Bullet")
    p = para("Generated by examples/prepay/xva_writeup.py. The prose is a template filled from the results: read it "
             "before sending it on.", italic=True)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    doc.save(path)
