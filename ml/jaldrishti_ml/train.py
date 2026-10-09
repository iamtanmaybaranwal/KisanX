"""Train + evaluate the moisture forecasting models.

Model: physics-informed gradient boosting. For each horizon h (1/3/6/12 h) a
small-tree HistGradientBoostingRegressor learns the ERROR of the FAO-56 physics
forecast ("residual" target); prediction = physics + learned correction.

Target region: Punjab, Haryana, Delhi, UP, Bihar, Jharkhand, Maharashtra. Generalising to new
places is what matters (a farm is never in the training data), so the number
of boosting rounds is chosen by LEAVE-ONE-STATE-OUT: train on 6 states, score
on the 7th, rotate, average. The 7 held-out test cities (one per state) are
never used for any choice - only for the final report.

Evaluation:
    valid : training cities, later time period (Jul-Dec 2024)
    test  : 7 unseen cities, reported per state
Baselines: persistence ("no change"), trend ("last 3 h rate continues"),
           physics (FAO-56 with the farmer's settings + forecast).

The deployment model is then refit on ALL data (all 30 cities) with the chosen
rounds and saved to models/moisture_model.joblib.
"""
from __future__ import annotations

import argparse
import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.inspection import permutation_importance

from . import __version__, config, features
from .dataset import DATA_PATH

MODEL_DIR = Path(__file__).resolve().parents[1] / "models"
MODEL_PATH = MODEL_DIR / "moisture_model.joblib"
METRICS_PATH = MODEL_DIR / "metrics.json"
DECISION_H = 6
MAX_ROUNDS = 500
ROUND_STEP = 10
CV_ROWS_PER_STATE = 80_000
MODE = "residual"


def matrix(df: pd.DataFrame, h: int) -> np.ndarray:
    return features.assemble(
        m_now=df["m_now"], m_1h=df["m_1h"], m_3h=df["m_3h"],
        hours_since_water=df["hours_since_water"],
        fc_pct=df["fc_pct"], wp_pct=df["wp_pct"], depth_mm=df["depth_mm"],
        kc=df["kc"], p=df["p"],
        fc_et0_mm=df[f"fc_et0_mm_{h}"], fc_rain_mm=df[f"fc_rain_mm_{h}"],
        fc_rain_prob=df[f"fc_rain_prob_{h}"], phys_delta=df[f"phys_{h}"],
    ).astype(np.float32)


def target(df, h):
    """The model learns only what the physics gets wrong."""
    return df[f"y_{h}"].to_numpy() - df[f"phys_{h}"].to_numpy()


def predict_delta(model, df, h):
    return model.predict(matrix(df, h)) + df[f"phys_{h}"].to_numpy()


def new_model(max_iter: int) -> HistGradientBoostingRegressor:
    # small trees + strong regularisation generalise best to unseen cities (tested)
    return HistGradientBoostingRegressor(
        learning_rate=0.1, max_iter=max_iter, max_leaf_nodes=15, min_samples_leaf=2000,
        l2_regularization=5.0, early_stopping=False, random_state=0)


def clip_delta(df, delta):
    """Predicted moisture must stay on the 0-100 % scale."""
    return np.clip(df["m_now"].to_numpy() + delta, 0, 100) - df["m_now"].to_numpy()


def choose_rounds(train: pd.DataFrame, h: int) -> tuple[int, list[float]]:
    """Leave-one-state-out curve of MAE vs boosting rounds; returns the best count."""
    curves = []
    for state in sorted(train["state"].unique()):
        fit = train[train["state"] != state]
        hold = train[train["state"] == state]
        hold = hold.sample(min(CV_ROWS_PER_STATE, len(hold)), random_state=0)
        m = new_model(MAX_ROUNDS).fit(matrix(fit, h), target(fit, h))
        y = hold[f"y_{h}"].to_numpy()
        phys = hold[f"phys_{h}"].to_numpy()
        curve = [np.mean(np.abs(clip_delta(hold, p + phys) - y))
                 for i, p in enumerate(m.staged_predict(matrix(hold, h)), 1) if i % ROUND_STEP == 0]
        curves.append(curve)
    mean_curve = np.mean(curves, axis=0)
    best = (int(np.argmin(mean_curve)) + 1) * ROUND_STEP
    return best, [round(float(v), 4) for v in mean_curve]


def baselines(df: pd.DataFrame, h: int) -> dict[str, np.ndarray]:
    trend = np.nan_to_num((df["m_now"] - df["m_3h"]).to_numpy() / config.HISTORY_H) * h
    return {
        "persistence": np.zeros(len(df)),
        "trend": clip_delta(df, trend),
        "physics": df[f"phys_{h}"].to_numpy(),
    }


def error_stats(y, pred) -> dict[str, float]:
    err = pred - y
    return {"mae": float(np.mean(np.abs(err))), "rmse": float(np.sqrt(np.mean(err ** 2))),
            "p90_abs": float(np.quantile(np.abs(err), 0.9)), "bias": float(np.mean(err))}


def need_water_stats(df, y, pred) -> dict[str, float]:
    """Early warning: zone is fine NOW, will it drop below min within h hours?"""
    now_ok = df["m_now"].to_numpy() >= df["min_pct"].to_numpy()
    actual = (df["m_now"].to_numpy() + y < df["min_pct"].to_numpy())[now_ok]
    guess = (df["m_now"].to_numpy() + pred < df["min_pct"].to_numpy())[now_ok]
    tp = np.sum(actual & guess)
    prec = tp / max(guess.sum(), 1)
    rec = tp / max(actual.sum(), 1)
    return {"precision": float(prec), "recall": float(rec),
            "f1": float(2 * prec * rec / max(prec + rec, 1e-9)), "events": int(actual.sum())}


