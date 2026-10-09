"""Agronomy constants and shared settings for Jal-Drishti.

Moisture scale used everywhere (sensor, model, dashboard):
    0 %   = air-dry soil      (theta_ad = 0.5 * theta_wp, FAO-56 convention)
    100 % = saturated soil    (theta_sat)
This is exactly what the two-point sensor calibration produces
("Dry" = air-dried soil, "Wet" = freshly soaked soil), so the physics
model, the ML model and the live sensors all speak the same units.

Sources
  Soils   FAO-56 Table 19 (mid-range FC / WP), saturation from typical porosity.
          Maharashtra black cotton soil (Vertisol): measured FC 33-41 %, PWP 13-16 %
          (Water retention of swell-shrink soils of Maharashtra, krishikosh.egranth.ac.in).
          Jharkhand red & lateritic soil: gravelly sandy loam - sandy clay loam, low
          available water (Indian soil-survey literature).
  Crops   Kc: FAO-56 Table 12. Depletion fraction p: FAO-56 Table 22
          (fao.org/4/x0490e), adjusted for the day's crop ET with FAO-56 eq. 83.
Target regions: Punjab, Haryana, Delhi, Uttar Pradesh, Bihar, Jharkhand, Maharashtra.
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- time grid -------------------------------------------------------------
STEP_MIN = 15                      # simulation / feature grid resolution
STEPS_PER_HOUR = 60 // STEP_MIN    # 4
HORIZONS_H = (1, 3, 6, 12)         # prediction horizons (hours ahead)
HISTORY_H = 3                      # history needed for slope features

# A rain hour counts as a "water event" (resets hours_since_water)
WET_EVENT_RAIN_MM_PER_HOUR = 0.4
HOURS_SINCE_WATER_CAP = 48.0

TIMEZONE = "Asia/Kolkata"


@dataclass(frozen=True)
class SoilType:
    theta_wp: float      # wilting point (m3/m3)
    theta_fc: float      # field capacity (m3/m3)
    theta_sat: float     # saturation (m3/m3)
    drain_tau_h: float   # e-folding time of drainage above field capacity (h)

    @property
    def theta_ad(self) -> float:
        """Air-dry water content (FAO-56: ~0.5 * wilting point)."""
        return 0.5 * self.theta_wp

    def to_pct(self, theta):
        return 100.0 * (theta - self.theta_ad) / (self.theta_sat - self.theta_ad)

    def from_pct(self, pct):
        return self.theta_ad + pct / 100.0 * (self.theta_sat - self.theta_ad)

    @property
    def fc_pct(self) -> float:
        return float(self.to_pct(self.theta_fc))

    @property
    def wp_pct(self) -> float:
        return float(self.to_pct(self.theta_wp))


SOILS: dict[str, SoilType] = {
    "sand":        SoilType(theta_wp=0.05, theta_fc=0.12, theta_sat=0.40, drain_tau_h=2.0),
    "sandy_loam":  SoilType(theta_wp=0.11, theta_fc=0.23, theta_sat=0.43, drain_tau_h=4.0),
    "loam":        SoilType(theta_wp=0.12, theta_fc=0.25, theta_sat=0.45, drain_tau_h=8.0),
    "silt_loam":   SoilType(theta_wp=0.15, theta_fc=0.29, theta_sat=0.47, drain_tau_h=12.0),
    "clay_loam":   SoilType(theta_wp=0.20, theta_fc=0.34, theta_sat=0.48, drain_tau_h=16.0),  # Bihar low-lands
    "clay":        SoilType(theta_wp=0.22, theta_fc=0.36, theta_sat=0.50, drain_tau_h=24.0),
    # Jharkhand plateau: red & lateritic, sandy loam - sandy clay loam, low water holding
    "red_lateritic": SoilType(theta_wp=0.09, theta_fc=0.20, theta_sat=0.40, drain_tau_h=4.0),
    # Maharashtra Deccan: black cotton soil (Vertisol), high clay, very slow drainage
    "black_cotton":  SoilType(theta_wp=0.15, theta_fc=0.37, theta_sat=0.52, drain_tau_h=30.0),
    # Bagged garden / potting soil used in the table-top demo farm
    "potting_mix": SoilType(theta_wp=0.12, theta_fc=0.40, theta_sat=0.60, drain_tau_h=3.0),
}


@dataclass(frozen=True)
class Crop:
    label: str
    kc_ini: float
    kc_mid: float
    kc_end: float
    p: float             # fraction of available water that can be used before stress

    def kc(self, stage: str) -> float:
        return {
            "initial": self.kc_ini,
            "development": 0.5 * (self.kc_ini + self.kc_mid),
            "mid": self.kc_mid,
            "late": self.kc_end,
        }[stage]


CROPS: dict[str, Crop] = {
    "tomato":  Crop("Tomato",  0.60, 1.15, 0.80, 0.40),
    "wheat":   Crop("Wheat",   0.30, 1.15, 0.30, 0.55),
    "potato":  Crop("Potato",  0.50, 1.15, 0.75, 0.35),
    "onion":   Crop("Onion",   0.70, 1.05, 0.75, 0.30),
    "chilli":  Crop("Chilli",  0.60, 1.05, 0.90, 0.30),
    "maize":   Crop("Maize",   0.30, 1.20, 0.35, 0.55),
    "mint":    Crop("Mint",    0.60, 1.15, 1.10, 0.40),
    "spinach": Crop("Spinach", 0.70, 1.00, 0.95, 0.20),
    # major Indo-Gangetic crops suited to drip / zone irrigation
    "sugarcane": Crop("Sugarcane", 0.40, 1.25, 0.75, 0.65),
    "mustard":   Crop("Mustard",   0.35, 1.10, 0.35, 0.60),   # FAO-56 rapeseed values
    "cotton":    Crop("Cotton",    0.35, 1.18, 0.60, 0.65),
    "chickpea":  Crop("Chickpea",  0.40, 1.00, 0.35, 0.50),
    # vegetables, pulses, oilseeds, millets and fruit of UP / Bihar / Jharkhand / Maharashtra
    "cabbage":     Crop("Cabbage",     0.70, 1.05, 0.95, 0.45),
    "cauliflower": Crop("Cauliflower", 0.70, 1.05, 0.95, 0.45),
    "brinjal":     Crop("Brinjal",     0.60, 1.05, 0.90, 0.45),   # FAO-56 egg plant
    "peas":        Crop("Peas",        0.40, 1.15, 0.30, 0.40),
    "lentil":      Crop("Lentil",      0.40, 1.10, 0.30, 0.50),
    "soybean":     Crop("Soybean",     0.40, 1.15, 0.50, 0.50),
    "groundnut":   Crop("Groundnut",   0.40, 1.15, 0.60, 0.50),
    "sorghum":     Crop("Sorghum (jowar)",      0.30, 1.05, 0.55, 0.55),
    "millet":      Crop("Pearl millet (bajra)", 0.30, 1.00, 0.30, 0.55),
    "banana":      Crop("Banana",      0.50, 1.10, 1.00, 0.35),
    "grapes":      Crop("Grapes",      0.30, 0.78, 0.45, 0.45),
    # Paddy is not included: it is grown flooded/puddled, not under drip control.
    # Okra and pigeon pea are not in the FAO-56 tables, so they are not guessed here.
}

SOIL_LABELS: dict[str, str] = {
    "sand": "Sandy (south-west Punjab & Haryana)",
    "sandy_loam": "Sandy loam (Punjab, Haryana)",
    "loam": "Loam (UP, Punjab, Haryana, Delhi)",
    "silt_loam": "Silt loam (UP, Bihar)",
    "clay_loam": "Clay loam (Bihar low-lands)",
    "clay": "Clay",
    "red_lateritic": "Red & lateritic (Jharkhand)",
    "black_cotton": "Black cotton soil (Maharashtra)",
    "potting_mix": "Potting mix (pots / demo tray)",
}

# Typical soil and major drip-suitable crops per state (for the dashboard's region picker)
REGIONS: dict[str, dict] = {
    "Punjab":        {"soil": "sandy_loam", "crops": ["wheat", "maize", "cotton", "potato", "mustard", "sugarcane"]},
    "Haryana":       {"soil": "sandy_loam", "crops": ["wheat", "mustard", "cotton", "millet", "chickpea", "sugarcane"]},
    "Delhi":         {"soil": "loam", "crops": ["wheat", "cauliflower", "cabbage", "tomato", "spinach", "mustard"]},
    "Uttar Pradesh": {"soil": "loam", "crops": ["wheat", "sugarcane", "potato", "mustard", "lentil", "peas"]},
    "Bihar":         {"soil": "silt_loam", "crops": ["wheat", "maize", "lentil", "potato", "onion", "cauliflower"]},
    "Jharkhand":     {"soil": "red_lateritic", "crops": ["tomato", "cabbage", "cauliflower", "brinjal", "maize", "groundnut"]},
    "Maharashtra":   {"soil": "black_cotton", "crops": ["cotton", "sugarcane", "soybean", "onion", "sorghum", "grapes"]},
}

STAGES = ("initial", "development", "mid", "late")


def depletion_fraction(crop_name: str, etc_mm_day: float | None = None) -> float:
    """FAO-56 p, adjusted for the day's crop ET (eq. 83): hot, dry days -> water sooner."""
    p = CROPS[crop_name].p
    if etc_mm_day is not None:
        p = p + 0.04 * (5.0 - etc_mm_day)
    return min(max(p, 0.10), 0.80)


