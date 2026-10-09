# Jal-Drishti — Build Steps

The step-by-step plan for building the whole system, with what is done and what comes next.
For *how* each part works, see [ARCHITECTURE.md](ARCHITECTURE.md).

Status: ✅ done & tested · 🔄 in progress · ⏳ waiting for hardware · 📋 to do

| # | Step | Needs hardware? | Status |
|---|---|---|---|
| 0 | Plan the idea, demo story and parts list | No | ✅ |
| 1 | ML: physics model, data, training, testing | No | ✅ |
| 2 | Pi server: database, weather, decision engine, pump safety, API | No | ✅ |
| 3 | Simulated ESP32 + full-system test on the laptop | No | ✅ |
| 4 | Dashboard (React, 28 languages, phones, QR code) | No | ✅ |
| 5 | Set up the Raspberry Pi 5 and deploy | Pi | 📋 **next** |
| 6 | ESP32 firmware (6a written + tested ✅ · 6b flash ⏳) | ESP32 | 🔄 |
| 7 | Wiring + sensor calibration | All parts | ⏳ |
| 8 | Build the mini farm (2 zones) | All parts | ⏳ |
| 9 | Let the Pi learn the real tray | Running system | ⏳ (only ~1 day available) |
| 10 | Demo rehearsal + pitch | Everything | 📋 |


## ⏰ Timeline (hackathon on day 3, hardware handed over on day 2)

| Day | Goal |
|---|---|
| **Day 0** (6 Oct) ✅ | ML (5 regions), Pi server, simulator, firmware, dashboard — all tested on the laptop |
| **Day 1** (7 Oct) | Step 5: set up the Pi (needs Pi IP + login), deploy, hotspot, QR codes; dry-run the whole demo on the Pi with the simulated farm; buy/collect tray, soil, plants, tubing |
| **Day 2** (8 Oct) — hardware day | Flash → wire → calibrate → build the mini farm → rehearse (checklist below) |
| **Day 3** (9 Oct) | Hackathon. Arrive early, power up, check the 5-minute script once |

### Hardware-day checklist (target times)
| Time | Task | How to check it worked |
|---|---|---|
| 0:00–0:20 | Flash the ESP32 (`pio run -t upload`), open `pio device monitor` | banner "Jal-Drishti ESP32 firmware" |
| 0:20–0:50 | Wire 2 sensors (3V3, GND, GPIO 32 + 33) | `read`: dry ≈ 2600-3100, in water ≈ 1300-1600 |
| 0:50–1:30 | Wire relay (GPIO 25 + 26) + 2 pumps (own 5 V supply) | `test`: each relay clicks, right pump runs |
| 1:40–2:00 | Join the Pi hotspot | LED solid; dashboard shows "Farm connected" |
| 2:00–2:30 | Calibrate each zone: Dry / Wet buttons | moisture reads ~0 % dry, ~100 % soaked |
| 2:30–4:30 | Build the mini farm: tray, soil, plants, drip line per zone, labels | water reaches only its own zone |
| 4:30–5:00 | Set pump flow per zone (fill a cup for 30 s → L/min) in zone settings | "Water now" refill matches the estimate |
| 5:00–6:00 | Rehearse the 5-minute demo 3× (`JD_PROFILE=demo`) | dry → water → rain postpones → unplug internet |
| evening | Leave the farm running overnight so each zone's AI starts learning | "Forecasts checked" rising in zone details |

Spare parts to bring: 1 extra sensor, jumper wires, extra tubing, a full water bottle
for the tank, phone power bank, printed QR codes.

---

## Step 0 — Plan ✅
* Idea, demo flow and pitch: `project.md`
* Decisions: ESP32 = sensing + pumps + safety; Raspberry Pi 5 = AI + decisions + database
  + dashboard; phone/laptop view the dashboard over the Pi's own Wi-Fi (fully local).
* Parts (bought ✅, 2-zone demo): 2-3× capacitive soil sensor v1.2, 2/4-channel 5 V relay
  (optocoupled), 2× mini submersible pump 3–6 V + silicone tube, 5 V 2 A adapter for the pumps,
  DHT11 (optional), jumper wires, tray, soil, 4 plants, transparent tank.

## Step 1 — ML ✅ (folder `ml/`)
1. Download 3 years (2022-24) of real hourly weather for 18 cities in Punjab, Haryana,
   Uttar Pradesh and Bihar (free Open-Meteo archive).
2. Write the FAO-56 soil water-balance physics model and unit-test it (water is conserved).
3. Simulate 900 virtual farm zones with hidden differences → 2.76 M training samples.
4. Train the physics-informed model (physics forecast + ML correction), 4 horizons
   (1/3/6/12 h). Training length chosen by leave-one-state-out.
