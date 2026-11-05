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
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), ns.csa, mkt)["margined"]
    assert r["RC"][0] == pytest.approx(20000 + 5000 - 3000)
    assert r["AddOn"][0] == pytest.approx(0.32 * 1e5 * 1.5 * np.sqrt(10 / 250))


HIGH_TH_CSA = [dict(netting_set="NS", collateralized="Y", threshold_cp=8e6, threshold_bank=1e15, mta=4e5,
                    mpor_days=10)]


def test_saccr_zero_after_netting_set_matures():
    mkt = market()
    (ns,), _ = build([opt(expiry="4M")], mkt, HIGH_TH_CSA)
    t = ns.legs[0].expiry_t
    S = {"ABC": np.array([100.0, 130.0])}
    for tt in (t, t + 0.1):
        r = saccr(ns.legs, tt, S, np.zeros(2), ns.csa, mkt, breakdown=True)
        for k in ("RC", "AddOn", "PFE", "EAD"):
            assert np.all(r[k] == 0.0), (tt, k)
        assert r["legs"] == [] and r["entities"] == []
    live = saccr(ns.legs, t - 0.01, S, np.zeros(2), ns.csa, mkt)
    assert np.all(live["margined"]["RC"] == pytest.approx(8e6 + 4e5))    # floor applies while live


def test_saccr_cap_reports_binding_branch_components():
    mkt = market()
    (ns,), _ = build([opt(shares=1e5, expiry="6M")], mkt, HIGH_TH_CSA)   # TH + MTA >> exposure: cap binds
    v0 = leg_mtm0(ns.legs[0], mkt)
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([v0]), ns.csa, mkt, breakdown=True)
    assert bool(r["cap"][0])
    assert r["EAD"][0] == pytest.approx(r["unmargined"]["EAD"][0])
    assert r["EAD"][0] < r["margined"]["EAD"][0]
    assert r["RC"][0] == pytest.approx(v0) and r["AddOn"][0] == pytest.approx(r["unmargined"]["AddOn"][0])
    assert r["EAD"][0] == pytest.approx(1.4 * (r["RC"][0] + r["multiplier"][0] * r["AddOn"][0]))
    assert r["entities"][0]["entity_addon"] == pytest.approx(r["unmargined"]["AddOn"][0])
    assert r["legs"][0]["MF"] == pytest.approx(np.sqrt(ns.legs[0].expiry_t))
    res = run_monte_carlo([ns], mkt, cfg(buckets="1M,3M,6M", mode="AVERAGE", n=2000))
    sm, p = res.summary.iloc[0], res.ns_profile
    assert sm.SACCR_basis0 == "UNMARGINED_CAP"
    assert sm.SACCR_EAD0 == pytest.approx(1.4 * (sm.SACCR_RC0 + sm.SACCR_PFE0))
    assert np.allclose(p.SACCR_EAD_mean, 1.4 * (p.SACCR_RC_mean + p.SACCR_PFE_mean), rtol=1e-10)
    assert (p.SACCR_cap_share[:-1] > 0.5).all()


def test_saccr_time_averaged_ead_in_average_mode():
    mkt = market()
    nss, _ = build([opt(expiry="4M")], mkt, HIGH_TH_CSA)
    c = cfg(buckets="1M,6M,9M", mode="AVERAGE", n=2000)
    c.event_buckets = "MATURITY"
    p = run_monte_carlo(nss, mkt, c).ns_profile.reset_index(drop=True)
    assert list(p.bucket_type) == ["STANDARD", "EVENT", "STANDARD", "STANDARD"]
    e = p.iloc[1]                                   # bucket ending on the expiry date
    assert e.SACCR_EAD_mean == 0.0 and e.SACCR_RC_mean == 0.0     # point value on expiry: matured
    assert e.SACCR_EAD_avg > 0.0                    # but the trade was live through the bucket
    assert p.SACCR_EAD_avg.iloc[2:].eq(0.0).all()
    assert p.SACCR_EAD_avg[0] == pytest.approx(p.SACCR_EAD_mean[0], rel=0.25)
    pt = cfg(buckets="1M,6M,9M", mode="POINT", n=2000)
    q = run_monte_carlo(nss, mkt, pt).ns_profile
    assert np.allclose(q.SACCR_EAD_avg, q.SACCR_EAD_mean)


