from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy.integrate import quad

from eqxva.analytic import run_analytic
from eqxva.commodity import analytic as com
from eqxva.commodity import examples as ex
from eqxva.commodity.nfactor import covariance, simulate_futures
from eqxva.commodity.swap import swap_value
from eqxva.exposure import build_grid, leg_mtm0, run_monte_carlo
from eqxva.io_excel import parse_inputs, read_workbook
from eqxva.market import Underlying, VolSurface
from eqxva.model import build_netting_sets
from eqxva.saccr import saccr

VAL = ex.VAL_DATE


def sheets(**over):
    s = ex.example_sheets()
    s.update(over)
    return s


def parsed(**over):
    return parse_inputs(sheets(**over))


def ns_named(nss, name):
    return next(n for n in nss if n.netting_set == name)


def fast(cfg, n=20000):
    cfg.n_paths = n
    return cfg


# ----------------------------------------------------------------------------- model
def test_calibration_reprices_option_variance():
    mkt, _, _, _ = parsed()
    for c in (u for u in mkt.underlyings.values()):
        v = covariance(mkt.commodity, c, c.option_expiry_t, c, c.option_expiry_t)
        assert v == pytest.approx(c.implied_vol ** 2 * c.option_expiry_t, rel=1e-10)


def test_variance_matches_page_11_integral():
    mkt, _, _, _ = parsed()
    c = mkt.underlyings["ZSK8"]
    beta, k, T, t = ex.BETA, ex.SHORT_RATIO, c.expiry_t, 1.1
    qL, qS = c.q_ref, ex.SHORT_RATIO * c.q_ref
    assert k == pytest.approx(qS / qL)
    ref = quad(lambda u: qS ** 2 * np.exp(-2 * beta * (T - u)) + qL ** 2, 0, t)[0]   # rho_SL = 0 in the example
    assert covariance(mkt.commodity, c, t, c, t) == pytest.approx(ref, rel=1e-9)


def test_variance_with_short_long_correlation():
    corr = pd.DataFrame([(f"{ex.BEANS}.SHORT", f"{ex.BEANS}.LONG", 0.3)], columns=["factor1", "factor2", "rho"])
    mkt, _, _, _ = parsed(FactorCorrelation=corr)
    c = mkt.underlyings["ZSK8"]
    b, T, t, qL = ex.BETA, c.expiry_t, 1.1, c.q_ref
    qS = ex.SHORT_RATIO * qL
    ref = quad(lambda u: qS ** 2 * np.exp(-2 * b * (T - u)) + qL ** 2
               + 2 * 0.3 * qS * qL * np.exp(-b * (T - u)), 0, t)[0]
    assert covariance(mkt.commodity, c, t, c, t) == pytest.approx(ref, rel=1e-9)


def test_simulation_martingale_variance_and_cross_covariance():
    mkt, _, _, _ = parsed()
    names = ["ZSK8", "ZMK8", "ZLK8", "ZSK7"]
    grid = np.array([0.0, 0.25, mkt.underlyings["ZSK7"].expiry_t, 1.0, 1.4])
    S = simulate_futures(mkt, names, grid, 400_000, seed=3)
    for n in names:
        c = mkt.underlyings[n]
        for k in (2, 4):
            t = grid[k]
            se = S[n][:, k].std() / np.sqrt(S[n].shape[0])
            assert abs(S[n][:, k].mean() - c.spot) < 5 * se
            assert np.log(S[n][:, k]).var() == pytest.approx(covariance(mkt.commodity, c, t, c, t), rel=0.01)
    a, b = mkt.underlyings["ZSK8"], mkt.underlyings["ZLK8"]
    emp = np.cov(np.log(S["ZSK8"][:, 3]), np.log(S["ZLK8"][:, 3]))[0, 1]
    assert emp == pytest.approx(covariance(mkt.commodity, a, 1.0, b, 1.0), rel=0.02)


def test_contract_frozen_after_expiry():
    mkt, _, _, _ = parsed()
    c = mkt.underlyings["ZSK7"]
    grid = np.array([0.0, c.expiry_t, c.expiry_t + 0.25, 1.0])
    S = simulate_futures(mkt, ["ZSK7"], grid, 100)["ZSK7"]
    assert np.array_equal(S[:, 1], S[:, 2]) and np.array_equal(S[:, 1], S[:, 3])


