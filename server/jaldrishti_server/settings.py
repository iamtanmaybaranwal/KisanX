"""All tunable constants of the Pi server in one place."""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("JD_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
DB_PATH = DATA_DIR / "jaldrishti.sqlite3"
DASHBOARD_DIR = Path(os.environ.get("JD_DASHBOARD_DIR",
                                    Path(__file__).resolve().parents[2] / "dashboard" / "dist"))

N_ZONES = 2                    # table-top demo farm: 2 zones (sensors GPIO 32/33, relays 25/26)

# ---- timing (seconds) -----------------------------------------------------------
SENSOR_PERIOD_S = 2            # ESP32 sends readings this often
STORE_PERIOD_S = 10            # one median per zone is written to the DB this often
DECISION_PERIOD_S = 30         # decision engine runs this often
PREDICT_PERIOD_S = 300         # ML forecast refresh (moisture changes slowly)
LEARN_LOG_PERIOD_S = 900       # one forecast per 15 min is checked later for learning
HEARTBEAT_PERIOD_S = 5         # Pi -> ESP32 "I am alive"
SENSOR_STALE_S = 30            # no reading for this long -> sensor offline
DEVICE_STALE_S = 15            # no message at all -> ESP32 offline
WEATHER_REFRESH_S = 1800
STATE_SAVE_PERIOD_S = 600      # persist learning state
HISTORY_KEEP_DAYS = 60

# ---- irrigation ------------------------------------------------------------------
# JD_PROFILE=demo -> longer pulses / shorter soaks so a refill fits a 5-minute pitch.
# Tune SOAK_S on the real tray: it must be long enough for drip water to reach the sensor
# (keep the sensor ~3-5 cm from the dripper).
PROFILE = os.environ.get("JD_PROFILE", "field")
PULSE_MAX_S = float(os.environ.get("JD_PULSE_S", 15 if PROFILE == "demo" else 8))   # pump pulse ...
SOAK_S = float(os.environ.get("JD_SOAK_S", 15 if PROFILE == "demo" else 30))        # ... then soak
MAX_SESSION_PUMP_S = 150       # hard cap per irrigation session (a fully dry tray needs ~100 s)
NO_RISE_CHECK_S = 20           # after this much pumping ...
NO_RISE_MIN_PCT = 0.5          # ... moisture must have risen this much, else FAULT
TARGET_TOLERANCE_PCT = 0.5
COOLDOWN_S = 600               # after a session, let water spread before re-deciding
DEVICE_MAX_PUMP_S = 120        # the ESP32 enforces this too

# ---- decision engine -----------------------------------------------------------------
LOOKAHEAD_H = 2.0              # irrigate early if the zone will cross min within this
RAIN_POSTPONE_MM = 2.0         # forecast rain in next 6 h ...
RAIN_POSTPONE_PROB = 50.0      # ... with at least this probability ...
                               # ... AND the AI predicts the soil recovers -> postpone
CRITICAL_FRACTION = 0.3        # below wp + 0.3*(min-wp) = critically dry: water anyway

# ---- sensors -----------------------------------------------------------------------
RAW_VALID = (180, 4095)        # ESP32 12-bit ADC. A resistive probe in dry soil reads up to
                               # 4095, so the top of the range is valid; an unplugged probe is
                               # caught by the ESP32 (noisy pin -> null) instead. Below 180 =
                               # board without power (the fork reads >= ~220 even in water).
DEFAULT_RAW_DRY = 4000         # FC-28 fork (AO): dry soil / air ~4000-4095 - calibrate per sensor!
DEFAULT_RAW_WET = 400          # ... soaked soil / water ~200-1600 (measured on the real sensor)

# ---- water accounting ------------------------------------------------------------------
DEFAULT_FLOW_L_PER_MIN = 1.0   # per pump through drip line; calibrate on the real farm

# ---- Wi-Fi shown as a QR code on the dashboard's Phone tab (editable there) ----------
DEFAULT_SHARE_WIFI = {"ssid": "TanmayPi", "password": ""}   # set the password in the Phone tab

# ---- farm location (forecast) ------------------------------------------------------------
DEFAULT_LOCATION = {"name": "Lucknow, Uttar Pradesh", "lat": 26.85, "lon": 80.95}

DEFAULT_ZONES = [
    {"zone": 1, "name": "Zone 01", "crop": "tomato", "stage": "mid", "soil": "potting_mix", "depth_mm": 100},
    {"zone": 2, "name": "Zone 02", "crop": "wheat", "stage": "mid", "soil": "potting_mix", "depth_mm": 100},
]
