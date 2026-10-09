"""Synthetic training data from physics + real weather.

For every location we simulate N virtual zones for two years on a 15-min grid.
Each zone gets random (hidden) properties the model must cope with:
    soil parameters off by +-10 % from the nominal soil type,
    unknown ET exposure (shaded / indoor tray vs open field),
    drainage speed, sensor offset, rain capture,
and a random irrigation habit (threshold controller, timer, ad-hoc, rain-fed)
so the soil visits wet, dry and in-between states.

Labels are COUNTERFACTUAL: from each sample time we re-run the physics with
NO irrigation for 12 h. The model therefore learns "where will this zone be
if we do nothing?" - exactly what the decision engine needs to ask.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, features, weather
from .soil_model import ZoneParams, step

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "dataset.pkl"
DT_H = 1.0 / config.STEPS_PER_HOUR
SOIL_NAMES = list(config.SOILS)
CROP_NAMES = list(config.CROPS)
N_FORECAST_GROUPS = 8
SAMPLE_EVERY_STEPS = 12            # one sample every 3 h per zone
MAX_H_STEPS = max(config.HORIZONS_H) * config.STEPS_PER_HOUR


def sample_zones(n: int, rng: np.random.Generator):
    """Random zones: what the farmer configures (nominal) + hidden truth."""
    soil_idx = rng.integers(0, len(SOIL_NAMES), n)
    crop_idx = rng.integers(0, len(CROP_NAMES), n)
    stage_idx = rng.integers(0, len(config.STAGES), n)
    soils = [config.SOILS[SOIL_NAMES[i]] for i in soil_idx]
    crops = [config.CROPS[CROP_NAMES[i]] for i in crop_idx]

    tray = rng.random(n) < 0.4                      # 40 % shallow trays / pots
    depth_nom = np.where(tray, rng.uniform(60, 200, n), rng.uniform(200, 600, n))
    exposure = np.where(tray, rng.uniform(0.2, 1.0, n), rng.uniform(0.6, 1.3, n))

    wp_nom = np.array([s.theta_wp for s in soils])
    fc_nom = np.array([s.theta_fc for s in soils])
    sat_nom = np.array([s.theta_sat for s in soils])
    tau_nom = np.array([s.drain_tau_h for s in soils])
    kc_nom = np.array([c.kc(config.STAGES[k]) for c, k in zip(crops, stage_idx)])
    p_nom = np.array([c.p for c in crops])

    jit = lambda: rng.uniform(0.9, 1.1, n)  # noqa: E731
    wp = wp_nom * jit()
    fc = np.maximum(fc_nom * jit(), wp + 0.04)
    sat = np.maximum(sat_nom * jit(), fc + 0.08)
    actual = ZoneParams(
        theta_wp=wp, theta_fc=fc, theta_sat=sat,
        drain_tau_h=tau_nom * rng.lognormal(0, 0.4, n),
        depth_mm=depth_nom * jit(),
        kc=kc_nom * jit(), p=p_nom,
        exposure=exposure,
        rain_capture=rng.uniform(0.8, 1.0, n),
    )
    nominal = features.nominal_params(wp_nom, fc_nom, sat_nom, tau_nom, depth_nom, kc_nom, p_nom)
    meta = pd.DataFrame({
        "soil": [SOIL_NAMES[i] for i in soil_idx],
        "crop": [CROP_NAMES[i] for i in crop_idx],
        "stage": [config.STAGES[k] for k in stage_idx],
        "fc_pct": [s.fc_pct for s in soils],
        "wp_pct": [s.wp_pct for s in soils],
        "depth_mm": depth_nom, "kc": kc_nom, "p": p_nom,
        "sensor_bias": rng.normal(0, 2.0, n),
        "policy": rng.choice(4, n, p=[0.45, 0.25, 0.2, 0.1]),  # thr, timer, ad-hoc, rain-fed
        "fgroup": np.arange(n) % N_FORECAST_GROUPS,
    })
    meta["min_pct"] = meta["fc_pct"] - meta["p"] * (meta["fc_pct"] - meta["wp_pct"])
    return actual, nominal, meta


def simulate_location(name: str, wx: pd.DataFrame, n_zones: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    actual, nominal, meta = sample_zones(n_zones, rng)
    n = n_zones

    # ---- weather on the 15-min grid -----------------------------------------
    rain_s = weather.expand_hourly_to_steps(wx["rain_mm"].to_numpy(), True)
    et0_s = weather.expand_hourly_to_steps(wx["et0_mm"].to_numpy(), True)
    temp_s = weather.expand_hourly_to_steps(wx["temp_c"].to_numpy(), False)
    rh_s = weather.expand_hourly_to_steps(wx["rh_pct"].to_numpy(), False)
    wet_s = weather.expand_hourly_to_steps(
        (wx["rain_mm"].to_numpy() >= config.WET_EVENT_RAIN_MM_PER_HOUR).astype(float), False) > 0
    times = pd.date_range(wx.index[0], periods=len(rain_s), freq=f"{config.STEP_MIN}min")
    hour_s = (times.hour + times.minute / 60.0).to_numpy()
    S = len(rain_s)

    # imperfect forecasts, one per forecast group
    fc = [weather.simulate_forecast(wx["rain_mm"].to_numpy(), wx["et0_mm"].to_numpy(), rng)
          for _ in range(N_FORECAST_GROUPS)]
    fc_rain_s = np.stack([weather.expand_hourly_to_steps(f[0], True) for f in fc])
    fc_prob_s = np.stack([weather.expand_hourly_to_steps(f[1], False) for f in fc])
    fc_et0_s = np.stack([weather.expand_hourly_to_steps(f[2], True) for f in fc])

    # ---- main trajectory with the zone's irrigation habit -----------------------
    bias = meta["sensor_bias"].to_numpy()
    policy = meta["policy"].to_numpy()
    fc_pct_nom, wp_pct_nom = meta["fc_pct"].to_numpy(), meta["wp_pct"].to_numpy()
    thr_pct = wp_pct_nom + rng.uniform(0.2, 0.8, n) * (fc_pct_nom - wp_pct_nom)
    refill_theta = actual.theta_fc * rng.uniform(0.9, 1.08, n)
    taw_mm = (actual.theta_fc - actual.theta_wp) * actual.depth_mm
    timer_every = rng.integers(1, 4, n) * 24 * config.STEPS_PER_HOUR
    timer_phase = rng.integers(5, 20, n) * config.STEPS_PER_HOUR
    fixed_mm = rng.uniform(0.15, 0.6, n) * taw_mm

    theta = actual.from_pct(rng.uniform(25, 75, n))
    theta = np.clip(theta, actual.theta_ad, actual.theta_sat)
    hsw = rng.uniform(0, 24, n)
    cooldown = np.zeros(n)
    theta_hist = np.empty((S, n), dtype=np.float32)
    hsw_hist = np.empty((S, n), dtype=np.float32)
    obs_noise = rng.normal(0, 0.3, (S, n)).astype(np.float32)

    for s in range(S):
        theta_hist[s] = theta
        hsw_hist[s] = hsw
        obs = actual.to_pct(theta) + bias + obs_noise[s]
        irr = np.zeros(n)
        want_thr = (policy == 0) & (obs < thr_pct) & (cooldown <= 0)
        irr = np.where(want_thr, np.maximum(refill_theta - theta, 0) * actual.depth_mm
                       * rng.lognormal(0, 0.2, n), irr)
        want_timer = (policy == 1) & ((s - timer_phase) % timer_every == 0)
        irr = np.where(want_timer, fixed_mm, irr)
        want_adhoc = (policy == 2) & (rng.random(n) < 0.35 / (24 * config.STEPS_PER_HOUR))
        irr = np.where(want_adhoc, fixed_mm * rng.uniform(0.5, 1.5, n), irr)
        cooldown = np.where(irr > 0, 6.0, cooldown - DT_H)
        theta, *_ = step(theta, actual, et0_s[s], rain_s[s], irr, DT_H)
        hsw = np.where((irr > 0) | wet_s[s], 0.0, hsw + DT_H)

    # ---- sample times ----------------------------------------------------------
    hist = config.HISTORY_H * config.STEPS_PER_HOUR
    idx = np.arange(hist, S - MAX_H_STEPS, SAMPLE_EVERY_STEPS)
    ns = len(idx)

    def observe(theta_block, rows):
        pct = actual.reshape((1, n)).to_pct(theta_block.astype(float)) + bias[None, :]
        return np.clip(pct + obs_noise[rows], 0, 100)

    m_now = observe(theta_hist[idx], idx)
    m_1h = observe(theta_hist[idx - config.STEPS_PER_HOUR], idx - config.STEPS_PER_HOUR)
    m_3h = observe(theta_hist[idx - hist], idx - hist)

    # ---- counterfactual rollouts (no irrigation), all samples at once -----------
    prm = actual.reshape((n, 1))
    th = theta_hist[idx].T.astype(float)                    # (n, ns)
    zeros = np.zeros((1, ns))
    labels = {}
    for k in range(MAX_H_STEPS):                            # truth: real weather, hidden params
        t = idx + k
        th, *_ = step(th, prm, et0_s[t][None, :], rain_s[t][None, :], zeros, DT_H)
        h_done = (k + 1) / config.STEPS_PER_HOUR
        if h_done in config.HORIZONS_H:
            future = np.clip(prm.to_pct(th) + bias[:, None], 0, 100)    # sensor, noise-free
            labels[int(h_done)] = future - m_now.T
    # physics forecast: nominal settings + FORECAST weather (same code as on the Pi)
    grp = meta["fgroup"].to_numpy()
    g_col = grp[:, None]
    phys = features.physics_deltas(
        nominal.reshape((n, 1)), m_now.T,
        et0_at=lambda k: fc_et0_s[g_col, (idx + k)[None, :]],
        rain_at=lambda k: fc_rain_s[g_col, (idx + k)[None, :]],
        horizons_h=config.HORIZONS_H)

    # ---- assemble rows -------------------------------------------------------------
    rows = {
        "location": name,
        "zone": np.repeat(np.arange(n), ns),
        "time": np.tile(times[idx].to_numpy(), n),
        "m_now": m_now.T.ravel(),
        "m_1h": m_1h.T.ravel(),
        "m_3h": m_3h.T.ravel(),
        "temp_c": np.tile(temp_s[idx], n) + rng.normal(0, 1.0, n * ns),
        "rh_pct": np.clip(np.tile(rh_s[idx], n) + rng.normal(0, 4.0, n * ns), 0, 100),
        "hours_since_water": hsw_hist[idx].T.ravel(),
        "hour": np.tile(hour_s[idx], n),
    }
    for col in ("fc_pct", "wp_pct", "depth_mm", "kc", "p", "min_pct", "soil", "crop", "policy"):
        rows[col] = np.repeat(meta[col].to_numpy(), ns)
    for h in config.HORIZONS_H:
        steps_h = h * config.STEPS_PER_HOUR
        fr, fp, fe = (np.empty((n, ns)) for _ in range(3))
        for g in range(N_FORECAST_GROUPS):
            r, p_, e = features.window_aggregates(fc_rain_s[g], fc_prob_s[g], fc_et0_s[g], idx, steps_h)
            fr[grp == g], fp[grp == g], fe[grp == g] = r, p_, e
        rows[f"fc_rain_mm_{h}"] = fr.ravel()
        rows[f"fc_rain_prob_{h}"] = fp.ravel()
        rows[f"fc_et0_mm_{h}"] = fe.ravel()
        rows[f"y_{h}"] = labels[h].ravel()
        rows[f"phys_{h}"] = phys[h].ravel()
    df = pd.DataFrame(rows)

    # sensors that just came online: no history yet (model must cope)
    miss_1h = rng.random(len(df)) < 0.04
    miss_3h = miss_1h | (rng.random(len(df)) < 0.04)
    df.loc[miss_1h, "m_1h"] = np.nan
    df.loc[miss_3h, "m_3h"] = np.nan
    return df


def build(n_zones: int = 40, keep_frac: float = 0.3, seed: int = 7, offline: bool = False) -> pd.DataFrame:
    frames = []
    for i, name in enumerate(config.LOCATIONS):
        t0 = time.time()
        rng = np.random.default_rng(seed + 1000 + i)
        hours = int((pd.Timestamp(config.WEATHER_END) - pd.Timestamp(config.WEATHER_START)).days + 1) * 24
        if offline:
            wx = weather.synthetic_weather(hours, rng)
        else:
            try:
                wx = weather.fetch_location(name)
            except Exception as exc:  # network down -> still produce data
                print(f"  ! {name}: archive unavailable ({exc}); using synthetic weather")
                wx = weather.synthetic_weather(hours, rng)
        df = simulate_location(name, wx, n_zones, seed + i)
        if keep_frac < 1:
            df = df.sample(frac=keep_frac, random_state=seed + i)
        frames.append(df)
        print(f"  {name:12s} {config.STATE[name]:14s} {len(df):>8,d} samples  ({time.time() - t0:.1f}s)", flush=True)
    out = pd.concat(frames, ignore_index=True)
    out["time"] = pd.to_datetime(out["time"])
    out["state"] = out["location"].map(config.STATE)
    valid_from = pd.Timestamp(config.VALID_FROM)
    gap = pd.Timedelta(hours=max(config.HORIZONS_H))
    out["split"] = np.where(out["location"].isin(config.TEST_LOCATIONS), "test",
                            np.where(out["time"] >= valid_from, "valid", "train"))
    # drop train samples whose label window leaks into the validation period
    leak = (out["split"] == "train") & (out["time"] > valid_from - gap)
    return out[~leak].reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description="Generate the Jal-Drishti training dataset")
    ap.add_argument("--zones", type=int, default=40, help="virtual zones per location")
    ap.add_argument("--keep", type=float, default=0.3, help="fraction of samples kept")
    ap.add_argument("--offline", action="store_true", help="use synthetic weather only")
    args = ap.parse_args()
    print("Generating dataset ...")
    df = build(args.zones, args.keep, offline=args.offline)
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(DATA_PATH)
    print(df["split"].value_counts().to_string())
    print(f"Saved {len(df):,d} rows -> {DATA_PATH}")


if __name__ == "__main__":
    main()