def test_one_factor_and_three_factor_inputs_run():
    f1 = pd.DataFrame([(p, "LONG", 0.0, 1.0) for p in (ex.BEANS, ex.MEAL, ex.OIL)],
                      columns=["product", "factor", "beta", "vol_ratio"])
    mkt, _, _, w = parsed(CommodityFactors=f1, FactorCorrelation=pd.DataFrame(columns=["factor1", "factor2", "rho"]))
    c = mkt.underlyings["ZSK8"]
    assert covariance(mkt.commodity, c, 1.0, c, 1.0) == pytest.approx(ex.VOL[ex.BEANS] ** 2, rel=1e-10)
    assert any("rho = 0" in x for x in w)
    f3 = pd.DataFrame([r for p in (ex.BEANS, ex.MEAL, ex.OIL) for r in
                       ((p, "A", 4.0, 0.5), (p, "B", 1.0, 0.5), (p, "C", 0.0, 1.0))],
                      columns=["product", "factor", "beta", "vol_ratio"])
    mkt, _, _, _ = parsed(CommodityFactors=f3, FactorCorrelation=pd.DataFrame(columns=["factor1", "factor2", "rho"]))
    assert mkt.commodity.corr.shape == (9, 9)


# ----------------------------------------------------------------------------- swap
def test_unit_conversion_and_notionals():
    mkt, nss, _, _ = parsed()
    assert mkt.underlyings["ZSK7"].spot == pytest.approx(11.81)
    assert mkt.underlyings["ZLK7"].spot == pytest.approx(0.6222)
    legs = {l.leg_id: l for l in ns_named(nss, "NS_DEC27").legs}
    assert legs[ex.BEANS].quantity * mkt.underlyings[legs[ex.BEANS].underlying].spot == pytest.approx(59_050_000)
    assert legs[ex.MEAL].quantity * mkt.underlyings[legs[ex.MEAL].underlying].spot == pytest.approx(34_914_000)
    assert legs[ex.OIL].quantity * mkt.underlyings[legs[ex.OIL].underlying].spot == pytest.approx(34_221_000)


def test_direction_and_at_market():
    mkt, nss, _, _ = parsed()
    legs = {l.leg_id: l for l in ns_named(nss, "NS_MAY27").legs}
    assert (legs[ex.BEANS].omega, legs[ex.MEAL].omega, legs[ex.OIL].omega) == (-1, 1, 1)
    assert sum(leg_mtm0(l, mkt) for l in legs.values()) == 0.0


def test_dec27_all_legs_fix_with_x7():
    _, nss, _, _ = parsed()
    assert {l.fixing_date for l in ns_named(nss, "NS_DEC27").legs} == {date(2027, 11, 12)}


def test_swap_value_before_after_fixing_and_payment():
    t = ex._trades()
    t = t[t.trade_id == "CRUSH_MAY27"].copy()
    t["payment_date"] = date(2027, 5, 21)
    mkt, nss, _, _ = parsed(Trades=t)
    leg = nss[0].legs[1]
    F = np.array([300.0])
    df = mkt.curve.df_fwd(0.25, leg.payment_t)
    before = swap_value(leg, 0.25, F, mkt)[0]
    assert before == pytest.approx(leg.quantity * (300.0 - leg.fixed_price) * df)
    mid = (leg.fixing_t + leg.payment_t) / 2
    after = swap_value(leg, mid, np.array([999.0]), mkt, F_fix=F)[0]
    assert after == pytest.approx(leg.quantity * (300.0 - leg.fixed_price) * mkt.curve.df_fwd(mid, leg.payment_t))
    assert swap_value(leg, leg.payment_t, F, mkt)[0] == 0.0


def test_lots_warning_and_lots_input():
    _, _, _, w = parsed()
    assert any("916.67 lots" in x for x in w)
    t = ex._trades()
    m = t.leg_id == ex.OIL
    t.loc[m, "quantity"] = np.nan
    t.loc[m, "lots"] = 900
    _, nss, _, _ = parsed(Trades=t)
    assert next(l for l in nss[0].legs if l.leg_id == ex.OIL).quantity == 54_000_000


@pytest.mark.parametrize("col,val,msg", [("price_unit", "C/TON", "price_unit"), ("hedging_set", "GRAINS", "hedging_set")])
def test_bad_product_inputs(col, val, msg):
    p = ex.example_sheets()["CommodityProducts"]
    p.loc[0, col] = val
    with pytest.raises(ValueError, match=msg):
        parsed(CommodityProducts=p)


