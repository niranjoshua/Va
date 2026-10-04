"""Foundation model study: Chronos-2 and TimesFM against Vaticore's planning model.

Pretrained time series foundation models forecast "zero shot", with no
training on the site. This study asks whether they should replace or join
the day-ahead quantile GBM, on the same Elia data and hours as research note
2, and whether they help where the GBM is weakest: new sites with only a few
weeks of history.

Only Elia (Belgian) data is used. The Spanish dataset of research note 1 is in
the models' pretraining corpus (GiftEvalPretrain, "spain"), value for value
for 2015 to 2018, so they have already seen its test year: scoring them on it
would measure memory, not forecasting. Elia is not in the corpus, and the test
period (2025 on) is newer than the models' training data.

Four parts, run separately, each writing a markdown and JSON report:

  design  Choose each model's context length (how much history it sees) on
          a design period only: Elia load, July to December 2024.
  load    Test, run once: Elia total load, every day from 2025-01-01, against
          persistence, the GBM and Elia's own forecast (as in note 2).
  value   Test, run once: the Belgian-shaped solar mini-grid of note 2, net
          load day ahead, then litres, outages and money per planning policy.
  short   New sites: every model sees only the last 2, 4 or 8 weeks before
          each forecast. Scored on the first 182 days of 2025.

    uv sync --extra foundation
    uv run python examples/foundation_study.py design --load-csv ods001.csv --out results/
    uv run python examples/foundation_study.py load   --load-csv ods001.csv --out results/
    uv run python examples/foundation_study.py value  --load-csv ods001.csv \\
        --solar-csv ods032.csv.gz --out results/
    uv run python examples/foundation_study.py short  --load-csv ods001.csv --out results/

Chronos-2 and TimesFM 2.5 are Apache 2.0. TimesFM 3.0 is not part of this
study: its weights are under a non-commercial licence that also forbids using
its results in commercial decisions. Weights download from Hugging Face on first use. Data files are
never committed. Models run on the CPU unless --device says otherwise.
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from vaticore.decisions.dispatch import SiteAssets
from vaticore.evaluation.backtest import FoldForecast, backtest_site
from vaticore.evaluation.benchmark import PooledScores, external_forecasts, pooled_scores
from vaticore.evaluation.calibration import conformalize_forecasts
from vaticore.evaluation.value import PolicySource, value_backtest
from vaticore.forecasting import (
    ChronosForecaster,
    Forecaster,
    PersistenceForecaster,
    QuantileGBMForecaster,
    TimesFMForecaster,
)
from vaticore.forecasting.base import DEFAULT_QUANTILES
from vaticore.forecasting.foundation import TIMESFM_MODEL
from vaticore.forecasting.quantile_gbm import DAY_AHEAD_LAGS
from vaticore.ingestion.elia import read_elia_load, read_elia_solar, to_site_frame
from vaticore.schemas import GENERATION_KW, LOAD_KW, NET_LOAD_KW, TIMESTAMP, with_net_load

QUANTILES = (0.1, 0.5, 0.9)
GBM = "quantile_gbm_day_ahead"
CHRONOS = "chronos-2"
TIMESFM = "timesfm-2.5"
FOUNDATION: dict[str, Callable[..., Forecaster]] = {
    CHRONOS: ChronosForecaster,
    TIMESFM: lambda target, **kw: TimesFMForecaster(target, model_id=TIMESFM_MODEL, **kw),
}
# Suffix for output file names; empty for the published study.
OUTPUT_SUFFIX = ""
DEFAULT_CONTEXT_HOURS = 24 * 7 * 48
DESIGN_CONTEXTS = (24 * 7 * 4, 24 * 7 * 12, 24 * 7 * 48)
SHORT_WEEKS = (2, 4, 8)


class RecentWindow(Forecaster):
    """Fit a model on only the most recent rows: a site with short history."""

    def __init__(self, inner: Forecaster, rows: int) -> None:
        self.inner = inner
        self.rows = rows

    def fit(self, history: pd.DataFrame) -> RecentWindow:
        self.inner.fit(history.sort_values(TIMESTAMP).tail(self.rows))
        return self

    def predict_quantiles(
        self, horizon: int, quantiles: tuple[float, ...] = DEFAULT_QUANTILES
    ) -> pd.DataFrame:
        return self.inner.predict_quantiles(horizon, quantiles)


# -- data ------------------------------------------------------------------


def _utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def elia_load_site(path: Path, start: str = "2022-01-01") -> pd.DataFrame:
    """Belgian total load in MW, from a UTC midnight on (as in note 2)."""
    site = to_site_frame(read_elia_load(path), None, operator_id="elia", site_id="belgium-load")
    site = site[site[TIMESTAMP] >= _utc(start).normalize()].reset_index(drop=True)
    for col in [c for c in site.columns if c.endswith("_kw")]:
        site[col] = site[col] / 1000.0
    return site


def elia_minigrid_site(args: argparse.Namespace) -> pd.DataFrame:
    """The Belgian-shaped solar mini-grid of note 2: real shapes, site sizes."""
    site = to_site_frame(
        read_elia_load(args.load_csv),
        read_elia_solar(args.solar_csv),
        operator_id="elia",
        site_id="belgium-minigrid",
    )
    both = site.dropna(subset=[LOAD_KW, GENERATION_KW])
    start = both[TIMESTAMP].min().normalize() + pd.Timedelta(days=1)
    end = both[TIMESTAMP].max().normalize()
    site = site[(site[TIMESTAMP] >= start) & (site[TIMESTAMP] < end)].reset_index(drop=True)
    site[LOAD_KW] = site[LOAD_KW] * (args.peak_load_kw / site[LOAD_KW].max())
    site[GENERATION_KW] = site[GENERATION_KW] * (args.pv_kwp / site[GENERATION_KW].max())
    return with_net_load(site[[TIMESTAMP, LOAD_KW, GENERATION_KW]])


def _rows_before(site: pd.DataFrame, when: str) -> int:
    return int((site[TIMESTAMP] < _utc(when)).sum())


def _until(site: pd.DataFrame, when: str) -> pd.DataFrame:
    return site[site[TIMESTAMP] < _utc(when)].reset_index(drop=True)


# -- models ----------------------------------------------------------------


def factory(
    name: str, target: str, nonnegative: bool, context: int, device: str
) -> Callable[[], Forecaster]:
    if name == GBM:
        return lambda: QuantileGBMForecaster(
            target=target, quantiles=QUANTILES, lags=DAY_AHEAD_LAGS, nonnegative=nonnegative
        )
    if name == "persistence":
        return lambda: PersistenceForecaster(target=target, nonnegative=nonnegative)
    build = FOUNDATION[name]
    return lambda: build(target, context_length=context, nonnegative=nonnegative, device=device)


def run(
    site: pd.DataFrame,
    target: str,
    make: Callable[[], Forecaster],
    name: str,
    initial: int,
    nonnegative: bool,
) -> tuple[list[FoldForecast], list[FoldForecast], float]:
    """Day-ahead folds for one model; returns its forecasts, persistence's, seconds."""
    t0 = time.time()
    result = backtest_site(
        site,
        target=target,
        make_model=make,
        horizon=24,
        initial=initial,
        step=24,
        quantiles=QUANTILES,
        model_name=name,
        nonnegative=nonnegative,
    )
    return result.forecasts_for(name), result.forecasts_for("persistence"), time.time() - t0


