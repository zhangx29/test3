from datetime import date

import numpy as np
import pytest
from scipy.stats import norm

from eqxva import dates as dt
from eqxva.analytic import ns_ee_nee, run_analytic
from eqxva.exposure import leg_mtm0, run_monte_carlo
from eqxva.market import MarketData, Underlying, VolSurface, ZeroCurve
from eqxva.model import RunConfig, build_netting_sets
from eqxva.products import bs_price, option_value, trs_value
from eqxva.saccr import saccr

VAL = date(2026, 10, 9)


def market(r=0.04, q=0.02, vol=0.3, spot=100.0, is_index=False):
    return MarketData(VAL, ZeroCurve.flat(r), {"ABC": Underlying("ABC", spot, q, is_index, VolSurface.flat(vol))})


def trs(tid="T", ns="NS", direction="RECEIVE_EQUITY", shares=1000, maturity="2Y", reset="NONE", fund="3M",
        spread=0.0, basis="CURRENT", avg=None, im=None):
    return dict(trade_id=tid, netting_set=ns, leg_type="TRS", underlying="ABC", direction=direction,
                shares=shares, maturity=maturity, equity_reset_freq=reset, funding_reset_freq=fund,
                funding_spread=spread, notional_basis=basis, avg_price=avg, initial_mtm=im)


def opt(tid="T", ns="NS", direction="BUY", otype="CALL", shares=1000, strike=100.0, expiry="1Y"):
    return dict(trade_id=tid, netting_set=ns, leg_type="OPTION", underlying="ABC", direction=direction,
                option_type=otype, shares=shares, strike=strike, option_maturity=expiry)


def build(rows, mkt, ns_rows=None):
    w = []
    nss = build_netting_sets(rows, ns_rows or [], mkt, w)
    return nss, w


def cfg(buckets="45D,4M,7M,10M,13M,19M", n=20000, mode="POINT"):
    return RunConfig(VAL, dt.parse_buckets(buckets, VAL, VAL), n_paths=n, seed=7, output_mode=mode)


# ----------------------------------------------------------------------------- pricing
def test_put_call_parity():
    S, K, T, r, q, v = 100.0, 95.0, 1.3, 0.04, 0.02, 0.3
    df = np.exp(-r * T)
    c = bs_price(S, K, T, df, q, v, True)
    p = bs_price(S, K, T, df, q, v, False)
    assert c - p == pytest.approx(S * np.exp(-q * T) - K * df, abs=1e-10)


def test_trs_par_at_inception():
    mkt = market()
    for reset in ("NONE", "3M", "1M"):
        (ns,), _ = build([trs(reset=reset)], mkt)
        assert leg_mtm0(ns.legs[0], mkt) == pytest.approx(0.0, abs=1e-6)


def test_trs_initial_mtm_current_basis():
    mkt = market()
    (ns,), w = build([trs(reset="3M", im=12345.0)], mkt)
    assert leg_mtm0(ns.legs[0], mkt) == pytest.approx(12345.0, abs=1e-6)
    assert not [m for m in w if "initial_mtm" in m]
    leg = ns.legs[0]
    # carried MTM is settled at the first reset
    after = trs_value(leg, leg.im_settle + 1e-6, np.array([100.0]), mkt, np.array([100.0]))[0]
    assert abs(after) < 50.0


def test_extension_average_basis_no_double_count():
    mkt = market()
    (ns,), w = build([trs(basis="AVERAGE", avg=90.0, im=10000.0)], mkt)
    assert leg_mtm0(ns.legs[0], mkt) == pytest.approx(1000 * (100.0 - 90.0), rel=1e-12)
    assert any("ignored" in m for m in w)


def test_spread_reduces_receiver_value():
    mkt = market()
    (ns,), _ = build([trs(spread=0.01, maturity="2Y")], mkt)
    v = leg_mtm0(ns.legs[0], mkt)
    assert v == pytest.approx(-1000 * 100 * 0.01 * 2 * 365 / 360, rel=0.08)


def test_call_minus_put_is_forward_and_zero_rate_trs():
    mkt = market(r=0.0, q=0.0)
    (ns,), _ = build([opt(direction="BUY", otype="CALL", strike=105.0, expiry="2Y"),
                      opt(direction="SELL", otype="PUT", strike=105.0, expiry="2Y")], mkt)
    (ns_trs,), _ = build([trs(basis="AVERAGE", avg=105.0, maturity="2Y")], mkt)
    T = ns.legs[0].expiry_t
    S = np.linspace(50, 200, 31)
    for t in (0.0, 0.5, 1.2, T - 0.01):
        v_opt = sum(option_value(l, t, S, mkt) for l in ns.legs)
        assert np.allclose(v_opt, 1000 * (S - 105.0), atol=1e-8)
        v_trs = trs_value(ns_trs.legs[0], t, S, mkt)
        assert np.allclose(v_trs, 1000 * (S - 105.0), atol=1e-8)
    # with rates and dividends: call - put = plain forward
    mkt2 = market(r=0.04, q=0.02)
    (ns2,), _ = build([opt(direction="BUY", otype="CALL", strike=105.0, expiry="2Y"),
                       opt(direction="SELL", otype="PUT", strike=105.0, expiry="2Y")], mkt2)
    for t in (0.0, 0.7, 1.5):
        tau = T - t
        v = sum(option_value(l, t, S, mkt2) for l in ns2.legs)
        fwd = 1000 * (S * np.exp(-0.02 * tau) - 105.0 * np.exp(-0.04 * tau))
        assert np.allclose(v, fwd, atol=1e-6)


