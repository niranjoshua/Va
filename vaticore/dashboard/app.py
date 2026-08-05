"""Streamlit dashboard skeleton.

The first credible demo surface. It shows a site's forecast against actuals with
the P10 to P90 uncertainty band, and the battery/genset advisory. Front end
polish stays deliberately behind forecast quality (Golden Rule: do not let front
end polish precede a good forecast).

Run with: uv sync --extra dashboard, then
uv run streamlit run vaticore/dashboard/app.py
"""

from __future__ import annotations


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="Vaticore", layout="wide")
    st.title("Vaticore")
    st.caption("Probabilistic load and solar forecasting for distributed energy operators.")

    st.info(
        "Demo skeleton. Wire this to the backtest harness and a fitted model to plot "
        "forecast versus actual with the P10 to P90 band, and the battery and genset advisory."
    )
    # TODO(build task 6): site selector, forecast vs actual chart with uncertainty
    # band, and the advisory panel from vaticore.decisions.


if __name__ == "__main__":
    main()
