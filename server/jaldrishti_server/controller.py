"""The control loop: sensors -> AI forecast -> decision -> pulse irrigation -> learning.

`Controller.tick(now)` does everything and is called every ~0.5 s by the server
(or as fast as a test wants, with a simulated clock - so whole days of farm
time can be tested in seconds).
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import deque
from datetime import datetime

from jaldrishti_ml import config
from jaldrishti_ml.adaptation import PumpResponse, ZoneAdapter
from jaldrishti_ml.predictor import IST, MoisturePredictor, ZoneSetup, hours_until

from . import irrigation, settings
from .decision import ZoneInputs, decide

log = logging.getLogger("jaldrishti")


class ZoneRuntime:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.raw_window: deque = deque()                    # (ts, raw, pct) last 30 s
        self.buffer: deque = deque(maxlen=4 * 360 + 30)     # (ts, pct) 10-s medians, ~4 h
        self.last_valid_ts: float | None = None
        self.sensor_error: str | None = None
        self.last_raw: float | None = None
        self.forecast: dict | None = None
        self.forecast_error: str | None = None
        self.adapter = ZoneAdapter()
        self.pump = PumpResponse(prior_gain=0.3)
        self.session: irrigation.Session | None = None
        self.cooldown_until = 0.0
        self.fault: str | None = None
        self.decision: dict | None = None
        self.last_logged: tuple = (None, 0.0)
        self.pending: list[dict] = []
        self.water_events: deque = deque(maxlen=500)       # pump pulse times
        self.pump_on = False
        self.last_pulse: tuple | None = None                 # (rowid, start, seconds, flow)

    @property
    def zone(self) -> int:
        return self.cfg["zone"]

    def setup(self) -> ZoneSetup:
        c = self.cfg
        return ZoneSetup(c["soil"], c["crop"], c["stage"], float(c["depth_mm"]))

    def to_pct(self, raw: float) -> float:
        dry, wet = self.cfg["raw_dry"], self.cfg["raw_wet"]
        return float(min(max((dry - raw) / (dry - wet) * 100.0, 0.0), 100.0))

    def recent(self, now: float, seconds: float) -> float | None:
        vals = [p for t, _, p in self.raw_window if t > now - seconds]
        return statistics.median(vals) if vals else None

    def recent_raw(self, now: float, seconds: float = 10) -> float | None:
        vals = [r for t, r, _ in self.raw_window if t > now - seconds]
        return statistics.median(vals) if vals else None


class Controller:
    def __init__(self, db, link, weather, predictor: MoisturePredictor | None = None, now: float = 0.0):
        self.db = db
        self.link = link
        self.weather = weather
        self.predictor = predictor or MoisturePredictor()
        self.zones = {c["zone"]: ZoneRuntime(c) for c in db.zones()}
        self.mode = db.get("mode", "auto")
        self.device = {"last_msg": None, "mode": None, "uptime": None, "fw": None, "status": None,
                       "temp": None, "rh": None}
        self.started = now
        self.cmd_id = 0
        self._was_online = False
        self.weather.local_climate = self._local_climate
        self._last = {k: -1e18 for k in ("hb", "store", "predict", "decide", "learn", "save", "env", "prune")}
        self._load_state()

    # ===== persistence ===================================================================
    def _load_state(self):
        for z in self.zones.values():
            st = self.db.get(f"learn_zone_{z.zone}")
            if st:
                z.adapter = ZoneAdapter.from_dict(st["adapter"])
                z.pump = PumpResponse.from_dict(st["pump"])
            z.fault = self.db.get(f"fault_zone_{z.zone}")

    def save_state(self):
        for z in self.zones.values():
            self.db.put(f"learn_zone_{z.zone}", {"adapter": z.adapter.to_dict(), "pump": z.pump.to_dict()})

    # ===== main loop ========================================================================
    def tick(self, now: float) -> None:
        for msg in self.link.poll(now):
            self._on_message(msg, now)
        if now - self._last["hb"] >= settings.HEARTBEAT_PERIOD_S:
            self._last["hb"] = now
            try:
                display = self.lcd_view(now)
            except Exception:                     # the heartbeat itself must never fail
                log.exception("LCD view failed")
                display = None
            self.link.send_heartbeat(now, display)

        online = self.device_online(now)
        for z in self.zones.values():
            self._run_session(z, now, online)

        if now - self._last["store"] >= settings.STORE_PERIOD_S:
            self._last["store"] = now
            self._store(now)
        if now - self._last["predict"] >= settings.PREDICT_PERIOD_S:
            self._last["predict"] = now
            for z in self.zones.values():
                self._predict(z, now)
        if online != self._was_online:              # ESP32 (re)connected or lost: re-decide now
            self._was_online = online
            self._last["decide"] = -1e18
        if now - self._last["decide"] >= settings.DECISION_PERIOD_S:
            self._last["decide"] = now
            for z in self.zones.values():
                self._decide(z, now, online)
        if now - self._last["learn"] >= settings.LEARN_LOG_PERIOD_S:
            self._last["learn"] = now
            for z in self.zones.values():
                self._learn(z, now)
        if now - self._last["save"] >= settings.STATE_SAVE_PERIOD_S:
            self._last["save"] = now
            self.save_state()
        if now - self._last["prune"] >= 86400:
            self._last["prune"] = now
            self.db.prune(now)

    # ===== sensors ==============================================================================
    def _on_message(self, msg: dict, now: float) -> None:
        if "status" in msg:
            self.device["status"] = msg["status"]
            return
        raws = msg.get("raw")
        if not isinstance(raws, list):
            return
        self.device.update(last_msg=now, mode=msg.get("mode"), uptime=msg.get("uptime"), fw=msg.get("fw"),
                           status="online", temp=msg.get("temp"), rh=msg.get("rh"))
        pumps = msg.get("pumps") or []
        for i, z in enumerate(self.zones.values()):
            z.pump_on = bool(pumps[i]) if i < len(pumps) else False
            raw = raws[i] if i < len(raws) else None
            if not isinstance(raw, (int, float)) or not settings.RAW_VALID[0] <= raw <= settings.RAW_VALID[1]:
                z.sensor_error = "out_of_range" if raw is not None else "missing"
                continue
            z.sensor_error = None
            z.last_raw = float(raw)
            z.last_valid_ts = now
            z.raw_window.append((now, float(raw), z.to_pct(raw)))
            while z.raw_window and z.raw_window[0][0] < now - 30:
                z.raw_window.popleft()
        if now - self._last["env"] >= 60 and msg.get("temp") is not None:
            self._last["env"] = now
            self.db.add_env(now, msg.get("temp"), msg.get("rh"))

    def _local_climate(self):
        """(tmin, tmax) over the last 24 h from the DHT11, if it has run for 12 h+."""
        last = self.device.get("last_msg")                  # ~ "now" (also on a test clock)
        if last is None:
            return None
        rows = [r for r in self.db.env_history(last - 86400) if r["temp_c"] is not None]
        if len(rows) < 2 or rows[-1]["ts"] - rows[0]["ts"] < 12 * 3600:
            return None
        temps = [r["temp_c"] for r in rows]
        return min(temps), max(temps)

    def device_online(self, now: float) -> bool:
        last = self.device["last_msg"]
        return last is not None and now - last < settings.DEVICE_STALE_S

    def sensor_ok(self, z: ZoneRuntime, now: float) -> bool:
        return z.last_valid_ts is not None and now - z.last_valid_ts < settings.SENSOR_STALE_S

    def moisture(self, z: ZoneRuntime, now: float, seconds: float = 30) -> float | None:
        return z.recent(now, seconds) if self.sensor_ok(z, now) else None

    def _store(self, now: float) -> None:
        rows = []
        for z in self.zones.values():
            m = z.recent(now, settings.STORE_PERIOD_S)
            if m is not None and self.sensor_ok(z, now):
                z.buffer.append((now, m))
                rows.append((now, z.zone, z.recent_raw(now), round(m, 3)))
        if rows:
            self.db.add_readings(rows)

    # ===== AI forecast ===========================================================================
    def _hours_since_water(self, z: ZoneRuntime, now: float) -> float:
        events = list(z.water_events)
        if z.cfg.get("rain_exposed"):
            events += self.weather.observed_rain_events(now - 2 * 86400, now)
        past = [t for t in events if t <= now]
        return config.HOURS_SINCE_WATER_CAP if not past else (now - max(past)) / 3600.0

    def _predict(self, z: ZoneRuntime, now: float) -> None:
        if not self.sensor_ok(z, now) or not z.buffer:
            z.forecast, z.forecast_error = None, "no sensor data"
            return
        try:
            fc = self.weather.forecast(now)
            out = self.predictor.predict(z.setup(), list(z.buffer), datetime.fromtimestamp(now, IST),
                                         self._hours_since_water(z, now), fc)
        except (ValueError, KeyError) as exc:
            z.forecast, z.forecast_error = None, str(exc)
            return
        # on-device learning correction (dry forecasts only - see adaptation.py)
        for hz in out["horizons"]:
            hz["raw_delta"] = hz["delta"]
            used = z.adapter.adjust(hz["h"], hz["delta"], fc_rain_mm=hz["fc_rain_mm"])
            m = min(max(out["m_now"] + used, 0.0), 100.0)
            shift = m - hz["moisture"]
            hz["delta"], hz["moisture"] = round(m - out["m_now"], 3), round(m, 2)
            hz["low"] = round(min(max(hz["low"] + shift, 0.0), 100.0), 2)
            hz["high"] = round(min(max(hz["high"] + shift, 0.0), 100.0), 2)
        out["thresholds"] = self.thresholds(z, now)
        out["hours_until_min"] = hours_until(out["m_now"], out["horizons"], out["thresholds"]["min_pct"])
        out["made_at"] = now
        z.forecast, z.forecast_error = out, None

    def thresholds(self, z: ZoneRuntime, now: float) -> dict:
        """The zone's ideal range, adjusted for today's crop water use (FAO-56 eq. 83)."""
        etc = self.weather.et0_next_24h(now) * config.CROPS[z.cfg["crop"]].kc(z.cfg["stage"])
        thr = config.thresholds(z.cfg["soil"], z.cfg["crop"], etc)
        thr["etc_mm_day"] = round(etc, 2)
        return thr

    # ===== decisions ===============================================================================
    def _decide(self, z: ZoneRuntime, now: float, online: bool) -> None:
        if not z.cfg.get("enabled", 1):
            z.decision = {"action": "OFF", "code": "disabled", "seconds": 0, "details": {},
                          "reason_en": "Zone disabled.", "reason_hi": "ज़ोन बंद है।"}
            return
        nxt = self.weather.next_hours(now, 6)
        inputs = ZoneInputs(
            m_now=self.moisture(z, now), thresholds=self.thresholds(z, now), forecast=z.forecast,
            fc_rain_6h=nxt["rain_mm"], fc_prob_6h=nxt["prob_max"],
            fault=z.fault or (None if online else "device_offline"),
            session_active=bool(z.session and z.session.active),
            cooling_down=now < z.cooldown_until, pump_gain=z.pump.gain,
            flow_l_per_min=z.cfg.get("flow_l_per_min") or settings.DEFAULT_FLOW_L_PER_MIN)
        d = decide(inputs).as_dict()
        d["auto"] = self.mode == "auto"
        z.decision = d
        key = (d["action"], d["code"])
        if key != z.last_logged[0] or now - z.last_logged[1] > 600:
            z.last_logged = (key, now)
            self.db.add_decision(now, z.zone, d)
        if d["action"] == "IRRIGATE" and self.mode == "auto" and online:
            try:
                self.start_session(z.zone, now, target=d["details"]["goal_pct"], planned_s=d["seconds"],
                                   source="auto")
            except RuntimeError as exc:            # e.g. reading gap - retry next round
                d["details"]["start_error"] = str(exc)

    # ===== irrigation sessions ==============================================================================
    def start_session(self, zone: int, now: float, target: float | None = None,
                      planned_s: float | None = None, source: str = "manual") -> dict:
        z = self.zones[zone]
        if z.fault:
            raise RuntimeError(f"zone {zone} has a fault: {z.fault} - reset it first")
        if not self.device_online(now):
            raise RuntimeError("ESP32 is offline")
        m = self.moisture(z, now, 10)
        if m is None:
            raise RuntimeError("no valid sensor reading")
        if z.session and z.session.active:
            return {"started": False, "reason": "already watering"}
        thr = self.thresholds(z, now)
        target = thr["target_pct"] if target is None else target
        if planned_s is None:
            planned_s = min(max(target - m, 0) / max(z.pump.gain, 1e-3), settings.MAX_SESSION_PUMP_S)
        z.session = irrigation.Session(zone=zone, target_pct=float(target), m_start=m,
                                       planned_s=float(max(planned_s, 4.0)), source=source)
        self._run_session(z, now, True)
        return {"started": True, "target_pct": round(target, 1), "planned_s": round(planned_s, 1)}

    def stop_session(self, zone: int, now: float) -> None:
        z = self.zones[zone]
        self.link.send_pump(zone, 0, self._next_id())
        self._cut_pulse(z, now)
        if z.session and z.session.active:
            z.session.state, z.session.end_reason = "DONE", "stopped_by_user"
            self._finish_session(z, now)

    def _cut_pulse(self, z: ZoneRuntime, now: float) -> None:
        """If a pulse was still running, shorten its pump event to the real pumped time."""
        if z.last_pulse is None:
            return
        rowid, start, seconds, flow = z.last_pulse
        z.last_pulse = None
        ran = max(0.0, min(seconds, now - start))
        if ran < seconds:
            self.db.shorten_pump_event(rowid, round(ran, 1), ran * flow / 60.0)

    def _next_id(self) -> int:
        self.cmd_id += 1
        return self.cmd_id

    def _run_session(self, z: ZoneRuntime, now: float, online: bool) -> None:
        s = z.session
        if not s or not s.active:
            return
        if not online:
            s.state, s.end_reason = "DONE", "device_offline"
            self._cut_pulse(z, now)
            self._finish_session(z, now)
            return
        flow = z.cfg.get("flow_l_per_min") or settings.DEFAULT_FLOW_L_PER_MIN

        def send(zone, seconds):
            self.link.send_pump(zone, seconds, self._next_id())
            z.water_events.append(now)
            rowid = self.db.add_pump_event(now, zone, seconds, seconds * flow / 60.0, s.source)
            z.last_pulse = (rowid, now, seconds, flow)

        irrigation.step(s, now, self.moisture(z, now, 10), z.pump.gain, send, z.pump.update)
        if not s.active:
            self._finish_session(z, now)

    def _finish_session(self, z: ZoneRuntime, now: float) -> None:
        s = z.session
        z.cooldown_until = now + settings.COOLDOWN_S
        if s.state == "FAULT" and s.end_reason == "no_rise":
            z.fault = "no_rise"
            self.db.put(f"fault_zone_{z.zone}", z.fault)
            self.link.send_pump(z.zone, 0, self._next_id())
        self.db.add_decision(now, z.zone, {
            "action": "SESSION_END", "code": s.end_reason, "seconds": s.pumped_s,
            "reason_en": f"Irrigation finished: {s.end_reason.replace('_', ' ')} "
                         f"({s.pulses} pulses, {s.pumped_s:.0f} s).",
            "reason_hi": f"सिंचाई समाप्त: {s.pulses} बार, {s.pumped_s:.0f} सेकंड।",
            "details": {"log": s.log[-20:], "m_start": s.m_start, "target": s.target_pct}})
        self._predict(z, now)

    def reset_fault(self, zone: int) -> None:
        z = self.zones[zone]
        z.fault = None
        self.db.put(f"fault_zone_{zone}", None)

    # ===== on-device learning =========================================================================
    def _learn(self, z: ZoneRuntime, now: float) -> None:
        keep = []
        for p in z.pending:
            if p["due"] > now:
                keep.append(p)
                continue
            vals = [m for t, m in z.buffer if t > now - 900]
            if not vals:
                continue
            obs = statistics.median(vals) - p["m_now"]
            watered = any(p["issued"] <= t <= p["due"] for t in z.water_events)
            rained = z.cfg.get("rain_exposed") and self.weather.observed_rain_events(p["issued"], p["due"] + 3600)
            if not watered and not rained:
                z.adapter.update(p["h"], p["raw_delta"], obs, fc_rain_mm=p["fc_rain"])
        z.pending = keep
        if z.forecast and now - z.forecast["made_at"] < settings.PREDICT_PERIOD_S * 2:
            for hz in z.forecast["horizons"]:
                z.pending.append({"h": hz["h"], "issued": now, "due": now + hz["h"] * 3600,
                                  "m_now": z.forecast["m_now"], "raw_delta": hz["raw_delta"],
                                  "fc_rain": hz["fc_rain_mm"]})

    # ===== settings from the dashboard ===============================================================
    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual"):
            raise ValueError("mode must be auto or manual")
        self.mode = mode
        self.db.put("mode", mode)

    def update_zone(self, zone: int, **fields) -> dict:
        z = self.zones[zone]
        if "crop" in fields and fields["crop"] not in config.CROPS:
            raise ValueError("unknown crop")
        if "soil" in fields and fields["soil"] not in config.SOILS:
            raise ValueError("unknown soil")
        if "stage" in fields and fields["stage"] not in config.STAGES:
            raise ValueError("unknown stage")
        if "depth_mm" in fields and not 30 <= float(fields["depth_mm"]) <= 1000:
            raise ValueError("depth must be 30-1000 mm")
        if "flow_l_per_min" in fields and not 0.05 <= float(fields["flow_l_per_min"]) <= 20:
            raise ValueError("flow must be 0.05-20 L/min")
        self.db.update_zone(zone, **fields)
        z.cfg = next(c for c in self.db.zones() if c["zone"] == zone)
        if {"crop", "soil", "stage", "depth_mm"} & set(fields):
            z.adapter = ZoneAdapter()              # new situation -> learn afresh
            z.pending = []
        return z.cfg

    def calibrate(self, zone: int, point: str, now: float) -> dict:
        z = self.zones[zone]
        raw = z.recent_raw(now, 10)
        if raw is None:
            raise RuntimeError("no sensor reading to calibrate with")
        if point not in ("dry", "wet"):
            raise ValueError("point must be dry or wet")
        cal = self.db.get(f"cal_zone_{zone}", {"dry": False, "wet": False})
        cal[point] = True
        new = {"raw_dry": z.cfg["raw_dry"], "raw_wet": z.cfg["raw_wet"]}
        new[f"raw_{point}"] = round(raw, 1)
        if new["raw_dry"] - new["raw_wet"] < 300:
            raise ValueError(f"dry ({new['raw_dry']:.0f}) must read at least 300 higher than wet "
                             f"({new['raw_wet']:.0f}) - is the sensor in the right soil?")
        self.db.put(f"cal_zone_{zone}", cal)
        old_dry, old_wet = z.cfg["raw_dry"], z.cfg["raw_wet"]
        self.update_zone(zone, **new, calibrated=int(cal["dry"] and cal["wet"]))
        # re-express recent history on the new scale (exact: calibration is linear), so the
        # AI's trend inputs stay consistent and the next calibration point can follow at once
        z.raw_window = deque((t, r, z.to_pct(r)) for t, r, _ in z.raw_window)

        def rescale(p):
            r = old_dry - p / 100.0 * (old_dry - old_wet)
            return z.to_pct(r)
        z.buffer = deque(((t, rescale(p)) for t, p in z.buffer), maxlen=z.buffer.maxlen)
        return {"zone": zone, "point": point, "raw": round(raw, 1), **new, "calibrated": cal["dry"] and cal["wet"]}

    # ===== dashboard snapshot ===========================================================================
    def water_summary(self, now: float) -> dict:
        """Water used, and the saving versus a conventional timer.

        A timer cannot tell zones apart, so to keep every crop healthy it must give
        EVERY zone what the thirstiest zone needed. Baseline = that amount x number
        of zones. No invented timer settings - only what this farm actually needed."""
        day_start = datetime.fromtimestamp(now, IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = self.db.water_used(day_start)
        first = self.db.first_ts() or now
        total = self.db.water_used(first)
        used = sum(total.values())
        thirstiest = max(total.values(), default=0.0)
        baseline = thirstiest * len(self.zones)
        return {"today_l": round(sum(today.values()), 2),
                "today_by_zone": {str(k): round(v, 2) for k, v in today.items()},
                "since_start_l": round(used, 2),
                "by_zone_l": {str(k): round(v, 2) for k, v in total.items()},
                "timer_baseline_l": round(baseline, 2),
                "saved_l": round(baseline - used, 2),
                "saved_pct": round(100 * (1 - used / baseline), 1) if baseline > 0 else None,
                "days": round(max(now - first, 0) / 86400.0, 2),
                "baseline_note": "vs. a timer that must give every zone what the thirstiest zone needed"}

    def lcd_view(self, now: float) -> dict:
        """What the ESP32's 16x2 LCD shows, sent inside the heartbeat (small: MQTT buffer 512 B)."""
        zones = []
        for z in self.zones.values():
            thr = self.thresholds(z, now)
            m = self.moisture(z, now) if self.sensor_ok(z, now) else None
            label = config.CROPS[z.cfg["crop"]].label            # "Pearl millet (bajra)" -> "Bajra"
            short = label.split("(")[1].rstrip(")").title() if "(" in label else label
            zones.append({"c": short[:10], "m": None if m is None else round(m),
                          "lo": round(thr["min_pct"]), "hi": round(thr["target_pct"]),
                          "f": bool(z.fault)})
        return {"mode": self.mode, "z": zones}

    def snapshot(self, now: float) -> dict:
        zones = []
        for z in self.zones.values():
            thr = self.thresholds(z, now)
            m = self.moisture(z, now)
            status = "unknown"
            if z.fault:
                status = "fault"
            elif m is not None:
                until = z.forecast.get("hours_until_min") if z.forecast else None
                status = ("dry" if m < thr["min_pct"] else
                          "soon" if until is not None and until <= 6 and m < thr["target_pct"] else "ok")
            s = z.session
            zones.append({
                "zone": z.zone, "name": z.cfg["name"], "crop": z.cfg["crop"],
                "crop_label": config.CROPS[z.cfg["crop"]].label, "stage": z.cfg["stage"],
                "soil": z.cfg["soil"], "depth_mm": z.cfg["depth_mm"],
                "calibrated": bool(z.cfg["calibrated"]), "enabled": bool(z.cfg["enabled"]),
                "flow_l_per_min": z.cfg["flow_l_per_min"], "rain_exposed": bool(z.cfg["rain_exposed"]),
                "moisture": None if m is None else round(m, 1), "raw": z.last_raw,
                "sensor_error": z.sensor_error if not self.sensor_ok(z, now) else None,
                "thresholds": thr, "status": status, "pump_on": z.pump_on, "fault": z.fault,
                "decision": z.decision, "forecast": z.forecast, "forecast_error": z.forecast_error,
                "session": None if not (s and s.active) else {
                    "state": s.state, "pumped_s": round(s.pumped_s, 1), "pulses": s.pulses,
                    "target_pct": round(s.target_pct, 1), "source": s.source},
                "cooldown_s": max(0, round(z.cooldown_until - now)),
                "ai": {"learning": z.adapter.stats(6), "pump_gain_pct_per_s": round(z.pump.gain, 3),
                       "pump_samples": z.pump.n},
            })
        return {
            "time": now, "time_ist": datetime.fromtimestamp(now, IST).isoformat(timespec="seconds"),
            "mode": self.mode,
            "device": {**self.device, "online": self.device_online(now), "link": self.link.kind},
            "weather": self.weather.summary(now),
            "water": self.water_summary(now),
            "zones": zones,
            "model": {"version": self.predictor.bundle.get("package_version"),
                      "trained": self.predictor.bundle.get("created_utc"),
                      "horizons": self.predictor.horizons},
            "uptime_s": round(now - self.started),
        }
