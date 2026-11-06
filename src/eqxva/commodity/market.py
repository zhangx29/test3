"""Commodity market objects: products, factors, futures contracts.

Prices are held in currency per quantity unit (e.g. $/bu, $/ton, $/lb) whatever the quote unit,
so quantity * price is always a currency amount. PRICE_UNITS gives the conversion.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from ..market import Underlying

# quote unit -> (multiplier to currency per quantity unit, quantity unit)
PRICE_UNITS = {
    "C/BU": (0.01, "BU"), "$/BU": (1.0, "BU"),
    "$/TON": (1.0, "TON"),
    "C/LB": (0.01, "LB"), "$/LB": (1.0, "LB"), "$/CWT": (0.01, "LB"),
    "$/BBL": (1.0, "BBL"), "$/MMBTU": (1.0, "MMBTU"), "$/MWH": (1.0, "MWH"),
    "$/MT": (1.0, "MT"), "$/OZ": (1.0, "OZ"),
}


@dataclass
class Factor:
    name: str          # unique across products, e.g. SOYBEANS.S
    beta: float        # mean reversion; 0 for a long factor
    ratio: float       # q_i(T) / q_ref(T): this factor's vol relative to the reference factor (ratio 1)


@dataclass
class Product:
    name: str
    factors: list[Factor]
    hedging_set: str = "AGRICULTURAL"
    commodity_type: str = ""
    lot_size: float = 1.0
    quantity_unit: str = ""
    price_unit: str = ""
    ccy: str = ""


@dataclass
class CommodityModel:
    """Factor list and correlation matrix of the N-factor model across all products."""
    products: dict[str, Product]
    factor_names: list[str]
    corr: np.ndarray

    def __post_init__(self):
        pos = {n: i for i, n in enumerate(self.factor_names)}
        self.beta = np.zeros(len(pos))
        self.ratio = np.zeros(len(pos))
        for p in self.products.values():
            for f in p.factors:
                self.beta[pos[f.name]] = f.beta
                self.ratio[pos[f.name]] = f.ratio
        self.index = {p.name: np.array([pos[f.name] for f in p.factors]) for p in self.products.values()}


@dataclass
class FuturesContract(Underlying):
    product: str = ""
    expiry_t: float = 0.0
    expiry_date: date | None = None
    option_expiry_t: float = 0.0
    implied_vol: float = 0.0
    quoted_price: float = 0.0
    price_scale: float = 1.0
    q_ref: float = 0.0            # calibrated q_ref(T) of this contract

    def forward(self, t, curve=None):
        """A futures price is a martingale: its expected value is today's price at every date."""
        return self.spot * np.ones_like(np.asarray(t, dtype=float))


def is_future(mkt, name: str) -> bool:
    return isinstance(mkt.underlyings[name], FuturesContract)
