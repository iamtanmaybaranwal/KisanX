# KisanX - Irrigation that thinks ahead

**KisanX predicts when each part of a field will need water, and waters only that part,
only as much as it needs.** If rain is coming, it waits. Everything runs on the farm
(ESP32 + Raspberry Pi 5), so it keeps working without internet.

> Internal code name: **Jal-Drishti** (you will see `jaldrishti_*` in the code).

---

## The problem
* Farmers water on a fixed timer or by guesswork, so some plants get too much water and
  others too little. Water and electricity are wasted, and crops get stressed.
* Typical "smart" systems switch the pump on only *after* the soil is already dry, and
  they still water when rain is about to fall.
* Most smart-farming products need a steady internet connection, which many villages
  do not have.

## What KisanX does
* A soil sensor in every zone is read **every 2 seconds**.
* An AI model predicts each zone's moisture **1, 3, 6 and 12 hours ahead**.
* Every crop gets its own **ideal moisture range**, worked out from the crop, the soil and
  the region (FAO-56 standard). Below the range the pump starts **automatically**; it stops
  when the zone is back in range.
* **Rain forecast?** It postpones watering and saves the water.
* Pumps run in short **pulses** (pump → soak → measure), so water soaks in instead of
  running off.
* Each zone **keeps learning its own soil** on the Pi.
* Farmers see everything on their phone by scanning a QR code: **28 Indian languages**,
  no app to install, no cloud. A **16x2 LCD** on the device shows the same numbers.

## How it works
```
 Soil sensors + DHT11 ─► ESP32  ──── Wi-Fi / MQTT ────►  Raspberry Pi 5
 Relay ─► 2 pumps        (sensing, pumps, safety)        (AI forecast, decisions,
 16x2 LCD                                                 database, dashboard)
                                                                │
                                         Phones open the dashboard (QR code)
 Internet is used only for the weather forecast — without it, everything keeps running.
```

| Device | Job | Why there |
|---|---|---|
| ESP32 DevKit V1 | reads sensors, switches pumps, hard safety limits, LCD | real-time, keeps the farm safe even if the Pi fails |
| Raspberry Pi 5 | AI predictions, on-device learning, decisions, database, dashboard | enough power, fully offline |
| Phone | dashboard in the browser | nothing to install |
| Laptop | development and model training only | the trained model is copied to the Pi |

---

## The AI model
**Question it answers:** *"If we do nothing, what will this zone's moisture be in 1, 3, 6
and 12 hours?"* That one answer drives every decision (irrigate now, postpone for rain,
or skip and say when water will be needed).

**Training data (we had no farm data, so we built it from real weather + physics):**
* **5 years (2020–2024) of real hourly weather** from **30 cities** in Punjab, Haryana,
  Delhi, Uttar Pradesh, Bihar, Jharkhand and Maharashtra (Open-Meteo archive).
* An **FAO-56 soil water-balance model** (the UN FAO standard for crop water use).
* 1,200 virtual farm zones (40 per city) with hidden differences: soil, shade, drainage, sensor
  error and irrigation habits.
* Weather forecasts with realistic errors (missed storms, false rain alarms).
* Result: **5.26 million samples** (3.63 M for training).

**Model:** physics forecast **+** a machine-learning correction (scikit-learn gradient
boosting) that learns only what the physics gets wrong. 1 MB, runs in milliseconds on the Pi.

**Results — tested in 7 cities the model never saw (one per state):**

| Forecast | KisanX error | FAO-56 physics alone | "No change" guess |
|---|---|---|---|
| 1 h | **0.28** | 0.46 | 0.42 |
| 6 h | **0.68** | 1.30 | 1.33 |
| 12 h | **1.02** | 1.88 | 2.12 |

*(mean absolute error in moisture %-points, lower is better)*

* **About 1.8–2× more accurate than standard FAO-56 physics in every state.**
* "This zone will need water within 6 h" warning: F1 **0.79** (physics alone: 0.71).
* **On-device learning:** in a 3-week simulated rehearsal, the indoor demo tray's 12 h
  forecast error fell by **70 %**; on an open field, where the model is already accurate,
  learning changes nothing (it can only switch on when it is clearly better).

Full details and per-state numbers: [ARCHITECTURE.md](ARCHITECTURE.md), section 4.

## Ideal moisture range per crop, soil and region
* **23 crops** (wheat, sugarcane, cotton, mustard, chickpea, potato, onion, tomato, soybean,
  jowar, bajra, grapes, banana …) and **9 soil types** (sandy loam of Punjab, alluvial loam
  of UP, Jharkhand red & lateritic soil, Maharashtra black cotton soil, …).
* Values from **FAO Irrigation & Drainage Paper 56** (Tables 12, 19, 22) and Indian soil
  studies.
* The lower limit moves with the weather: on hot days the crop is watered earlier
  (FAO-56 eq. 83).
* Examples: wheat in Punjab sandy loam **29–45 %**, onion in Maharashtra black soil
  **51–64 %**, tomato in Jharkhand red soil **31–42 %**.

## Safety (works even if Wi-Fi or the Pi fails)
* Every pump run is **timed**; the ESP32 cuts any run at 120 s.
* Pi silent for 30 s → **all pumps off**; the ESP32 then only gives a very dry zone a short
  pulse, at most once every 4 hours.