# ----------------------------------------------------------------------------- analytic closed forms
def test_long_option_ee_is_forward_value():
    mkt = market()
    (ns,), _ = build([opt(direction="BUY", otype="PUT", strike=95.0, expiry="1Y")], mkt)
    v0 = leg_mtm0(ns.legs[0], mkt)
    for t in (0.1, 0.5, 0.9):
        ee, nee = ns_ee_nee(ns, mkt, t, 301)
        assert ee == pytest.approx(v0 / float(mkt.curve.df(t)), rel=2e-3)
        assert nee == pytest.approx(0.0, abs=1e-9)


def test_reset_trs_ee_is_forward_start_option():
    r, q, vol = 0.04, 0.02, 0.3
    mkt = market(r=r, q=q, vol=vol)
    (ns,), _ = build([trs(reset="3M", fund="3M", maturity="2Y")], mkt)
    leg = ns.legs[0]
    p = leg.periods[2]
    t = p.start + 0.6 * (p.end - p.start)
    a = p.start
    c = float(mkt.curve.df(a) / mkt.curve.df(t))           # strike multiplier
    Fa = 100.0 * np.exp((r - q) * a)
    m, s = np.exp((r - q) * (t - a)), vol * np.sqrt(t - a)
    d1 = (np.log(m / c) + 0.5 * s * s) / s
    call = m * norm.cdf(d1) - c * norm.cdf(d1 - s)
    put = c * norm.cdf(-(d1 - s)) - m * norm.cdf(-d1)
    ee, nee = ns_ee_nee(ns, mkt, t, 301)
    assert ee == pytest.approx(1000 * Fa * call, rel=2e-3)
    assert nee == pytest.approx(-1000 * Fa * put, rel=2e-3)


# ----------------------------------------------------------------------------- MC vs analytic
CASES = {
    "trs_no_reset": [trs(spread=0.005)],
    "trs_reset": [trs(reset="3M", spread=0.005)],
    "trs_plus_call": [trs(reset="3M", spread=0.005), opt(direction="SELL", otype="CALL", strike=110.0)],
    "collar": [opt(direction="BUY", otype="PUT", strike=90.0), opt(direction="SELL", otype="CALL", strike=110.0)],
    "short_put": [opt(direction="SELL", otype="PUT", strike=95.0, expiry="18M")],
    "extension_avg": [trs(basis="AVERAGE", avg=92.0, reset="1M", maturity="1Y")],
}


@pytest.mark.parametrize("case", list(CASES))
def test_mc_matches_analytic(case):
    mkt = market()
    nss, _ = build(CASES[case], mkt)
    c = cfg()
    res = run_monte_carlo(nss, mkt, c)
    an = run_analytic(nss, mkt, c, res)
    prof = res.ns_profile.set_index("t")
    for _, row in an.iterrows():
        se_ee = prof.loc[row.t, "EE_stderr"]
        se_nee = prof.loc[row.t, "NEE_stderr"]
        assert abs(row.EE_diff) <= 4 * se_ee + 1e-6 * 1e5, (case, row.t, row.EE_diff, se_ee)
        assert abs(row.NEE_diff) <= 4 * se_nee + 1e-6 * 1e5, (case, row.t, row.NEE_diff, se_nee)


def test_average_mode_matches_analytic():
    mkt = market()
    nss, _ = build(CASES["trs_reset"], mkt)
    c = cfg(buckets="3M,6M,1Y,2Y", mode="AVERAGE", n=20000)
    res = run_monte_carlo(nss, mkt, c)
    an = run_analytic(nss, mkt, c, res)
    assert np.all(np.abs(an.EE_diff) <= 4 * an.EE_stderr + 1.0)
    assert np.all(an.EE_analytic[:-1] > 0)       # no artificial zeros at reset dates


# ----------------------------------------------------------------------------- qualitative
def test_reset_lowers_peak_ee():
    mkt = market()
    nr, _ = build([trs(reset="NONE", maturity="3Y")], mkt)
    rr, _ = build([trs(reset="3M", maturity="3Y")], mkt)
    c = cfg(buckets="EVERY:1M", n=5000)
    a = run_monte_carlo(nr, mkt, c).summary.peak_EE[0]
    b = run_monte_carlo(rr, mkt, c).summary.peak_EE[0]
    assert b < 0.5 * a