# ----------------------------------------------------------------------------- buckets / input guards
def test_average_bucket_straddling_maturity_uses_full_width():
    mkt = market()
    nss, _ = build([opt(direction="BUY", otype="CALL", strike=100.0, expiry="4M")], mkt)
    c = cfg(buckets="3M,6M", mode="AVERAGE", n=4000)
    res = run_monte_carlo(nss, mkt, c)
    g = res.grid_profile
    t0, t1 = c.bucket_times()
    live = g[(g.t >= t0 - 1e-9)]
    expected = np.trapezoid(live.EE, live.t) / (t1 - t0)
    ee6m = res.ns_profile.EE.iloc[1]
    assert ee6m > 0
    assert ee6m == pytest.approx(expected, rel=1e-9)
    assert ee6m < 0.5 * res.ns_profile.EE.iloc[0]     # ~1 month live out of a 3-month bucket


def _area(prof):
    """Sum of bucket value x bucket width per netting set (the integral a CVA/FVA engine sees)."""
    w = prof.groupby("netting_set").t.diff().fillna(prof.t)
    return (prof.EE * w).groupby(prof.netting_set).sum(), (prof.FCA_exposure * w).groupby(prof.netting_set).sum()


def test_event_buckets_maturity_adds_expiry_and_keeps_area():
    mkt = market()
    rows = [opt(tid="O", ns="NS_OPT", expiry="4M"), trs(tid="T", ns="NS_TRS", maturity="1Y", reset="3M")]
    nss, _ = build(rows, mkt)
    base = cfg(buckets="3M,6M,9M,1Y", mode="AVERAGE", n=4000)
    ev = cfg(buckets="3M,6M,9M,1Y", mode="AVERAGE", n=4000)
    ev.event_buckets = "MATURITY"
    r0, r1 = run_monte_carlo(nss, mkt, base), run_monte_carlo(nss, mkt, ev)
    p0, p1 = r0.ns_profile, r1.ns_profile
    o = p1[p1.netting_set == "NS_OPT"].reset_index(drop=True)
    exp_date = dt.add_tenor(VAL, "4M")
    assert list(o.bucket_type) == ["STANDARD", "EVENT", "STANDARD", "STANDARD", "STANDARD"]
    assert o.bucket_date[1] == exp_date and o.bucket_start[2] == exp_date
    assert o.EE[1] > 0 and o.EE[2] == 0.0            # nothing after expiry once the expiry is a bucket
    # TRS maturity is already a standard bucket: nothing added; other netting set unaffected
    assert (p1.netting_set == "NS_TRS").sum() == 4
    pd.testing.assert_frame_equal(p1[p1.netting_set == "NS_TRS"].reset_index(drop=True),
                                  p0[p0.netting_set == "NS_TRS"].reset_index(drop=True))
    # area under every profile unchanged (same simulation grid, so equal to rounding)
    for a0, a1 in zip(_area(p0), _area(p1)):
        assert np.allclose(a0.sort_index(), a1.sort_index(), rtol=1e-12, atol=1e-9)
    # trade-level output gets the same buckets
    assert (r1.trade_profile.trade_id == "O").sum() == 5


def test_event_buckets_all_adds_trs_resets():
    mkt = market()
    nss, _ = build([trs(maturity="1Y", reset="3M")], mkt)
    c = cfg(buckets="2M,1Y", mode="AVERAGE", n=2000)
    c.event_buckets = "ALL"
    p = run_monte_carlo(nss, mkt, c).ns_profile
    added = sorted(p[p.bucket_type == "EVENT"].bucket_date)
    assert added == [dt.add_tenor(VAL, t) for t in ("3M", "6M", "9M")]


def test_event_buckets_point_mode_uses_day_before_event():
    mkt = market()
    nss, _ = build([opt(expiry="4M")], mkt)
    c = cfg(buckets="3M,6M", mode="POINT", n=2000)
    c.event_buckets = "MATURITY"
    p = run_monte_carlo(nss, mkt, c).ns_profile
    e = p[p.bucket_type == "EVENT"].iloc[0]
    assert e.bucket_date == dt.add_tenor(VAL, "4M") - dt.timedelta(days=1)
    assert e.EE > 0


