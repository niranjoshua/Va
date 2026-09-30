"""The internal time-series schema and its validation.

Every dataframe that crosses an ingestion boundary is validated against this
schema. It is the single source of truth for column names, dtypes and the
tenant scoping keys. If a column name needs to change, it changes here and
nowhere else.

Design commitments encoded here:
  - Data is always scoped by operator_id and site_id (multi tenant, multi site).
  - Time is UTC internally and timezone aware.
  - load_kw and generation_kw are nullable, because gaps are expected and are
    handled explicitly downstream rather than being silently dropped.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
import pandera.pandas as pa
from pandera.typing import Series

# Canonical column names. Import these instead of typing string literals.
OPERATOR_ID = "operator_id"
SITE_ID = "site_id"
TIMESTAMP = "timestamp"
LOAD_KW = "load_kw"
GENERATION_KW = "generation_kw"

# Optional: whether the grid supplied the site in that interval (1 on, 0 off,
# missing if unknown). Weak networks make this a quantity to forecast.
GRID_AVAILABLE = "grid_available"

# Derived, signed: load minus generation. Positive means the battery or genset
# must serve it; negative means surplus solar. Never ingested, always computed.
NET_LOAD_KW = "net_load_kw"

SCOPE_KEYS = (OPERATOR_ID, SITE_ID)
TARGET_COLUMNS = (LOAD_KW, GENERATION_KW)


class TimeSeriesSchema(pa.DataFrameModel):
    """The internal, validated shape of all site time series."""

    operator_id: Series[str] = pa.Field(nullable=False)
    site_id: Series[str] = pa.Field(nullable=False)
    # Timezone aware UTC timestamps. tz coercion is enforced in validate().
    timestamp: Series[pd.DatetimeTZDtype] = pa.Field(
        dtype_kwargs={"unit": "ns", "tz": "UTC"},
        nullable=False,
    )
    # Targets are nullable: a gap is represented as NA, not as a dropped row.
    load_kw: Series[float] = pa.Field(nullable=True, ge=0.0)
    generation_kw: Series[float] = pa.Field(nullable=True, ge=0.0)

    class Config:
        strict = False  # weather and derived feature columns may be joined on later
        coerce = True
        unique: ClassVar[list[str]] = [OPERATOR_ID, SITE_ID, TIMESTAMP]


def validate(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate and coerce a dataframe to the internal schema.

    Raises pandera.errors.SchemaError on any violation. This is deliberately
    strict at the boundary so that malformed operator data fails loud and early
    rather than corrupting a forecast.
    """
    validated: pd.DataFrame = TimeSeriesSchema.validate(frame)
    return validated


def with_net_load(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of frame with the derived net_load_kw column added.

    Net load is load minus generation. If either input is missing for a row,
    net load is missing too: a gap stays a gap rather than being guessed.
    """
    out = frame.copy()
    out[NET_LOAD_KW] = out[LOAD_KW] - out[GENERATION_KW]
    return out
