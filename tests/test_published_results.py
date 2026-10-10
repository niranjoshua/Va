"""Published numbers are locked to the result files they came from.

Every study writes its numbers twice: a JSON file (the record) and a Markdown
table (what people read), under docs/research/results/. The research notes,
the research index and the README then quote the headline numbers in prose.

These tests check every number in every results table against its JSON, to
the precision printed, and every headline quoted in prose against the JSON it
came from. A study rerun that changes a result must therefore change the
tables and the prose with it, in the same commit, or CI fails. Nothing drifts
silently. (tests/test_golden.py does the same for the code that produces the
numbers.)
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "docs" / "research" / "results"


@cache
def result(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{name}.json").read_text())
    return data


def _number(cell: str) -> tuple[float, int, bool] | None:
    """A printed number: value, decimals shown, and whether it is a percentage."""
    match = re.fullmatch(r"\s*([+-]?[\d,]*\.?\d+)\s*(%?)\s*", cell)
    if not match:
        return None
    text = match.group(1).replace(",", "")
    decimals = len(text.split(".")[1]) if "." in text else 0
    return float(text), decimals, bool(match.group(2))


def _tables(markdown: str) -> list[tuple[list[str], list[list[str]]]]:
    tables, lines = [], markdown.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and set(lines[i + 1]) <= set("|-: "):
            header = [c.strip() for c in lines[i].strip("|").split("|")]
            rows, i = [], i + 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            tables.append((header, rows))
        else:
            i += 1
    return tables


def _scores(data: dict[str, Any]) -> dict[str, dict[str, float]]:
    return {s["model"]: s for s in data.get("scores", [])}


def _calibration(data: dict[str, Any], model: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in data["calibration"][model]:
        if row["kind"].startswith("band"):
            out["held"], out["width"] = row["observed"], row["mean_width"]
        elif row["nominal"] == 0.1:
            out["below10"] = row["observed"]
        elif row["nominal"] == 0.9:
            out["below90"] = row["observed"]
    return out


def _expected(name: str, header: list[str], label: str, column: str) -> float | None:
    """The JSON value a table cell should show, or None if the column is not numeric."""
    data = result(name)
    if any("Pinball" in h for h in header):
        scores = _scores(data)
        row = scores[label]
        reference = scores["persistence"]["pinball"]
        if column.startswith("Pinball"):
            return float(row["pinball"])
        if column == "vs persistence":
            return 100 * (1 - row["pinball"] / reference)
        if column.startswith("MAE"):
            return float(row["mae"])
        if column.startswith("P10-P90"):
            return 100 * row["coverage_80"]
    elif header[0] == "Policy" and any("per kWh" in h for h in header):
        return float(data["sensitivity"][label][str(float(column.split()[0]))])
    elif header[0] == "Policy":
        policy = data["value"][label]
        fields = {
            "Diesel (L)": policy["fuel_l"],
            "Generator hours": policy["genset_hours"],
            "Unserved (kWh)": policy["unserved_kwh"],
            "Outage hours": policy["unserved_hours"],
            "Hours with outages": policy["unserved_hours"],
            "Total cost": policy.get("total_cost"),
            "Saved vs baseline": policy.get("savings_vs_baseline"),
            "Starts": policy.get("genset_starts"),
        }
        if column == "Saved vs today":
            return 100 * policy["fuel_saved_vs_reference"]
        if column in fields:
            value = fields[column]
            if value is None:
                raise KeyError(column)
            return float(value)
        if column.startswith("Share of possible"):
            return 100 * policy["share_of_possible"]
    elif any("holds" in h for h in header):
        cal = _calibration(data, label)
        if column.startswith("P10-P90"):
            return 100 * cal["held"]
        if column.startswith("Mean width"):
            return cal["width"]
        if column.startswith("Below P10"):
            return 100 * cal["below10"]
        if column.startswith("Below P90"):
            return 100 * cal["below90"]
    return None


RESULT_FILES = sorted(p.stem for p in RESULTS.glob("*.md"))


@pytest.mark.parametrize("name", RESULT_FILES)
def test_every_table_cell_matches_its_json(name: str) -> None:
    checked, problems = 0, []
    for header, rows in _tables((RESULTS / f"{name}.md").read_text()):
        for row in rows:
            label = row[0]
            for column, cell in zip(header[1:], row[1:], strict=False):
                printed = _number(cell)
                if printed is None:
                    continue
                try:
                    expected = _expected(name, header, label, column)
                except KeyError:
                    problems.append(f"{label!r} / {column!r}: not in {name}.json")
                    continue
                if expected is None:
                    continue
                value, decimals, _ = printed
                if abs(value - expected) > 0.5 * 10**-decimals + 1e-9:
                    problems.append(f"{label!r} / {column!r}: printed {cell}, JSON {expected:.6g}")
                checked += 1
    assert not problems, f"{name}.md disagrees with {name}.json:\n" + "\n".join(problems)
    assert checked >= 10, f"only {checked} cells of {name}.md were checked"


# -- headlines quoted in prose ---------------------------------------------------


def pct(x: float, decimals: int = 0) -> str:
    return f"{100 * x:.{decimals}f}%"


def _value(name: str, policy: str, field: str) -> float:
    return float(result(name)["value"][policy][field])


def _reduction(name: str, field: str, policy: str, baseline: str = "persistence P50") -> float:
    return 1 - _value(name, policy, field) / _value(name, baseline, field)


def _range(values: list[float]) -> str:
    low, high = sorted(round(100 * v) for v in values)[:: len(values) - 1]
    return f"{low}%" if low == high else f"{low} to {high}%"


SITES = ("value_study_site-A-600kWh", "value_study_site-B-300kWh")
CQR = "quantile_gbm_day_ahead+conformal"
CQR_P90 = f"{CQR} P90"
ELIA = "Elia day-ahead (professional)"


def _cov(name: str, model: str) -> float:
    return float(_scores(result(name))[model]["coverage_80"])


def _pinball(name: str, model: str) -> float:
    return float(_scores(result(name))[model]["pinball"])


def _skill(name: str, model: str) -> float:
    return 1 - _pinball(name, model) / _pinball(name, "persistence")


BEST_RUN = "Today: start when the battery runs out"
TIMER = "Today: evening timer"
ATS = "Today: generator whenever the grid is off"
V_PLAN = "Vaticore plan, today's planner"
V_HARD = "Vaticore plan, run hard"
V_AHEAD = "Vaticore plan, run hard + look ahead"
SETTING = "Charger setting alone, no forecast"
BOUND = "Perfect forecast, run hard + look ahead"


def _diesel(site: str, policy: str, field: str = "fuel_l") -> float:
    return float(result(f"diesel_test_{site}")["value"][policy][field])


def _less(site: str, policy: str, reference: str) -> str:
    """'19.6%': the diesel a policy saved against a reference practice."""
    return pct(1 - _diesel(site, policy) / _diesel(site, reference), 1)


NOTE4 = "docs/research/diesel-practice.md"

# (document, a function giving the exact text it must contain)
CLAIMS: list[tuple[str, Callable[[], str]]] = [
    # Research note 4: diesel against how sites run today.
    (NOTE4, lambda: f"**{_less('tower', V_AHEAD, BEST_RUN)} less diesel than"),
    ("docs/pipeline.md", lambda: f"Saved {_less('tower', SETTING, BEST_RUN)} at the study's"),
    ("docs/pipeline.md", lambda: f"battery first cut {_less('tower-grid', BEST_RUN, ATS)} of"),
    (
        "README.md",
        lambda: (
            f"Vaticore's plan used {_less('tower', V_AHEAD, BEST_RUN)} less diesel than the "
            f"best-run practice and {_less('tower', V_AHEAD, TIMER)}\nless than an evening timer"
        ),
    ),
    (
        "README.md",
        lambda: f"used {_less('tower-grid', BEST_RUN, ATS)} less diesel than running",
    ),
    (
        "README.md",
        lambda: (
            f"diesel fell {_less('minigrid', V_HARD, BEST_RUN)} and\noutage hours fell from "
            f"{_diesel('minigrid', BEST_RUN, 'unserved_hours'):.0f} to "
            f"{_diesel('minigrid', V_HARD, 'unserved_hours'):.0f}"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: (
            f"Off-grid tower: {_less('tower', V_AHEAD, BEST_RUN)} less diesel than the best-run "
            f"practice, {_less('tower', V_AHEAD, TIMER)} less than an evening timer; the run-hard "
            f"setting alone {_less('tower', SETTING, BEST_RUN)}. Weak-grid tower: battery first "
            f"{_less('tower-grid', BEST_RUN, ATS)} less than the generator whenever the grid is "
            f"off. Mini-grid: {_less('minigrid', V_HARD, BEST_RUN)} less diesel, outage hours "
            f"{_diesel('minigrid', BEST_RUN, 'unserved_hours'):.0f} to "
            f"{_diesel('minigrid', V_HARD, 'unserved_hours'):.0f}"
        ),
    ),
    (NOTE4, lambda: f"**{_less('tower', V_AHEAD, TIMER)} less than an evening timer**"),
    (NOTE4, lambda: f"saved\n  **{_less('tower', SETTING, BEST_RUN)}**"),
    (NOTE4, lambda: f"perfect forecast allows ({_less('tower', BOUND, BEST_RUN)})"),
    (NOTE4, lambda: f"Battery first cut diesel by **{_less('tower-grid', BEST_RUN, ATS)}**"),
    (NOTE4, lambda: f"by **{_less('tower-grid', V_PLAN, ATS)}**"),
    (NOTE4, lambda: f"diesel fell only **{_less('minigrid', V_HARD, BEST_RUN)}**"),
    (
        NOTE4,
        lambda: (
            f"**from {_diesel('minigrid', BEST_RUN, 'unserved_hours'):.0f} to "
            f"{_diesel('minigrid', V_HARD, 'unserved_hours'):.0f}**"
        ),
    ),
    # Research note 1: the 2018 value study, two sites.
    (
        "README.md",
        lambda: (
            "cut unserved\nenergy by "
            + _range([_reduction(s, "unserved_kwh", CQR_P90) for s in SITES])
        ),
    ),
    (
        "README.md",
        lambda: (
            "outage hours by " + _range([_reduction(s, "unserved_hours", CQR_P90) for s in SITES])
        ),
    ),
    (
        "README.md",
        lambda: (
            "for "
            + _range([-_reduction(s, "fuel_l", CQR_P90) for s in SITES]).replace(
                "%", "% more diesel"
            )
        ),
    ),
    (
        "README.md",
        lambda: "total cost by " + _range([_reduction(s, "total_cost", CQR_P90) for s in SITES]),
    ),
    (
        "README.md",
        lambda: (
            "captured "
            + _range([_value(s, CQR_P90, "share_of_possible") for s in SITES])
            + " of the value of a perfect forecast"
        ),
    ),
    (
        "README.md",
        lambda: (
            "Pinball loss fell "
            + _range(
                [
                    1
                    - result(s)["pinball"]["quantile_gbm_day_ahead"]["pinball"]
                    / result(s)["pinball"]["persistence"]["pinball"]
                    for s in SITES
                ]
            )
        ),
    ),
    (
        "README.md",
        lambda: (
            "range held " + pct(_calibration(result(SITES[0]), CQR)["held"], 1) + " of\noutcomes"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: (
            "Calibrated P90 plan: "
            + _range([_reduction(s, "unserved_kwh", CQR_P90) for s in SITES])
            + " less unserved energy, "
            + _range([_reduction(s, "unserved_hours", CQR_P90) for s in SITES])
            + " fewer outage hours, "
            + _range([_reduction(s, "total_cost", CQR_P90) for s in SITES])
            + " lower cost"
        ),
    ),
    # Research note 2: against Elia.
    (
        "README.md",
        lambda: f"pinball loss ({_pinball('elia_load', CQR):.1f} MW\neach)",
    ),
    (
        "README.md",
        lambda: (
            f"MAE ({_scores(result('elia_load'))[CQR]['mae']:.1f} against "
            f"{_scores(result('elia_load'))[ELIA]['mae']:.1f} MW)"
        ),
    ),
    (
        "README.md",
        lambda: (
            f"{pct(_cov('elia_load', CQR), 1)} of outcomes against Elia's "
            f"{pct(_cov('elia_load', ELIA), 1)}"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: (
            f"better calibrated range ({pct(_cov('elia_load', CQR), 1)} against "
            f"{pct(_cov('elia_load', ELIA), 1)})"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: (
            f"Elia {pct(_skill('elia_solar', ELIA))} better than persistence, Vaticore "
            f"{pct(_skill('elia_solar', CQR))}"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: (
            f"calibrated P90 captured {pct(_value('elia_value', CQR_P90, 'share_of_possible'))} "
            f"of possible savings, Elia's medians "
            f"{pct(_value('elia_value', 'Elia medians (load P50 minus solar P50)', 'share_of_possible'))}"
        ),
    ),
    # Research note 3: foundation models.
    (
        "docs/research/README.md",
        lambda: (
            f"{pct(1 - _pinball('foundation_load', 'chronos-2+conformal') / _pinball('foundation_load', ELIA))}"
            " lower pinball than Elia's own load forecast"
        ),
    ),
    (
        "docs/research/README.md",
        lambda: f"calibrated range {pct(_cov('foundation_load', 'chronos-2+conformal'), 1)}",
    ),
    (
        "docs/research/README.md",
        lambda: (
            f"value {pct(_value('foundation_value', 'chronos-2+conformal P90', 'share_of_possible'), 1)}"
            f" of possible against {pct(_value('foundation_value', CQR_P90, 'share_of_possible'), 1)}"
        ),
    ),
]


@pytest.mark.parametrize(("document", "claim"), CLAIMS)
def test_headlines_quote_the_results(document: str, claim: Callable[[], str]) -> None:
    text = (ROOT / document).read_text()
    expected = claim()
    assert expected in text, f"{document} should say {expected!r} (from the results files)"


def test_the_chronos_headline_holds_against_both_references() -> None:
    """'21% lower pinball than Elia's own load forecast and Vaticore's GBM'."""
    chronos = _pinball("foundation_load", "chronos-2+conformal")
    for reference in (ELIA, CQR):
        lift = 1 - chronos / _pinball("foundation_load", reference)
        assert pct(lift) == "21%", reference
