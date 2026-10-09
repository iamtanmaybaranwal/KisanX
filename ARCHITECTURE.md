# Jal-Drishti — Architecture & How It Works

> **Edge-AI precision irrigation.** Jal-Drishti does not ask *"is the soil dry?"* —
> it predicts **when** each crop zone will need water, **how much**, and delivers it
> only to that zone, with all intelligence running locally on a Raspberry Pi 5.

Status legend: ✅ built & tested · 🛠️ next · 📋 planned

| Part | Status |
|---|---|
| ML: physics simulator, dataset, models, live predictor, on-device learning (19 tests) | ✅ |
| Pi server (MQTT + FastAPI + SQLite + decision engine + safety, 40 tests) | ✅ |
| Simulated ESP32 + mini farm (in-process and over MQTT) | ✅ |
| React dashboard (28 languages, phone-ready, QR codes) | ✅ |
| ESP32 firmware (PlatformIO, compiles; 10 safety tests) | ✅ (flash on hardware day) |
| Deployment scripts for the Pi | 📋 |

---

## 1. System overview

```
                         ┌──────────────────────────────────────────────┐
  Open-Meteo forecast ──►│              RASPBERRY PI 5  (brain)          │
  (only when online)     │                                              │
                         │  Mosquitto MQTT broker                       │
                         │  FastAPI server ── SQLite (all readings)     │
                         │   ├─ ML predictor  (moisture 1/3/6/12 h)     │
                         │   ├─ On-device learning (per zone)           │
                         │   ├─ Decision engine (irrigate/skip/postpone)│
                         │   └─ Serves React dashboard (PWA)            │
                         │  Wi-Fi hotspot "JAL-DRISHTI"                 │
                         └───────────────┬──────────────────────────────┘
                                         │ Wi-Fi (MQTT)
               phones / laptop ◄─────────┤  http://192.168.4.1
                                         ▼
                         ┌──────────────────────────────────────────────┐
                         │          ESP32 DevKit V1  (hands)             │
                         │  reads 2 soil sensors + DHT11 every 2 s      │
                         │  drives relay → 2 submersible pumps          │
                         │  failsafe: max pump time, Pi-silent → OFF,   │
                         │            local fallback thresholds         │
                         └───────────────┬──────────────────────────────┘
                                         ▼
                           Zone 1        Zone 2   (real soil + drip)
```

**Everything is local.** Data never leaves the table. Internet is used *only* to
refresh the weather forecast; without it the Pi keeps running on the last
forecast (padded with "no rain, yesterday's evaporation pattern").

**Who does what**

| Device | Responsibility | Why there |
|---|---|---|
| ESP32 | Sensing, switching pumps, hard safety limits | Real-time, survives Pi crash |
| Raspberry Pi 5 | ML inference, learning, decisions, database, dashboard | Enough CPU/RAM, offline |
| Laptop | Development + model training only | Fast; result copied to Pi |
| Phone | Views the dashboard over the Pi's Wi-Fi | No app install needed |

---

## 2. Hardware (planned wiring)

| Item | Qty | ESP32 pin |
|---|---|---|
| Capacitive soil sensor v1.2 (3.3 V) | 2 (+1 spare) | GPIO 32, 33 (ADC1 — ADC2 is unusable while Wi-Fi is on) |
| 2- or 4-ch relay module, 5 V, optocoupled | 1 | IN1, IN2 → GPIO 25, 26 (boot-safe pins) |
| Mini submersible pump 3–6 V | 2 | via relay, own 5 V supply |
| DHT11 temperature/humidity | 1 | GPIO 4 |

Notes: most relay boards are **active-LOW** (firmware handles it). Pumps are never
powered from the ESP32 or Pi. Mini farm: tray with 2 compartments,
potting soil ~10 cm deep, drip tubing per zone.

---

## 3. Moisture scale and the ideal range per crop, soil and region

