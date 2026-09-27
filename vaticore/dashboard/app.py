"""Streamlit dashboard: the first credible demo surface.

Shows a site's probabilistic forecast against actuals with a P10 to P90
uncertainty band, the backtest scorecard against the persistence baseline, and
the battery and genset advisory. Everything runs through the engine facade, so
the dashboard shows exactly what the API would return.

Front end polish stays behind forecast quality on purpose. This is a working
demo of a real forecast, not a mockup.

Run with:
    uv sync --extra dashboard
    uv run streamlit run vaticore/dashboard/app.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from vaticore import engine
from vaticore.datasets import make_synthetic_fleet
from vaticore.decisions.dispatch import DispatchPlan, SiteAssets
from vaticore.evaluation.calibration import recent_coverage
from vaticore.forecasting.base import ForecasterError, quantile_column
from vaticore.schemas import GENERATION_KW, LOAD_KW, OPERATOR_ID, SITE_ID, TIMESTAMP

QUANTILES = (0.1, 0.5, 0.9)
HISTORY_TAIL = 24 * 7  # show one week of context behind the forecast

# Brand palette, matching docs/landing/brand. Teal marks the forecast only.
INK = "#1D1A1E"
TEAL = "#0E8F82"
TEAL_BAND = "rgba(14,143,130,0.18)"
HISTORY_GREY = "#98949A"
AMBER = "#B86F05"
ASSETS = Path(__file__).parent / "assets"


def _load_fleet(days: int) -> pd.DataFrame:
    # Demo data. Replace with a repository read against real operator feeds.
    return make_synthetic_fleet(days=days, seed=1)


def _forecast_figure(history: pd.DataFrame, target: str, forecast: pd.DataFrame) -> Any:
    import plotly.graph_objects as go

    context = history.tail(HISTORY_TAIL)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=context[TIMESTAMP],
            y=context[target],
            name="actual",
            line={"color": HISTORY_GREY},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=forecast.index,
            y=forecast[quantile_column(0.9)],
            name="P90",
            line={"width": 0},
            showlegend=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=forecast.index,
            y=forecast[quantile_column(0.1)],
            name="P10 to P90",
            fill="tonexty",
            fillcolor=TEAL_BAND,
            line={"width": 0},
        )
    )
    fig.add_trace(
        go.Scatter(
            x=forecast.index,
            y=forecast[quantile_column(0.5)],
            name="forecast (P50)",
            line={"color": TEAL, "dash": "dash"},
        )
    )
    fig.update_layout(
        margin={"l": 10, "r": 10, "t": 30, "b": 10},
        legend={"orientation": "h"},
        yaxis_title="kW",
        height=420,
        font={"color": INK},
        plot_bgcolor="#FFFFFF",
        paper_bgcolor="#FFFFFF",
    )
    fig.update_xaxes(gridcolor="rgba(29,26,30,0.08)")
    fig.update_yaxes(gridcolor="rgba(29,26,30,0.08)")
    return fig


def _plan_figure(plan: DispatchPlan) -> Any:
    """Planned net load and generator output (kW) above the battery (kWh)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38], vertical_spacing=0.08
    )
    x = plan.timestamps
    fig.add_trace(
        go.Bar(x=x, y=plan.expected.genset_kw, name="generator (kW)", marker_color=AMBER),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=plan.planned_net_load_kw,
            name="net load to plan for, P90 (kW)",
            line={"color": TEAL},
        ),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=plan.expected.soc_kwh, name="battery (kWh)", line={"color": INK}, fill="tozeroy"
        ),
        row=2,
        col=1,
    )
    fig.update_yaxes(title_text="kW", row=1, col=1, gridcolor="rgba(29,26,30,0.08)")
    fig.update_yaxes(title_text="kWh", row=2, col=1, gridcolor="rgba(29,26,30,0.08)")
    fig.update_layout(
        height=420,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        legend={"orientation": "h"},
        font={"color": INK},
        plot_bgcolor="#FFFFFF",
        paper_bgcolor="#FFFFFF",
        bargap=0.15,
    )
    return fig


