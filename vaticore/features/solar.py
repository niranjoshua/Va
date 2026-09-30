"""Solar output per kWp from weather: the input a sizing study needs.

A site that has no panels yet has no solar history, so its potential output is
estimated from the weather at its location. This is a deliberately simple,
standard model, good enough to compare system sizes:

    cell temperature   T_cell = T_air + (NOCT - 20) / 800 * G
    output per kWp     P      = G / 1000 * PR * (1 + gamma * (T_cell - 25))

where G is global horizontal irradiance (W/m2), PR the performance ratio
(inverter, wiring, soiling and mismatch losses together), NOCT the module's
nominal operating cell temperature and gamma its power temperature
coefficient. Horizontal irradiance is used as is: close to the equator, as in
Nigeria, the gain from tilting towards the sun is small, so the estimate is
slightly conservative there. Hot cells lose output, which matters in the
Sahel.

Where a site already has panels, its own measured output divided by its
installed kWp is better than any model, and should be used instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from vaticore.schemas import TIMESTAMP


def pv_output_per_kwp(
    weather: pd.DataFrame,
    *,
    irradiance_col: str = "shortwave_radiation",
    temperature_col: str = "temperature_2m",
    performance_ratio: float = 0.80,
    noct_c: float = 45.0,
    temp_coeff_per_c: float = -0.004,
) -> pd.Series:
    """Estimated AC output in kW per kWp installed, indexed by UTC timestamp.

    Missing irradiance stays missing, so the caller decides how to treat a gap.
    Missing temperature falls back to no temperature derating for that hour.
    """
    if not 0.0 < performance_ratio <= 1.0:
        raise ValueError("performance_ratio must be in (0, 1]")
    if irradiance_col not in weather.columns:
        raise ValueError(f"weather has no {irradiance_col!r} column")
    frame = weather.set_index(TIMESTAMP) if TIMESTAMP in weather.columns else weather
    g = frame[irradiance_col].astype(float).clip(lower=0.0)
    if temperature_col in frame.columns:
        t_air = frame[temperature_col].astype(float)
        t_cell = t_air + (noct_c - 20.0) / 800.0 * g
        derate = (1.0 + temp_coeff_per_c * (t_cell - 25.0)).fillna(1.0)
    else:
        derate = pd.Series(1.0, index=frame.index)
    out = g / 1000.0 * performance_ratio * derate
    out = out.clip(lower=0.0).where(~g.isna(), np.nan)
    out.name = "pv_kw_per_kwp"
    out.index = pd.DatetimeIndex(out.index, name=TIMESTAMP)
    return out
