"""A virtual ESP32 + mini farm (settings.N_ZONES zones), for building and testing without hardware.

Physics: the same FAO-56 soil model the ML was trained on, set up like the
table-top demo (potting soil, 10 cm deep, indoors). Realistic details:
  * sensors report raw 12-bit ADC values (dry ~2900, wet ~1300, each sensor a bit
    different, with noise) - so calibration matters, as on real hardware
  * drip water reaches the sensor with a delay (infiltration buffer)
  * a water tank that can run empty; a pipe that can be blocked; a sensor that can
    be unplugged - to test the fault handling
  * the ESP32's own safety: every pump run is timed and capped at 120 s, and all
    pumps stop if the Pi's heartbeat is missing for 30 s

Use in-process (SimLink) or as a stand-alone fake ESP32 over MQTT:
    python -m jaldrishti_server.simdevice --host localhost
"""
from __future__ import annotations

import argparse
import json
import math
import time

import numpy as np
from jaldrishti_ml import config
from jaldrishti_ml.soil_model import ZoneParams, step

from . import settings
from .link import TOPIC_CMD, TOPIC_HEARTBEAT, TOPIC_SENSORS, TOPIC_STATUS, Link

ZONE_AREA_M2 = 0.10                 # 40 x 25 cm compartment
INFILTRATION_TAU_S = 12.0           # drip water reaches the sensor depth with this lag
HEARTBEAT_TIMEOUT_S = 30.0


class SimFarm:
    def __init__(self, start: float, seed: int = 0, init_pct=(52, 30), tank_l: float = 15.0,
                 flow_l_per_min: float = 1.0, exposure: float = 0.35):
        self.rng = np.random.default_rng(seed)
        soil = config.SOILS["potting_mix"]
        n = settings.N_ZONES
        crops = [config.CROPS[z["crop"]] for z in settings.DEFAULT_ZONES]
        self.prm = ZoneParams(
            theta_wp=np.full(n, soil.theta_wp * 1.04), theta_fc=np.full(n, soil.theta_fc * 0.97),
            theta_sat=np.full(n, soil.theta_sat), drain_tau_h=np.full(n, soil.drain_tau_h * 1.2),
            depth_mm=np.full(n, 100.0), kc=np.array([c.kc_mid for c in crops]),
            p=np.array([c.p for c in crops]), exposure=np.full(n, exposure), rain_capture=np.zeros(n))
        self.theta = self.prm.from_pct(np.array(init_pct, dtype=float))
        self.buffer_mm = np.zeros(n)                      # water on its way down
        self.raw_dry = np.array([2950.0, 2880.0, 3010.0, 2905.0])[:n]   # each real sensor differs a bit
        self.raw_wet = np.array([1350.0, 1280.0, 1405.0, 1310.0])[:n]
        self.flow = flow_l_per_min
        self.tank_l = tank_l
        self.pump_until = np.zeros(n)
        self.blocked = np.zeros(n, dtype=bool)
        self.unplugged = np.zeros(n, dtype=bool)
        self.t = start
        self.boot = start
        self.last_hb = start
        self.mode = "remote"
        self.delivered_l = np.zeros(n)

    # ---- physics ------------------------------------------------------------------
    def true_pct(self) -> np.ndarray:
        return np.clip(self.prm.to_pct(self.theta), 0, 100)

    def advance(self, now: float) -> None:
        while self.t < now:
            dt = min(1.0, now - self.t)
            # ESP32 watchdog
            if self.t - self.last_hb > HEARTBEAT_TIMEOUT_S:
                self.pump_until[:] = 0
                self.mode = "fallback"
            on = self.pump_until > self.t
            flow_l = np.where(on & ~self.blocked, self.flow / 60.0 * dt, 0.0)
            if flow_l.sum() > self.tank_l:                # tank runs dry
                flow_l *= self.tank_l / flow_l.sum() if flow_l.sum() else 0
            self.tank_l = max(self.tank_l - flow_l.sum(), 0.0)
            self.delivered_l += flow_l
            self.buffer_mm += flow_l / ZONE_AREA_M2       # 1 L on 0.1 m2 = 10 mm
            infil = self.buffer_mm * (1 - math.exp(-dt / INFILTRATION_TAU_S))
            self.buffer_mm -= infil
            et0 = _indoor_et0(self.t) * dt / 3600.0
            self.theta, *_ = step(self.theta, self.prm, et0, 0.0, infil, dt / 3600.0)
            self.t += dt

    # ---- the ESP32 side -------------------------------------------------------------------
    def sensor_message(self) -> dict:
        pct = self.true_pct()
        raw = self.raw_dry - (self.raw_dry - self.raw_wet) * pct / 100.0 + self.rng.normal(0, 6, pct.shape)
        hour = _hour(self.t)
        # like the real firmware: an unplugged sensor gives a noisy pin -> sent as null
        return {"raw": [None if u else int(round(r)) for r, u in zip(raw, self.unplugged)],
                "temp": round(29 + 4 * math.sin(math.pi * (hour - 9) / 12) + self.rng.normal(0, 0.2), 1),
                "rh": round(55 - 12 * math.sin(math.pi * (hour - 9) / 12), 1),
                "pumps": [int(u > self.t) for u in self.pump_until],
                "mode": self.mode, "uptime": int(self.t - self.boot), "fw": "sim-1.0"}

    def command(self, msg: dict) -> None:
        if msg.get("all_off"):
            self.pump_until[:] = 0
            return
        z = int(msg["zone"]) - 1
        if not 0 <= z < len(self.pump_until):
            return
        secs = max(0.0, min(float(msg.get("seconds", 0)), settings.DEVICE_MAX_PUMP_S))
        self.pump_until[z] = self.t + secs

    def heartbeat(self, now: float) -> None:
        self.last_hb = now
        self.mode = "remote"

    # ---- demo / test helpers -------------------------------------------------------------
    def set_moisture(self, zone: int, pct: float) -> None:
        self.theta[zone - 1] = float(self.prm.from_pct(np.full(len(self.theta), float(pct)))[zone - 1])
        self.buffer_mm[zone - 1] = 0.0

    def state(self) -> dict:
        return {"true_pct": [round(float(x), 2) for x in self.true_pct()], "tank_l": round(self.tank_l, 2),
                "blocked": self.blocked.tolist(), "unplugged": self.unplugged.tolist(),
                "delivered_l": [round(float(x), 3) for x in self.delivered_l], "mode": self.mode}