**Scale.** 0 % = air-dry soil, 100 % = saturated soil. The resistive FC-28 forks are
converted with a fixed range (raw 4000 = 0 %, raw 400 = 100 %), so there is nothing
to calibrate on site. Raw values below 180 mean "board has no power" and are rejected.

**Ideal range** (shown on every zone as a green strip under the moisture bar):

* `min_pct` = FC − p × (FC − WP) → below this, irrigation starts automatically
* `target_pct` = FC − 5 % of the available water → irrigation stops here
* `p` = FAO-56 Table 22 depletion fraction, **adjusted every day for the crop's water use**
  with FAO-56 eq. 83: `p = p_table + 0.04 (5 − ETc)`, limited to 0.1–0.8.
  ETc = forecast ET0 for the next 24 h × the crop's Kc for its growth stage.
  Hot, dry days → smaller p → the zone is watered earlier.

**Crops (23)**, with FAO-56 Kc (Table 12) and p (Table 22): wheat, maize, sugarcane,
cotton, mustard, chickpea, lentil, peas, soybean, groundnut, sorghum (jowar), pearl millet
(bajra), potato, onion, tomato, chilli, brinjal, cabbage, cauliflower, spinach, mint,
banana, grapes. Not included: paddy (grown flooded, not under drip), okra and pigeon pea
(not in the FAO-56 tables, so no values were guessed).

**Soils (9)**, field capacity / wilting point from FAO-56 Table 19 and Indian soil studies:

| Soil | Typical region | FC / WP (m³/m³) |
|---|---|---|
| Sandy | south-west Punjab & Haryana | 0.12 / 0.05 |
| Sandy loam | Punjab, Haryana | 0.23 / 0.11 |
| Loam | UP, Punjab, Haryana, Delhi | 0.25 / 0.12 |
| Silt loam | UP, Bihar | 0.29 / 0.15 |
| Clay loam | Bihar low-lands | 0.34 / 0.20 |
| Clay | – | 0.36 / 0.22 |
| Red & lateritic | Jharkhand | 0.20 / 0.09 |
| Black cotton (Vertisol) | Maharashtra (measured FC 33–41 %, PWP 13–16 %) | 0.37 / 0.15 |
| Potting mix | pots / demo tray | 0.40 / 0.12 |

Examples (normal day): wheat in sandy loam 29–45 %, potato in loam 37–47 %,
sugarcane in black cotton soil 34–64 %, onion in black cotton soil 51–64 %,
tomato in red & lateritic soil 31–42 %.

**Regions** (dashboard → zone → Change crop → Region) pick the region's typical soil and
list its main drip-suitable crops first: Punjab, Haryana, Delhi, Uttar Pradesh, Bihar,
Jharkhand, Maharashtra.

Sources: FAO Irrigation & Drainage Paper 56 (fao.org/4/x0490e), Tables 12, 19, 22 and
eq. 21, 52, 83; "Water retention characteristics of the swell-shrink soils of Maharashtra"
(krishikosh.egranth.ac.in).

