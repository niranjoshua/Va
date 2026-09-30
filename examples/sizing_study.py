"""Sizing study: what solar and battery should a site add, and what will it save?

Takes a site from a portfolio (examples/sites/*.toml), its hourly load, and a
year of weather at its location (fetched from the Open-Meteo archive), then
simulates every solar and battery size and prices each against the site today.

    uv run --extra weather python examples/sizing_study.py \\
        --site-id kog-rur-0077 --history tower_load.csv --year 2025 \\
        --pv-capex 850000 --battery-capex 500000 --discount-rate 0.18 \\
        --pv-life 25 --battery-life 10 --out results/

History is a CSV with timestamp (UTC) and load_kw, plus grid_available for a
site on an unreliable grid. With no history, --demo-flat-load builds an
illustrative flat load (typical of a telecom tower) and says so in the report.

To study a site before hybridisation, --current-pv-kwp 0 --current-battery-kwh 0
treats it as diesel only today. Prices come from the site record and the flags;
there are no hidden defaults for capital costs.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from vaticore.decisions.sizing import SizingCosts, SizingReport, size_site
from vaticore.features.solar import pv_output_per_kwp
from vaticore.features.weather import OpenMeteoProvider
from vaticore.schemas import GRID_AVAILABLE, LOAD_KW, TIMESTAMP
from vaticore.sites import Site, load_portfolio


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _history(args: argparse.Namespace, index: pd.DatetimeIndex) -> tuple[pd.DataFrame, str]:
    if args.history:
        frame = pd.read_csv(args.history)
        frame[TIMESTAMP] = pd.to_datetime(frame[TIMESTAMP], utc=True)
        return frame, f"measured load from `{Path(args.history).name}`"
    if args.demo_flat_load is None:
        raise SystemExit("pass --history, or --demo-flat-load KW for an illustrative study")
    rng = np.random.default_rng(7)
    load = args.demo_flat_load * (1.0 + rng.normal(0.0, 0.05, len(index)))
    return (
        pd.DataFrame({TIMESTAMP: index, LOAD_KW: load}),
        f"ILLUSTRATIVE flat load of {args.demo_flat_load:g} kW (plus 5% noise); "
        "replace with the site's measured load",
    )


def _markdown(report: SizingReport, site: Site, args: argparse.Namespace, notes: list[str]) -> str:
    cur = report.currency
    frame = report.to_frame().sort_values("total_annual_cost")
    best, reliable = report.recommended, report.recommended_reliable
    lines = [
        f"# Sizing study: {site.name}",
        "",
        f"`{site.operator_id} / {site.site_id}` ({site.site_type.value}), "
        f"{site.latitude:.2f}, {site.longitude:.2f}. {report.hours} hours studied, "
        "results scaled to one year.",
        "",
        f"**{report.summary()}**",
        "",
        "## Inputs",
        "",
        *[f"- {n}" for n in notes],
        f"- Capital: solar {cur} {args.pv_capex:,.0f} per kWp, battery {cur} "
        f"{args.battery_capex:,.0f} per kWh; discount rate {100 * args.discount_rate:.0f}%; "
        f"lives {args.pv_life:g} years (solar) and {args.battery_life:g} years (battery).",
        f"- Running: diesel {cur} {site.generator.fuel_price_per_l:,.0f} per litre"
        if site.generator
        else "- Running: no generator",
        f"- An unserved kWh priced at {cur} {site.value_of_lost_load_per_kwh:,.0f}.",
        f"- Gaps filled: {report.filled_load_hours} load hours, "
        f"{report.filled_solar_hours} solar hours.",
        "",
        "## Options, cheapest first",
        "",
        "| Solar (kWp) | Battery (kWh) | Added cost | Diesel (L/yr) | Generator h/yr | "
        "Unserved (kWh/yr) | Running cost/yr | Total cost/yr | Saving/yr | Payback (yr) | CO2 avoided (t/yr) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in frame.head(args.top).iterrows():
        tag = " (today)" if r["is_current"] else ""
        if r["pv_kwp"] == best.pv_kwp and r["battery_kwh"] == best.battery_kwh:
            tag += " **recommended**"
        payback = "" if pd.isna(r["payback_years"]) else f"{r['payback_years']:.1f}"
        lines.append(
            f"| {r['pv_kwp']:g}{tag} | {r['battery_kwh']:g} | {r['capex']:,.0f} | "
            f"{r['fuel_l']:,.0f} | {r['genset_hours']:,.0f} | {r['unserved_kwh']:,.0f} | "
            f"{r['operating_cost']:,.0f} | {r['total_annual_cost']:,.0f} | "
            f"{r['annual_saving']:,.0f} | {payback} | {r['co2_avoided_t']:,.1f} |"
        )
    if reliable is not best:
        lines += [
            "",
            f"At least as reliable as today: {reliable.pv_kwp:g} kWp, {reliable.battery_kwh:g} kWh.",
        ]
    lines += [
        "",
        "## Method",
        "",
        "Each option runs hour by hour through the same model as Vaticore's daily "
        "planner: grid when on, then the generator (started when the site would "
        "otherwise go short, as a site controller does), then the battery. Equipment "
        "is annualised with the capital recovery factor over its life. Solar output "
        "per kWp comes from hourly irradiance and air temperature at the site "
        "(performance ratio 0.80, temperature derating; horizontal irradiance, slightly "
        "conservative near the equator). CO2 at 2.68 kg per litre of diesel.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--portfolio", type=Path, default=Path("examples/sites/nigeria_portfolio.toml")
    )
    parser.add_argument("--operator-id", default=None)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--history", type=Path, default=None)
    parser.add_argument("--demo-flat-load", type=float, default=None)
    parser.add_argument("--year", type=int, default=2025, help="weather year for solar")
    parser.add_argument("--current-pv-kwp", type=float, default=None)
    parser.add_argument("--current-battery-kwh", type=float, default=None)
    parser.add_argument("--pv-options", default="0,5,10,15,20,25,30")
    parser.add_argument("--battery-options", default="0,15,30,45,60")
    parser.add_argument("--pv-capex", type=float, required=True)
    parser.add_argument("--battery-capex", type=float, required=True)
    parser.add_argument("--discount-rate", type=float, required=True)
    parser.add_argument("--pv-life", type=float, required=True)
    parser.add_argument("--battery-life", type=float, required=True)
    parser.add_argument("--pv-om", type=float, default=0.0, help="per kWp per year")
    parser.add_argument("--battery-om", type=float, default=0.0, help="per kWh per year")
    parser.add_argument("--top", type=int, default=12, help="options to list in the report")
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args()

    portfolio = load_portfolio(args.portfolio)
    matches = [
        s
        for s in portfolio.sites
        if s.site_id == args.site_id and (args.operator_id in (None, s.operator_id))
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one site {args.site_id!r}, found {len(matches)}")
    site = matches[0]

    weather = OpenMeteoProvider().hourly(
        site.latitude,
        site.longitude,
        pd.Timestamp(f"{args.year}-01-01"),
        pd.Timestamp(f"{args.year}-12-31"),
    )
    solar = pv_output_per_kwp(weather)
    history, load_note = _history(args, pd.DatetimeIndex(solar.index))
    frame = history.set_index(TIMESTAMP).sort_index()
    frame = frame[(frame.index >= solar.index[0]) & (frame.index <= solar.index[-1])]

    base = site.dispatch_assets()
    current_pv = site.solar.kwp if site.solar else 0.0
    notes = [
        f"Load: {load_note}.",
        f"Solar: Open-Meteo archive weather for {args.year} at the site "
        f"({solar.mean() * 24:.2f} kWh per kWp per day on average).",
    ]
    if args.current_pv_kwp is not None:
        current_pv = args.current_pv_kwp
        notes.append(f"Today's solar taken as {current_pv:g} kWp (study override).")
    if args.current_battery_kwh is not None:
        kwh = args.current_battery_kwh
        base = replace(
            base,
            battery_kwh=kwh,
            battery_power_kw=max(kwh * 0.5, 1e-6),
            min_soc_kwh=0.1 * kwh,
        )
        notes.append(f"Today's battery taken as {kwh:g} kWh (study override).")

    grid = None
    if base.grid_kw > 0:
        if site.grid is not None and site.grid.reliable:
            grid = pd.Series(1.0, index=frame.index)
        elif GRID_AVAILABLE in frame.columns:
            grid = frame[GRID_AVAILABLE].astype(float)
        else:
            raise SystemExit("this site has an unreliable grid: history needs grid_available")

    report = size_site(
        frame[LOAD_KW],
        solar,
        base,
        current_pv_kwp=current_pv,
        pv_options_kwp=_floats(args.pv_options),
        battery_options_kwh=_floats(args.battery_options),
        costs=SizingCosts(
            pv_capex_per_kwp=args.pv_capex,
            battery_capex_per_kwh=args.battery_capex,
            discount_rate=args.discount_rate,
            pv_life_years=args.pv_life,
            battery_life_years=args.battery_life,
            pv_om_per_kwp_year=args.pv_om,
            battery_om_per_kwh_year=args.battery_om,
        ),
        currency=site.currency,
        grid_available=grid,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    md = _markdown(report, site, args, notes)
    (args.out / f"sizing_{site.site_id}.md").write_text(md)
    (args.out / f"sizing_{site.site_id}.json").write_text(
        json.dumps(
            {
                "site": site.model_dump(mode="json"),
                "notes": notes,
                "summary": report.summary(),
                "options": json.loads(report.to_frame().to_json(orient="records")),
            },
            indent=2,
        )
    )
    print(md)


if __name__ == "__main__":
    main()