@pytest.mark.parametrize("mode", ["AVERAGE", "POINT"])
def test_zero_day_bucket_reports_valuation_date_values(mode):
    mkt = market()
    nss, _ = build([opt(expiry="4M")], mkt)
    c = cfg(buckets="0D,1M,6M", mode=mode, n=2000)
    c.event_buckets = "MATURITY"
    res = run_monte_carlo(nss, mkt, c)
    p = res.ns_profile.reset_index(drop=True)
    assert p.bucket_date[0] == VAL and p.t[0] == 0.0 and p.bucket_type[0] == "STANDARD"
    v0 = leg_mtm0(nss[0].legs[0], mkt)
    assert p.EE[0] == pytest.approx(v0, rel=1e-9) and p.MTM[0] == pytest.approx(v0, rel=1e-9)
    assert p.SACCR_EAD_mean[0] == pytest.approx(res.summary.SACCR_EAD0[0], rel=1e-9)
    assert p.SACCR_EAD_avg[0] == pytest.approx(res.summary.SACCR_EAD0[0], rel=1e-9)
    assert p.bucket_start[1] == VAL and len(p) == 4


def test_event_buckets_bad_value_rejected():
    sheets = ccy_sheets()
    sheets["Config"] = pd.concat([sheets["Config"], pd.DataFrame([("event_buckets", "RESETS")],
                                                                  columns=["key", "value"])])
    with pytest.raises(ValueError, match="event_buckets"):
        parse_inputs(sheets)


@pytest.mark.parametrize("strike, warns", [(1.1121, True), ("11.21%", True), (75.0, False), ("111.21%", False)])
def test_strike_far_below_spot_warns(strike, warns):
    mkt = market(spot=67.44)
    _, w = build([opt(strike=strike)], mkt)
    assert any("strike" in x for x in w) == warns


# ----------------------------------------------------------------------------- currency
import pandas as pd  # noqa: E402

from eqxva.examples import NS_COLUMNS, TRADE_COLUMNS  # noqa: E402
from eqxva.io_excel import parse_fx, parse_inputs  # noqa: E402

AMOUNTS = ["MTM", "EE", "NEE", "PFE", "NPFE", "EE_uncoll", "NEE_uncoll", "PFE_uncoll", "FCA_exposure",
           "FBA_exposure", "SACCR_EAD_mean", "SACCR_RC_mean", "SACCR_AddOn_mean", "SACCR_EAD_fwd",
           "SACCR_PFE_mean", "SACCR_EAD_avg"]


def ccy_sheets(reporting="USD", usdcad=1.40, extra_trades=(), csa_ccy="CAD"):
    """TOU-style CAD call under a CAD CSA; separate USD and CAD curves."""
    return dict(
        Config=pd.DataFrame([("valuation_date", "2026-10-23"), ("buckets", "1W,1M,2M,4M,6M"), ("n_paths", 4000),
                             ("seed", 3), ("output_mode", "POINT"), ("reporting_ccy", reporting)],
                            columns=["key", "value"]),
        Market=pd.DataFrame([dict(underlying="TOU", spot=67.44, div_yield=0.0297, flat_vol=0.2576, ccy="CAD"),
                             dict(underlying="SPY", spot=600.0, div_yield=0.012, flat_vol=0.18, ccy="USD")]),
        Curve=pd.DataFrame(dict(tenor=["1M", "1Y", "1M", "1Y"], zero_rate=[0.040, 0.038, 0.0225, 0.0228],
                                ccy=["USD", "USD", "CAD", "CAD"])),
        FX=pd.DataFrame(dict(pair=["USDCAD Curncy"], spot=[usdcad])),
        NettingSets=pd.DataFrame([dict(netting_set="NS", collateralized="Y", threshold_cp=8e6, threshold_bank=1e15,
                                       mta=4e5, mpor_days=10, csa_ccy=csa_ccy)], columns=NS_COLUMNS),
        Trades=pd.DataFrame([dict(trade_id="C", netting_set="NS", leg_type="OPTION", underlying="TOU",
                                  direction="BUY", shares=1e6, option_type="CALL", strike=75.0,
                                  option_maturity="2027-02-27"), *extra_trades], columns=TRADE_COLUMNS))