## 3b. What the DHT11 is for (the forecast comes from the internet)
The weather API gives the **outdoor forecast for the city**. The DHT11 measures the air
**at the farm itself** (shade, greenhouse, the demo table indoors). It is used for:
1. **Working offline.** When the internet is down and the forecast runs out, the Pi
   estimates evaporation from the DHT11's last 24 h of temperatures with the FAO-56
   **Hargreaves** method (eq. 52, with Ra from eq. 21 for the farm's latitude and date),
   instead of guessing. This feeds both the AI forecast and the daily ideal-range
   adjustment.
2. **The dashboard** shows the measured on-site temperature and humidity.
It is deliberately **not** a direct input of the AI model: raw temperature/humidity made
the model memorise training cities (tested), so evaporation reaches the model through ET0.

## 4. The ML — how it works

### 4.1 The question it answers
> *"If we do **nothing**, what will this zone's moisture be in 1, 3, 6 and 12 hours?"*

That single answer drives every decision:
* falls below `min_pct` soon and no real rain coming → **irrigate now**
* falls below but forecast rain will refill it → **postpone**
* stays above → **skip**, and the dashboard shows *"needs water in ~4.5 h"*.

### 4.2 Training data without a farm (we have no data yet)
`ml/jaldrishti_ml/dataset.py`

1. **Real weather from the target region — Punjab, Haryana, Delhi, Uttar Pradesh, Bihar**
   (Indo-Gangetic plains): 3 years (2022-24 → three rabi + three kharif seasons) of
   hourly temperature, humidity, rain and FAO reference evapotranspiration (ET0) from
   the free Open-Meteo archive, for 18 cities covering each state's agro-climatic zones:

   | State | Training cities | Held-out test city |
   |---|---|---|
   | Punjab | Ludhiana, Bathinda, Patiala | **Amritsar** |
   | Haryana | Hisar, Karnal, Sirsa | **Rohtak** |
   | Delhi | New Delhi | **Narela** |
   | Uttar Pradesh | Meerut, Agra, Lucknow, Gorakhpur, Jhansi | **Varanasi** |
   | Bihar | Patna, Gaya, Purnia | **Muzaffarpur** |

   Soils include the region's alluvial sandy loam, loam, silt loam and Bihar's clay
   loam; crops include wheat, potato, sugarcane, mustard, maize, cotton, chickpea and
   vegetables (FAO-56 coefficients). Paddy is excluded — it is grown flooded, not
   under drip.
2. **Physics**: an FAO-56 root-zone water balance (`soil_model.py`) on a 15-min grid —
   rain/irrigation in, drainage above field capacity, crop ET with water-stress
   coefficient Ks. Unit-tested for exact water conservation.
3. **1,000 virtual zones** (50 per city), each with *hidden* properties the model must
   cope with: soil ±10 % off its label, unknown sun/shade exposure (indoor tray vs
   field), drainage speed, sensor offset ±2 %, and a random irrigation habit
   (smart threshold, timer, ad-hoc, rain-fed).
4. **Imperfect forecasts**: the "forecast" is the real future with realistic errors —
   timing blur, ±50 % amounts, 15 % missed storms, 6 % false alarms.
5. **Counterfactual labels**: from every sample time the physics is re-run with **no
   irrigation**, so labels mean exactly "what happens if we do nothing".

Result: 3.07 M samples (20 cities × 3 years).

### 4.3 Inputs (features) — `features.py`
| Feature | Meaning |
|---|---|
| **phys_delta** | the FAO-56 physics forecast itself (hybrid / physics-informed ML) |
| m_now, d_1h, d_3h | current moisture and recent drying trend (reveals hidden shade/soil) |
| rel_aw | where we are between wilting point and field capacity |
| hours_since_water | since last irrigation / real rain |
| fc_pct, wp_pct, depth_mm | soil + tray configuration |
| kc, p | crop + growth stage (FAO-56) |
| fc_et0_mm, fc_rain_mm, fc_rain_prob | weather forecast over the horizon |

**Deliberately not inputs:** raw temperature, humidity and time of day. With them the
model memorised the training cities' climates and got *worse* in a new city
(tested); the evaporation forecast (ET0) already carries that physics. This also
means the AI does not depend on the DHT11 (it stays for the dashboard).

One function builds features in training and on the Pi → no train/serve skew
(enforced by unit tests, including a bit-exact check of the physics input).

### 4.4 Model — physics-informed gradient boosting
For each horizon (1, 3, 6, 12 h):

```
prediction = FAO-56 physics forecast  +  ML correction
```
The ML part (scikit-learn `HistGradientBoostingRegressor`, small trees, strong
regularisation) learns only **what the physics gets wrong** — shade, drainage,
sensor offset, forecast errors. Missing history (a freshly booted sensor) is
handled natively.

**How it was chosen (no peeking at the test city):** the number of boosting rounds
is picked by *leave-one-city-out* — train on 4 cities, score on the 5th, rotate.
Experiments showed big trees + random early-stopping looked better on known cities
but were worse on a new one (overfitting to climate), so the final design favours
generalisation. Model file: **0.5 MB**, prediction in milliseconds on the Pi.

### 4.5 Results — honest evaluation (7 states, 30 cities, 5 years)
Training data: real weather 2020–2024 for 30 cities in Punjab, Haryana, Delhi, UP, Bihar,
Jharkhand and Maharashtra → 5.26 M samples (3.63 M train, 0.41 M valid, 1.23 M test).
Mean absolute error in moisture %-points (lower is better). **Test = 7 cities never
used for training or for any design choice — one per state.**

| Horizon | **Jal-Drishti** | "No change" | "Trend continues" | FAO-56 physics alone |
|---|---|---|---|---|
| 1 h | **0.28** | 0.42 | 0.57 | 0.46 |
| 3 h | **0.46** | 0.82 | 1.28 | 0.87 |
| 6 h | **0.68** | 1.33 | 2.33 | 1.30 |
| 12 h | **1.02** | 2.12 | 4.19 | 1.88 |

Early warning *"this zone will need water within 6 h"* (F1, higher is better):
**0.79** vs 0.71 for physics alone.

**Per state** (its unseen city, 6 h ahead):

| State (unseen city) | MAE Jal-Drishti | MAE physics | F1 Jal-Drishti | F1 physics |
|---|---|---|---|---|
| Punjab (Amritsar) | **0.68** | 1.23 | **0.76** | 0.69 |
| Haryana (Rohtak) | **0.55** | 1.00 | **0.79** | 0.73 |
| Delhi (Narela) | **0.64** | 1.15 | **0.78** | 0.69 |
| Uttar Pradesh (Varanasi) | **0.74** | 1.44 | **0.81** | 0.80 |
| Bihar (Muzaffarpur) | **0.64** | 1.29 | **0.81** | 0.69 |
| Jharkhand (Hazaribagh) | **0.75** | 1.52 | **0.74** | 0.71 |
| Maharashtra (Solapur) | **0.73** | 1.47 | **0.82** | 0.65 |

* About **1.8–2× more accurate than physics alone in every state**, in cities it never saw.
* Training length chosen by *leave-one-state-out* (train on 6 states, score on the 7th):
  440 / 360 / 280 / 230 rounds for 1 / 3 / 6 / 12 h.
* Most important inputs (6 h): physics forecast, available water, soil wilting point,
  3 h trend, hours since water, 1 h trend.
* Model file **1.0 MB**. The previous 5-state model (3.07 M samples, 0.65 MB) is kept as
  `ml/models/moisture_model_5states_backup.joblib`.

Full numbers: `ml/models/metrics.json`.

### 4.6 On-device learning (edge AI that improves itself) — `adaptation.py`
Each zone checks every forecast once it matures. If nothing disturbed the window —
no irrigation, no real rain **and no rain forecast** — it learns a correction
`observed ≈ a·predicted + b` by recursive least squares (forgetting ≈ 2 days).

Safety rules (each one added because a test showed it was needed):
1. **Two days of evidence first** (192 checks): neighbouring 15-min checks are nearly
   identical, so fewer checks = one sunny afternoon, not the zone.
2. **Must beat the raw model by ≥ 15 %** on the zone's own recent checks (scored
   *before* each update). Otherwise the raw model is used — learning cannot make
   things worse.