def main() -> None:
    import streamlit as st

    st.set_page_config(
        page_title="Vaticore", page_icon=str(ASSETS / "favicon-32.png"), layout="wide"
    )
    st.logo(str(ASSETS / "vaticore-lockup.png"), icon_image=str(ASSETS / "vaticore-icon-512.png"))
    st.caption("Probabilistic load and solar forecasting for distributed energy operators.")

    fleet = _load_fleet(days=90)
    sites = fleet[[OPERATOR_ID, SITE_ID]].drop_duplicates()

    with st.sidebar:
        st.header("Site")
        labels = [f"{r[OPERATOR_ID]} / {r[SITE_ID]}" for _, r in sites.iterrows()]
        choice = st.selectbox("Site", labels)
        operator_id, site_id = (part.strip() for part in choice.split("/"))
        battery = st.number_input("Usable battery (kWh)", min_value=10.0, value=600.0, step=50.0)
        battery_kw = st.number_input("Battery power (kW)", min_value=1.0, value=150.0, step=10.0)
        soc = st.slider("Battery charge now (kWh)", 0.0, float(battery), float(battery) / 2)
        genset_kw = st.number_input("Generator size (kW)", min_value=0.0, value=100.0, step=10.0)
        st.header("Forecast view")
        target_label = st.radio("Target", ["Load", "Solar generation"])
        target = LOAD_KW if target_label == "Load" else GENERATION_KW
        model = st.selectbox(
            "Model", engine.available_models(), index=len(engine.available_models()) - 1
        )
        horizon = st.slider("Forecast horizon (hours)", 6, 48, 24, step=6)

    history = engine.select_site(fleet, operator_id, site_id)

    # Site Today: lead with the schedule an operator acts on, then the chart,
    # then the evidence. Answers first, jargon later. Advisory only.
    st.subheader(f"Site today: {site_id}")
    assets = SiteAssets(battery_kwh=battery, battery_power_kw=battery_kw, genset_kw=genset_kw)
    try:
        with st.spinner("Planning the next 24 hours..."):
            plan = engine.dispatch_plan_for_site(history, assets=assets, soc_kwh=soc)
    except (ValueError, ForecasterError) as exc:
        st.error(f"Could not build today's plan: {exc}")
        plan = None

    if plan is not None:
        banner = (
            f"background:{INK};border-left:4px solid {TEAL};border-radius:12px;"
            "padding:14px 18px;margin-bottom:8px;color:#F5F5F7"
        )
        st.markdown(
            f"<div style='{banner}'><b>Today's plan.</b> {plan.summary()}</div>",
            unsafe_allow_html=True,
        )
        windows = plan.run_windows
        c1, c2, c3 = st.columns(3)
        c1.metric(
            "Generator",
            ", ".join(plan.run_window_labels) if windows else "Not needed",
        )
        c2.metric("Expected diesel", f"{plan.expected.fuel_l:.0f} L")
        c3.metric(
            "Battery at the end of the day",
            f"{100 * plan.expected.soc_end_kwh / assets.battery_kwh:.0f}%",
        )
        st.plotly_chart(_plan_figure(plan), width="stretch")
        st.caption(
            "Planned on the P90 of net load (demand minus solar), calibrated on this site's "
            "last week, so the plan holds on a bad day. Times are UTC. Advisory only: "
            "a person approves every run."
        )

    left, right = st.columns([3, 1])
    forecast = engine.forecast_site(
        history, target, horizon=horizon, model=model, quantiles=QUANTILES
    )
    with left:
        st.subheader(f"{target_label} forecast")
        st.plotly_chart(_forecast_figure(history, target, forecast), width="stretch")
    with right:
        st.subheader("How to read this")
        st.write(
            "The line is the expected forecast; the shaded band is the likely "
            "range (P10 to P90). Plan for the top of the band, so you stay "
            "covered on a bad day."
        )

    st.subheader("Track record")
    st.caption(
        "Rolling origin evaluation against the persistence baseline. Lower pinball loss is "
        "better. The candidate must beat persistence to earn its place, and its range must "
        "hold about 80% of outcomes to be trusted."
    )
    with st.spinner("Running backtest..."):
        initial = min(len(history) - horizon - 1, 24 * 45)
        result = engine.run_backtest(
            history,
            target,
            horizon=horizon,
            initial=initial,
            step=horizon * 3,
            model=model,
            quantiles=QUANTILES,
        )
    held = recent_coverage(result.forecasts_for(model), hours=24 * 30)
    m1, m2 = st.columns([1, 3])
    m1.metric(
        "Range held, recent backtest",
        f"{100 * held.observed:.0f}%",
        f"{100 * held.gap:+.0f} pts vs 80% target",
        delta_color="off",
    )
    m2.dataframe(result.summary().round(3), width="stretch")


if __name__ == "__main__":
    main()