5. Test on 4 unseen cities (one per state): ~2× more accurate than physics alone.
6. Write the live predictor (used on the Pi) and on-device learning per zone.
7. 3-week live rehearsals in each state → on-Pi learning makes the demo tray's 12 h
   forecast 73 % more accurate.
8. Bugs found and fixed along the way: 1 h-ago reading off by 4 min, Pi time-zone,
   city memorisation, learning switching on too early, learning from false-alarm rain.

Re-run any time:
```
cd ml
../.venv/Scripts/python -m jaldrishti_ml.dataset       # ~25 min
../.venv/Scripts/python -m jaldrishti_ml.train         # ~15 min
../.venv/Scripts/python -m pytest tests                # all checks
../.venv/Scripts/python -m jaldrishti_ml.replay_suite  # ~13 min rehearsal
```

## Step 2 — Pi server ✅ (folder `server/`)
1. SQLite database: sensor readings, decisions, pump events, zone settings, learning state.
2. Weather service: live Open-Meteo forecast every 30 min (tested live: Lucknow), cached
   for offline use, **"Simulate rain"** switch for the demo.
3. Decision engine: every zone every 30 s → IRRIGATE / POSTPONE / SKIP / WAIT / FAULT /
   NO_SENSOR, each with a reason in English and Hindi.
4. Pulse irrigation: short pulses + soak + re-measure, stops at the target.
5. Pump safety: timed commands, session cap, "tank empty / pipe blocked" lock-out,
   ESP32 heartbeat + offline handling, bad sensor values rejected.
6. Forecasts are checked when they mature → on-device learning per zone.
7. REST API + live WebSocket; manual "Irrigate now", calibration (Dry/Wet buttons),
   crop/soil settings, auto/manual mode.
8. Water saved vs. an honest timer baseline (every zone gets what the thirstiest needed).

Bugs found and fixed while testing:
* The 90 s session cap stopped a fully dry tray short of its target → raised to 150 s.
* Pressing "Dry" during calibration threw away recent readings, so "Wet" failed and
  the AI's 3 h trend stayed on the old scale → readings are now re-scaled exactly.
* The newest sensor reading was dropped (sub-microsecond timestamp rounding) → AI
  forecast missing for 5 min after start-up → fixed in the predictor + regression test.
* A decision could crash the loop if a reading gap hit at the wrong moment → guarded.
* After the ESP32 reconnected, an old "offline" fault stayed up to 30 s → re-decides now.
* "Water saved" first used an invented timer setting (showed 92 %) → replaced with the
  honest baseline above.

## Step 3 — Simulated ESP32 + full-system test ✅
* Virtual ESP32 + 4-zone farm: same physics as the ML, raw ADC sensor values with noise,
  drip delay, water tank, blockable pipes, unpluggable sensors, the ESP32's own
  safety rules. Runs in-process (`--sim`) or as a separate fake ESP32 over real MQTT.
* Automatic tests (fast clock, real AI model): dry zone watered (only that zone),
  simulated rain postpones, empty tank detected + locked + recovers after reset,
  unplugged sensor never waters, ESP32 offline stops everything, manual mode only
  recommends, 48 h unattended run (no wilting, no flooding, learning running).
* Live tests: server + browser API, and server + broker + separate fake ESP32 over MQTT
  (pump command reached the "ESP32" and switched its relay).
* Results: **60 automatic tests pass** (20 ML + 40 server).

Run it yourself (from `server/`):
```
../.venv/Scripts/python -m jaldrishti_server --sim                 # open http://localhost:8000
set JD_PROFILE=demo  (PowerShell: $env:JD_PROFILE="demo")          # faster pulses for a live pitch
../.venv/Scripts/python -m jaldrishti_server --mqtt localhost --broker   # MQTT path, then in a 2nd window:
../.venv/Scripts/python -m jaldrishti_server.simdevice              # the fake ESP32
../.venv/Scripts/python -m pytest tests                            # all server tests (~1.5 min)
```
API docs: http://localhost:8000/docs

## Step 4 — Dashboard ✅ (folder `dashboard/`)
* "Fresh" theme (white + emerald, charcoal dark mode; chosen from 3 options in
  `design/theme-options.png`), farmer-first: 4 crop cards, one status, one sentence,
  one button. Tap a card for the AI forecast chart, AI learning stats, crop/soil settings
  and sensor calibration.
* **28 languages** (22 Indian scheduled languages minus Bodo, Kashmiri, Manipuri, Santali,
  where I could not guarantee quality, plus 9 regional languages of UP/Bihar/Haryana/
  Rajasthan/Chhattisgarh, plus English). RTL for Urdu/Sindhi. Machine-drafted: ask
  native speakers to review.
