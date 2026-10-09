"""On-device learning that runs continuously on the Pi.

ZoneAdapter
    Every forecast is checked when its horizon has passed. If no irrigation
    and no real rain happened in between AND no rain was forecast (a "clean",
    dry window), the zone learns
        observed_change ~= a * predicted_change + b
    with recursive least squares (forgetting factor), starting from a=1, b=0.
    A shaded indoor tray, for example, dries slower than the general model
    expects -> a drops below 1 within a day or two.
    Safety: the correction is used only once it has PROVEN to beat the raw
    model on that zone's own recent checks (scored before each update).
    It is learned and applied only for dry forecasts: forecast errors are a
    property of the weather, not of the zone, and the main model already
    learned them from millions of examples. (Found with replay_suite.py: a
    false-alarm rain forecast taught the zone a wrong correction.)

PumpResponse
    Learns how many %-points of moisture one second of pumping adds in that
    zone (measured after the water has soaked in), so the system can compute
    how long to run the pump.

Both serialise to plain dicts (JSON) so the server can persist them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

A_RANGE = (0.2, 3.0)
B_RANGE = (-3.0, 3.0)
DRY_FORECAST_MM = 0.5     # learn / correct only when forecast rain <= this


@dataclass
class _HorizonState:
    theta: list[float] = field(default_factory=lambda: [1.0, 0.0])
    P: list[list[float]] = field(default_factory=lambda: [[0.25, 0.0], [0.0, 0.5]])
    n: int = 0
    mae_raw: float | None = None      # EWMA of |raw error|, scored before update
    mae_adj: float | None = None      # EWMA of |adjusted error|
    mae_still: float | None = None    # EWMA of |observed change| (= "no change" guess)


class ZoneAdapter:
    # Checks arrive every 15 min and neighbours are nearly identical, so the
    # learner needs two days of evidence before it may switch on, must beat the
    # raw model by a clear margin (15 %), and remembers ~2 days (forgetting 0.995).
    # Tuned with replay_suite.py: faster settings switched on after one sunny
    # afternoon and made week-1 forecasts worse.
    def __init__(self, horizons=(1, 3, 6, 12), forgetting: float = 0.995,
                 min_updates: int = 192, ewma: float = 0.02, margin: float = 0.85):
        self.forgetting = forgetting
        self.min_updates = min_updates
        self.ewma = ewma
        self.margin = margin
        self.state = {int(h): _HorizonState() for h in horizons}

    # -- learning ------------------------------------------------------------------
    def update(self, h: int, predicted_delta: float, observed_delta: float,
               fc_rain_mm: float = 0.0) -> bool:
        """Call when a horizon-h forecast matures over a window with no irrigation
        and no real rain. `fc_rain_mm` = rain that was FORECAST for that window.
        Returns True if the sample was used."""
        if fc_rain_mm > DRY_FORECAST_MM:
            return False
        s = self.state[int(h)]
        x = np.array([predicted_delta, 1.0])
        theta, P = np.array(s.theta), np.array(s.P)

        # score first (prequential) so the comparison is honest
        adj = float(x @ theta)
        s.mae_raw = self._ewma(s.mae_raw, abs(predicted_delta - observed_delta))
        s.mae_adj = self._ewma(s.mae_adj, abs(adj - observed_delta))
        s.mae_still = self._ewma(s.mae_still, abs(observed_delta))

        lam = self.forgetting
        k = P @ x / (lam + x @ P @ x)
        theta = theta + k * (observed_delta - x @ theta)
        P = (P - np.outer(k, x) @ P) / lam
        # keep P bounded when the input is not exciting (avoids wind-up)
        tr = np.trace(P)
        if tr > 2.0:
            P *= 2.0 / tr
        theta[0] = np.clip(theta[0], *A_RANGE)
        theta[1] = np.clip(theta[1], *B_RANGE)
        s.theta, s.P = theta.tolist(), P.tolist()
        s.n += 1
        return True

    def _ewma(self, old, new):
        return new if old is None else (1 - self.ewma) * old + self.ewma * new

    # -- use -------------------------------------------------------------------------
    def active(self, h: int) -> bool:
        s = self.state[int(h)]
        return (s.n >= self.min_updates and s.mae_adj is not None
                and s.mae_adj < self.margin * s.mae_raw)

    def adjust(self, h: int, predicted_delta: float, fc_rain_mm: float = 0.0) -> float:
        if fc_rain_mm > DRY_FORECAST_MM or not self.active(h):
            return predicted_delta
        a, b = self.state[int(h)].theta
        return a * predicted_delta + b

    def stats(self, h: int) -> dict:
        """For the dashboard: how well the AI knows this zone."""
        s = self.state[int(h)]
        used = s.mae_adj if self.active(h) else s.mae_raw
        skill = None
        if used is not None and s.mae_still:
            skill = max(0.0, 1.0 - used / max(s.mae_still, 0.05))
        return {"checks": s.n, "correction_active": self.active(h),
                "a": round(s.theta[0], 3), "b": round(s.theta[1], 3),
                "avg_error_pct": None if used is None else round(used, 2),
                "skill_vs_no_change_pct": None if skill is None else round(100 * skill, 1)}

    # -- persistence -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"forgetting": self.forgetting, "min_updates": self.min_updates, "ewma": self.ewma,
                "margin": self.margin,
                "state": {str(h): vars(s) for h, s in self.state.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "ZoneAdapter":
        obj = cls(horizons=[int(h) for h in d["state"]], forgetting=d["forgetting"],
                  min_updates=d["min_updates"], ewma=d["ewma"], margin=d.get("margin", 0.85))
        obj.state = {int(h): _HorizonState(**s) for h, s in d["state"].items()}
        return obj


class PumpResponse:
    """%-points of soil moisture added per second of pumping, per zone."""

    def __init__(self, prior_gain: float = 0.5, ewma: float = 0.3,
                 gain_range=(0.01, 20.0)):
        self.gain = prior_gain
        self.ewma = ewma
        self.gain_range = gain_range
        self.n = 0

    def update(self, pump_seconds: float, rise_pct: float) -> bool:
        """Learn from one irrigation pulse. Returns False if the sample was
        unusable (too short, or no rise -> possible empty tank / blockage)."""
        if pump_seconds < 2 or rise_pct <= 0.2:
            return False
        g = float(np.clip(rise_pct / pump_seconds, *self.gain_range))
        self.gain = g if self.n == 0 else (1 - self.ewma) * self.gain + self.ewma * g
        self.n += 1
        return True

    def seconds_for(self, deficit_pct: float, max_s: float = 120.0) -> float:
        return float(np.clip(deficit_pct / self.gain, 0.0, max_s))

    def to_dict(self) -> dict:
        return {"gain": self.gain, "ewma": self.ewma, "gain_range": list(self.gain_range), "n": self.n}

    @classmethod
    def from_dict(cls, d: dict) -> "PumpResponse":
        obj = cls(prior_gain=d["gain"], ewma=d["ewma"], gain_range=tuple(d["gain_range"]))
        obj.n = d["n"]
        return obj