* Pump runs but moisture does not rise → **"tank empty / pipe blocked"** alert and lock-out.
* Broken or unplugged sensor → that zone is never watered blind.
* No internet → the DHT11's temperatures are used to estimate evaporation (FAO Hargreaves).

---

## Hardware
| Part | Qty |
|---|---|
| ESP32 DevKit V1 | 1 |
| Raspberry Pi 5 | 1 |
| Soil moisture sensor (FC-28 fork + LM393 board) | 2 |
| DHT11 temperature / humidity sensor | 1 |
| 2-channel 5 V relay module | 1 |
| Mini submersible pump (5 V) + drip tubing | 2 |
| 16x2 LCD (JHD162A) with I2C board | 1 |
| 5 V adapter for the pumps (separate from the electronics) | 1 |

### Wiring (ESP32)
| ESP32 pin | Connects to |
|---|---|
| D32 / D33 | soil sensor 1 / 2 — AO |
| D27 | both soil sensors — VCC (powered only while reading, so the probes don't corrode) |
| D4 | DHT11 — S (data); DHT11 + to 3V3 |
| D25 / D26 | relay IN1 / IN2 |
| D21 / D22 | LCD SDA / SCL |
| VIN (5 V) | relay VCC, LCD VCC |
| GND | all grounds |

Pumps: adapter **+** → relay COM1 and COM2 · relay NO1 / NO2 → pump 1 / pump 2 (+) ·
adapter **−** → both pumps (−). The pump supply never touches the ESP32.
More detail: [firmware/README.md](firmware/README.md).

### LCD screens
```
 power-up :  "     KisanX     "  ->  "    Welcome!    "
             "Smart Irrigation"      " Starting up... "
 then every 3 s:
             "Z1 45%* Z2 38%  "      "Z1 Tomato    45%"      "Z2 Wheat     38%"
             "31°C H:48%  AUTO"      "Ideal 36-62%  ON"      "Ideal 29-45% OFF"
```
`*` = pump running. Numbers come from the Pi (same as the dashboard); if the Pi is silent,
the ESP32 shows its own reading and "Pi offline".

---

## Tech stack
| Part | Technology |
|---|---|
| AI / ML | Python, scikit-learn (HistGradientBoosting), NumPy, pandas, FAO-56 physics |
| Weather | Open-Meteo (forecast + historical archive) |
| Pi backend | FastAPI, asyncio, MQTT (Mosquitto + paho-mqtt), SQLite, systemd |
| Firmware | C++ (Arduino on ESP32), PlatformIO, FreeRTOS safety task, ArduinoJson, PubSubClient |
| Dashboard | React 18 + Vite, custom SVG charts, PWA, QR codes, 28 languages |
| Tests | pytest (ML + server), Unity (firmware logic on the laptop) |

## Repository layout
```
ml/          AI: FAO-56 physics, dataset builder, training, live predictor, on-device learning
server/      Raspberry Pi backend: control loop, decision engine, MQTT link, API, database
dashboard/   React phone dashboard (served by the Pi)
firmware/    ESP32 firmware (PlatformIO) + wiring guide
deploy/      Pi install / update scripts
ARCHITECTURE.md   how everything works, with full results
STEPS.md          build plan and progress log
```

---

## Run it

### 1. Try it on a laptop (no hardware needed)
```bash
python -m venv .venv
.venv/Scripts/pip install -e ml -r server/requirements.txt     # Linux/macOS: .venv/bin/pip
cd dashboard && npm install && npm run build && cd ..
cd server
../.venv/Scripts/python -m jaldrishti_server --sim              # open http://localhost:8000
```
`--sim` runs a built-in virtual ESP32 and farm, so you can watch zones dry out, get
watered, and react to "Simulate rain".

### 2. Flash the ESP32
```bash
cd firmware
cp include/secrets.example.h include/secrets.h    # put your Wi-Fi name and password here
pio test -e native                                # 16 logic tests on the laptop
pio run -e esp32dev -t upload                     # flash (USB data cable)
pio device monitor                                # serial console: read, test, pump 1 5, lcd
```

### 3. Raspberry Pi
```bash
# first time, on the Pi:            bash ~/jaldrishti/deploy/pi_install.sh
# every update, from the laptop:    bash deploy/deploy.sh
```
The Pi serves the dashboard on port 80; the Phone tab shows QR codes to join the Wi-Fi
and open the dashboard.

### 4. Retrain the model (optional, ~1.5 h on a laptop)
```bash
cd ml
../.venv/Scripts/python -m jaldrishti_ml.dataset        # build the training data
../.venv/Scripts/python -m jaldrishti_ml.train          # train + evaluate + save
../.venv/Scripts/python -m jaldrishti_ml.replay_suite   # 3-week live rehearsal
```

## Tests
| Part | Tests |
|---|---|
| ML (physics, features, predictor, learning) | 20 |
| Pi server (decisions, irrigation, safety, API, MQTT, full-system runs) | 49 |
| ESP32 logic (pump timers, watchdog, fallback, LCD text) | 16 |

## Future scope
Solar power · more zones and LoRa for large fields · fertiliser through the drip line
(fertigation) · retraining on real data collected by the Pi.

---
Made by [@iamtanmaybaranwal](https://github.com/iamtanmaybaranwal).
