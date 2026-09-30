"""Elia study: Vaticore against a grid operator's own forecasts, on real data.

Elia (Belgium's transmission system operator) publishes its day-ahead P10, P50
and P90 alongside the measured values. This study scores Vaticore and the
persistence baseline against Elia on exactly the same hours.

Three parts, run separately (each writes a markdown and JSON report):

  load   Total Belgian load. Vaticore's day-ahead model with fold-by-fold
         conformal calibration, persistence, and Elia's day-ahead 6 PM
         forecast. Trained from --load-start; every day from --score-from on
         is scored.
  solar  Belgian solar generation against Elia's day-ahead 11 AM forecast.
         Vaticore runs without weather here, so this part measures how much a
         weather-driven forecast is worth.
  value  A representative solar mini-grid shaped from real Belgian demand and
         solar over the months both series cover, run through the value
         backtest, including a policy that plans on Elia's own medians.

    uv run python examples/elia_study.py load  --load-csv ods001.csv --out results/
    uv run python examples/elia_study.py solar --solar-csv ods032.csv.gz --out results/
    uv run python examples/elia_study.py value --load-csv ods001.csv \\
        --solar-csv ods032.csv.gz --out results/

Data files are never committed. Elia's hourly quantiles are means of its 15
minute quantiles (see vaticore.ingestion.elia for why that is a close, slightly
wide approximation).
"""

from __future__ import annotations

import argparse
import json
import time
import warnings
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from vaticore.decisions.dispatch import SiteAssets
from vaticore.engine import PLANNING_MODEL, run_value_backtest
from vaticore.evaluation.backtest import FoldForecast, backtest_site
from vaticore.evaluation.benchmark import PooledScores, external_forecasts, pooled_scores
from vaticore.evaluation.calibration import calibration_report, conformalize_forecasts
from vaticore.evaluation.value import PolicySource, value_backtest
from vaticore.forecasting import QuantileGBMForecaster
from vaticore.forecasting.quantile_gbm import DAY_AHEAD_LAGS
from vaticore.ingestion.elia import read_elia_load, read_elia_solar, to_site_frame
from vaticore.schemas import GENERATION_KW, LOAD_KW, TIMESTAMP

QUANTILES = (0.1, 0.5, 0.9)


def _utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _from_midnight(frame: pd.DataFrame, start: str) -> pd.DataFrame:
    """Rows from a UTC midnight on, so every scored day is 00:00 to 23:00 UTC."""
    t0 = _utc(start).normalize()
    return frame[frame[TIMESTAMP] >= t0].reset_index(drop=True)


def _benchmark(
    site: pd.DataFrame,
    target: str,
    elia_cols: dict[float, str],
    score_from: str,
    conformal_window: int,
) -> tuple[list[PooledScores], dict[str, object], int]:
    first = _utc(score_from)
    initial = int((site[TIMESTAMP] < first).sum())
    result = backtest_site(
        site,
        target=target,
        make_model=lambda: QuantileGBMForecaster(
            target=target, quantiles=QUANTILES, lags=DAY_AHEAD_LAGS
        ),
        horizon=24,
        initial=initial,
        step=24,
        quantiles=QUANTILES,
        model_name=PLANNING_MODEL,
    )
    ours = result.forecasts_for(PLANNING_MODEL)
    conf = conformalize_forecasts(ours, window_folds=conformal_window).forecasts
    elia = external_forecasts(
        ours, site.set_index(TIMESTAMP), elia_cols, model="Elia day-ahead (professional)"
    )
    sets: list[list[FoldForecast]] = [result.forecasts_for("persistence"), ours, conf, elia]
    scores = [pooled_scores(s) for s in sets]
    calibration = {
        s[0].model: json.loads(calibration_report(s).to_frame().to_json(orient="records"))
        for s in sets
    }
    return scores, calibration, len(ours)