def _contexts(args: argparse.Namespace) -> dict[str, int]:
    """History length per foundation model taking part in this run."""
    hours = {CHRONOS: args.chronos_context, TIMESFM: args.timesfm_context}
    return {name: hours.get(name, DEFAULT_CONTEXT_HOURS) for name in FOUNDATION}


def all_models(
    site: pd.DataFrame,
    target: str,
    nonnegative: bool,
    initial: int,
    args: argparse.Namespace,
) -> tuple[dict[str, list[FoldForecast]], dict[str, float]]:
    """Persistence, the GBM and each foundation model, raw and conformal, same folds."""
    sets: dict[str, list[FoldForecast]] = {}
    seconds: dict[str, float] = {}
    contexts = _contexts(args)
    for name in (GBM, *FOUNDATION):
        make = factory(name, target, nonnegative, contexts.get(name, 0), args.device)
        ours, base, secs = run(site, target, make, name, initial, nonnegative)
        sets.setdefault("persistence", base)
        sets[name] = ours
        sets[f"{name}+conformal"] = conformalize_forecasts(
            ours, window_folds=args.conformal_window, nonnegative=nonnegative
        ).forecasts
        seconds[name] = secs
        print(f"  {name}: {len(ours)} days in {secs / 60:.1f} min", flush=True)
    return sets, seconds


def _score_rows(scores: list[PooledScores], unit: str) -> list[str]:
    base = scores[0]
    lines = [
        f"| Forecast | Pinball ({unit}) | vs persistence | MAE ({unit}) | P10-P90 held (target 80%) |",
        "|---|---|---|---|---|",
    ]
    for s in scores:
        held = "" if s.coverage_80 is None else f"{100 * s.coverage_80:.1f}%"
        lines.append(
            f"| {s.model} | {s.pinball:,.2f} | {100 * (1 - s.pinball / base.pinball):+.1f}% | "
            f"{s.mae:,.2f} | {held} |"
        )
    return lines


