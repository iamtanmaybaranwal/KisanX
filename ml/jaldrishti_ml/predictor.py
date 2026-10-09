"""Live inference on the Raspberry Pi.

    from jaldrishti_ml.predictor import MoisturePredictor, ZoneSetup, Forecast
    pred = MoisturePredictor()                       # loads models/moisture_model.joblib
    out = pred.predict(zone, readings, now, hours_since_water, forecast)

Times: readings use real epoch seconds (time.time()). `now` / forecast.start may be
timezone-aware (any zone) or naive; naive means India time (IST, UTC+5:30, no DST).
Features use the IST clock, matching the training data.
Uses exactly the same feature code as training (features.py).
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import joblib
import numpy as np
import sklearn

from . import config, features, weather

DEFAULT_MODEL = Path(__file__).resolve().parents[1] / "models" / "moisture_model.joblib"
STEP = timedelta(minutes=config.STEP_MIN)
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def to_ist(dt: datetime) -> datetime:
    """Aware datetime in IST (naive input is taken to already be IST)."""
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


@dataclass
class ZoneSetup:
    soil: str          # key of config.SOILS
    crop: str          # key of config.CROPS
    stage: str         # one of config.STAGES
    depth_mm: float    # root-zone / tray depth

    def __post_init__(self):
        if self.soil not in config.SOILS:
            raise ValueError(f"unknown soil {self.soil!r}")
        if self.crop not in config.CROPS:
            raise ValueError(f"unknown crop {self.crop!r}")
        if self.stage not in config.STAGES:
            raise ValueError(f"unknown stage {self.stage!r}")
        if not 10 <= self.depth_mm <= 2000:
            raise ValueError("depth_mm out of range")

    @property
    def thresholds(self) -> dict[str, float]:
        return config.thresholds(self.soil, self.crop)


@dataclass
class Forecast:
    """Hourly forecast; start = beginning of the first hour (local time)."""
    start: datetime
    rain_mm: Sequence[float]
    rain_prob: Sequence[float]
    et0_mm: Sequence[float]
    fetched_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        n = len(self.rain_mm)
        if not (n == len(self.rain_prob) == len(self.et0_mm)) or n == 0:
            raise ValueError("forecast arrays must be non-empty and equally long")


# datetime keeps only microseconds, so a reading stamped "now" can sit a hair after
# now.timestamp(); without this tolerance the newest reading was silently dropped.
TIME_EPS_S = 0.5


def _value_at(readings_t, readings_v, t_end: float, window_s: float) -> float:
    """Median of readings in (t_end - window_s, t_end]; NaN if none."""
    mask = (readings_t > t_end - window_s) & (readings_t <= t_end + TIME_EPS_S)
    return float(np.median(readings_v[mask])) if mask.any() else math.nan


class MoisturePredictor:
    def __init__(self, path: str | Path = DEFAULT_MODEL):
        bundle = joblib.load(path)
        if bundle["features"] != features.FEATURES:
            raise RuntimeError("model was trained with a different feature list - retrain")
        trained = bundle["versions"]["sklearn"]
        if trained != sklearn.__version__:
            warnings.warn(f"model trained with scikit-learn {trained}, running {sklearn.__version__}")
        self.bundle = bundle
        self.models = bundle["models"]
        self.horizons = [int(h) for h in bundle["horizons_h"]]
        self.band = {int(h): float(v) for h, v in bundle["band_q80"].items()}
        self.target_mode = bundle.get("target_mode", "delta")

    # -- forecast handling -----------------------------------------------------
    def _forecast_steps(self, fc: Forecast, now: datetime):
        """Expand to the 15-min grid, aligned to `now`; pad if the forecast is short."""
        offset = int((now - to_ist(fc.start)) // STEP)
        if offset < 0:
            raise ValueError("forecast starts in the future")
        need_hours = math.ceil((offset + max(self.horizons) * config.STEPS_PER_HOUR)
                               / config.STEPS_PER_HOUR)
        rain, prob, et0 = (list(map(float, a)) for a in (fc.rain_mm, fc.rain_prob, fc.et0_mm))
        padded = max(0, need_hours - len(rain))
        for i in range(len(rain), need_hours):
            # offline / stale forecast: assume no rain, repeat yesterday's ET pattern
            rain.append(0.0)
            prob.append(0.0)
            et0.append(et0[i - 24] if i >= 24 else float(np.mean(et0)))
        return (weather.expand_hourly_to_steps(np.array(rain), True),
                weather.expand_hourly_to_steps(np.array(prob), False),
                weather.expand_hourly_to_steps(np.array(et0), True),
                offset, padded)

    # -- main entry ------------------------------------------------------------
    def predict(self, zone: ZoneSetup, readings: Sequence[tuple[float, float]], now: datetime,
                hours_since_water: float, forecast: Forecast) -> dict:
        """readings: (epoch_seconds, moisture_pct) pairs, any order, any rate."""
        now = to_ist(now)
        arr = np.asarray(readings, dtype=float).reshape(-1, 2)
        t, v = arr[:, 0], arr[:, 1]
        # history windows are anchored on the real clock so "1 h ago" is exactly 1 h
        t_now = now.timestamp()
        win = STEP.total_seconds()
        m_now = _value_at(t, v, t_now, win)
        if math.isnan(m_now):
            m_now = _value_at(t, v, t_now, 2 * win)
        if math.isnan(m_now):
            raise ValueError("no sensor reading in the last 30 minutes")
        m_1h = _value_at(t, v, t_now - 3600, win)
        m_3h = _value_at(t, v, t_now - 3 * 3600, win)
        # forecast windows use the 15-min grid, as in training
        now = now.replace(second=0, microsecond=0)
        now = now - timedelta(minutes=now.minute % config.STEP_MIN)

        rain_s, prob_s, et0_s, offset, padded = self._forecast_steps(forecast, now)
        setup = config.SOILS[zone.soil]
        crop = config.CROPS[zone.crop]
        thr = zone.thresholds
        out = {"time": now.isoformat(), "m_now": round(m_now, 2), "forecast_padded_hours": padded,
               "history_ok": not (math.isnan(m_1h) or math.isnan(m_3h)), "horizons": []}
        nominal = features.nominal_params(setup.theta_wp, setup.theta_fc, setup.theta_sat,
                                          setup.drain_tau_h, zone.depth_mm, crop.kc(zone.stage), crop.p)
        phys = features.physics_deltas(nominal, m_now, et0_at=lambda k: et0_s[offset + k],
                                       rain_at=lambda k: rain_s[offset + k], horizons_h=self.horizons)
        for h in self.horizons:
            r, p, e = features.window_aggregates(rain_s, prob_s, et0_s, [offset],
                                                 h * config.STEPS_PER_HOUR)
            x = features.assemble(
                m_now=m_now, m_1h=m_1h, m_3h=m_3h,
                hours_since_water=hours_since_water, fc_pct=setup.fc_pct, wp_pct=setup.wp_pct,
                depth_mm=zone.depth_mm, kc=crop.kc(zone.stage), p=crop.p,
                fc_et0_mm=e, fc_rain_mm=r, fc_rain_prob=p, phys_delta=phys[h]).astype(np.float32)
            delta = float(self.models[h].predict(x)[0])
            if self.target_mode == "residual":          # model learned the physics error
                delta += float(phys[h])
            m = float(np.clip(m_now + delta, 0, 100))
            out["horizons"].append({
                "h": h, "delta": round(m - m_now, 3), "moisture": round(m, 2),
                "low": round(max(m - self.band[h], 0.0), 2),
                "high": round(min(m + self.band[h], 100.0), 2),
                "fc_rain_mm": round(float(r[0]), 2), "fc_rain_prob": round(float(p[0]), 1),
                "fc_et0_mm": round(float(e[0]), 3), "physics_delta": round(float(phys[h]), 3),
            })
        out["hours_until_min"] = hours_until(m_now, out["horizons"], thr["min_pct"])
        out["thresholds"] = thr
        return out


def hours_until(m_now: float, horizons: list[dict], level: float) -> float | None:
    """First time the forecast path crosses below `level` (linear between horizons).
    0 if already below, None if not within the longest horizon."""
    if m_now < level:
        return 0.0
    pts = [(0.0, m_now)] + [(float(h["h"]), h["moisture"]) for h in horizons]
    for (t0, m0), (t1, m1) in zip(pts, pts[1:]):
        if m1 < level <= m0:
            return round(t0 + (m0 - level) / (m0 - m1) * (t1 - t0), 2)
    return None