def run_sheets(sheets):
    mkt, nss, c, w = parse_inputs(sheets)
    return mkt, run_monte_carlo(nss, mkt, c, w)


def test_fx_pair_direction_and_crosses():
    assert parse_fx(pd.DataFrame(dict(pair=["USDCAD"], spot=[1.40])), "USD") == {"CAD": pytest.approx(1 / 1.40)}
    assert parse_fx(pd.DataFrame(dict(pair=["USDCAD"], spot=[1.40])), "CAD") == {"USD": pytest.approx(1.40)}
    fx = parse_fx(pd.DataFrame(dict(pair=["EURCAD", "USDCAD"], spot=[1.50, 1.40])), "USD")
    assert fx["EUR"] == pytest.approx(1.50 / 1.40)


def test_reporting_ccy_scales_every_output_by_fx_forward():
    mkt, cad = run_sheets(ccy_sheets("CAD"))
    _, usd = run_sheets(ccy_sheets("USD"))
    c, u = cad.ns_profile, usd.ns_profile
    x = mkt.fx("CAD", "USD", c.t.values)                 # CAD -> USD forward, starts at 1/1.40
    assert x[0] == pytest.approx(1 / 1.40 * mkt.curve_for("CAD").df(c.t[0]) / mkt.curve_for("USD").df(c.t[0]))
    for col in AMOUNTS:
        assert np.allclose(u[col], c[col] * x, rtol=1e-10, atol=1e-6), col
    # discounted EE is the same PV whichever currency it is reported in
    assert np.allclose(u.EE_disc, c.EE_disc / 1.40, rtol=1e-10)
    assert (u.ccy == "USD").all() and (c.ccy == "CAD").all()
    s_c, s_u = cad.summary.iloc[0], usd.summary.iloc[0]
    for col in ["MTM0", "SACCR_EAD0", "SACCR_RC0", "SACCR_AddOn0"]:
        assert s_u[col] == pytest.approx(s_c[col] / 1.40, rel=1e-12), col
    # LegMTM stays in the leg currency (OVME reconciliation), with a reporting-ccy column added
    lm = usd.leg_mtm.iloc[0]
    assert lm.leg_ccy == "CAD" and lm.mtm0 == pytest.approx(cad.leg_mtm.mtm0[0])
    assert lm.mtm0_reporting == pytest.approx(lm.mtm0 / 1.40)
    assert 1.3e6 < lm.mtm0 < 1.7e6                       # ~C$1.5mm, not the full notional


def test_mixed_currency_netting_set():
    spy = dict(trade_id="S", netting_set="NS", leg_type="OPTION", underlying="SPY", direction="SELL",
               shares=1e4, option_type="PUT", strike="95%", option_maturity="6M")
    _, res = run_sheets(ccy_sheets("USD", extra_trades=[spy]))
    assert res.summary.MTM0[0] == pytest.approx(res.leg_mtm.mtm0_reporting.sum(), rel=1e-12)
    assert list(res.leg_mtm.leg_ccy) == ["CAD", "USD"]
    # the same set under a USD CSA: SA-CCR notionals are converted, totals stay close
    _, res_usd_csa = run_sheets(ccy_sheets("USD", extra_trades=[spy], csa_ccy="USD"))
    assert res_usd_csa.summary.MTM0[0] == pytest.approx(res.summary.MTM0[0], rel=1e-12)


def test_missing_fx_or_curve_or_reporting_ccy_raises():
    s = ccy_sheets("USD")
    s["FX"] = s["FX"].iloc[:0]
    with pytest.raises(ValueError, match="No FX rate"):
        parse_inputs(s)
    s = ccy_sheets("USD")
    s["Curve"] = s["Curve"][s["Curve"].ccy == "USD"]
    with pytest.raises(ValueError, match="No discount curve for CAD"):
        parse_inputs(s)
    s = ccy_sheets("")
    with pytest.raises(ValueError, match="reporting_ccy"):
        parse_inputs(s)


