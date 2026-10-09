"""Weather data for training: real hourly history + simulated forecasts.

* Real history comes from the free Open-Meteo archive (ERA5-based) and is
  cached under ml/data/weather/ so training works offline afterwards.
* Forecasts are simulated from the real future by adding realistic errors
  (timing blur, amount error, missed storms, false alarms), so the model
  learns to treat a forecast as uncertain - just like the live API.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "weather"
HOURLY_VARS = ("temperature_2m", "relative_humidity_2m", "precipitation", "et0_fao_evapotranspiration")
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def fetch_location(name: str, start: str = config.WEATHER_START, end: str = config.WEATHER_END,
                   refresh: bool = False) -> pd.DataFrame:
    """Hourly weather for a named location (cached CSV). Local time (IST)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}_{start}_{end}.csv.gz"
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["time"])
    else:
        lat, lon = config.LOCATIONS[name]
        query = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
            "hourly": ",".join(HOURLY_VARS), "timezone": config.TIMEZONE,
        })
        payload = None
        for attempt in range(4):                       # the free API sometimes resets
            try:
                with urllib.request.urlopen(f"{ARCHIVE_URL}?{query}", timeout=60) as resp:
                    payload = json.load(resp)
                break
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        df = pd.DataFrame(payload["hourly"])
        df["time"] = pd.to_datetime(df["time"])
        df.to_csv(path, index=False, compression="gzip")
    return clean(df)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Fill the occasional gap and enforce a continuous hourly index."""
    df = df.rename(columns={
        "temperature_2m": "temp_c", "relative_humidity_2m": "rh_pct",
        "precipitation": "rain_mm", "et0_fao_evapotranspiration": "et0_mm",
    })
    df = df.set_index("time").sort_index()
    df = df[~df.index.duplicated()]
    full = pd.date_range(df.index[0], df.index[-1], freq="h")
    df = df.reindex(full)
    df[["temp_c", "rh_pct"]] = df[["temp_c", "rh_pct"]].interpolate(limit_direction="both")
    df[["rain_mm", "et0_mm"]] = df[["rain_mm", "et0_mm"]].fillna(0.0).clip(lower=0.0)
    df.index.name = "time"
    return df


def synthetic_weather(hours: int, rng: np.random.Generator, start: str = config.WEATHER_START) -> pd.DataFrame:
    """Offline fallback: plausible Indian weather if the archive is unreachable."""
    t = np.arange(hours)
    hod = t % 24
    doy = (t // 24) % 365
    season = np.sin(2 * np.pi * (doy - 80) / 365)          # peak ~ June
    temp = 27 + 6 * season + 6 * np.sin(2 * np.pi * (hod - 9) / 24) + rng.normal(0, 1.0, hours)
    rh = np.clip(60 - 2.0 * (temp - 27) + 15 * (season > 0.4) + rng.normal(0, 5, hours), 10, 100)
    sun = np.clip(np.sin(np.pi * (hod - 6) / 12), 0, None)
    et0 = np.clip(0.55 * sun * (1 + 0.3 * season) * (1 - rh / 200), 0, None)
    monsoon = (doy > 160) & (doy < 270)
    storm = rng.random(hours) < np.where(monsoon, 0.04, 0.004)
    rain = np.convolve(storm * rng.exponential(4.0, hours), np.ones(3) / 3, mode="same")
    return pd.DataFrame({"temp_c": temp, "rh_pct": rh, "rain_mm": rain, "et0_mm": et0},
                        index=pd.date_range(start, periods=hours, freq="h", name="time"))


def simulate_forecast(rain_h: np.ndarray, et0_h: np.ndarray, rng: np.random.Generator):
    """Turn real hourly weather into an imperfect hourly forecast.

    Returns (fc_rain_mm, fc_rain_prob_pct, fc_et0_mm), all hourly.
    """
    n = len(rain_h)
    # timing blur: forecasts smear storms over neighbouring hours
    kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
    fc_rain = np.convolve(rain_h, kernel, mode="same")
    # amount error, correlated over ~6 h blocks
    block = np.repeat(rng.lognormal(0.0, 0.5, n // 6 + 1), 6)[:n]
    fc_rain = fc_rain * block
    # missed storms (15 % of 12 h blocks) and false alarms (6 % of blocks)
    nb = n // 12 + 1
    missed = np.repeat(rng.random(nb) < 0.15, 12)[:n]
    fc_rain = np.where(missed, 0.0, fc_rain)
    false_alarm = np.repeat(rng.random(nb) < 0.06, 12)[:n]
    fake = rng.exponential(0.8, n) * (rng.random(n) < 0.35)
    fc_rain = np.where(false_alarm & (fc_rain < 0.05), fake, fc_rain)
    fc_rain = np.where(fc_rain < 0.05, 0.0, fc_rain)

    prob = 100.0 * (1.0 - np.exp(-fc_rain / 0.5)) + rng.normal(0, 8, n)
    prob = np.where(fc_rain > 0, np.clip(prob, 20, 100), rng.uniform(0, 15, n))

    fc_et0 = np.clip(et0_h * (1.0 + rng.normal(0, 0.12, n)), 0, None)
    return fc_rain, np.clip(prob, 0, 100), fc_et0


def expand_hourly_to_steps(hourly: np.ndarray, per_hour_amount: bool) -> np.ndarray:
    """Hourly series -> STEP_MIN series. Amounts (mm) are split evenly,
    states (temp, rh, prob) are repeated. Shared by training and inference."""
    hourly = np.asarray(hourly, dtype=float)
    out = np.repeat(hourly, config.STEPS_PER_HOUR, axis=-1)
    return out / config.STEPS_PER_HOUR if per_hour_amount else out
