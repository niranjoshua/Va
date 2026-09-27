"""Value study: what a forecast is worth to a solar mini-grid, on real data.

Takes real hourly demand and solar series, shapes them into a representative
solar mini-grid (the assumptions are printed in the report), then runs
vaticore.engine.run_value_backtest: day ahead net load forecasts on back to
back days, fold by fold conformal calibration, and a battery and generator
plan per policy, each operated against what actually happened.

Works on any CSV with a timestamp, a demand column and a solar column. The
defaults match the public ENTSO-E Spain dataset bundled with the Darts library:

    curl -L -o energy_dataset.csv \\
      https://raw.githubusercontent.com/unit8co/darts/master/datasets/energy_dataset.csv
    uv run python examples/value_study.py energy_dataset.csv --out results/

For two separate files (for example Elia load and Elia solar), pass the solar
file with --solar-csv and name each file's columns.

Data files are never committed. The report records the file, columns, period
and every assumption, so a run can be reproduced exactly. If
VATICORE_MLFLOW_TRACKING_URI is set, the run is also logged to MLflow.
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from vaticore.decisions.dispatch import SiteAssets
from vaticore.engine import ValueBacktest, run_value_backtest
from vaticore.schemas import GENERATION_KW, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP
from vaticore.tracking import get_tracker


def _read_series(path: Path, time_col: str, value_col: str) -> pd.Series:
    frame = pd.read_csv(path, usecols=[time_col, value_col])
    stamps = pd.to_datetime(frame[time_col], utc=True)
    series = pd.Series(frame[value_col].to_numpy(dtype=float), index=stamps, name=value_col)
    return series[~series.index.duplicated(keep="first")].sort_index()


def build_site(args: argparse.Namespace) -> tuple[pd.DataFrame, dict[str, object]]:
    """Shape real demand and solar into one mini-grid site on an hourly grid."""
    load = _read_series(args.csv, args.time_col, args.load_col)
    solar_path = args.solar_csv or args.csv
    solar = _read_series(solar_path, args.solar_time_col or args.time_col, args.solar_col)

    start = pd.Timestamp(args.start, tz="UTC") if args.start else max(load.index[0], solar.index[0])
    end = pd.Timestamp(args.end, tz="UTC") if args.end else min(load.index[-1], solar.index[-1])
    grid = pd.date_range(start.ceil("h"), end.floor("h"), freq="h", name=TIMESTAMP)
    load, solar = load.reindex(grid), solar.reindex(grid)

    # Demand keeps its real shape and variability, scaled to the site's peak.
    load_kw = load / load.max() * args.peak_load_kw
    # Solar output per kWp: the real series divided by its own peak, times the
    # array size. Night-time negatives from metering are clipped to zero.
    pv_kw = (solar / solar.max()).clip(lower=0.0) * args.pv_kwp

    site = pd.DataFrame(
        {
            OPERATOR_ID: "study",
            SITE_ID: args.site_id,
            TIMESTAMP: grid,
            LOAD_KW: load_kw.to_numpy(),
            GENERATION_KW: pv_kw.to_numpy(),
        }
    )
    meta: dict[str, object] = {
        "source_csv": str(args.csv),
        "solar_csv": str(solar_path),
        "columns": {"time": args.time_col, "load": args.load_col, "solar": args.solar_col},
        "period_utc": [str(grid[0]), str(grid[-1])],
        "hours": len(grid),
        "missing_load_hours": int(load.isna().sum()),
        "missing_solar_hours": int(solar.isna().sum()),
        "peak_load_kw": args.peak_load_kw,
        "pv_kwp": args.pv_kwp,
        "mean_daily_load_kwh": float(load_kw.mean() * 24),
        "mean_daily_pv_kwh": float(pv_kw.mean() * 24),
    }
    return site, meta


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def render_markdown(
    outcome: ValueBacktest, meta: dict[str, object], args: argparse.Namespace, seconds: float
) -> str:
    value = outcome.value
    frame = value.to_frame()
    assets = value.assets
    lines = [
        "# Vaticore value study",
        "",
        f"Site: `{args.site_id}`, built from `{Path(str(meta['source_csv'])).name}`.",
        f"Period (UTC): {meta['period_utc'][0]} to {meta['period_utc'][1]}"  # type: ignore[index]
        f" ({meta['hours']} hours). Scored days: {value.results[outcome.baseline_policy].days:.0f}.",
        "",
        "## Site assumptions",
        "",
        f"- Demand scaled to a {args.peak_load_kw:.0f} kW peak "
        f"({meta['mean_daily_load_kwh']:.0f} kWh on an average day).",
        f"- Solar: real output shape on a {args.pv_kwp:.0f} kWp array "
        f"({meta['mean_daily_pv_kwh']:.0f} kWh on an average day).",
        f"- Battery {assets.battery_kwh:.0f} kWh usable, {assets.battery_power_kw:.0f} kW, "
        f"floor {assets.min_soc_kwh:.0f} kWh, efficiency {assets.charge_efficiency:.2f} each way.",
        f"- Generator {assets.genset_kw:.0f} kW, minimum load {_pct(assets.genset_min_load)}, "
        "HOMER default fuel curve (0.08145 L/h per kW rated + 0.246 L/kWh).",
        f"- Diesel {assets.diesel_price_per_l:.2f} per litre; an unserved kWh priced at "
        f"{assets.value_of_lost_load_per_kwh:.2f} (see the sensitivity table).",
        f"- Missing hours: load {meta['missing_load_hours']}, solar {meta['missing_solar_hours']}.",
        "",
        "## Calibration: does the range mean what it says?",
        "",
        "| Forecast | P10-P90 holds (target 80%) | Mean width (kW) | Below P10 (target 10%) | Below P90 (target 90%) |",
        "|---|---|---|---|---|",
    ]
    for name, report in outcome.calibration.items():
        band = report.intervals[0]
        by_q = {q.quantile: q.observed for q in report.quantiles}
        lines.append(
            f"| {name} | {_pct(band.observed)} | {band.mean_width:.1f} | "
            f"{_pct(by_q.get(0.1, float('nan')))} | {_pct(by_q.get(0.9, float('nan')))} |"
        )
    lines += [
        "",
        "## Value: litres, outages and money",
        "",
        "Each policy plans the generator day ahead from its forecast, then the day is run on "
        "what actually happened. The baseline plans on persistence (tomorrow looks like "
        "yesterday). The perfect forecast row uses the same planning rule on the actual outcome: "
        "it bounds what any forecast can achieve here.",
        "",
        "| Policy | Diesel (L) | Generator hours | Unserved (kWh) | Hours with outages | Total cost | Saved vs baseline | Share of possible gain |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, row in frame.iterrows():
        share = row["share_of_possible"]
        share_txt = "" if share is None or pd.isna(share) else _pct(float(share))
        lines.append(
            f"| {name} | {row['fuel_l']:,.0f} | {row['genset_hours']:,.0f} | "
            f"{row['unserved_kwh']:,.0f} | {row['unserved_hours']:,.0f} | "
            f"{row['total_cost']:,.0f} | {row['savings_vs_baseline']:,.0f} | {share_txt} |"
        )
    sens = value.sensitivity(args.voll_grid)
    lines += [
        "",
        "## Sensitivity: savings against the baseline by cost of an unserved kWh",
        "",
        "| Policy | " + " | ".join(f"{v:g} per kWh" for v in sens.columns) + " |",
        "|---|" + "---|" * len(sens.columns),
    ]
    for name, row in sens.iterrows():
        lines.append(f"| {name} | " + " | ".join(f"{x:,.0f}" for x in row) + " |")
    lines += [
        "",
        "## Method",
        "",
        "- Rolling origin, 24 hours ahead, one forecast per day at 00:00 UTC, trained only on "
        "data before each origin. Net load (demand minus solar) is forecast directly.",
        "- Conformal calibration (CQR) adjusts each day's P10-P90 using only the misses of the "
        f"previous {args.conformal_window} days.",
        "- Every policy starts with the battery half full and carries its own battery state "
        "from day to day.",
        f"- Runtime {seconds / 60:.1f} minutes.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("--time-col", default="time")
    parser.add_argument("--load-col", default="total load actual")
    parser.add_argument("--solar-col", default="generation solar")
    parser.add_argument("--solar-csv", type=Path, default=None)
    parser.add_argument("--solar-time-col", default=None)
    parser.add_argument("--start", default=None, help="first timestamp to use (UTC)")
    parser.add_argument("--end", default=None, help="last timestamp to use (UTC)")
    parser.add_argument(
        "--initial-days", type=int, default=180, help="training history before the first scored day"
    )
    parser.add_argument("--site-id", default="solar-minigrid-120kw")
    parser.add_argument("--peak-load-kw", type=float, default=120.0)
    parser.add_argument("--pv-kwp", type=float, default=200.0)
    parser.add_argument("--battery-kwh", type=float, default=600.0)
    parser.add_argument("--battery-power-kw", type=float, default=150.0)
    parser.add_argument("--min-soc-kwh", type=float, default=60.0)
    parser.add_argument("--genset-kw", type=float, default=100.0)
    parser.add_argument("--diesel-price", type=float, default=1.10)
    parser.add_argument("--voll", type=float, default=1.0, help="price of an unserved kWh")
    parser.add_argument("--voll-grid", type=float, nargs="+", default=[0.25, 0.5, 1.0, 2.0, 5.0])
    parser.add_argument("--conformal-window", type=int, default=28)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=UserWarning)

    site, meta = build_site(args)
    assets = SiteAssets(
        battery_kwh=args.battery_kwh,
        battery_power_kw=args.battery_power_kw,
        genset_kw=args.genset_kw,
        min_soc_kwh=args.min_soc_kwh,
        diesel_price_per_l=args.diesel_price,
        value_of_lost_load_per_kwh=args.voll,
    )
    t0 = time.time()
    outcome = run_value_backtest(
        site,
        assets=assets,
        initial=24 * args.initial_days,
        conformal_window=args.conformal_window,
        tracker=get_tracker(experiment="vaticore-value"),
        operator_id="study",
        site_id=args.site_id,
    )
    seconds = time.time() - t0

    args.out.mkdir(parents=True, exist_ok=True)
    report_md = render_markdown(outcome, meta, args, seconds)
    (args.out / f"value_study_{args.site_id}.md").write_text(report_md)
    payload = {
        "meta": meta,
        "assets": asdict(assets),
        "value": json.loads(outcome.value.to_frame().to_json(orient="index")),
        "sensitivity": json.loads(
            outcome.value.sensitivity(args.voll_grid).to_json(orient="index")
        ),
        "calibration": {
            name: json.loads(report.to_frame().to_json(orient="records"))
            for name, report in outcome.calibration.items()
        },
        "pinball": json.loads(outcome.backtest.summary().to_json(orient="index")),
    }
    (args.out / f"value_study_{args.site_id}.json").write_text(
        json.dumps(
            payload, indent=2, default=lambda o: float(o) if isinstance(o, np.floating) else str(o)
        )
    )
    print(report_md)


if __name__ == "__main__":
    main()
