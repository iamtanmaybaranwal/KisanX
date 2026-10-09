"""Feature construction - the ONLY place features are defined.

Training (dataset.py) and live inference on the Pi (predictor.py) both call
`assemble()`, `window_aggregates()` and `physics_deltas()`, so there is no
train/serve skew.

Deliberately NOT inputs: raw temperature, humidity and time of day. They let
the model memorise each training city's climate and hurt accuracy in a new
city (tested); the evaporation forecast (ET0) already carries that physics.
"""
from __future__ import annotations

import numpy as np

from . import config
from .soil_model import ZoneParams, step

FEATURES = [
    "m_now",              # current soil moisture (%)
    "d_1h",               # change over the last hour (% points), NaN if unknown
    "d_3h",               # average change per hour over the last 3 h, NaN if unknown
    "rel_aw",             # relative available water: 0 = wilting point, 1 = field capacity
    "hours_since_water",  # hours since last irrigation or real rain (capped)
    "fc_pct",             # soil field capacity on the % scale
    "wp_pct",             # soil wilting point on the % scale
    "depth_mm",           # root-zone / tray depth
    "kc",                 # crop coefficient for the current growth stage
    "p",                  # crop depletion fraction
    "fc_et0_mm",          # forecast reference ET over the horizon
    "fc_rain_mm",         # forecast rain over the horizon
    "fc_rain_prob",       # max rain probability over the horizon (%)
    "phys_delta",         # FAO-56 physics forecast of the change (% points) -> hybrid model
]

PHYS_EXPOSURE = 1.0       # physics baseline assumes an open field ...
PHYS_RAIN_CAPTURE = 0.9   # ... and that 90 % of rain reaches the roots


def nominal_params(theta_wp, theta_fc, theta_sat, drain_tau_h, depth_mm, kc, p):
    """ZoneParams as the farmer configured them (no hidden truth)."""
    shape = np.broadcast(*map(np.asarray, (theta_wp, theta_fc, theta_sat, drain_tau_h, depth_mm, kc, p))).shape
    full = lambda x: np.broadcast_to(np.asarray(x, dtype=float), shape).copy()  # noqa: E731
    return ZoneParams(theta_wp=full(theta_wp), theta_fc=full(theta_fc), theta_sat=full(theta_sat),
                      drain_tau_h=full(drain_tau_h), depth_mm=full(depth_mm), kc=full(kc), p=full(p),
                      exposure=full(PHYS_EXPOSURE), rain_capture=full(PHYS_RAIN_CAPTURE))


def physics_deltas(nominal: ZoneParams, m_now, et0_at, rain_at, horizons_h) -> dict[int, np.ndarray]:
    """Run the FAO-56 bucket forward from the observed moisture with the FORECAST
    weather and no irrigation. et0_at(k) / rain_at(k) return step-k amounts (mm),
    broadcastable to m_now. Returns {h: predicted change in % points}."""
    m_now = np.asarray(m_now, dtype=float)
    theta = np.clip(nominal.from_pct(m_now), nominal.theta_ad, nominal.theta_sat)
    wanted = {h * config.STEPS_PER_HOUR: h for h in horizons_h}
    out = {}
    for k in range(max(wanted)):
        theta, *_ = step(theta, nominal, et0_at(k), rain_at(k), 0.0, 1.0 / config.STEPS_PER_HOUR)
        if k + 1 in wanted:
            out[wanted[k + 1]] = np.clip(nominal.to_pct(theta), 0, 100) - m_now
    return out


def window_aggregates(rain_step, prob_step, et0_step, starts, n_steps: int):
    """Forecast totals over [start, start + n_steps) on the STEP_MIN grid.

    rain_step / et0_step are per-step amounts (mm), prob_step per-step %.
    `starts` is an int array; every window must fit inside the arrays.
    """
    starts = np.asarray(starts, dtype=np.int64)
    if starts.size and (starts.min() < 0 or starts.max() + n_steps > len(rain_step)):
        raise ValueError("forecast window outside available data")
    c_rain = np.concatenate([[0.0], np.cumsum(rain_step)])
    c_et0 = np.concatenate([[0.0], np.cumsum(et0_step)])
    rain = c_rain[starts + n_steps] - c_rain[starts]
    et0 = c_et0[starts + n_steps] - c_et0[starts]
    prob = np.zeros(starts.shape, dtype=float)
    for k in range(n_steps):
        np.maximum(prob, prob_step[starts + k], out=prob)
    # cumsum differences can leave -1e-16 style noise
    return np.maximum(rain, 0.0), prob, np.maximum(et0, 0.0)


def assemble(*, m_now, m_1h, m_3h, hours_since_water, fc_pct, wp_pct, depth_mm, kc, p,
             fc_et0_mm, fc_rain_mm, fc_rain_prob, phys_delta) -> np.ndarray:
    """Build the (n, len(FEATURES)) matrix. Inputs are scalars or arrays."""
    m_now = np.atleast_1d(np.asarray(m_now, dtype=float))
    n = m_now.shape[0]

    def col(x):
        return np.broadcast_to(np.asarray(x, dtype=float), (n,))

    m_1h, m_3h = col(m_1h), col(m_3h)
    fc_pct, wp_pct = col(fc_pct), col(wp_pct)
    cols = {
        "m_now": m_now,
        "d_1h": m_now - m_1h,
        "d_3h": (m_now - m_3h) / config.HISTORY_H,
        "rel_aw": (m_now - wp_pct) / np.maximum(fc_pct - wp_pct, 1e-6),
        "hours_since_water": np.minimum(col(hours_since_water), config.HOURS_SINCE_WATER_CAP),
        "fc_pct": fc_pct,
        "wp_pct": wp_pct,
        "depth_mm": col(depth_mm),
        "kc": col(kc),
        "p": col(p),
        "fc_et0_mm": col(fc_et0_mm),
        "fc_rain_mm": col(fc_rain_mm),
        "fc_rain_prob": col(fc_rain_prob),
        "phys_delta": col(phys_delta),
    }
    return np.column_stack([cols[f] for f in FEATURES])
