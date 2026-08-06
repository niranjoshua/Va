"""Translate a forecast-accuracy gain into operator currency.

Model accuracy (pinball loss, MAE) does not win pilots. Litres of diesel, battery
cycles and CO2 do. This script converts a forecast-error reduction into an
ILLUSTRATIVE estimate of diesel, cost and CO2 avoided, with every assumption made
explicit and editable. It is deliberately conservative and transparent: it is a
sales and planning tool, not a measured result. The real number comes from a
shadow-mode pilot on the operator's own site.

The one lever that carries the uncertainty is buffer_fraction: the share of
diesel burn that exists as an operational buffer against forecast uncertainty,
and that better forecasts can therefore shave. Published fuel savings from
forecasting and dispatch optimisation on diesel-solar hybrids typically land in
the mid single digits to low double digits of percent; the defaults below stay at
the conservative end of that range.

    uv run python examples/operator_savings.py
"""

from __future__ import annotations

from dataclasses import dataclass

HOURS_PER_YEAR = 8760
CO2_KG_PER_LITRE_DIESEL = 2.68  # Standard emission factor for diesel fuel.


@dataclass(frozen=True)
class SiteAssumptions:
    """Everything the estimate depends on. Edit these per site."""

    name: str
    avg_load_kw: float  # Average site demand.
    diesel_energy_share: float  # Fraction of energy currently served by the genset.
    diesel_litres_per_kwh: float  # Genset fuel intensity.
    diesel_price_usd_per_litre: float  # Local pump or delivered price.
    forecast_error_reduction: float  # Fractional MAE cut vs the baseline (from backtest).
    buffer_fraction: float  # Share of diesel burn attributable to forecast uncertainty.


@dataclass(frozen=True)
class SavingsEstimate:
    """Illustrative annual impact for one site."""

    diesel_litres_total: float
    diesel_litres_avoided: float
    usd_saved: float
    co2_tonnes_avoided: float
    diesel_reduction_pct: float


def estimate_savings(site: SiteAssumptions) -> SavingsEstimate:
    """Convert a forecast-error reduction into an illustrative annual saving.

    The avoidable share of diesel is buffer_fraction * forecast_error_reduction:
    of the diesel burned as a hedge against getting the forecast wrong, we assume
    a better forecast removes it in proportion to how much it cuts the error.
    """
    total_kwh = site.avg_load_kw * HOURS_PER_YEAR
    diesel_kwh = total_kwh * site.diesel_energy_share
    diesel_litres_total = diesel_kwh * site.diesel_litres_per_kwh

    avoidable_share = site.buffer_fraction * site.forecast_error_reduction
    diesel_litres_avoided = diesel_litres_total * avoidable_share

    usd_saved = diesel_litres_avoided * site.diesel_price_usd_per_litre
    co2_tonnes_avoided = diesel_litres_avoided * CO2_KG_PER_LITRE_DIESEL / 1000.0

    return SavingsEstimate(
        diesel_litres_total=diesel_litres_total,
        diesel_litres_avoided=diesel_litres_avoided,
        usd_saved=usd_saved,
        co2_tonnes_avoided=co2_tonnes_avoided,
        diesel_reduction_pct=avoidable_share * 100.0,
    )


def _print_report(site: SiteAssumptions, est: SavingsEstimate, fleet_sites: int) -> None:
    print(f"Site: {site.name}   (ILLUSTRATIVE estimate, not a measured result)")
    print("-" * 60)
    print("Assumptions")
    print(f"  Average load                {site.avg_load_kw:,.0f} kW")
    print(f"  Diesel share of energy      {site.diesel_energy_share:.0%}")
    print(f"  Genset fuel intensity       {site.diesel_litres_per_kwh:.2f} L/kWh")
    print(f"  Diesel price                ${site.diesel_price_usd_per_litre:.2f}/L")
    print(f"  Forecast error reduction    {site.forecast_error_reduction:.0%} vs baseline")
    print(f"  Buffer attributable to fcst {site.buffer_fraction:.0%}")
    print()
    print("Illustrative annual impact per site")
    print(f"  Diesel burned today         {est.diesel_litres_total:,.0f} L")
    print(
        f"  Diesel avoided              {est.diesel_litres_avoided:,.0f} L "
        f"({est.diesel_reduction_pct:.1f}% of diesel)"
    )
    print(f"  Money saved                 ${est.usd_saved:,.0f}")
    print(f"  CO2 avoided                 {est.co2_tonnes_avoided:,.1f} tonnes")
    print()
    print(f"Across a {fleet_sites}-site fleet (same assumptions)")
    print(f"  Money saved                 ${est.usd_saved * fleet_sites:,.0f} / year")
    print(
        f"  CO2 avoided                 {est.co2_tonnes_avoided * fleet_sites:,.0f} tonnes / year"
    )
    print()
    print("Every number scales with the assumptions above. Replace them with a")
    print("real site's figures, and replace the estimate with a pilot measurement.")


def main() -> None:
    # A representative mid-size hybrid mini-grid. Conservative defaults.
    site = SiteAssumptions(
        name="Representative 120 kW hybrid mini-grid",
        avg_load_kw=120.0,
        diesel_energy_share=0.35,
        diesel_litres_per_kwh=0.30,
        diesel_price_usd_per_litre=1.10,
        forecast_error_reduction=0.40,
        buffer_fraction=0.15,
    )
    estimate = estimate_savings(site)
    _print_report(site, estimate, fleet_sites=50)


if __name__ == "__main__":
    main()