def _hour(t: float) -> float:
    return ((t + 19800) % 86400) / 3600.0          # IST hour of day


def _indoor_et0(t: float) -> float:
    """Outdoor-like ET0 shape (mm/h); `exposure` in ZoneParams scales it for indoors."""
    return max(0.0, math.sin(math.pi * (_hour(t) - 6) / 12)) * 0.55 + 0.02


class SimLink(Link):
    """In-process link to a SimFarm (no MQTT needed)."""

    def __init__(self, farm: SimFarm):
        self.farm = farm
        self.last_sent = -1e18

    def poll(self, now: float) -> list[dict]:
        self.farm.advance(now)
        if now - self.last_sent >= settings.SENSOR_PERIOD_S:
            self.last_sent = now
            return [self.farm.sensor_message()]
        return []

    def send_pump(self, zone, seconds, cmd_id):
        self.farm.command({"zone": zone, "seconds": seconds, "id": cmd_id})

    def send_all_off(self):
        self.farm.command({"all_off": True})

    def send_heartbeat(self, now, display=None):
        self.farm.heartbeat(now)


def run_mqtt_device(host: str, port: int, speed: float = 1.0):
    """Stand-alone fake ESP32 talking MQTT, for testing the Pi before hardware arrives."""
    import paho.mqtt.client as mqtt

    farm = SimFarm(time.time())
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jaldrishti-sim-esp32")
    client.will_set(TOPIC_STATUS, "offline", qos=1, retain=True)

    def on_connect(c, u, f, rc, p=None):
        c.subscribe([(TOPIC_CMD, 1), (TOPIC_HEARTBEAT, 0)])
        c.publish(TOPIC_STATUS, "online", qos=1, retain=True)

    def on_message(c, u, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        if msg.topic == TOPIC_CMD:
            farm.command(data)
        elif msg.topic == TOPIC_HEARTBEAT:
            farm.heartbeat(farm.t)

    client.on_connect, client.on_message = on_connect, on_message
    client.connect(host, port, keepalive=20)
    client.loop_start()
    print(f"fake ESP32 connected to {host}:{port} - Ctrl+C to stop")
    try:
        while True:
            farm.advance(farm.t + settings.SENSOR_PERIOD_S * speed)
            client.publish(TOPIC_SENSORS, json.dumps(farm.sensor_message()), qos=0)
            time.sleep(settings.SENSOR_PERIOD_S)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(TOPIC_STATUS, "offline", qos=1, retain=True)
        client.loop_stop()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fake ESP32 + mini farm over MQTT")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()
    run_mqtt_device(args.host, args.port)
