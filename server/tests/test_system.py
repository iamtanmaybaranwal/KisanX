"""Whole-system tests: controller + real AI model + simulated ESP32/farm + weather,
on a fast clock. These are the demo scenarios, checked automatically."""
import pytest
from conftest import T0, fake_open_meteo

from jaldrishti_server import settings
from jaldrishti_server.controller import Controller
from jaldrishti_server.db import Database
from jaldrishti_server.simdevice import SimFarm, SimLink
from jaldrishti_server.weather_service import WeatherService


class Rig:
    def __init__(self, predictor, init_pct=(55, 55), rain_at=None, tank_l=15.0, mode="auto"):
        self.db = Database(":memory:")
        self.weather = WeatherService(self.db, fetcher=fake_open_meteo(rain_at))
        self.weather.refresh(T0)
        self.farm = SimFarm(T0, init_pct=init_pct, tank_l=tank_l)
        self.link = SimLink(self.farm)
        self.now = T0
        self.ctl = Controller(self.db, self.link, self.weather, predictor=predictor, now=T0)
        self.ctl.set_mode(mode)
        for z in self.ctl.zones.values():      # use the simulated sensors' true calibration
            i = z.zone - 1
            self.ctl.update_zone(z.zone, raw_dry=float(self.farm.raw_dry[i]),
                                 raw_wet=float(self.farm.raw_wet[i]), calibrated=1)

    def run(self, seconds, dt=1.0):
        end = self.now + seconds
        while self.now < end:
            self.now += dt
            self.weather.last_attempt = self.now          # no network in tests
            self.ctl.tick(self.now)

    def zone(self, z):
        return self.ctl.snapshot(self.now)["zones"][z - 1]

    def pumped(self, z=None):
        ev = self.db.pump_events(0)
        return sum(e["seconds"] for e in ev if z is None or e["zone"] == z)

    def thr(self, z):
        """The same weather-adjusted ideal range the controller uses right now."""
        return self.ctl.thresholds(self.ctl.zones[z], self.now)


def warm(rig, hours=3.2):
    """Give the AI its 3 h of history first (zones kept moist)."""
    rig.run(hours * 3600, dt=2.0)


def test_dry_zone_gets_watered_and_only_that_zone(predictor):
    rig = Rig(predictor)
    warm(rig)
    rig.farm.set_moisture(2, 20)                    # Zone 2 dries out (wheat)
    rig.run(30 * 60)
    z2 = rig.zone(2)
    thr = rig.thr(2)
    assert rig.pumped(2) > 0, "zone 2 was not irrigated"
    assert rig.pumped(1) == 0, "the wet zone was irrigated"
    assert z2["moisture"] >= thr["target_pct"] - 2.5
    assert max(rig.farm.true_pct()) < 85, "flooded"
    ends = [d for d in rig.db.decisions(200) if d["action"] == "SESSION_END"]
    assert ends and ends[0]["code"] == "target_reached"
    # the pump response was learned from the pulses
    assert rig.ctl.zones[2].pump.n >= 1


def test_simulated_rain_postpones_irrigation(predictor):
    rig = Rig(predictor)
    warm(rig)
    thr = rig.thr(2)
    rig.weather.simulate("rain", rig.now)
    rig.farm.set_moisture(2, thr["min_pct"] - 5)    # dry, but not critical
    rig.run(10 * 60)
    d = rig.zone(2)["decision"]
    assert d["action"] == "POSTPONE", d
    assert rig.pumped(2) == 0
    # rain cancelled -> the same zone is now irrigated
    rig.weather.simulate(None, rig.now)
    rig.ctl._predict(rig.ctl.zones[2], rig.now)
    rig.run(10 * 60)
    assert rig.pumped(2) > 0


def test_empty_tank_is_detected_and_locks_the_pump(predictor):
    rig = Rig(predictor, tank_l=0.0)
    warm(rig)
    rig.farm.set_moisture(2, 25)
    rig.run(20 * 60)
    z2 = rig.zone(2)
    assert z2["fault"] == "no_rise" and z2["decision"]["action"] == "FAULT"
    pumped = rig.pumped(2)
    assert pumped <= settings.NO_RISE_CHECK_S + settings.PULSE_MAX_S
    rig.run(20 * 60)
    assert rig.pumped(2) == pumped, "pump kept running with an empty tank"
    # farmer refills the tank and resets -> it works again
    rig.farm.tank_l = 10
    rig.ctl.reset_fault(2)
    rig.run(20 * 60)
    assert rig.pumped(2) > pumped and rig.zone(2)["fault"] is None


def test_unplugged_sensor_never_waters(predictor):
    rig = Rig(predictor)
    warm(rig)
    rig.farm.set_moisture(2, 20)
    rig.farm.unplugged[1] = True
    rig.run(10 * 60)
    z2 = rig.zone(2)
    assert z2["decision"]["action"] == "NO_SENSOR" and z2["sensor_error"] == "missing"
    assert rig.pumped(2) == 0


def test_esp32_offline_stops_everything(predictor):
    rig = Rig(predictor)
    warm(rig)
    rig.farm.set_moisture(1, 25)
    rig.run(40)                                      # session starts
    assert rig.ctl.zones[1].session is not None
    rig.link.poll = lambda now: []                   # ESP32 goes silent
    rig.run(60)
    z1 = rig.zone(1)
    assert z1["decision"]["action"] == "FAULT" and z1["decision"]["code"] == "device_offline"
    assert not (rig.ctl.zones[1].session and rig.ctl.zones[1].session.active)
    del rig.link.poll                                 # ESP32 comes back
    rig.run(4)
    assert rig.zone(1)["decision"]["action"] != "FAULT", "stale offline fault after reconnect"


def test_manual_mode_only_recommends(predictor):
    rig = Rig(predictor, mode="manual")
    warm(rig)
    rig.farm.set_moisture(2, 20)
    rig.run(10 * 60)
    assert rig.zone(2)["decision"]["action"] == "IRRIGATE" and rig.pumped() == 0
    rig.ctl.start_session(2, rig.now)               # farmer presses "Irrigate now"
    rig.run(10 * 60)
    assert rig.pumped(2) > 0


def test_two_days_unattended(predictor):
    """48 h of farm time: zones dry out naturally, get watered when needed, no flooding,
    the AI keeps forecasting, learning checks accumulate, water use beats a timer."""
    rig = Rig(predictor, init_pct=(46, 38))
    rig.run(48 * 3600, dt=2.0)
    snap = rig.ctl.snapshot(rig.now)
    for z in snap["zones"]:
        assert z["forecast"] is not None and z["decision"] is not None
        assert z["fault"] is None
        assert z["moisture"] >= rig.thr(z["zone"])["wp_pct"], "a zone was left to wilt"
    assert max(rig.farm.true_pct()) < 85
    assert sum(z["ai"]["learning"]["checks"] for z in snap["zones"]) > 0
    assert snap["water"]["saved_l"] > 0
    assert rig.farm.mode == "remote"                 # heartbeats kept the ESP32 in remote mode