def test_unit_quantity_mismatch():
    p = ex.example_sheets()["CommodityProducts"]
    p.loc[0, "quantity_unit"] = "TON"
    with pytest.raises(ValueError, match="does not match"):
        parsed(CommodityProducts=p)


def test_invalid_correlation_rejected():
    c = pd.DataFrame([(f"{ex.BEANS}.LONG", f"{ex.MEAL}.LONG", 0.95), (f"{ex.BEANS}.LONG", f"{ex.OIL}.LONG", 0.95),
                      (f"{ex.MEAL}.LONG", f"{ex.OIL}.LONG", -0.95)], columns=["factor1", "factor2", "rho"])
    with pytest.raises(ValueError, match="not a valid correlation"):
        parsed(FactorCorrelation=c)


def test_fixing_after_contract_expiry_rejected():
    t = ex._trades()
    t["fixing_date"] = date(2030, 1, 1)
    with pytest.raises(ValueError, match="after the expiry"):
        parsed(Trades=t)


# ----------------------------------------------------------------------------- SA-CCR
def t0_ead(ns, mkt):
    S0 = {l.underlying: np.array([mkt.underlyings[l.underlying].spot]) for l in ns.legs}
    return saccr(ns.legs, 0.0, S0, np.zeros(1), ns.csa, mkt)


@pytest.mark.parametrize("ns,addon,ead", [("NS_DEC27", 12.66786, 17.7350), ("NS_MAY28", 12.66786, 17.7350),
                                          ("NS_MAY27", 9.1398, 12.7957)])
def test_saccr_three_types_matches_hand_calculation(ns, addon, ead):
    mkt, nss, _, _ = parsed()
    r = t0_ead(ns_named(nss, ns), mkt)
    assert r["AddOn"][0] / 1e6 == pytest.approx(addon, abs=2e-3)
    assert r["EAD"][0] / 1e6 == pytest.approx(ead, abs=3e-3)


def test_saccr_one_type_is_an_input():
    p = ex.example_sheets()["CommodityProducts"]
    p["commodity_type"] = "SOYBEAN_COMPLEX"
    mkt, nss, _, _ = parsed(CommodityProducts=p)
    assert t0_ead(ns_named(nss, "NS_DEC27"), mkt)["EAD"][0] / 1e6 == pytest.approx(1.4 * 1.8153, abs=1e-3)


def test_alpha_is_an_input():
    n = ex.example_sheets()["NettingSets"]
    n["sa_alpha"] = 1.0
    mkt, nss, _, _ = parsed(NettingSets=n)
    assert t0_ead(ns_named(nss, "NS_DEC27"), mkt)["EAD"][0] / 1e6 == pytest.approx(12.668, abs=2e-3)


def test_fixed_unpaid_leg_stays_in_rc_with_no_addon():
    t = ex._trades()
    t = t[t.trade_id == "CRUSH_MAY27"].copy()
    t["payment_date"] = date(2027, 5, 28)
    mkt, nss, _, _ = parsed(Trades=t)
    ns = nss[0]
    tt = ns.legs[0].fixing_t + 2 / 365
    S = {l.underlying: np.array([mkt.underlyings[l.underlying].spot]) for l in ns.legs}
    r = saccr(ns.legs, tt, S, np.array([500_000.0]), ns.csa, mkt)
    assert r["AddOn"][0] == 0.0 and r["RC"][0] == 500_000.0
    assert r["EAD"][0] == pytest.approx(1.4 * 500_000)


def test_hedging_sets_do_not_offset():
    p = ex.example_sheets()["CommodityProducts"]
    p.loc[p["product"] == ex.BEANS, "hedging_set"] = "OTHER"
    mkt, nss, _, _ = parsed(CommodityProducts=p)
    a = 0.18 * 59.05
    b = 0.18 * np.array([34.914, 34.221])
    other = np.sqrt((0.4 * b.sum()) ** 2 + 0.84 * (b ** 2).sum())
    assert t0_ead(ns_named(nss, "NS_DEC27"), mkt)["AddOn"][0] / 1e6 == pytest.approx(a + other, abs=1e-3)