def test_csa_shrinks_exposure():
    mkt = market()
    rows = [trs(reset="NONE", maturity="2Y")]
    nss, _ = build(rows, mkt, [dict(netting_set="NS", collateralized="Y", mpor_days=10)])
    res = run_monte_carlo(nss, mkt, cfg(n=5000))
    p = res.ns_profile
    mpor_scale = 0.4 * 0.3 * 1e5 * np.sqrt(10 / 252)       # E[max(dV,0)] over one MPOR
    assert np.all(p.EE <= 1.25 * mpor_scale)
    assert p.EE.iloc[-1] < 0.2 * p.EE_uncoll.iloc[-1]
    assert res.summary.EPE[0] < 0.3 * res.summary.EPE_uncoll[0]
    # funding exposure uses today's margin: zero threshold / MTA -> fully funded by VM
    assert np.allclose(p.FCA_exposure, 0.0) and np.allclose(p.FBA_exposure, 0.0)


def test_funding_exposure_bounded_by_threshold_and_mta():
    mkt = market()
    th, mta = 5000.0, 1000.0
    nss, _ = build([trs(reset="NONE", maturity="2Y")], mkt,
                   [dict(netting_set="NS", collateralized="Y", threshold_cp=th, threshold_bank=th, mta=mta)])
    p = run_monte_carlo(nss, mkt, cfg(n=5000)).ns_profile
    assert np.all(p.FCA_exposure <= th + mta + 1e-6) and np.all(p.FBA_exposure >= -(th + mta) - 1e-6)
    assert p.FCA_exposure.max() > 0.2 * th   # mean of a quantity capped at TH+MTA


def test_no_exposure_after_maturity_under_csa():
    mkt = market()
    nss, _ = build([trs(maturity="1Y")], mkt, [dict(netting_set="NS", collateralized="Y", mpor_days=10)])
    res = run_monte_carlo(nss, mkt, cfg(buckets="6M,13M,2Y", n=2000))
    late = res.ns_profile[res.ns_profile.t > 1.01]
    assert np.allclose(late[["EE", "NEE", "PFE"]].values, 0.0)


# ----------------------------------------------------------------------------- SA-CCR
def test_saccr_single_trs_no_reset():
    mkt = market()
    (ns,), _ = build([trs(maturity="2Y", spread=0.005)], mkt)
    v0 = leg_mtm0(ns.legs[0], mkt)
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([v0]), ns.csa, mkt)
    addon = 0.32 * 1000 * 100.0 * 1.0
    mult = min(1.0, 0.05 + 0.95 * np.exp(v0 / (2 * 0.95 * addon)))
    assert r["AddOn"][0] == pytest.approx(addon)
    assert r["EAD"][0] == pytest.approx(1.4 * (max(v0, 0) + mult * addon))


def test_saccr_reset_trs_uses_time_to_next_reset():
    mkt = market()
    (ns,), _ = build([trs(maturity="2Y", reset="3M")], mkt)
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), ns.csa, mkt)
    M = ns.legs[0].periods[0].end
    assert r["AddOn"][0] == pytest.approx(0.32 * 1e5 * np.sqrt(M))


def test_saccr_option_delta_and_hedging_set():
    mkt = market()
    (ns,), _ = build([trs(maturity="2Y"), opt(direction="BUY", otype="PUT", strike=90.0, expiry="1Y")], mkt)
    T = ns.legs[1].expiry_t
    d1 = (np.log(100 / 90) + 0.5 * 1.2 ** 2 * T) / (1.2 * np.sqrt(T))
    delta_put = -norm.cdf(-d1)
    en = 1e5 * 1.0 * 1.0 + delta_put * 1e5 * np.sqrt(min(T, 1.0))
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), ns.csa, mkt, breakdown=True)
    assert r["AddOn"][0] == pytest.approx(0.32 * abs(en), rel=1e-10)
    assert r["legs"][1]["supervisory_delta"] == pytest.approx(delta_put)
    # index underlying -> 20% supervisory factor, 75% option vol
    mi = market(is_index=True)
    (nsi,), _ = build([opt(direction="SELL", otype="CALL", strike=100.0, expiry="6M")], mi)
    Ti = nsi.legs[0].expiry_t
    d1i = (0.5 * 0.75 ** 2 * Ti) / (0.75 * np.sqrt(Ti))
    ri = saccr(nsi.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), nsi.csa, mi)
    assert ri["AddOn"][0] == pytest.approx(0.20 * norm.cdf(d1i) * 1e5 * np.sqrt(Ti))


def test_saccr_margined_rc_floor_and_mf():
    mkt = market()
    (ns,), _ = build([trs(maturity="2Y")], mkt,
                     [dict(netting_set="NS", collateralized="Y", threshold_cp=20000, mta=5000,
                           ia_received=3000, mpor_days=10)])
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), ns.csa, mkt)
    assert r["RC"][0] == pytest.approx(20000 + 5000 - 3000)
    assert r["AddOn"][0] == pytest.approx(0.32 * 1e5 * 1.5 * np.sqrt(10 / 250))
