"""Commodity input sheets.

  CommodityProducts  product | hedging_set | commodity_type | lot_size | quantity_unit | price_unit | ccy
  CommodityFactors   product | factor | beta | vol_ratio       (N rows per product; one ratio must be 1)
  FactorCorrelation  factor1 | factor2 | rho                   (factors named PRODUCT.FACTOR; blank pair = 0)
  Contracts          contract | product | expiry | option_expiry | price | implied_vol | bbg_ticker
Price is in the product's price_unit. A blank option_expiry means the contract expiry.
"""
from __future__ import annotations

import numpy as np

from .. import dates as dt
from ..io_excel import _ccy, _clean, _num, _records
from .market import PRICE_UNITS, CommodityModel, Factor, FuturesContract, Product
from .nfactor import calibrate

HEDGING_SETS = ("ENERGY", "METALS", "AGRICULTURAL", "OTHER")


def _txt(x) -> str:
    return "" if x is None else str(x).strip()


def parse_products(sheets, rep: str) -> dict[str, Product]:
    prods: dict[str, Product] = {}
    for r in _records(_clean(sheets["CommodityProducts"])):
        name = _txt(r.get("product")).upper()
        if not name:
            continue
        hs = _txt(r.get("hedging_set")).upper()
        if hs not in HEDGING_SETS:
            raise ValueError(f"Product {name}: hedging_set must be one of {', '.join(HEDGING_SETS)} (got '{hs}')")
        pu, qu = _txt(r.get("price_unit")).upper(), _txt(r.get("quantity_unit")).upper()
        if pu not in PRICE_UNITS:
            raise ValueError(f"Product {name}: price_unit must be one of {', '.join(PRICE_UNITS)} (got '{pu}')")
        if PRICE_UNITS[pu][1] != qu:
            raise ValueError(f"Product {name}: price_unit {pu} does not match quantity_unit {qu}")
        prods[name] = Product(name, [], hs, _txt(r.get("commodity_type")).upper() or name,
                              _num(r.get("lot_size"), f"Product {name} lot_size"), qu, pu,
                              _ccy(r.get("ccy")) or rep)
    return prods


def parse_factors(sheets, prods: dict[str, Product]) -> list[str]:
    for r in _records(_clean(sheets["CommodityFactors"])):
        p = _txt(r.get("product")).upper()
        if not p:
            continue
        if p not in prods:
            raise ValueError(f"CommodityFactors: product '{p}' is not on CommodityProducts")
        f = f"{p}.{_txt(r.get('factor')).upper()}"
        beta = _num(r.get("beta"), f"Factor {f} beta")
        if beta < 0:
            raise ValueError(f"Factor {f}: beta must not be negative")
        prods[p].factors.append(Factor(f, beta, _num(r.get("vol_ratio"), f"Factor {f} vol_ratio")))
    names = []
    for p in prods.values():
        if not p.factors:
            raise ValueError(f"Product {p.name}: no rows on CommodityFactors")
        if not any(abs(f.ratio - 1.0) < 1e-12 for f in p.factors):
            raise ValueError(f"Product {p.name}: one factor needs vol_ratio = 1 (the reference factor)")
        names += [f.name for f in p.factors]
    return names


def parse_correlation(sheets, names: list[str], warnings: list[str]) -> np.ndarray:
    pos = {n: i for i, n in enumerate(names)}
    c = np.eye(len(names))
    seen = set()
    df = sheets.get("FactorCorrelation")
    if df is not None:
        for r in _records(_clean(df)):
            a, b = _txt(r.get("factor1")).upper(), _txt(r.get("factor2")).upper()
            if not a and not b:
                continue
            for x in (a, b):
                if x not in pos:
                    raise ValueError(f"FactorCorrelation: unknown factor '{x}' (use PRODUCT.FACTOR)")
            rho = _num(r.get("rho"), f"FactorCorrelation {a}/{b}")
            if abs(rho) > 1:
                raise ValueError(f"FactorCorrelation {a}/{b}: rho {rho} is outside [-1, 1]")
            c[pos[a], pos[b]] = c[pos[b], pos[a]] = rho
            seen.add(frozenset((a, b)))
    w = np.linalg.eigvalsh(c)
    if w.min() < -1e-8:
        raise ValueError(f"FactorCorrelation is not a valid correlation matrix (smallest eigenvalue "
                         f"{w.min():.4f}): adjust the rho values")
    n_all = len(names) * (len(names) - 1) // 2
    if len(seen) < n_all:
        warnings.append(f"FactorCorrelation: {n_all - len(seen)} of {n_all} factor pairs are blank and use rho = 0.")
    return c


def parse_commodity(sheets, val, rep: str, warnings: list[str]):
    """Return (CommodityModel, {contract: FuturesContract}); (None, {}) with no Contracts sheet."""
    if sheets.get("Contracts") is None:
        return None, {}
    for s in ("CommodityProducts", "CommodityFactors"):
        if sheets.get(s) is None:
            raise ValueError(f"Contracts needs the {s} sheet")
    prods = parse_products(sheets, rep)
    names = parse_factors(sheets, prods)
    model = CommodityModel(prods, names, parse_correlation(sheets, names, warnings))
    out: dict[str, FuturesContract] = {}
    for r in _records(_clean(sheets["Contracts"])):
        name = _txt(r.get("contract"))
        if not name:
            continue
        p = _txt(r.get("product")).upper()
        if p not in prods:
            raise ValueError(f"Contract {name}: product '{p}' is not on CommodityProducts")
        exp = dt.to_date(r["expiry"])
        oexp = exp if r.get("option_expiry") is None else dt.to_date(r["option_expiry"])
        if exp <= val or oexp <= val:
            raise ValueError(f"Contract {name}: expiry and option_expiry must be after the valuation date")
        if oexp > exp:
            raise ValueError(f"Contract {name}: option_expiry {oexp} is after the futures expiry {exp}")
        px = _num(r.get("price"), f"Contract {name} price")
        vol = _num(r.get("implied_vol"), f"Contract {name} implied_vol")
        if px <= 0 or vol <= 0:
            raise ValueError(f"Contract {name}: price and implied_vol must be positive")
        scale = PRICE_UNITS[prods[p].price_unit][0]
        c = FuturesContract(name, px * scale, bbg_ticker=_txt(r.get("bbg_ticker")), ccy=prods[p].ccy, product=p,
                            expiry_t=dt.yf(val, exp), expiry_date=exp, option_expiry_t=dt.yf(val, oexp),
                            implied_vol=vol, quoted_price=px, price_scale=scale)
        calibrate(model, c)
        out[name] = c
    return model, out
