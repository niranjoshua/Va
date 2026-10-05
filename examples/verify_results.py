"""Rerun the published studies and check they still give the published numbers.

The studies' data stays off the repository (licences, size), so CI cannot
rerun them; it checks the code paths on synthetic data instead
(tests/test_golden.py). Run this wherever the data is, before publishing and
after any change the golden tests flag:

    uv run python examples/verify_results.py --all \\
        --elia-load-csv data/elia/ods001.csv --elia-solar-csv data/elia/ods032.csv \\
        --spain-csv data/spain/energy_dataset.csv

    uv run python examples/verify_results.py elia_load foundation_short ...

Each study is rerun into a temporary folder with the settings recorded in its
committed result (assets, period, scored days), then every number is compared
with docs/research/results/<study>.json. Runtimes, file paths and timing
fields are ignored. Exit status 1 if any study drifted, with every changed
number listed. Foundation studies need the foundation extra and take longest.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vaticore.evaluation.regression import differences  # noqa: E402

RESULTS = ROOT / "docs" / "research" / "results"
# Fields that legitimately differ between runs or machines.
IGNORE = {"seconds", "runtime", "source_csv", "solar_csv"}


def _committed(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((RESULTS / f"{name}.json").read_text())
    return data


def _use_results(folder: Path) -> None:
    global RESULTS
    RESULTS = folder


def _value_study_args(name: str, args: argparse.Namespace) -> list[str]:
    """Rebuild a value study's command from what its committed result recorded."""
    data = _committed(name)
    meta, assets = data["meta"], data["assets"]
    scored = int(re.search(r"Scored days: (\d+)", (RESULTS / f"{name}.md").read_text())[1])  # type: ignore[index]
    total = int(meta["hours"]) // 24
    start, end = meta["period_utc"]
    return [
        str(ROOT / "examples" / "value_study.py"),
        str(args.spain_csv),
        "--site-id", name.removeprefix("value_study_"),
        "--start", start, "--end", end,
        "--initial-days", str(total - scored),
        "--peak-load-kw", str(meta["peak_load_kw"]), "--pv-kwp", str(meta["pv_kwp"]),
        "--battery-kwh", str(assets["battery_kwh"]),
        "--battery-power-kw", str(assets["battery_power_kw"]),
        "--min-soc-kwh", str(assets["min_soc_kwh"]), "--genset-kw", str(assets["genset_kw"]),
        "--diesel-price", str(assets["diesel_price_per_l"]),
        "--voll", str(assets["value_of_lost_load_per_kwh"]),
    ]  # fmt: skip


def _command(name: str, args: argparse.Namespace) -> tuple[list[str], str]:
    """The command that reproduces a study, and the result file it writes."""
    elia = str(ROOT / "examples" / "elia_study.py")
    foundation = str(ROOT / "examples" / "foundation_study.py")
    data = {
        "elia_load": [elia, "load", "--load-csv", args.elia_load_csv],
        "elia_solar": [elia, "solar", "--solar-csv", args.elia_solar_csv],
        "elia_value": [elia, "value", "--load-csv", args.elia_load_csv,
                       "--solar-csv", args.elia_solar_csv],
        "foundation_design": [foundation, "design", "--load-csv", args.elia_load_csv],
        "foundation_load": [foundation, "load", "--load-csv", args.elia_load_csv],
        "foundation_short": [foundation, "short", "--load-csv", args.elia_load_csv],
        "foundation_value": [foundation, "value", "--load-csv", args.elia_load_csv,
                             "--solar-csv", args.elia_solar_csv],
    }  # fmt: skip
    if name.startswith("value_study_"):
        return _value_study_args(name, args), f"value_study_{name.removeprefix('value_study_')}"
    if name not in data:
        raise SystemExit(f"unknown study {name!r}; known: {', '.join(studies())}")
    command = [str(c) for c in data[name]]
    if "None" in command:
        raise SystemExit(f"{name} needs its data files (see --help)")
    return command, name


def _needs(name: str) -> tuple[str, ...]:
    """The data files a study reads."""
    if name.startswith("value_study_"):
        return ("spain_csv",)
    if name in ("elia_value", "foundation_value"):
        return ("elia_load_csv", "elia_solar_csv")
    if name == "elia_solar":
        return ("elia_solar_csv",)
    return ("elia_load_csv",)


def studies() -> list[str]:
    return sorted(p.stem for p in RESULTS.glob("*.json"))


def verify(name: str, args: argparse.Namespace) -> list[str]:
    command, output = _command(name, args)
    with tempfile.TemporaryDirectory() as out:
        print(f"rerunning {name} ...", flush=True)
        run = subprocess.run(
            [sys.executable, *command, "--out", out], cwd=ROOT, capture_output=True, text=True
        )
        if run.returncode != 0:
            return [f"the study failed: {run.stderr.strip()[-800:]}"]
        fresh = json.loads((Path(out) / f"{output}.json").read_text())
    return differences(_committed(name), fresh, rtol=args.rtol, atol=1e-9, ignore=IGNORE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("study", nargs="*", help=f"any of: {', '.join(studies())}")
    parser.add_argument("--all", action="store_true", help="every study with data given")
    parser.add_argument("--elia-load-csv", type=Path)
    parser.add_argument("--elia-solar-csv", type=Path)
    parser.add_argument("--spain-csv", type=Path)
    parser.add_argument("--rtol", type=float, default=1e-6, help="relative tolerance")
    parser.add_argument("--results", type=Path, help="published results folder (for tests)")
    args = parser.parse_args(argv)
    if args.results:
        _use_results(args.results)

    chosen = list(args.study)
    if args.all:
        given = {k for k, v in vars(args).items() if v is not None and v is not False}
        chosen = [s for s in studies() if set(_needs(s)) <= given]
    if not chosen:
        parser.error("name studies, or --all with their data files")

    drifted = 0
    for name in chosen:
        problems = verify(name, args)
        if problems:
            drifted += 1
            print(f"{name}: DRIFTED ({len(problems)} change(s))")
            for problem in problems[:50]:
                print(f"  {problem}")
        else:
            print(f"{name}: matches the published result")
    print(f"\n{len(chosen) - drifted} of {len(chosen)} studies reproduce their published numbers.")
    return 1 if drifted else 0


if __name__ == "__main__":
    sys.exit(main())