def _score_table(scores: list[PooledScores], unit: str) -> list[str]:
    base = scores[0]
    lines = [
        f"| Forecast | Pinball ({unit}) | vs persistence | MAE ({unit}) | P10-P90 held (target 80%) |",
        "|---|---|---|---|---|",
    ]
    for s in scores:
        skill = 1.0 - s.pinball / base.pinball
        held = "" if s.coverage_80 is None else f"{100 * s.coverage_80:.1f}%"
        lines.append(
            f"| {s.model} | {s.pinball:,.1f} | {100 * skill:+.1f}% | {s.mae:,.1f} | {held} |"
        )
    return lines


def part_load(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    load = read_elia_load(args.load_csv)
    site = _from_midnight(
        to_site_frame(load, None, operator_id="elia", site_id="belgium-load"), args.load_start
    )
    # Report in MW: the grid scale is easier to read.
    for col in [c for c in site.columns if c.endswith("_kw")]:
        site[col] = site[col] / 1000.0
    cols = {0.1: "load_elia_da_p10_kw", 0.5: "load_elia_da_p50_kw", 0.9: "load_elia_da_p90_kw"}
    scores, calibration, days = _benchmark(
        site, LOAD_KW, cols, args.score_from, args.conformal_window
    )
    md = [
        "# Elia study: total load, day ahead",
        "",
        f"Trained from {args.load_start}; scored on {days} days from {args.score_from} "
        f"(every hour, 00:00 to 23:00 UTC). Values in MW.",
        "",
        *_score_table(scores, "MW"),
        "",
        "Elia's forecast is issued at 18:00 local time the day before and uses weather "
        "and operator knowledge. Vaticore's day-ahead model uses only calendar features "
        "and load lags known at midnight UTC, no weather.",
    ]
    return "\n".join(md), {"scores": [asdict(s) for s in scores], "calibration": calibration}


def part_solar(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    solar = read_elia_solar(args.solar_csv, region=args.region)
    site = to_site_frame(None, solar, operator_id="elia", site_id=f"solar-{args.region}")
    site = _from_midnight(site, str(site[TIMESTAMP].min().normalize() + pd.Timedelta(days=1)))
    for col in [c for c in site.columns if c.endswith("_kw")]:
        site[col] = site[col] / 1000.0
    cols = {0.1: "solar_elia_da_p10_kw", 0.5: "solar_elia_da_p50_kw", 0.9: "solar_elia_da_p90_kw"}
    score_from = str(site[TIMESTAMP].min() + pd.Timedelta(days=args.solar_initial_days))
    scores, calibration, days = _benchmark(
        site, GENERATION_KW, cols, score_from, args.conformal_window
    )
    md = [
        f"# Elia study: solar generation ({args.region}), day ahead",
        "",
        f"Trained on the first {args.solar_initial_days} days; scored on {days} days. "
        "Values in MW.",
        "",
        *_score_table(scores, "MW"),
        "",
        "Vaticore runs without weather in this part: history and calendar only. Elia's "
        "forecast is driven by weather forecasts. The gap is the value of weather.",
    ]
    return "\n".join(md), {"scores": [asdict(s) for s in scores], "calibration": calibration}


def part_value(args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    load = read_elia_load(args.load_csv)
    solar = read_elia_solar(args.solar_csv, region=args.region)
    site = to_site_frame(load, solar, operator_id="elia", site_id="belgium-minigrid")
    both = site.dropna(subset=[LOAD_KW, GENERATION_KW])
    start = both[TIMESTAMP].min().normalize() + pd.Timedelta(days=1)
    end = both[TIMESTAMP].max().normalize()
    site = site[(site[TIMESTAMP] >= start) & (site[TIMESTAMP] < end)].reset_index(drop=True)

    # Shape into a representative mini-grid: real shapes, site-scale sizes.
    k_load = args.peak_load_kw / site[LOAD_KW].max()
    k_pv = args.pv_kwp / site[GENERATION_KW].max()
    for col in [c for c in site.columns if c.startswith("load_") or c == LOAD_KW]:
        site[col] = site[col] * k_load
    for col in [c for c in site.columns if c.startswith("solar_") or c == GENERATION_KW]:
        site[col] = site[col] * k_pv

    assets = SiteAssets(
        battery_kwh=args.battery_kwh,
        battery_power_kw=args.battery_power_kw,
        genset_kw=args.genset_kw,
        min_soc_kwh=args.min_soc_kwh,
    )
    outcome = run_value_backtest(
        site, assets=assets, initial=24 * args.value_initial_days, conformal_window=28
    )
    ours = outcome.backtest.forecasts_for(PLANNING_MODEL)
    conf = conformalize_forecasts(ours, window_folds=28, nonnegative=False).forecasts
    frame = site.set_index(TIMESTAMP)
    frame["elia_net_p50"] = frame["load_elia_da_p50_kw"] - frame["solar_elia_da_p50_kw"]
    elia = external_forecasts(ours, frame, {0.5: "elia_net_p50"}, model="elia")
    policies = {
        "persistence P50": PolicySource(outcome.backtest.forecasts_for("persistence"), 0.5),
        "persistence P90": PolicySource(outcome.backtest.forecasts_for("persistence"), 0.9),
        "Elia medians (load P50 minus solar P50)": PolicySource(elia, 0.5),
        f"{PLANNING_MODEL} P50": PolicySource(ours, 0.5),
        f"{PLANNING_MODEL} P90": PolicySource(ours, 0.9),
        f"{PLANNING_MODEL}+conformal P90": PolicySource(conf, 0.9),
    }
    report = value_backtest(policies, assets=assets, baseline="persistence P50")
    table = report.to_frame()
    days = report.results["persistence P50"].days
    md = [
        "# Elia study: value on a Belgian-shaped solar mini-grid",
        "",
        f"Real Belgian demand and solar, {start:%Y-%m-%d} to {end:%Y-%m-%d}; the first "
        f"{args.value_initial_days} days train, {days:.0f} days are scored "
        f"(mostly autumn and winter, when solar is weakest). Demand scaled to a "
        f"{args.peak_load_kw:.0f} kW peak, solar to {args.pv_kwp:.0f} kWp, battery "
        f"{args.battery_kwh:.0f} kWh, generator {args.genset_kw:.0f} kW.",
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
    md += [
        "",
        "Elia publishes separate load and solar quantiles, and quantiles do not subtract, "
        "so only Elia's medians can form a net load plan. That policy shows what a "
        "professional point forecast is worth to the same site.",
    ]
    return "\n".join(md), {
        "value": json.loads(table.to_json(orient="index")),
        "calibration": {
            k: json.loads(v.to_frame().to_json(orient="records"))
            for k, v in outcome.calibration.items()
        },
        "period": [str(start), str(end)],
        "assets": asdict(assets),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("part", choices=["load", "solar", "value"])
    parser.add_argument("--load-csv", type=Path)
    parser.add_argument("--solar-csv", type=Path)
    parser.add_argument("--region", default="Belgium")
    parser.add_argument("--load-start", default="2022-01-01")
    parser.add_argument("--score-from", default="2025-01-01")
    parser.add_argument("--solar-initial-days", type=int, default=120)
    parser.add_argument("--value-initial-days", type=int, default=120)
    parser.add_argument("--conformal-window", type=int, default=28)
    parser.add_argument("--peak-load-kw", type=float, default=120.0)
    parser.add_argument("--pv-kwp", type=float, default=200.0)
    parser.add_argument("--battery-kwh", type=float, default=600.0)
    parser.add_argument("--battery-power-kw", type=float, default=150.0)
    parser.add_argument("--min-soc-kwh", type=float, default=60.0)
    parser.add_argument("--genset-kw", type=float, default=100.0)
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()
    warnings.filterwarnings("ignore", category=UserWarning)

    t0 = time.time()
    run = {"load": part_load, "solar": part_solar, "value": part_value}[args.part]
    markdown, payload = run(args)
    markdown += f"\n\nRuntime {(time.time() - t0) / 60:.1f} minutes.\n"
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"elia_{args.part}.md").write_text(markdown)
    (args.out / f"elia_{args.part}.json").write_text(json.dumps(payload, indent=2, default=str))
    print(markdown)


if __name__ == "__main__":
    main()