def _renamed(s: PooledScores, model: str) -> PooledScores:
    return PooledScores(model, s.pinball, s.mae, s.rmse, s.coverage_80, s.hours)


def _context_note(args: argparse.Namespace) -> str:
    return "Context (chosen in the design part): " + ", ".join(
        f"{name} {hours} h" for name, hours in _contexts(args).items()
    )


# -- parts -----------------------------------------------------------------


def part_design(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    site = _until(elia_load_site(args.load_csv), "2025-01-01")
    initial = _rows_before(site, "2024-07-01")
    rows: list[PooledScores] = []
    persistence: PooledScores | None = None
    best: dict[str, tuple[float, int]] = {}
    for model in FOUNDATION:
        for ctx in DESIGN_CONTEXTS:
            make = factory(model, LOAD_KW, True, ctx, args.device)
            ours, base, secs = run(site, LOAD_KW, make, model, initial, True)
            persistence = persistence or pooled_scores(base)
            score = pooled_scores(ours)
            rows.append(_renamed(score, f"{model}, {ctx // 168} weeks"))
            if model not in best or score.pinball < best[model][0]:
                best[model] = (score.pinball, ctx)
            print(f"  {model} {ctx}: {secs:.0f}s", flush=True)
    assert persistence is not None
    md = [
        "# Foundation model study: design (context length)",
        "",
        "Elia total load, 2024-07-01 to 2024-12-31, a design period only; the test period "
        "(2025 on) is untouched. Each model forecasts each day at 00:00 UTC from the last N "
        "hours. Values in MW.",
        "",
        *_score_rows([persistence, *rows], "MW"),
        "",
        "## Chosen for the test",
        "",
        *[f"- {m}: {c} hours ({c // 168} weeks), the lowest pinball loss." for m, (_, c) in best.items()],
    ]  # fmt: skip
    payload = {
        "scores": [asdict(r) for r in [persistence, *rows]],
        "chosen_context_hours": {m: c for m, (_, c) in best.items()},
    }
    return "\n".join(md), payload


def part_load(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    site = elia_load_site(args.load_csv)
    initial = _rows_before(site, "2025-01-01")
    sets, seconds = all_models(site, LOAD_KW, True, initial, args)
    cols = {0.1: "load_elia_da_p10_kw", 0.5: "load_elia_da_p50_kw", 0.9: "load_elia_da_p90_kw"}
    sets["Elia day-ahead (professional)"] = external_forecasts(
        sets[GBM], site.set_index(TIMESTAMP), cols, model="Elia day-ahead (professional)"
    )
    scores = [pooled_scores(fc) for fc in sets.values()]
    md = [
        "# Foundation model study: Elia total load, day ahead (test)",
        "",
        f"Scored on {len(sets[GBM])} days from 2025-01-01, every hour, the same hours as "
        f"research note 2. Values in MW. {_context_note(args)}.",
        "",
        *_score_rows(scores, "MW"),
        "",
        "No model here uses weather; Elia's forecast does. Conformal calibration uses only "
        f"the previous {args.conformal_window} days' misses.",
        "",
        "Compute for the whole test on a 4-core CPU: "
        + ", ".join(f"{k} {v / 60:.1f} min" for k, v in seconds.items())
        + ".",
    ]
    return "\n".join(md), {"scores": [asdict(s) for s in scores], "seconds": seconds}


def part_value(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    site = elia_minigrid_site(args)
    initial = 24 * args.value_initial_days
    sets, seconds = all_models(site, NET_LOAD_KW, False, initial, args)
    scores = [pooled_scores(fc) for fc in sets.values()]
    assets = SiteAssets(
        battery_kwh=args.battery_kwh,
        battery_power_kw=args.battery_power_kw,
        genset_kw=args.genset_kw,
        min_soc_kwh=args.min_soc_kwh,
    )
    policies = {
        "persistence P50": PolicySource(sets["persistence"], 0.5),
        "persistence P90": PolicySource(sets["persistence"], 0.9),
        **{f"{name}+conformal P90": PolicySource(sets[f"{name}+conformal"], 0.9)
           for name in (GBM, *FOUNDATION)},
    }  # fmt: skip
    report = value_backtest(policies, assets=assets, baseline="persistence P50")
    table = report.to_frame()
    first, last = sets[GBM][0].timestamps[0], sets[GBM][-1].timestamps[-1]
    md = [
        "# Foundation model study: value on a Belgian-shaped solar mini-grid (test)",
        "",
        f"The site of research note 2: real Belgian demand and solar shaped to a "
        f"{args.peak_load_kw:.0f} kW peak and {args.pv_kwp:.0f} kWp, battery "
        f"{args.battery_kwh:.0f} kWh, generator {args.genset_kw:.0f} kW. Scored "
        f"{first:%Y-%m-%d} to {last:%Y-%m-%d} ({len(sets[GBM])} days) after "
        f"{args.value_initial_days} training days. {_context_note(args)}.",
        "",
        "## Net load accuracy and calibration",
        "",
        *_score_rows(scores, "kW"),
        "",
        "## Value: litres, outages and money (an unserved kWh at 1.00, diesel 1.10/L)",
        "",
        "| Policy | Diesel (L) | Unserved (kWh) | Outage hours | Total cost | Saved vs baseline | Share of possible |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, row in table.iterrows():
        share = row["share_of_possible"]
        share_txt = "" if share is None or pd.isna(share) else f"{100 * float(share):.1f}%"
        md.append(
            f"| {name} | {row['fuel_l']:,.0f} | {row['unserved_kwh']:,.0f} | "
            f"{row['unserved_hours']:,.0f} | {row['total_cost']:,.0f} | "
            f"{row['savings_vs_baseline']:,.0f} | {share_txt} |"
        )
    return "\n".join(md), {
        "scores": [asdict(s) for s in scores],
        "value": json.loads(table.to_json(orient="index")),
        "assets": asdict(assets),
        "seconds": seconds,
    }


def part_short(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    site = _until(elia_load_site(args.load_csv), "2025-07-02")
    initial = _rows_before(site, "2025-01-01")
    full_gbm, persistence, _ = run(
        site, LOAD_KW, factory(GBM, LOAD_KW, True, 0, args.device), GBM, initial, True
    )
    rows = [pooled_scores(persistence), _renamed(pooled_scores(full_gbm), f"{GBM}, full history")]
    failures: list[str] = []
    for weeks in SHORT_WEEKS:
        hours = 24 * 7 * weeks
        for name in ("persistence", GBM, *FOUNDATION):
            inner = factory(name, LOAD_KW, True, hours, args.device)
            label = f"{name}, {weeks} weeks"
            try:
                ours, _, secs = run(
                    site,
                    LOAD_KW,
                    lambda inner=inner, hours=hours: RecentWindow(inner(), hours),
                    label,
                    initial,
                    True,
                )
            except Exception as exc:  # a model that cannot run is reported, not hidden
                failures.append(f"- {label}: cannot run ({exc}).")
                continue
            rows.append(pooled_scores(ours))
            print(f"  {label}: {secs:.0f}s", flush=True)
    md = [
        "# Foundation model study: new sites with short history (test)",
        "",
        "Elia total load, 182 days from 2025-01-01. Every model, persistence included, sees "
        "only the last 2, 4 or 8 weeks before each forecast, as a newly connected site "
        "would. The full-history rows are the reference for an established site. No "
        "conformal calibration: a new site has too few past forecasts to calibrate on. "
        "Values in MW; skill is against persistence with full history.",
        "",
        *_score_rows(rows, "MW"),
        "",
        *failures,
    ]
    return "\n".join(md), {"scores": [asdict(r) for r in rows], "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("part", choices=["design", "load", "value", "short"])
    parser.add_argument("--load-csv", type=Path)
    parser.add_argument("--solar-csv", type=Path)
    parser.add_argument("--chronos-context", type=int, default=24 * 7 * 48)
    parser.add_argument("--timesfm-context", type=int, default=24 * 7 * 48)
    parser.add_argument("--conformal-window", type=int, default=28)
    parser.add_argument("--value-initial-days", type=int, default=120)
    parser.add_argument("--peak-load-kw", type=float, default=120.0)
    parser.add_argument("--pv-kwp", type=float, default=200.0)
    parser.add_argument("--battery-kwh", type=float, default=600.0)
    parser.add_argument("--battery-power-kw", type=float, default=150.0)
    parser.add_argument("--min-soc-kwh", type=float, default=60.0)
    parser.add_argument("--genset-kw", type=float, default=100.0)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=UserWarning)

    suffix = OUTPUT_SUFFIX
    t0 = time.time()
    parts = {"design": part_design, "load": part_load, "value": part_value, "short": part_short}
    markdown, payload = parts[args.part](args)
    markdown += f"\n\nRuntime {(time.time() - t0) / 60:.1f} minutes.\n"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"foundation_{args.part}{suffix}.md").write_text(markdown)
    (args.out / f"foundation_{args.part}{suffix}.json").write_text(
        json.dumps(payload, indent=2, default=str)
    )
    print(markdown)


if __name__ == "__main__":
    main()
