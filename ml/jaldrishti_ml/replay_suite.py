"""Robust check of on-device learning: many replays (seasons x random forecast
errors), averaged, so one lucky/unlucky week cannot mislead us.

    python -m jaldrishti_ml.replay_suite
"""
from __future__ import annotations

import pandas as pd

from . import config
from .predictor import MoisturePredictor
from .replay import SCENARIOS, run

# rabi season (Nov-Mar) and the hot pre-monsoon (Apr-Jun) - when irrigation matters
STARTS = ["2022-11-20", "2024-04-10"]
CITIES = config.TEST_LOCATIONS          # one unseen city per state


def main(days: int = 21):
    pred = MoisturePredictor()
    frames = []
    for name in SCENARIOS:
        for city in CITIES:
            for i, start in enumerate(STARTS):
                r = run(name, days=days, start=start, seed=i, city=city, predictor=pred, verbose=False)
                frames.append(r["rows"].assign(scenario=name, start=start, city=city))
    all_rows = pd.concat(frames)
    table = (all_rows.groupby(["scenario", "h", "week"])[["err_still", "err_raw", "err_used"]]
             .mean().round(3))
    table["learning_gain_%"] = (100 * (1 - table["err_used"] / table["err_raw"])).round(1)
    print(f"{len(STARTS) * len(CITIES)} replays per scenario, {days} days each, unseen cities {CITIES}")
    print(table.to_string())
    return table


if __name__ == "__main__":
    main()