# ----------------------------------------------------------------------------- off-market start
def shocked_market(shock=0.5, spot=100.0, r=0.04, q=0.02, vol=None):
    surf = vol or VolSurface.flat(0.3)
    u = Underlying("ABC", spot * (1 + shock), q, False, surf, ref_spot=spot)
    return MarketData(VAL, ZeroCurve.flat(r), {"ABC": u})


def test_spot_shock_trs_struck_at_pre_shock_spot():
    m0, m1 = market(), shocked_market(0.5)
    (ns0,), _ = build([trs(maturity="2Y")], m0)
    (ns1,), _ = build([trs(maturity="2Y")], m1)
    leg = ns1.legs[0]
    assert leg.periods[0].ref_price == 100.0                      # notional at the pre-shock spot
    assert leg_mtm0(ns0.legs[0], m0) == pytest.approx(0.0, abs=1e-6)
    # MTM = value of the same contract at the shocked spot
    expected = trs_value(ns0.legs[0], 0.0, np.array([150.0]), m0, np.array([150.0]))[0]
    assert leg_mtm0(leg, m1) == pytest.approx(expected, rel=1e-12)
    assert leg_mtm0(leg, m1) > 0.45 * 1000 * 100                  # ~ shares * 50 * e^{-q T}
    r = saccr(ns1.legs, 0.0, {"ABC": np.array([150.0])}, np.array([leg_mtm0(leg, m1)]), ns1.csa, m1,
              breakdown=True)
    assert r["legs"][0]["adjusted_notional"] == pytest.approx(1000 * 150.0)   # SA notional at shocked spot
    assert r["legs"][0]["sa_notional_basis"] == "MARKET"


def test_spot_shock_option_pct_strike_and_sticky_strike_vol():
    smile = VolSurface(np.array([1.0]), np.array([0.8, 1.0, 1.5]), np.array([[0.35, 0.30, 0.22]]))
    m1 = shocked_market(0.5, vol=smile)
    (ns,), _ = build([opt(strike="100%", expiry="1Y")], m1)
    leg = ns.legs[0]
    assert leg.strike == pytest.approx(100.0)                      # % of the pre-shock spot
    df = float(m1.curve.df(leg.expiry_t))
    px = bs_price(150.0, 100.0, leg.expiry_t, df, 0.02, 0.30, True)  # vol of the K=100 strike (sticky)
    assert leg_mtm0(leg, m1) == pytest.approx(1000 * px, rel=1e-10)
    assert float(m1.underlyings["ABC"].atm_vol(1.0)) == pytest.approx(0.22)   # simulation: vol at new ATM
    S = run_monte_carlo([ns], m1, cfg(buckets="1M", n=2000)).leg_mtm
    assert S.spot[0] == 150.0 and S.trade_spot[0] == 100.0


def test_trade_level_spot_shock_alongside_unshocked_copy():
    sheets = ccy_sheets(csa_ccy="USD")
    base = sheets["Trades"].iloc[0].to_dict()
    up = dict(base, trade_id="C_UP", netting_set="NS_UP", spot_shock="50%")
    sheets["Trades"] = pd.DataFrame([base, up], columns=TRADE_COLUMNS)
    sheets["NettingSets"] = pd.DataFrame([dict(netting_set=n, collateralized="N") for n in ("NS", "NS_UP")],
                                         columns=NS_COLUMNS)
    mkt, nss, c, w = parse_inputs(sheets)
    assert mkt.underlyings["TOU"].spot == 67.44 and mkt.underlyings["TOU"].ref_spot is None   # market untouched
    leg0, leg1 = nss[0].legs[0], nss[1].legs[0]
    assert leg0.underlying == "TOU" and leg1.underlying == "TOU+50%"
    u = mkt.underlyings["TOU+50%"]
    assert u.spot == pytest.approx(67.44 * 1.5) and u.trade_spot == 67.44 and u.root == "TOU"
    assert leg1.strike == 75.0 and any("C_UP: spot shocked +50.00%" in x for x in w)
    res = run_monte_carlo(nss, mkt, c, w)
    lm = res.leg_mtm.set_index("trade_id")
    assert lm.loc["C_UP", "spot_shock"] == 0.5 and lm.loc["C", "spot_shock"] == 0.0
    assert lm.loc["C_UP", "mtm0"] > 3 * lm.loc["C", "mtm0"]
    # same random numbers: with a flat vol the shocked paths are the market paths x 1.5
    from eqxva.exposure import build_grid
    from eqxva.simulation import simulate
    grid = build_grid(nss, c)
    S = simulate(mkt, ["TOU", "TOU+50%"], grid, 200, 3)
    assert np.allclose(S["TOU+50%"], 1.5 * S["TOU"])
    # unshocked netting set identical to a run without the shocked copy
    alone = run_monte_carlo([nss[0]], mkt, c, w).ns_profile
    pd.testing.assert_frame_equal(alone.reset_index(drop=True),
                                  res.ns_profile[res.ns_profile.netting_set == "NS"].reset_index(drop=True))
    # SA-CCR: the shocked copy is the same entity as its market underlying
    assert set(res.saccr_entities.underlying) == {"TOU"}