3. **Dry forecasts only.** A false-alarm rain forecast once taught a zone a wrong
   correction; forecast errors belong to the weather, not the zone, and the main
   model already learned them from millions of examples.

`PumpResponse` learns how many %-points one second of pumping adds per zone, so
the pump time is computed, not hard-coded.

**Live dress rehearsal** (`replay_suite.py`): 3 weeks of minute-by-minute sensor
readings with noise, the exact Pi code path every 15 min, in each state's unseen city
(7 cities), in the rabi season and the hot pre-monsoon (10 runs per scenario). Error at 12 h ahead:

| Zone | Week 1 | Week 2 | Week 3 |
|---|---|---|---|
| **Indoor demo tray** — model only | 1.06 | 1.13 | 1.23 |
| **Indoor demo tray** — with on-Pi learning | 0.78 (−27 %) | **0.38 (−66 %)** | **0.37 (−70 %)** |
| Open field (silt loam, wheat) — model only | 0.59 | 0.40 | 0.36 |
| Open field — with on-Pi learning | 0.59 (±0 %) | 0.40 (±0 %) | 0.37 (+1 %, noise) |

The shaded indoor tray behaves very differently from an open field, and the Pi learns
that within days. A field that the main model already predicts well is left alone.

### 4.7 Train on laptop → run on Pi
```
laptop:  python -m jaldrishti_ml.dataset      # build data   (~25 min, 18 cities)
         python -m jaldrishti_ml.train        # train + evaluate + save (~15 min)
         python -m pytest tests               # 19 checks
         python -m jaldrishti_ml.replay_suite # live dress rehearsal, 4 states
copy:    ml/jaldrishti_ml/ + ml/models/moisture_model.joblib  → Pi (deploy script)
Pi:      server loads MoisturePredictor once, calls it every 30–60 s per zone
```
Library versions are pinned (`ml/requirements.txt`) so the model file loads
identically on the Pi; the predictor warns if versions differ.
Later: pull logged real data from the Pi, retrain, push the new model back.

