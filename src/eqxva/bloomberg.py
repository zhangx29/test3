"""Optional Bloomberg market-data loader (Desktop API via xbbg/blpapi).

Falls back gracefully: if xbbg is not installed or no terminal session is available,
``available()`` is False and the caller keeps the manual / CSV market data.

Field names below are the usual Bloomberg mnemonics - verify on the terminal with FLDS
if your entitlement differs (implied-vol fields follow '<TENOR>_IMPVOL_<MNY>%MNY_DF').
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SPOT_FIELD = "PX_LAST"
DIV_FIELD = "EQY_DVD_YLD_IND"            # indicated dividend yield, percent
VOL_TENORS = {"1M": "30DAY", "2M": "60DAY", "3M": "3MTH", "6M": "6MTH", "12M": "12MTH",
              "18M": "18MTH", "24M": "24MTH"}
VOL_MONEYNESS = [80.0, 90.0, 95.0, 100.0, 105.0, 110.0, 120.0]


def vol_field(tenor_code: str, mny: float) -> str:
    return f"{tenor_code}_IMPVOL_{mny:.1f}%MNY_DF"


def available() -> bool:
    try:
        from xbbg import blp  # noqa: F401
        return True
    except Exception:
        return False


def fetch_market(underlyings: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """underlyings: {name: bbg_ticker}. Returns (Market rows, VolSurface rows) in input-sheet layout."""
    from xbbg import blp

    tickers = list(underlyings.values())
    vflds = [vol_field(c, m) for c in VOL_TENORS.values() for m in VOL_MONEYNESS]
    ref = blp.bdp(tickers, [SPOT_FIELD, DIV_FIELD] + vflds)
    ref.columns = [c.upper() for c in ref.columns]
    mrows, vrows = [], []
    for name, tk in underlyings.items():
        r = ref.loc[tk] if tk in ref.index else pd.Series(dtype=float)
        atm = r.get(vol_field("12MTH", 100.0).upper(), np.nan)
        mrows.append(dict(underlying=name, spot=r.get(SPOT_FIELD), div_yield=(r.get(DIV_FIELD) or 0.0) / 100.0,
                          is_index="Y" if tk.upper().endswith("INDEX") else "N",
                          flat_vol=atm / 100.0 if pd.notna(atm) else np.nan, bbg_ticker=tk))
        for ten, code in VOL_TENORS.items():
            for m in VOL_MONEYNESS:
                v = r.get(vol_field(code, m).upper(), np.nan)
                if pd.notna(v):
                    vrows.append(dict(underlying=name, tenor=ten, moneyness=m / 100.0, vol=v / 100.0))
    return pd.DataFrame(mrows), pd.DataFrame(vrows)