def test_trade_level_spot_shock_validation():
    mkt = market()
    with pytest.raises(ValueError, match="same spot_shock"):
        build([dict(opt(), leg_id="L1", spot_shock=0.5), dict(opt(), leg_id="L2", spot_shock=0.2)], mkt)
    with pytest.raises(ValueError, match="not 50"):
        build([dict(opt(), spot_shock=50)], mkt)
    with pytest.raises(ValueError, match="not in market data"):
        build([dict(opt(), underlying="ABC+50%")], mkt)


def test_blend_extend_sa_notional_uses_previous_strike():
    mkt = market()
    (ns,), w = build([trs(basis="BLEND_EXTEND", avg=90.0, maturity="2Y")], mkt)
    leg = ns.legs[0]
    assert leg.sa_notional_basis == "STRIKE" and leg.periods[0].ref_price == 90.0
    assert leg_mtm0(leg, mkt) > 0.9 * 1000 * 10                     # extension MTM carried in the strike
    r = saccr(ns.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), ns.csa, mkt, breakdown=True)
    assert r["legs"][0]["adjusted_notional"] == pytest.approx(1000 * 90.0)
    assert r["AddOn"][0] == pytest.approx(0.32 * 1000 * 90.0)
    # explicit MARKET override -> shares * current price
    (nsm,), _ = build([dict(trs(basis="AVERAGE", avg=90.0), sa_notional_basis="MARKET")], mkt)
    rm = saccr(nsm.legs, 0.0, {"ABC": np.array([100.0])}, np.array([0.0]), nsm.csa, mkt)
    assert rm["AddOn"][0] == pytest.approx(0.32 * 1000 * 100.0)
    # combined with a spot shock: still the previous strike, MTM at the shocked spot
    m1 = shocked_market(0.5)
    (nss,), _ = build([trs(basis="AVERAGE", avg=90.0, maturity="2Y")], m1)
    rs = saccr(nss.legs, 0.0, {"ABC": np.array([150.0])}, np.array([0.0]), nss.csa, m1, breakdown=True)
    assert rs["legs"][0]["adjusted_notional"] == pytest.approx(1000 * 90.0)


def test_blend_extend_with_reset_uses_reset_price_after_reset():
    mkt = market()
    (ns,), _ = build([trs(basis="AVERAGE", avg=90.0, reset="3M", maturity="1Y")], mkt)
    leg = ns.legs[0]
    t1 = leg.periods[1].start
    S = {"ABC": np.array([100.0, 120.0])}
    r0 = saccr(ns.legs, t1 / 2, S, np.zeros(2), ns.csa, mkt)
    assert np.allclose(r0["unmargined"]["AddOn"] / 0.32 / 1000 / np.sqrt(leg.periods[0].end - t1 / 2), 90.0)
    fixings = np.array([95.0, 130.0])
    r1 = saccr(ns.legs, t1 + 0.05, S, np.zeros(2), ns.csa, mkt, fix=lambda u, tf: fixings)
    M = leg.periods[1].end - (t1 + 0.05)
    assert np.allclose(r1["AddOn"], 0.32 * 1000 * fixings * np.sqrt(max(M, 10 / 250)))
    res = run_monte_carlo([ns], mkt, cfg(buckets="1M,4M,7M,1Y", n=2000))     # engine wiring
    assert (res.ns_profile.SACCR_EAD_mean.iloc[:3] > 0).all()


