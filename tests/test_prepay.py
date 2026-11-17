"""Gas prepay: window exposure, SA-CCR treatments and the amortization-weighted notional.

Targets come from docs/prepay/INTEGRATION.md and reference/prepay/*.py. Every input is an
illustrative placeholder (flat $5, 45% short vol kappa 1, 15% long vol, 20,000 MMBtu/d)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from eqxva.analytic import run_analytic
from eqxva.commodity import examples as soy
from eqxva.commodity import prepay_examples as pe
from eqxva.commodity.nfactor import _step_root, covariance, simulate_factors, simulate_futures
from eqxva.exposure import build_grid, leg_exposure0, leg_ccy, run_monte_carlo
from eqxva.io_excel import parse_inputs
from eqxva.saccr import leg_terms, saccr

MM = 1e6


def parsed(**kw):
    return parse_inputs(pe.example_sheets(**kw))


def ns_named(nss, name):
    return next(n for n in nss if n.netting_set == name)


def prof(res, name):
    return res.ns_profile[res.ns_profile.netting_set == name].reset_index(drop=True)


def with_extra(sheets, ns_rows, trade_rows):
    """Append netting sets and trades to example sheets."""
    s = dict(sheets)
    s["NettingSets"] = pd.concat([s["NettingSets"], pd.DataFrame(ns_rows)], ignore_index=True)
    s["Trades"] = pd.concat([s["Trades"], pd.DataFrame(trade_rows, columns=pe.TRADE_COLUMNS)], ignore_index=True)
    return s


def t0_saccr(ns, mkt):
    """The t = 0 SA-CCR of a netting set exactly as Summary computes it (no simulation needed)."""
    legs = ns.legs
    S0 = {l.underlying: np.array([mkt.underlyings[l.underlying].spot]) for l in legs}
    V0 = np.array([sum(leg_exposure0(l, mkt) * float(mkt.fx(leg_ccy(l, mkt), ns.csa.ccy, 0.0)) for l in legs)])
    return saccr(legs, 0.0, S0, V0, ns.csa, mkt)


# ----------------------------------------------------------------------------- 1. lazy paths
def _eager(mkt, names, grid, n_paths, seed=42, antithetic=True):
    """The pre-lazy simulate_futures loop, kept as the reference."""
    model = mkt.commodity
    rng = np.random.default_rng([seed, 1])
    cs = {n: mkt.underlyings[n] for n in names}
    used = sorted({int(i) for c in cs.values() for i in model.index[c.product]})
    idx, col = np.array(used), {f: k for k, f in enumerate(used)}
    half = (n_paths + 1) // 2 if antithetic else n_paths
    Y = np.zeros((n_paths, len(idx)))
    out = {n: np.empty((n_paths, len(grid))) for n in names}
    for n, c in cs.items():
        out[n][:, 0] = c.spot
    roots, beta = {}, model.beta[idx]
    for k in range(1, len(grid)):
        h = float(grid[k] - grid[k - 1])
        key = round(h * 1e9)
        if key not in roots:
            roots[key] = _step_root(model, idx, h)
        Z = rng.standard_normal((half, len(idx))) @ roots[key].T
        if antithetic:
            Z = np.concatenate([Z, -Z], axis=0)[:n_paths]
        Y = Y * np.exp(-beta * h) + Z
        t = float(grid[k])
        for n, c in cs.items():
            if t > c.expiry_t + 1e-9:
                out[n][:, k] = out[n][:, k - 1]
                continue
            i = model.index[c.product]
            w = model.ratio[i] * c.q_ref * np.exp(-model.beta[i] * (c.expiry_t - t))
            out[n][:, k] = c.spot * np.exp(Y[:, [col[int(f)] for f in i]] @ w - 0.5 * covariance(model, c, t, c, t))
    return out


def test_lazy_paths_identical_to_eager():
    mkt, nss, cfg, _ = parse_inputs(soy.example_sheets())
    grid = build_grid(nss, cfg)
    names = sorted({l.underlying for ns in nss for l in ns.legs})
    ref = _eager(mkt, names, grid, 1001)
    eager = simulate_futures(mkt, names, grid, 1001)
    lazy = simulate_factors(mkt, names, grid, 1001)
    for n in names:
        assert np.array_equal(ref[n], eager[n])
        assert lazy[n].shape == ref[n].shape
        for i in (0, 7, len(grid) // 2, len(grid) - 1):
            assert np.array_equal(lazy[n][:, i], ref[n][:, i])
            assert np.array_equal(lazy[n][:250, i], ref[n][:250, i])


# ----------------------------------------------------------------------------- 2. window = SELECT
def test_window_strip_equals_84_trade_booking():
    sh = pe.example_sheets(scenarios=("B84",), sides=("FE",), collateral=(False,), n_paths=2000)
    sh = with_extra(sh, [dict(netting_set="WS", counterparty="ISSUER", collateralized="N", csa_ccy="USD")],
                    pe.strip_rows("WS", "WS", "FE", 0, 84, window_months=2, sa_maturity=pe.M7))
    mkt, nss, cfg, w = parse_inputs(sh)
    assert not any("overlapping" in x for x in w)
    res = run_monte_carlo(nss, mkt, cfg, w)
    a, b = prof(res, "WS"), prof(res, "B84_FE_NOCSRA")
    t84 = (pe.month_start(84) - pe.VAL_DATE).days / 365.0
    m = a.t < t84 - 1e-9                 # in Jan34 the strip still holds month 84; trade 83 has ended
    assert m.sum() > 80
    for c in ("EE", "NEE", "PFE"):
        np.testing.assert_allclose(a.loc[m, c], b.loc[m, c], rtol=1e-12, atol=1e-6)
    # SA-CCR: equal while MF = 1; in the last year M differs (trade 083 runs to its own Feb34 payment)
    m1 = a.t < (pe.M7 - pe.VAL_DATE).days / 365.0 - 1.0
    np.testing.assert_allclose(a.loc[m1, "SACCR_EAD_mean"], b.loc[m1, "SACCR_EAD_mean"], rtol=1e-12)
    assert a.EE.max() / MM == pytest.approx(1.24, rel=0.1)        # INTEGRATION.md peak pre-CSRA window EE


def test_window_intervals_fwd_and_bwd():
    sh = pe.example_sheets(scenarios=(), n_paths=10)
    rows = (pe.strip_rows("F", "F", "FE", 0, 5, window_months=2) +
            pe.strip_rows("B", "B", "FE", 0, 5, window_months=2, window_convention="BWD"))
    sh = with_extra(sh, [dict(netting_set=n, collateralized="N", csa_ccy="USD") for n in ("F", "B")], rows)
    mkt, nss, _, w = parse_inputs(sh)
    f = [l.fixing_t for l in ns_named(nss, "F").legs]
    fw, bw = ns_named(nss, "F").legs, ns_named(nss, "B").legs
    assert fw[0].active_from == -np.inf and fw[1].active_from == -np.inf
    assert fw[3].active_from == pytest.approx(f[2]) and fw[3].active_to == pytest.approx(f[4])
    for t in (0.0, f[2] + 0.01, f[4] - 0.01):                      # exactly two months live
        assert sum(l.live(t) for l in fw) == 2
    # BWD: month i is paid at f[i+1], and a paid leg counts zero, so the window collapses to the
    # month in progress (a missed settlement still owed is not modelled) - flagged by a warning
    assert bw[0].active_from == -np.inf and bw[3].active_from == pytest.approx(f[3])
    assert bw[3].active_to == pytest.approx(bw[3].payment_t) == pytest.approx(f[4])
    assert sum(l.live(f[2] + 0.01) for l in bw) == 1
    assert any("B: window_convention BWD" in x for x in w)
    assert not any(x.startswith("F: window_convention") for x in w)


# ----------------------------------------------------------------------------- 3. analytic checks
def test_window_ee_matches_exact_and_black_with_csra():
    mkt, nss, cfg, w = parsed(scenarios=("W7",), sides=("FE",), collateral=(True, False), n_paths=20000)
    res = run_monte_carlo(nss, mkt, cfg, w)
    an = run_analytic(nss, mkt, cfg, res)
    a = an[(an.netting_set == "W7_FE_NOCSRA") & (an.t > 0.05) & (an.t < 7.0)]
    assert len(a) > 70
    assert (np.abs(a.EE_diff) <= 4 * a.EE_stderr + 1e-3 * a.EE_analytic).all()
    # window net of CSRA ~ q x Black call at strike K + C/q, Nov 2033 (INTEGRATION.md: 0.10 vs 0.097 sim)
    p = prof(res, "W7_FE_CSRA")
    t = (date(2033, 11, 13) - pe.VAL_DATE).days / 365.0
    row = p.iloc[int(np.argmin(np.abs(p.t - t)))]
    q = pe.quantity(82) + pe.quantity(83)
    c = mkt.underlyings[pe.contract_name(83)]
    s = np.sqrt(covariance(mkt.commodity, c, row.t, c, row.t))
    K = pe.K_FRONT + pe.CSRA / q
    d1 = (np.log(pe.F0 / K) + 0.5 * s * s) / s
    black = q * (pe.F0 * norm.cdf(d1) - K * norm.cdf(d1 - s)) * np.exp(-pe.RATE * 0.1)
    assert row.EE == pytest.approx(black, rel=0.10)
    assert row.EE / MM == pytest.approx(0.10, abs=0.03)


# ----------------------------------------------------------------------------- 4-5. double count, SPV IM
def test_naive_84_double_counts_and_warns():
    mkt, nss, cfg, w = parsed(scenarios=("F7", "N84", "B84"), sides=("FE",), collateral=(False,), n_paths=2000)
    over = [x for x in w if "overlapping" in x]
    assert len(over) == 1 and "N84_FE_NOCSRA" in over[0]
    res = run_monte_carlo(nss, mkt, cfg, w)
    n, f = prof(res, "N84_FE_NOCSRA"), prof(res, "F7_FE_NOCSRA")
    for k in (12, 36, 60):
        assert n.EE[k] / f.EE[k] == pytest.approx(2.0, abs=0.12)


def test_spv_window_ee_zero_when_im_covers_two_months():
    mkt, nss, cfg, w = parsed(scenarios=("W7",), sides=("BE",), collateral=(True, False), n_paths=2000)
    assert pe.IM_SPV >= 2 * 31 * pe.VOL_D * pe.K_BACK_7Y * 0.99
    res = run_monte_carlo(nss, mkt, cfg, w)
    assert prof(res, "W7_BE_IM").EE.max() == pytest.approx(0.0, abs=1e-6)
    assert prof(res, "W7_BE_NOIM").EE.max() > 1e4


# ----------------------------------------------------------------------------- 6-7. SA-CCR targets
def _proxy_sheets(i0, i1, csra=8e6, im=6.2e6):
    sh = pe.example_sheets(scenarios=(), n_paths=10)
    rows = []
    for side in ("FE", "BE"):
        for r in pe.strip_rows(f"P_{side}", f"P_{side}", side, i0, i1, sa_maturity="TRADE"):
            r["fixed_price"] = 5.00                       # the note's proxy: $5.00 on both sides
            rows.append(r)
    ns = [dict(netting_set="P_FE", counterparty="ISSUER", collateralized="N", ia_received=csra, csa_ccy="USD"),
          dict(netting_set="P_BE", counterparty="SPV", collateralized="N", ia_received=im, csa_ccy="USD")]
    return with_extra(sh, ns, rows)


@pytest.mark.parametrize("i0", [82, 370])              # Nov-Dec 2033 and Nov-Dec 2057
def test_saccr_two_month_proxy(i0):
    mkt, nss, _, _ = parse_inputs(_proxy_sheets(i0, i0 + 1))
    fe, be = (float(t0_saccr(ns_named(nss, n), mkt)["EAD"][0]) / MM for n in ("P_FE", "P_BE"))
    assert fe == pytest.approx(0.10841, rel=0.02)          # reference/prepay/prepay_note_check.py
    assert be == pytest.approx(0.15164, rel=0.02)


def test_saccr_literal_full_strip_to_2057():
    mkt, nss, _, _ = parsed(scenarios=("F30",), collateral=(True,), im=6.2e6)
    fe = float(t0_saccr(ns_named(nss, "F30_FE_CSRA"), mkt)["EAD"][0]) / MM
    be = float(t0_saccr(ns_named(nss, "F30_BE_IM"), mkt)["EAD"][0]) / MM
    assert fe == pytest.approx(279.797, rel=0.01)
    assert be == pytest.approx(282.104, rel=0.01)


def test_saccr_full_strip_to_2033():
    mkt, nss, _, _ = parsed(scenarios=("F7",), collateral=(True,), im=6.2e6)
    fe = float(t0_saccr(ns_named(nss, "F7_FE_CSRA"), mkt)["EAD"][0]) / MM
    be = float(t0_saccr(ns_named(nss, "F7_BE_IM"), mkt)["EAD"][0]) / MM
    assert fe == pytest.approx(59.085, rel=0.01)
    assert be == pytest.approx(61.267, rel=0.01)


# ----------------------------------------------------------------------------- 8. notional treatments
def test_window_keeps_two_month_notional_after_fixing():
    mkt, nss, _, _ = parsed(scenarios=("W7",), sides=("FE",), collateral=(False,))
    ns = ns_named(nss, "W7_FE_NOCSRA")
    t = (date(2030, 6, 15) - pe.VAL_DATE).days / 365.0     # mid-June 2030: June fixed, July not
    F = {l.underlying: np.array([5.0]) for l in ns.legs}
    terms = [leg_terms(l, t, F[l.underlying], mkt, "USD", fix=lambda u, tf: np.array([5.0])) for l in ns.legs]
    live = [x for x in terms if x is not None]
    assert len(live) == 2
    d = sum(float(x[2][0]) for x in live)
    i = (2030 - 2027) * 12 + 5
    assert d == pytest.approx(5.0 * (pe.quantity(i) + pe.quantity(i + 1)))
    assert all(x[4] == pytest.approx(ns.legs[0].sa_maturity_t - t) for x in live)
    # engine rule (sa_fixed_notional = N): the fixed month drops out
    sh = pe.example_sheets(scenarios=("W7",), sides=("FE",), collateral=(False,))
    sh["Trades"]["sa_fixed_notional"] = "N"
    mkt2, nss2, _, _ = parse_inputs(sh)
    d2 = sum(float(x[2][0]) for l in nss2[0].legs
             if (x := leg_terms(l, t, np.array([5.0]), mkt2, "USD", fix=lambda u, tf: np.array([5.0]))))
    assert d2 == pytest.approx(5.0 * pe.quantity(i + 1))


def test_amortization_weighted_notional():
    mkt, nss, cfg, w = parsed(scenarios=("F7", "F7A", "W7"), sides=("FE",), collateral=(False,), n_paths=10)
    m, a, win = (ns_named(nss, n) for n in ("F7_FE_NOCSRA", "F7A_FE_NOCSRA", "W7_FE_NOCSRA"))
    M = max(l.payment_t for l in a.legs)
    closed = sum(l.quantity * 5.0 * l.fixing_t for l in a.legs) / M       # time-average of the notional left
    S0 = {l.underlying: np.array([5.0]) for l in a.legs}
    d = sum(float(leg_terms(l, 0.0, S0[l.underlying], mkt, "USD")[2][0]) for l in a.legs)
    assert d == pytest.approx(closed, rel=1e-12)
    total = sum(l.quantity * 5.0 for l in m.legs)
    assert 0.45 < closed / total < 0.55
    r_m, r_a = t0_saccr(m, mkt), t0_saccr(a, mkt)
    assert float(r_a["AddOn"][0]) / float(r_m["AddOn"][0]) == pytest.approx(closed / total, rel=1e-12)
    from eqxva.exposure import sa_proxy_rows
    px = {r["netting_set"]: r for ns in (m, a, win) for r in sa_proxy_rows(ns, mkt, 1.0)}
    assert px["F7A_FE_NOCSRA"]["SA_NOTIONAL_AMORT"] == pytest.approx(closed, rel=1e-9)
    assert px["F7_FE_NOCSRA"]["SA_NOTIONAL_TOTAL"] == pytest.approx(total, rel=1e-12)
    assert px["F7_FE_NOCSRA"]["SA_NOTIONAL"] == pytest.approx(total, rel=1e-9)      # one MF: no longer NaN
    two = 5.0 * (pe.quantity(0) + pe.quantity(1))
    assert px["W7_FE_NOCSRA"]["SA_NOTIONAL_TOTAL"] == pytest.approx(two)
    assert px["W7_FE_NOCSRA"]["SA_NOTIONAL_AMORT"] == pytest.approx(two, rel=0.05)


def test_vm_posted_to_spv():
    mkt, nss, cfg, w = parsed(scenarios=("W7",), sides=("BE",), collateral=(), vm_posting=True, n_paths=2000)
    res = run_monte_carlo(nss, mkt, cfg, w)
    p = prof(res, "W7_BE_IMVM")
    assert p.E_VM_received.abs().max() == 0.0                 # the SPV never posts VM
    np.testing.assert_allclose(p.E_VM_posted, -p.NEE_uncoll, rtol=1e-12, atol=1e-6)   # BMO posts on V < 0
    assert p.E_VM_posted.max() > 1e5
    assert prof(res, "W7_BE_IMVM").FCA_exposure.max() == pytest.approx(prof(res, "W7_BE_IMVM").EE_uncoll.max())


# ----------------------------------------------------------------------------- 9. system path import
def test_import_round_trip_matches_model(tmp_path):
    from eqxva.exposure import build_grid, simulate_all, value_cube
    from eqxva.pathimport import build_path_import_sheet, load_path_import
    base = pe.example_sheets(scenarios=("B84", "N84"), sides=("FE",), collateral=(True, False), n_paths=2000)
    mkt, nss, cfg, w = parse_inputs(base)
    b84 = ns_named(nss, "B84_FE_CSRA")
    n84 = ns_named(nss, "N84_FE_NOCSRA")                 # same trades, no windows: the system's full PVs
    grid = build_grid([b84], cfg)
    S = simulate_all(mkt, sorted({l.underlying for l in b84.legs}), grid, cfg)
    dates = [pe.VAL_DATE] + cfg.buckets
    rng = np.random.default_rng(0)
    perm = rng.permutation(cfg.n_paths)                   # scrambled rows, aligned back by scenario id
    cubes = {tr.trade_id: value_cube(tr.legs, grid, S, mkt, "USD") for tr in n84.trades}
    for d in dates:
        i = int(np.argmin(np.abs(grid - (d - pe.VAL_DATE).days / 365.0)))
        df = pd.DataFrame({"scenario": np.arange(cfg.n_paths)[perm],
                           **{f"PV_{t}": c[perm, i] for t, c in cubes.items()}})
        df.to_csv(tmp_path / f"pv_{d:%Y%m%d}.csv", index=False)
    # the IMPORT netting set books the same 84 trades (with windows) under new ids
    rows = pe.booked_84_rows("B84I_FE_CSRA", "B84I_FE_CSRA", "FE")
    ns_row = dict(netting_set="B84I_FE_CSRA", counterparty="ISSUER", collateralized="N", ia_received=pe.CSRA,
                  csa_ccy="USD", value_source="IMPORT")
    sh = with_extra(pe.example_sheets(scenarios=("B84",), sides=("FE",), collateral=(True,), n_paths=2000),
                    [ns_row], rows)
    tids = [f"B84I_FE_CSRA_{k:03d}" for k in range(84)]
    sh["PathImport"] = build_path_import_sheet(tids, dates, "pv_{date:%Y%m%d}.csv", "PV_{trade_id}",
                                               scenario_column="scenario",
                                               export_ids=[t.trade_id for t in n84.trades])
    mkt, nss, cfg, w = parse_inputs(sh)
    cube = load_path_import(sh, tmp_path, mkt.valuation_date)
    assert cube.n_scen == 2000 and len(cube.dates) == len(dates)
    res = run_monte_carlo(nss, mkt, cfg, w, imported=cube)
    assert any("only PVs imported" in x for x in res.warnings)
    a, b = prof(res, "B84I_FE_CSRA"), prof(res, "B84_FE_CSRA")
    for c in ("EE", "NEE", "PFE", "EE_uncoll", "MTM"):
        np.testing.assert_allclose(a[c], b[c], rtol=1e-10, atol=1e-6)
    s = res.summary.set_index("netting_set")
    assert s.loc["B84I_FE_CSRA", "value_source"] == "IMPORT" and s.loc["B84I_FE_CSRA", "n_paths"] == 2000
    assert s.loc["B84I_FE_CSRA", "SACCR_EAD0"] == pytest.approx(s.loc["B84_FE_CSRA", "SACCR_EAD0"])
    m = (a.t > 0.1) & (a.t < 6.9)
    rel = a.loc[m, "SACCR_EAD_mean"] / b.loc[m, "SACCR_EAD_mean"]
    print("IMPLIED / model EAD:", rel.min(), rel.max())
    assert rel.between(0.95, 1.05).all()               # notional implied from the PVs tracks the model
    # FORWARD notional (ignores price co-movement) understates the path-wise EAD under the CSRA
    sh["NettingSets"].loc[sh["NettingSets"].netting_set == "B84I_FE_CSRA", "import_sa_price"] = "FORWARD"
    mkt, nss, cfg, w = parse_inputs(sh)
    f = prof(run_monte_carlo(nss, mkt, cfg, w, imported=cube), "B84I_FE_CSRA")
    assert (f.loc[m, "SACCR_EAD_mean"] < 0.9 * b.loc[m, "SACCR_EAD_mean"]).mean() > 0.8


def _mdy(d):
    return f"{d.month}/{d.day}/{d.year}"


def test_bmo_risk_profile_export_round_trip(tmp_path):
    """BMO export layout: per request, wide discounted Pos / Neg files (first row = dates, one row per
    scenario), an empty Base file, and a long path-wise SA-CCR file with future dates."""
    from eqxva.exposure import build_grid, simulate_all, value_cube
    from eqxva.pathimport import load_imports
    base = pe.example_sheets(scenarios=("B84", "N84"), sides=("FE",), collateral=(True, False), n_paths=500)
    mkt, nss, cfg, _ = parse_inputs(base)
    b84, n84 = ns_named(nss, "B84_FE_CSRA"), ns_named(nss, "N84_FE_NOCSRA")
    grid = build_grid([b84], cfg)
    S = simulate_all(mkt, sorted({l.underlying for l in b84.legs}), grid, cfg)
    dates = [pe.VAL_DATE] + cfg.buckets
    cols = [int(np.argmin(np.abs(grid - (d - pe.VAL_DATE).days / 365.0))) for d in dates]
    tau = np.array([(d - dates[0]).days / 365.0 for d in dates])
    df = mkt.curve.df(tau)
    req = {}
    for k, tr in enumerate(n84.trades):
        rid = str(281900 + k)
        req[f"B84I_FE_CSRA_{k:03d}"] = rid
        V = value_cube(tr.legs, grid, S, mkt, "USD")[:, cols] * df[None, :]          # discounted to t0
        hdr = [_mdy(d) for d in dates]
        pd.DataFrame(np.maximum(V, 0), columns=hdr).to_csv(tmp_path / f"RiskProfile_{rid}.Test DiscPosNet.csv",
                                                           index=False)
        pd.DataFrame(np.minimum(V, 0), columns=hdr).to_csv(tmp_path / f"RiskProfile_{rid}_Test DiscNegNet.csv",
                                                           index=False)
        (tmp_path / f"RiskProfile_{rid} Base DiscPosNet.csv").write_text("")            # no existing trades
        sa = [dict(NettingSet="N_PREPAY", Valuation_Date=_mdy(d), PathId=p, RC=0.0, Multiple=1.0,
                   PFE=1000.0 * k + p, EAD=1400.0 * k + 1.4 * p) for d in dates[:40] for p in range(3)]
        pd.DataFrame(sa).to_csv(tmp_path / f"Risk_Profile_{rid}_SACCR_PathEADs_TT_Test.csv", index=False)
    rows = pe.booked_84_rows("B84I_FE_CSRA", "B84I_FE_CSRA", "FE")
    ns_row = dict(netting_set="B84I_FE_CSRA", counterparty="ISSUER", collateralized="N", ia_received=pe.CSRA,
                  csa_ccy="USD", value_source="IMPORT")
    sh = with_extra(pe.example_sheets(scenarios=("B84",), sides=("FE",), collateral=(True,), n_paths=500),
                    [ns_row], rows)
    sh["SystemRequests"] = pd.DataFrame([dict(trade_id=t, request_id=int(r)) for t, r in req.items()])
    mkt, nss, cfg, w = parse_inputs(sh)
    cube = load_imports(sh, tmp_path, mkt)
    assert cube.n_scen == 500 and cube.dates == dates and len(cube.saccr) == 84
    res = run_monte_carlo(nss, mkt, cfg, w, imported=cube)
    a, b = prof(res, "B84I_FE_CSRA"), prof(res, "B84_FE_CSRA")
    for c in ("EE", "NEE", "PFE", "EE_uncoll"):                     # undiscounted system PVs = model values
        np.testing.assert_allclose(a[c], b[c], rtol=1e-9, atol=1e-4)
    # system SA-CCR: in month k the EAD of strip k (mean over paths), at the last export date <= bucket
    for i in (0, 5, 20):
        k = max(j for j in range(84) if j == 0 or pe.month_start(j) <= dates[i + 1])
        assert a.SACCR_EAD_system[i] == pytest.approx(1400.0 * k + 1.4)
        assert a.system_live_trades[i] == 1
    assert np.isnan(a.SACCR_EAD_system[60])                       # beyond the exported SA-CCR dates
    s = res.summary.set_index("netting_set")
    assert s.loc["B84I_FE_CSRA", "SACCR_EAD0_system"] == pytest.approx(1.4)
    assert len(res.system_saccr) == 84 * 40 and "SystemSACCR" in res.tables()


def test_import_errors(tmp_path):
    from eqxva.pathimport import build_path_import_sheet, load_path_import
    pd.DataFrame({"PV_A": [1.0, 2.0]}).to_csv(tmp_path / "a.csv", index=False)
    pd.DataFrame({"PV_A": [1.0, 2.0, 3.0]}).to_csv(tmp_path / "b.csv", index=False)
    sh = {"PathImport": build_path_import_sheet(["A"], [date(2027, 1, 13)], "a.csv", "PV_X")}
    with pytest.raises(ValueError, match="column 'PV_X'"):
        load_path_import(sh, tmp_path, pe.VAL_DATE)
    sh = {"PathImport": pd.concat([build_path_import_sheet(["A"], [date(2027, 1, 13)], "a.csv", "PV_A"),
                                   build_path_import_sheet(["A"], [date(2027, 2, 13)], "b.csv", "PV_A")])}
    with pytest.raises(ValueError, match="scenarios"):
        load_path_import(sh, tmp_path, pe.VAL_DATE)
    mkt, nss, cfg, w = parse_inputs(with_extra(pe.example_sheets(scenarios=(), n_paths=10),
                                               [dict(netting_set="I", collateralized="N", csa_ccy="USD",
                                                     value_source="IMPORT")],
                                               pe.strip_rows("I", "I", "FE", 0, 1)))
    with pytest.raises(ValueError, match="no PathImport"):
        run_monte_carlo(nss, mkt, cfg, w)


def test_amort_ignored_on_window_with_warning():
    sh = pe.example_sheets(scenarios=("W7",), sides=("FE",), collateral=(False,))
    sh["Trades"]["sa_notional_basis"] = "AMORT"
    _, nss, _, w = parse_inputs(sh)
    assert any("AMORT ignored" in x for x in w)
    assert all(l.sa_notional_basis == "MARKET" for l in nss[0].legs)