---

## 5. Pi server — how it works (`server/jaldrishti_server/`)

One Python process (FastAPI + asyncio). Everything — control loop, API, live feed —
runs in a single event loop, so there are no threading races; only the weather
download runs in a worker thread and it never touches the database.

| File | Job |
|---|---|
| `controller.py` | the control loop: sensors → AI forecast → decision → pulse irrigation → learning |
| `decision.py` | the decision engine (pure function, every rule unit-tested) |
| `irrigation.py` | pulse-irrigation state machine + dry-run detection |
| `weather_service.py` | Open-Meteo forecast, offline cache, "Simulate rain" |
| `link.py` | MQTT link to the ESP32 (the exact message protocol) |
| `simdevice.py` | virtual ESP32 + 4-zone farm (same physics as the ML) |
| `db.py` | SQLite: readings, decisions, pump events, zones, learning state |
| `app.py` | REST API + WebSocket + serves the dashboard |

### 5.1 The control loop (every 0.5 s)
| Every | What happens |
|---|---|
| 2 s | ESP32 readings arrive → raw ADC → % via each sensor's calibration; bad values (unplugged / shorted) rejected |
| 5 s | heartbeat to the ESP32 |
| 10 s | one median per zone stored (history for the AI and the charts) |
| 30 s | **decision** for every zone (and immediately when the ESP32 connects or drops) |
| 5 min | **AI forecast** 1/3/6/12 h per zone (+ on-device correction) |
| 15 min | log a forecast to check later → on-device learning |
| 30 min | refresh the weather forecast (background) |

### 5.2 Decision rules (`decision.py`)
1. Pump fault → **FAULT** (until the farmer resets). ESP32 silent → **FAULT** (device offline).
2. No valid reading → **NO_SENSOR** (never water blind).
3. ≥ target → **SKIP** "enough water".
4. Needs water if below `min_pct` **or** the AI says it will cross within 2 h
   (otherwise **SKIP** "will need water in ~X h").
5. Needs water **and** ≥ 2 mm rain at ≥ 50 % in the next 6 h **and** the AI predicts the
   soil recovers above `min_pct` → **POSTPONE** 🌧️ (unless critically dry → light top-up).