def evaluate(models, df_split, name) -> dict:
    out = {}
    for h, model in models.items():
        y = df_split[f"y_{h}"].to_numpy()
        preds = {"model": clip_delta(df_split, predict_delta(model, df_split, h))}
        preds.update(baselines(df_split, h))
        out[h] = {k: error_stats(y, p) for k, p in preds.items()}
        if h == DECISION_H:
            out[h]["need_water"] = {k: need_water_stats(df_split, y, p) for k, p in preds.items()}
    print(f"\n=== {name}: MAE in moisture %-points (lower is better) ===")
    print(f"{'horizon':>8} " + " ".join(f"{k:>12}" for k in ("model", "persistence", "trend", "physics")))
    for h in models:
        print(f"{h:>7}h " + " ".join(f"{out[h][k]['mae']:>12.3f}" for k in ("model", "persistence", "trend", "physics")))
    if DECISION_H in out:
        nw = out[DECISION_H]["need_water"]
        print(f"  'will need water within {DECISION_H}h' F1:  " +
              "  ".join(f"{k}={v['f1']:.3f}" for k, v in nw.items()) + f"   (events={nw['model']['events']})")
    return out


def per_state(models, df_split) -> dict:
    """6 h accuracy for each state's unseen city."""
    out = {}
    h = DECISION_H if DECISION_H in models else max(models)
    print(f"\n  Per state (unseen city, {h} h ahead):  MAE model / physics | need-water F1 model / physics")
    for (state, city), d in df_split.groupby(["state", "location"]):
        y = d[f"y_{h}"].to_numpy()
        pm = clip_delta(d, predict_delta(models[h], d, h))
        pp = d[f"phys_{h}"].to_numpy()
        r = {"city": city, "mae_model": float(np.mean(np.abs(pm - y))),
             "mae_physics": float(np.mean(np.abs(pp - y))),
             "f1_model": need_water_stats(d, y, pm)["f1"], "f1_physics": need_water_stats(d, y, pp)["f1"]}
        out[state] = r
        print(f"    {state:14s} ({city:11s})  {r['mae_model']:.3f} / {r['mae_physics']:.3f}   |   "
              f"{r['f1_model']:.3f} / {r['f1_physics']:.3f}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Train Jal-Drishti moisture models")
    ap.add_argument("--horizons", type=int, nargs="*", default=list(config.HORIZONS_H))
    ap.add_argument("--no-save", action="store_true", help="experiment only")
    args = ap.parse_args()

    df = pd.read_pickle(DATA_PATH)
    train, valid, test = (df[df["split"] == s] for s in ("train", "valid", "test"))
    print(f"rows: train={len(train):,d} valid={len(valid):,d} test={len(test):,d}")

    models, rounds, curves = {}, {}, {}
    for h in args.horizons:
        t0 = time.time()
        rounds[h], curves[h] = choose_rounds(train, h)
        models[h] = new_model(rounds[h]).fit(matrix(train, h), target(train, h))
        print(f"  horizon {h:>2}h: {rounds[h]} rounds (leave-one-state-out), {time.time() - t0:.0f}s", flush=True)

    metrics = {"valid": evaluate(models, valid, "VALID (later period, known cities)"),
               "test": evaluate(models, test, "TEST (7 unseen cities, one per state)")}
    metrics["test_per_state"] = per_state(models, test)
    if args.no_save:
        return

    # prediction band: 80 % of unseen-city errors fall inside +- q80 (honest width)
    q80 = {h: float(np.quantile(np.abs(clip_delta(test, predict_delta(models[h], test, h))
                                       - test[f"y_{h}"].to_numpy()), 0.8)) for h in models}

    importance = {}
    if DECISION_H in models:
        sub = valid.sample(20000, random_state=0)
        imp = permutation_importance(models[DECISION_H], matrix(sub, DECISION_H), target(sub, DECISION_H),
                                     n_repeats=3, random_state=0, scoring="neg_mean_absolute_error")
        importance = dict(sorted(zip(features.FEATURES, map(float, imp.importances_mean)),
                                 key=lambda kv: -kv[1]))
        print("\nTop inputs (6 h correction model):",
              ", ".join(f"{k} ({v:.3f})" for k, v in list(importance.items())[:6]))

    print("\nRefitting deployment models on all 30 cities ...", flush=True)
    for h in args.horizons:
        models[h] = new_model(rounds[h]).fit(matrix(df, h), target(df, h))

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    bundle = {
        "package_version": __version__,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features": list(features.FEATURES),
        "horizons_h": list(args.horizons),
        "target_mode": MODE,
        "models": models,
        "band_q80": q80,
        "versions": {"python": platform.python_version(), "sklearn": sklearn.__version__,
                     "numpy": np.__version__},
    }
    joblib.dump(bundle, MODEL_PATH, compress=3)
    report = {k: v for k, v in bundle.items() if k != "models"}
    report.update({"rows": {"train": len(train), "valid": len(valid), "test": len(test)},
                   "rounds": rounds, "loco_curves_every_10_rounds": curves,
                   "metrics": metrics, "importance_6h": importance})
    METRICS_PATH.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved {MODEL_PATH} ({MODEL_PATH.stat().st_size / 1e6:.2f} MB) and {METRICS_PATH}")


if __name__ == "__main__":
    main()