* Tabs: Farm · History (24 h chart + decision log) · Demo (rain simulation, location,
  virtual-farm controls) · Phone (QR codes).
* Checked by screenshots at desktop and true phone width (Bhojpuri, Urdu, Tamil),
  light + dark.

Run it:
```
cd dashboard
npm install          # once
npm run build        # -> dist/, the Pi server serves it automatically at http://<pi>:8000
npm run dev          # live-editing mode on http://localhost:5173 (server must run on :8000)
```

## Fixes and additions after hardware testing (7 Oct)
* Zones reduced to 2; resistive FC-28 forks (VCC switched on D27, AO on D32/D33, DO unused);
  DHT11 instead of DHT22; ESP32 finds the Pi on the phone hotspot by name (`raspberrypi.local`).
* No calibration needed any more: fixed conversion for the FC-28 (raw 4000 = 0 %, 400 = 100 %);
  readings < 180 (board without power) are shown as "no reading" instead of a fake 100 %.
* Ideal moisture range per crop + soil + region, adjusted daily for hot/cool weather
  (FAO-56 eq. 83), marked on every zone; "Change crop" with a region picker; 23 crops, 9 soils.
* DHT11 now estimates evaporation when the internet is down (FAO-56 Hargreaves).
* Bugs fixed: water use over-counted when "Stop" was pressed mid-pulse; API routes ran in a
  worker thread and clashed with the control loop on the database (sqlite3 InterfaceError on
  the Pi); "Water now" enabled for a zone without a sensor reading; dropdowns overflowing the
  card; "Water soon" badge contradicting "Enough water".

## Step 5 — Raspberry Pi setup ✅
Pi: Raspberry Pi 5, Debian 13 (trixie), Python 3.13, 8 GB. User `iamtanmaybaranwal`, hostname
`raspberrypi`. Laptop login key `~/.ssh/jaldrishti_pi`, short name **`jal-pi`** (`ssh jal-pi`).

**Network plan: everything joins the phone hotspot `TanmayPi`** (Pi, laptop, ESP32, judges'
phones). The Pi keeps internet for live weather and nothing has to be switched on demo day.
The ESP32 finds the Pi by name (`raspberrypi.local`), so a new IP from the hotspot is fine.
(The Pi's own hotspot, `deploy/hotspot.sh on`, stays available as a backup.)
⚠️ The ESP32 only works on **2.4 GHz** Wi-Fi: set the phone hotspot band to 2.4 GHz.

Done ✅
* MQTT broker + auto-start service installed (`deploy/pi_install.sh`, run once with sudo).
* Dashboard: **http://10.135.244.47/** (port 80) - starts on boot, restarts on crash.
* Identical forecasts to the laptop; model loads in 1.4 s, one forecast takes 0.14 s.
* Updates: `bash deploy/deploy.sh` from the laptop (Git Bash) - no password needed.
* Phone tab: Wi-Fi details for the QR code are editable in the dashboard (stored on the Pi);
  the "open dashboard" QR always uses the Pi's IP (phones often can't open `.local`).

## Step 6 — ESP32 firmware (folder `firmware/`, PlatformIO)
**6a ✅ written and tested without hardware:** compiles for the ESP32 DevKit V1 (RAM 14 %,
flash 61 %); 10 unit tests of the safety logic pass on the laptop (`pio test -e native`).
Found + fixed by the tests: fallback mode could run two pumps at once (power limit);
DHT library was not included because of the include order.
**6b ⏳ hardware day:** `pio run -e esp32dev -t upload` with a USB *data* cable, then
the serial console (`read`, `test`) to check the wiring. Details: `firmware/README.md`.

## Step 7 — Wiring + calibration ⏳
Wire sensors, relay, pumps (pumps on their own 5 V supply). On the dashboard: sensor
in dry soil → "Dry", in soaked soil → "Wet". Measure each zone's pump response.

## Step 8 — Mini farm ⏳
Tray split into 2 compartments (~40 × 30 cm each), ~10 cm potting soil, small plants,
one drip line per zone, transparent tank, labels (ZONE 01 — TOMATO, ZONE 02 — WHEAT),
electronics in a small box.

## Step 9 — Real data + learning ⏳
Run the farm for 1–2 weeks before the hackathon: the Pi logs everything and each zone's
learning switches on. Optionally pull the data to the laptop and retrain.

## Step 10 — Demo rehearsal 📋
5-minute script: dry zone → automatic irrigation → Simulate Rain → postponed →
unplug internet → still working → water saved → judges scan the QR code.
Tips: run with `JD_PROFILE=demo`; dry the demo zone to ~35-38 % (not bone dry) so the
refill finishes in ~2-3 min; keep each sensor 3-5 cm from its dripper.