def test_sa_notional_proxy():
    mkt, nss, cfg, w = parsed()
    res = run_monte_carlo(nss, mkt, fast(cfg, 500), w)
    px = res.saccr_proxy.set_index("trade_id")
    for t in ("CRUSH_MAY27", "CRUSH_DEC27", "CRUSH_MAY28"):
        assert px.loc[t, "SA_NOTIONAL"] / 1e6 == pytest.approx(70.377, abs=2e-3)
        assert 1.4 * px.loc[t, "SF"] * px.loc[t, "SA_NOTIONAL"] * px.loc[t, "MF"] == pytest.approx(
            res.summary.set_index("netting_set").loc["NS_" + t[-5:], "SACCR_EAD0"])


# ----------------------------------------------------------------------------- engine
@pytest.fixture(scope="module")
def run():
    mkt, nss, cfg, w = parsed()
    cfg = fast(cfg, 40000)
    res = run_monte_carlo(nss, mkt, cfg, w)
    res.analytic = run_analytic(nss, mkt, cfg, res)
    return mkt, nss, cfg, res


def test_mc_matches_exact_quadrature(run):
    _, _, _, res = run
    a = res.analytic[(res.analytic.netting_set != "NS_STRIP") & (res.analytic.EE_analytic > 1e5)]
    assert len(a) > 20
    assert (a.EE_diff.abs() / a.EE_analytic).max() < 0.03


def test_normal_approximation_is_above_exact_ee(run):
    _, _, _, res = run
    a = res.analytic[(res.analytic.netting_set != "NS_STRIP") & (res.analytic.EE_analytic > 1e5)]
    assert (a.EE_normal >= a.EE_analytic * 0.9999).all()
    assert (a.EE_normal <= a.EE_analytic * 1.08).all()


def test_exposure_ends_at_payment(run):
    mkt, nss, cfg, res = run
    p = res.ns_profile[res.ns_profile.netting_set == "NS_MAY27"]
    after = p[p.bucket_date > date(2027, 6, 30)]
    assert (after[["EE", "PFE", "SACCR_EAD_mean"]].abs().sum() == 0).all()


def test_ead_profile_starts_at_t0_and_falls_into_maturity(run):
    _, _, _, res = run
    p = res.ns_profile[res.ns_profile.netting_set == "NS_DEC27"]
    m = (date(2027, 11, 12) - p.bucket_date.iloc[0]).days / 365     # first bucket: MF already below 1
    assert p.SACCR_EAD_fwd.iloc[0] == pytest.approx(17.735e6 * np.sqrt(m), rel=0.001)
    assert p[p.bucket_date >= date(2027, 11, 1)].SACCR_EAD_fwd.iloc[0] < 0.5 * p.SACCR_EAD_fwd.iloc[0]


def test_collateral_reduces_exposure():
    n = ex.example_sheets()["NettingSets"]
    n["collateralized"] = "Y"
    n["threshold_cp"] = n["threshold_bank"] = n["mta"] = 0.0
    mkt, nss, cfg, w = parsed(NettingSets=n)
    res = run_monte_carlo(nss, mkt, fast(cfg, 5000), w)
    s = res.summary.set_index("netting_set").loc["NS_DEC27"]
    assert s.EPE < 0.3 * s.EPE_uncoll
    assert s.SACCR_EAD0 < 17.735e6


def test_strip_nets_trades(run):
    _, _, _, res = run
    s = res.summary.set_index("netting_set")
    assert s.loc["NS_STRIP", "n_trades"] == 3
    assert s.loc["NS_STRIP", "SACCR_EAD0"] == pytest.approx(
        s.loc[["NS_MAY27", "NS_DEC27", "NS_MAY28"], "SACCR_EAD0"].sum(), rel=0.002)


def test_mixed_equity_and_commodity_netting_set():
    mkt, nss, cfg, w = parsed()
    mkt.underlyings["ABC"] = Underlying("ABC", 100.0, 0.0, False, VolSurface.flat(0.3), ccy="USD")
    eq = dict(trade_id="EQ", netting_set="NS_DEC27", leg_type="TRS", underlying="ABC", direction="RECEIVE_EQUITY",
              shares=1000, maturity="1Y", equity_reset_freq="NONE", funding_reset_freq="3M", funding_spread=0.0,
              notional_basis="CURRENT")
    extra = build_netting_sets([eq], [], mkt, [])[0]
    ns = ns_named(nss, "NS_DEC27")
    alone = t0_ead(ns, mkt)["AddOn"][0]
    ns.trades += extra.trades
    res = run_monte_carlo([ns], mkt, fast(cfg, 1000), w)
    assert res.summary.SACCR_AddOn0.iloc[0] == pytest.approx(alone + 0.32 * 100_000, rel=1e-6)
    a = run_analytic([ns], mkt, cfg)
    assert np.isnan(a[a.t < ns.maturity].EE_analytic).all()