def test_sa_notional_basis_on_option_warns():
    mkt = market()
    _, w = build([dict(opt(), sa_notional_basis="STRIKE")], mkt)
    assert any("sa_notional_basis applies to TRS" in x for x in w)


def test_blend_extend_with_reset_defers_mtm_to_maturity():
    mkt = market()
    (ns,), w = build([trs(basis="BLEND_EXTEND", avg=90.0, reset="3M", maturity="1Y")], mkt)
    leg = ns.legs[0]
    assert leg.notional_basis == "BLEND_EXTEND" and leg.sa_notional_basis == "STRIKE"
    assert leg.periods[0].ref_price == 100.0                       # struck at market
    assert all(p.ref_price is None for p in leg.periods[1:])       # later periods re-fix on the path
    assert leg.initial_mtm == pytest.approx(1000 * (100.0 - 90.0))  # deferred extension MTM
    assert leg.im_settle == pytest.approx(leg.maturity)
    assert not any("ignored" in x for x in w)
    # MTM0 ~ value of the AVERAGE (settle at first reset) version: both carry shares*(S0 - prev)
    (na,), _ = build([trs(basis="AVERAGE", avg=90.0, reset="3M", maturity="1Y")], mkt)
    assert leg_mtm0(leg, mkt) == pytest.approx(leg_mtm0(na.legs[0], mkt), rel=0.01)
    # just after the first reset the deferred MTM is still owed (accreted); AVERAGE has settled it
    t = leg.periods[0].end + 1e-4
    S = np.array([100.0])
    v_be = trs_value(leg, t, S, mkt, S)[0]
    v_av = trs_value(na.legs[0], t, S, mkt, S)[0]
    assert v_be == pytest.approx(10000.0 * np.exp(0.04 * t), rel=1e-3) and abs(v_av) < 50.0
    # paid at maturity: nothing left after
    assert trs_value(leg, leg.maturity, S, mkt, S)[0] == 0.0
    # SA notional = shares * previous strike for life, also after resets
    r = saccr(ns.legs, t, {"ABC": np.array([100.0, 130.0])}, np.zeros(2), ns.csa, mkt,
              fix=lambda u, tf: np.array([95.0, 125.0]), breakdown=True)
    assert r["legs"][0]["adjusted_notional"] == pytest.approx(1000 * 90.0)
    # engine: exposure persists to maturity
    res = run_monte_carlo([ns], mkt, cfg(buckets="1M,4M,7M,11M", n=4000))
    lm = res.leg_mtm.iloc[0]
    assert lm.carried_mtm == pytest.approx(10000.0) and lm.carried_mtm_settle == leg.maturity_date
    assert (res.ns_profile.EE > 9000).all()


def test_blend_extend_with_reset_under_shock_and_user_initial_mtm():
    m1 = shocked_market(0.5)
    (ns,), w = build([trs(basis="BLEND_EXTEND", avg=90.0, reset="3M", im=5000.0)], m1)
    leg = ns.legs[0]
    assert any("initial_mtm=5,000.00 ignored" in x for x in w)
    assert leg.periods[0].ref_price == 100.0                       # pre-shock spot
    assert leg.initial_mtm == pytest.approx(1000 * (100.0 - 90.0))  # extension MTM at the pre-shock spot
    assert leg_mtm0(leg, m1) > 1000 * (150.0 - 90.0) * 0.97         # shock move on top, in the live period
    # no-reset blend & extend unchanged: single period struck at the previous strike
    (n2,), _ = build([trs(basis="BLEND_EXTEND", avg=90.0)], m1)
    assert n2.legs[0].periods[0].ref_price == 90.0 and n2.legs[0].initial_mtm == 0.0