def thresholds(soil_name: str, crop_name: str, etc_mm_day: float | None = None) -> dict[str, float]:
    """The ideal moisture range on the 0-100 % sensor scale.

    min_pct    : readily-available water used up (FAO-56 RAW) -> start irrigating
    target_pct : refill point, slightly below field capacity so the water is held
                 by the soil instead of draining away
    The ideal range is min_pct .. target_pct.
    """
    soil = SOILS[soil_name]
    fc, wp = soil.fc_pct, soil.wp_pct
    taw = fc - wp
    p = depletion_fraction(crop_name, etc_mm_day)
    return {
        "fc_pct": round(fc, 2),
        "wp_pct": round(wp, 2),
        "min_pct": round(fc - p * taw, 2),
        "target_pct": round(fc - 0.05 * taw, 2),   # > min_pct even at FAO's minimum p = 0.10
        "p": round(p, 3),
    }


# ---- weather locations: Punjab, Haryana, Delhi, UP, Bihar, Jharkhand, Maharashtra ----
# Training cities span each state's agro-climatic zones; one extra city per state is
# HELD OUT and used only for the final test ("accuracy in a city the model never saw").
LOCATIONS: dict[str, tuple[float, float]] = {
    # Punjab
    "ludhiana":    (30.90, 75.85),   # central Punjab, wheat-rice belt
    "bathinda":    (30.21, 74.95),   # south-west, semi-arid, cotton belt
    "patiala":     (30.34, 76.39),   # east Punjab
    # Haryana
    "hisar":       (29.15, 75.72),   # semi-arid, very hot summers
    "karnal":      (29.69, 76.99),   # wheat-rice, near Yamuna
    "sirsa":       (29.53, 75.03),   # arid fringe, sandy soils
    # Delhi (NCT)
    "new_delhi":   (28.61, 77.21),   # central Delhi, semi-arid, extreme summers
    # Uttar Pradesh
    "meerut":      (28.98, 77.71),   # western UP, sugarcane belt
    "agra":        (27.18, 78.01),   # south-west, hot semi-arid
    "lucknow":     (26.85, 80.95),   # central UP
    "gorakhpur":   (26.76, 83.37),   # eastern UP, humid
    "jhansi":      (25.45, 78.57),   # Bundelkhand, drought-prone
    # Bihar
    "patna":       (25.59, 85.14),   # central Bihar
    "gaya":        (24.79, 85.00),   # south Bihar, drier
    "purnia":      (25.78, 87.47),   # north-east, wettest
    # Jharkhand
    "ranchi":      (23.34, 85.31),   # Chota Nagpur plateau
    "jamshedpur":  (22.80, 86.20),   # south-east, humid
    "dumka":       (24.27, 87.25),   # Santhal Pargana, north-east
    # Maharashtra
    "pune":        (18.52, 73.86),   # western Deccan
    "nashik":      (19.99, 73.79),   # grapes & onion belt
    "aurangabad":  (19.88, 75.34),   # Marathwada, drought-prone
    "amravati":    (20.93, 77.75),   # Vidarbha cotton belt
    "kolhapur":    (16.70, 74.24),   # sugarcane belt, wetter
    # ---- held-out test cities (one per state) ----
    "amritsar":    (31.63, 74.87),   # Punjab
    "rohtak":      (28.90, 76.61),   # Haryana
    "narela":      (28.85, 77.09),   # Delhi (north-west, peri-urban farms)
    "varanasi":    (25.32, 82.97),   # Uttar Pradesh
    "muzaffarpur": (26.12, 85.39),   # Bihar
    "hazaribagh":  (23.99, 85.36),   # Jharkhand
    "solapur":     (17.66, 75.91),   # Maharashtra (semi-arid)
}
STATE: dict[str, str] = {
    "ludhiana": "Punjab", "bathinda": "Punjab", "patiala": "Punjab", "amritsar": "Punjab",
    "hisar": "Haryana", "karnal": "Haryana", "sirsa": "Haryana", "rohtak": "Haryana",
    "new_delhi": "Delhi", "narela": "Delhi",
    "meerut": "Uttar Pradesh", "agra": "Uttar Pradesh", "lucknow": "Uttar Pradesh",
    "gorakhpur": "Uttar Pradesh", "jhansi": "Uttar Pradesh", "varanasi": "Uttar Pradesh",
    "patna": "Bihar", "gaya": "Bihar", "purnia": "Bihar", "muzaffarpur": "Bihar",
    "ranchi": "Jharkhand", "jamshedpur": "Jharkhand", "dumka": "Jharkhand", "hazaribagh": "Jharkhand",
    "pune": "Maharashtra", "nashik": "Maharashtra", "aurangabad": "Maharashtra",
    "amravati": "Maharashtra", "kolhapur": "Maharashtra", "solapur": "Maharashtra",
}
TEST_LOCATIONS = ("amritsar", "rohtak", "narela", "varanasi", "muzaffarpur", "hazaribagh", "solapur")
TRAIN_LOCATIONS = tuple(c for c in LOCATIONS if c not in TEST_LOCATIONS)
WEATHER_START = "2020-01-01"       # 5 years: 5 rabi + 5 kharif seasons
WEATHER_END = "2024-12-31"
VALID_FROM = "2024-07-01"          # time split: train before, validate after
