"""Vectorised root-zone water balance (FAO-56 style, sub-hourly).

One call to `step` advances any number of zones by one time step.
All per-zone parameters are numpy arrays (or scalars) that broadcast
against `theta`, so the same code simulates 1 zone or 100 000 zones.

Order inside a step:
    1. water input (rain * capture + irrigation), excess above saturation = runoff
    2. drainage of water above field capacity (exponential, tau = drain_tau_h)
    3. crop evapotranspiration  ETc = exposure * ET0 * (Kc * Ks + Ke_dry)
       Ks  : FAO-56 water-stress coefficient (1 until RAW is used up)
       Ke_dry: small bare-soil evaporation term so shallow trays keep drying
               below wilting point down to air-dry, as real pots do
"""
from __future__ import annotations

from dataclasses import dataclass, fields

import numpy as np

KE_DRY = 0.05


@dataclass
class ZoneParams:
    theta_wp: np.ndarray
    theta_fc: np.ndarray
    theta_sat: np.ndarray
    drain_tau_h: np.ndarray
    depth_mm: np.ndarray        # effective root-zone / tray depth
    kc: np.ndarray
    p: np.ndarray
    exposure: np.ndarray        # ET multiplier (indoor tray ~0.3, open field ~1)
    rain_capture: np.ndarray    # fraction of rain reaching the root zone

    @property
    def theta_ad(self) -> np.ndarray:
        return 0.5 * self.theta_wp

    def to_pct(self, theta):
        return 100.0 * (theta - self.theta_ad) / (self.theta_sat - self.theta_ad)

    def from_pct(self, pct):
        return self.theta_ad + np.asarray(pct) / 100.0 * (self.theta_sat - self.theta_ad)

    def reshape(self, shape) -> "ZoneParams":
        """Return a copy with every field reshaped (e.g. (N,) -> (N, 1))."""
        return ZoneParams(**{f.name: np.asarray(getattr(self, f.name), dtype=float).reshape(shape)
                             for f in fields(self)})


def step(theta, prm: ZoneParams, et0_mm, rain_mm, irr_mm, dt_h: float):
    """Advance soil water content `theta` (m3/m3) by one step of `dt_h` hours.

    et0_mm, rain_mm, irr_mm are the amounts falling inside this step (mm).
    Returns (new_theta, et_mm, drain_mm, runoff_mm).
    """
    depth = prm.depth_mm
    # 1. inputs
    theta = theta + (rain_mm * prm.rain_capture + irr_mm) / depth
    runoff_mm = np.maximum(theta - prm.theta_sat, 0.0) * depth
    theta = np.minimum(theta, prm.theta_sat)

    # 2. drainage above field capacity
    excess = np.maximum(theta - prm.theta_fc, 0.0)
    drain = excess * (1.0 - np.exp(-dt_h / prm.drain_tau_h))
    theta = theta - drain

    # 3. evapotranspiration with FAO-56 stress coefficient
    taw = prm.theta_fc - prm.theta_wp
    depletion = np.maximum(prm.theta_fc - theta, 0.0)
    raw = prm.p * taw
    ks = np.where(depletion <= raw, 1.0,
                  np.clip((taw - depletion) / ((1.0 - prm.p) * taw), 0.0, 1.0))
    ke = KE_DRY * np.clip((theta - prm.theta_ad) / (prm.theta_fc - prm.theta_ad), 0.0, 1.0)
    et_mm = prm.exposure * et0_mm * (prm.kc * ks + ke)
    theta_new = np.maximum(theta - et_mm / depth, prm.theta_ad)
    et_mm = (theta - theta_new) * depth   # actual ET after the air-dry floor

    return theta_new, et_mm, drain * depth, runoff_mm