6. Just watered → **WAIT** (10 min for the water to spread).
7. Otherwise **IRRIGATE**: pump seconds = deficit ÷ learned pump gain.
Every decision has a reason in English **and Hindi** (Farmer Mode) and is logged.
In **manual mode** decisions are only recommendations; the farmer presses "Irrigate now".

### 5.3 Pulse irrigation (`irrigation.py`)
Pump a short pulse (8 s, or 15 s in the demo profile) → soak (30 s / 15 s) → re-measure →
learn the pump response → repeat until the target, a time budget (≤ 150 s per session)
or a fault. Every command to the ESP32 is **timed**, so a crashed Pi can never leave a
pump running. Measured on the virtual farm: 30 % → 57.9 % (target 57.8 %), no overshoot.

### 5.4 Water saved — an honest baseline
A timer cannot tell zones apart, so to keep every crop healthy it must give **every**
zone what the thirstiest zone needed. Baseline = that × number of zones; saving =
baseline − what Jal-Drishti used. No invented timer settings. Live demo run: 75 %.

## 6. Safety layers (all tested)
1. ESP32: every pump run is timed and capped at 120 s; no heartbeat for 30 s → all
   pumps OFF + local fallback mode.
2. Pi: max 150 s pumping per session; 20 s of pumping with < 0.5 % rise → **"tank empty /
   pipe blocked"** lock-out until reset; ESP32 offline → sessions stopped.
3. Bad sensor values → that zone is never watered.
4. The control loop survives any single error (logged, next tick continues).
5. Rules work even without the AI (no history yet → plain thresholds).

## 7. MQTT protocol (implemented in `link.py`; the firmware follows it)
| Topic | Direction | Payload |
|---|---|---|
| `jal/sensors` | ESP32 → Pi, every 2 s | `{"raw":[r1,r2], "temp":31.2, "rh":48, "pumps":[0,1], "mode":"remote", "uptime":1234, "fw":"1.0"}` |
| `jal/status` | ESP32 → Pi (retained, last-will) | `"online"` / `"offline"` |
| `jal/cmd` | Pi → ESP32 | `{"zone":3, "seconds":5.0, "id":17}` · stop: `"seconds":0` · `{"all_off":true}` |
| `jal/heartbeat` | Pi → ESP32, every 5 s | `{"t":1730000000, "mode":"auto", "z":[{"c":"Tomato","m":45,"lo":36,"hi":62,"f":false}, …]}` — `z` = what the ESP32's 16x2 LCD shows |

## 7b. API (for the dashboard)
`GET /api/state` (everything, also pushed on `ws://…/ws` every 2 s) · `GET /api/history`
· `GET /api/decisions` · `GET /api/options` · `POST /api/mode/{auto|manual}` ·
`POST /api/zones/{z}/irrigate|stop|reset_fault|config|calibrate/{dry|wet}` ·
`POST /api/weather/simulate?scenario=rain` · `POST /api/weather/location` ·
simulated farm only: `/api/sim/...` (set moisture, empty tank, block pipe, unplug sensor).
Interactive docs at `/docs`.


## 7c. ESP32 firmware (`firmware/`, PlatformIO) — see `firmware/README.md`
* Arduino framework on `espressif32 @ 7.0.1` (Arduino core 2.0.17), board `esp32doit-devkit-v1`.
* `lib/jal_core/` holds all safety logic with **no Arduino code**, so it is unit-tested on
  the laptop (`pio test -e native`, 11 tests): timed pumps capped at 120 s, heartbeat
  watchdog (30 s → all pumps off + fallback), fallback waters only a very dry zone, one
  pump at a time, millis() wrap-around, noisy/unplugged sensor detection.
* Pump timing runs in its **own high-priority FreeRTOS task every 50 ms**, so a Wi-Fi or
  MQTT hiccup can never delay switching a pump off. A task watchdog reboots the ESP32 if
  the main loop ever hangs.