def test_workbook_round_trip(tmp_path):
    f = tmp_path / "crush.xlsx"
    ex.write_example_inputs(f)
    mkt, nss, cfg, w = parse_inputs(read_workbook(f))
    assert len(nss) == 4
    assert sum(len(n.trades) for n in nss) == 6
    assert t0_ead(ns_named(nss, "NS_DEC27"), mkt)["EAD"][0] / 1e6 == pytest.approx(17.735, abs=3e-3)


def test_point_mode_and_non_usd_reporting():
    s = ex.example_sheets()
    cfg = s["Config"].copy()
    cfg.loc[cfg.key == "output_mode", "value"] = "POINT"
    cfg.loc[cfg.key == "reporting_ccy", "value"] = "CAD"
    curve = pd.concat([s["Curve"], s["Curve"].assign(ccy="CAD", zero_rate=0.025)])
    fx = pd.DataFrame(dict(pair=["USDCAD"], spot=[1.40]))
    t = s["Trades"]
    mkt, nss, c, w = parse_inputs(sheets(Config=cfg, Curve=curve, FX=fx, Trades=t[t.trade_id == "CRUSH_DEC27"]))
    res = run_monte_carlo(nss, mkt, fast(c, 40000), w)
    a = run_analytic(nss, mkt, c, res)
    a = a[a.EE_analytic > 1e5]
    assert len(a) > 5
    assert (a.EE_diff.abs() / a.EE_analytic).max() < 0.03
    assert res.summary.SACCR_EAD0.iloc[0] == pytest.approx(1.4 * 17.735e6, rel=1e-3)


def test_exposure_compare_columns_and_conservatism(run):
    _, _, _, res = run
    c = res.tables()["ExposureCompare"]
    assert {"EE_sim", "EE_exact", "EE_cons", "NEE_sim", "NEE_exact", "NEE_cons", "SACCR_EAD_sim",
            "SACCR_EAD_cons"} <= set(c.columns)
    live = c[c.SACCR_EAD_sim > 1e5]
    assert len(live) > 20
    assert (live.SACCR_EAD_cons >= live.SACCR_EAD_sim).all()
    assert (live.EE_cons >= live.EE_exact * 0.9999).all()
    assert (c[c.bucket_date > date(2028, 6, 30)].SACCR_EAD_cons == 0).all()


def test_conservative_ead_at_t0_matches_saccr():
    mkt, nss, _, _ = parsed()
    assert com.conservative_profile(ns_named(nss, "NS_DEC27"), 0.0, mkt)[5] / 1e6 == pytest.approx(17.735, abs=3e-3)


def test_analytic_only_run_has_conservative_ead():
    mkt, nss, cfg, _ = parsed()
    a = run_analytic(nss, mkt, cfg)
    assert {"EE_normal", "NEE_normal", "AddOn_fwd", "SACCR_EAD_cons"} <= set(a.columns)


def test_nee_is_negative_and_matches_simulation(run):
    _, _, _, res = run
    c = res.exposure_compare
    live = c[c.EE_exact > 1e5]
    assert (live.NEE_cons < 0).all() and (live.NEE_exact < 0).all() and (live.NEE_sim < 0).all()
    assert (live.NEE_cons <= live.NEE_exact * 0.9999).all()
    assert ((live.NEE_sim / live.NEE_exact - 1).abs() < 0.03).all()
    strip = res.analytic[(res.analytic.netting_set == "NS_STRIP") & (res.analytic.EE_analytic > 1e5)]
    assert (strip.NEE_analytic < 0).all()
    assert ((strip.NEE_MC_uncoll / strip.NEE_analytic - 1).abs() < 0.08).all()


def test_off_market_normal_approx_identity():
    t = ex._trades()
    t = t[t.trade_id == "CRUSH_DEC27"].copy()
    t.loc[t.leg_id == ex.MEAL, "fixed_price"] = 290.0
    mkt, nss, _, _ = parsed(Trades=t)
    m, _ = com.moments(nss[0].legs, 0.5, mkt)
    s, ee, nee, _ = com.normal_approx(nss[0].legs, 0.5, mkt)
    assert m > 0 and ee + nee == pytest.approx(m)
