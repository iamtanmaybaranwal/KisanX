"""End-to-end 'digital twin' replay - a dress rehearsal of the Pi.

A virtual zone is simulated with real weather from the UNSEEN city, the
sensor is read every minute (with noise), and every 15 minutes the code path
the server will use runs: MoisturePredictor -> ZoneAdapter, with forecasts
checked once they mature. Reports how accurate the live system is, before
and after on-device learning.

    python -m jaldrishti_ml.replay
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import timedelta

import numpy as np
import pandas as pd

from . import config, weather
from .adaptation import ZoneAdapter
from .predictor import IST, Forecast, MoisturePredictor, ZoneSetup
from .soil_model import ZoneParams, step

SCENARIOS = {
    # the hackathon table-top tray: potting mix, 10 cm deep, indoors (no rain, weak sun)
    "indoor_tray": dict(setup=ZoneSetup("potting_mix", "tomato", "mid", 100), exposure=0.35,
                        rain_capture=0.0, tau_mult=1.3, sensor_bias=1.5),
    # a real Indo-Gangetic field zone: alluvial silt loam, wheat, 30 cm roots, open sky
    "field_zone": dict(setup=ZoneSetup("silt_loam", "wheat", "mid", 300), exposure=1.0,
                       rain_capture=0.9, tau_mult=0.8, sensor_bias=-1.0),
}


def run(name: str, days: int = 21, start: str = "2024-03-01", seed: int = 0, city: str = "varanasi",
        predictor: MoisturePredictor | None = None, verbose: bool = True) -> dict:
    sc = SCENARIOS[name]
    setup: ZoneSetup = sc["setup"]
    rng = np.random.default_rng(seed)
    pred = predictor or MoisturePredictor()
    adapter = ZoneAdapter(horizons=pred.horizons)

    wx = weather.fetch_location(city)
    wx = wx.loc[start:].iloc[: (days + 2) * 24]
    fc_rain, fc_prob, fc_et0 = weather.simulate_forecast(wx["rain_mm"].to_numpy(), wx["et0_mm"].to_numpy(), rng)

    soil, crop = config.SOILS[setup.soil], config.CROPS[setup.crop]
    prm = ZoneParams(theta_wp=soil.theta_wp * 1.05, theta_fc=soil.theta_fc * 0.97, theta_sat=soil.theta_sat,
                     drain_tau_h=soil.drain_tau_h * sc["tau_mult"], depth_mm=setup.depth_mm * 1.05,
                     kc=crop.kc(setup.stage), p=crop.p, exposure=sc["exposure"],
                     rain_capture=sc["rain_capture"])
    thr = setup.thresholds
    theta = float(prm.from_pct(thr["target_pct"]))
    dt_min = 1.0
    readings: deque = deque(maxlen=4 * 60)            # last 4 h of 1-min readings
    pending, rows = [], []
    last_water_t = None
    water_events: list[float] = []                    # epoch of irrigation / real rain
    t0 = wx.index[0].to_pydatetime().replace(tzinfo=IST)

    for minute in range(days * 24 * 60):
        now = t0 + timedelta(minutes=minute)
        hour_idx = minute // 60
        w = wx.iloc[hour_idx]
        # --- physics, 1-minute step ------------------------------------------
        irr = 0.0
        sensor = float(prm.to_pct(theta)) + sc["sensor_bias"] + rng.normal(0, 0.8)
        sensor = float(np.clip(sensor, 0, 100))
        if minute % 15 == 0 and sensor < thr["min_pct"]:        # simple controller
            irr = float((prm.from_pct(thr["target_pct"]) - theta) * prm.depth_mm)
        theta = float(step(theta, prm, w.et0_mm / 60, w.rain_mm / 60, irr, dt_min / 60)[0])
        ts = now.timestamp()
        readings.append((ts, sensor))
        if irr > 0 or (minute % 60 == 0 and w.rain_mm >= config.WET_EVENT_RAIN_MM_PER_HOUR
                       and sc["rain_capture"] > 0):
            last_water_t = ts
            water_events.append(ts)

        if minute % 15 or minute < 3 * 60:
            continue
        # --- mature old forecasts ----------------------------------------------
        m_obs = float(np.median([v for t, v in readings if t > ts - 900]))
        keep = []
        for p in pending:
            if p["due"] > ts:
                keep.append(p)
                continue
            wet = any(p["issued"] <= e <= p["due"] for e in water_events)
            rain_in_window = wx.loc[p["issued_dt"].replace(tzinfo=None):now.replace(tzinfo=None), "rain_mm"]
            rainy = (rain_in_window >= config.WET_EVENT_RAIN_MM_PER_HOUR).any()
            obs_delta = m_obs - p["m_now"]
            if not wet and not rainy:
                rows.append({"h": p["h"], "day": (p["issued"] - t0.timestamp()) / 86400,
                             "err_raw": abs(p["raw"] - obs_delta), "err_used": abs(p["used"] - obs_delta),
                             "err_still": abs(obs_delta), "obs": obs_delta,
                             "fc_rain": p["fc_rain"], "active": p["active"], "raw": p["raw"]})
                adapter.update(p["h"], p["raw"], obs_delta, fc_rain_mm=p["fc_rain"])
        pending = keep
        # --- new forecast ------------------------------------------------------------
        fc = Forecast(start=now.replace(minute=0), rain_mm=fc_rain[hour_idx:hour_idx + 24],
                      rain_prob=fc_prob[hour_idx:hour_idx + 24], et0_mm=fc_et0[hour_idx:hour_idx + 24])
        hsw = 48.0 if last_water_t is None else (ts - last_water_t) / 3600
        out = pred.predict(setup, list(readings), now, hsw, fc)
        for hz in out["horizons"]:
            pending.append({"h": hz["h"], "issued": ts, "issued_dt": now, "due": ts + hz["h"] * 3600,
                            "m_now": out["m_now"], "raw": hz["delta"],
                            "used": adapter.adjust(hz["h"], hz["delta"], fc_rain_mm=hz["fc_rain_mm"]),
                            "fc_rain": hz["fc_rain_mm"], "active": adapter.active(hz["h"])})

    df = pd.DataFrame(rows)
    df["week"] = (df["day"] // 7).astype(int) + 1
    summary = df.groupby(["h", "week"])[["err_still", "err_raw", "err_used"]].mean().round(3)
    if verbose:
        print(f"\n### {name}: {days} days in {city} (unseen), MAE in %-points on clean windows")
        print("    err_still = 'no change' guess | err_raw = model | err_used = model + on-device learning")
        print(summary.to_string())
        for h in pred.horizons:
            print(f"  {h:>2}h  {adapter.stats(h)}")
    return {"summary": summary, "adapter": adapter, "rows": df}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    args = ap.parse_args()
    pred = MoisturePredictor()
    for name in SCENARIOS:
        run(name, days=args.days, predictor=pred)


if __name__ == "__main__":
    main()