* Serial console for hardware day (`read`, `test`, `pump 3 5`, `off`, `status`) works
  without the Pi; LED shows connection state.
* Build: RAM 13.8 %, flash 61 %.

## 7d. Dashboard (`dashboard/`, React + Vite, served by the Pi)
* "Fresh" theme: clean white + emerald green, neutral charcoal dark mode (picked from three
  candidates; all text ≥ 4.5:1 contrast), simple farmer-first layout: four big crop
  cards with one status, one sentence and one button; details open on tap.
* **28 languages**: English, the major scheduled languages (Hindi, Bengali, Telugu,
  Marathi, Tamil, Urdu, Gujarati, Kannada, Odia, Malayalam, Punjabi, Assamese, Nepali,
  Maithili, Konkani, Dogri, Sanskrit, Sindhi) and the local languages of the target region
  (Bhojpuri, Awadhi, Magahi, Haryanvi, Rajasthani, Bundeli, Braj, Angika, Chhattisgarhi).
  Urdu and Sindhi are shown right-to-left. Decision sentences are built on the phone from
  the Pi's structured decision, so every language gets the full explanation. Technical
  screens are English + Hindi. *Translations were machine-drafted: have native speakers
  review them before public use.*
* Charts follow the data-viz rules: validated colour-blind-safe palette (green, blue,
  mustard, violet), forecast dashed with an 80 % band, hover crosshair + tooltip, and a
  table view for every chart.
* Phone tab: two QR codes (join the Pi's Wi-Fi, open the dashboard). URL options
  `?lang=bho&theme=light&tab=history&zone=3` let a QR code open in a farmer's language.
* Live updates over a WebSocket every 2 s, with polling fallback; 91 KB gzipped.

## 8. Folder layout
```
Jal-Drishti/
├── ARCHITECTURE.md          ← this file
├── STEPS.md                 ← build plan + status
├── project.md               ← original idea + pitch notes
├── ml/
│   ├── jaldrishti_ml/
│   │   ├── config.py        soils, crops, thresholds, locations
│   │   ├── soil_model.py    FAO-56 water balance (vectorised)
│   │   ├── weather.py       Open-Meteo archive + forecast-error simulation
│   │   ├── features.py      single source of truth for features
│   │   ├── dataset.py       synthetic dataset generation
│   │   ├── train.py         training + evaluation + model bundle
│   │   ├── predictor.py     live inference (used on the Pi)
│   │   ├── adaptation.py    on-device learning + pump response
│   │   ├── replay.py        end-to-end digital-twin rehearsal (one run)
│   │   └── replay_suite.py  rehearsal across 4 states x 2 seasons
│   ├── tests/               unit + integration tests
│   ├── models/              moisture_model.joblib, metrics.json
│   └── data/weather/        cached weather for 18 cities (dataset.pkl is regenerated)
├── server/                  Pi backend (see section 5)
│   ├── jaldrishti_server/   controller, decision, irrigation, weather, link, sim, db, app
│   └── tests/               decision, irrigation, full-system, API, MQTT tests
├── dashboard/               React dashboard (src/App.jsx, i18n.js, charts.jsx)
├── firmware/                ESP32 firmware (PlatformIO) + README with wiring
└── deploy/      📋          Pi setup + deploy scripts
```

## 9. What to say to judges (ML in one breath)
> "We had no farm data, so we trained on 5 million samples from a physics model
> driven by five years of real weather from 30 cities across Punjab, Haryana, Delhi,
> UP, Bihar, Jharkhand and Maharashtra, and tested in a city of each state the model
> never saw — it is 1.8 to 2 times more accurate than standard FAO-56 physics. On the Pi
> it predicts each zone's moisture 12 hours ahead and keeps learning the real soil on
> our table, fully offline."
