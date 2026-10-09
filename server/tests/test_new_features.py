"""Ideal range adapted to the weather, DHT11-based evaporation offline, honest water accounting."""
import pytest
from conftest import T0, fake_open_meteo
from test_system import Rig, warm

from jaldrishti_ml import config
from jaldrishti_server.db import Database
from jaldrishti_server.weather_service import (DAY_SHAPE, WeatherService, extraterrestrial_radiation,
                                               hargreaves_et0_day)


def test_ra_matches_fao56_example_8():
    # FAO-56 Example 8: 20 deg S on 3 September (day 246) -> Ra = 32.2 MJ m-2 day-1
    assert extraterrestrial_radiation(-20.0, 246) == pytest.approx(32.2, abs=0.2)


def test_hargreaves_is_sensible():
    lat, doy = 26.85, 120                       # Lucknow, end of April
    hot = hargreaves_et0_day(26, 42, lat, doy)
    mild = hargreaves_et0_day(14, 26, lat, doy)
    assert 5 < hot < 9 and hot > mild > 0        # pre-monsoon UP: several mm/day
    assert hargreaves_et0_day(30, 30, lat, doy) == 0.0      # no daily range -> no estimate
    assert sum(DAY_SHAPE) == pytest.approx(1.0)


def test_offline_forecast_uses_dht11_climate():
    db = Database(":memory:")
    w = WeatherService(db, fetcher=lambda lat, lon: (_ for _ in ()).throw(OSError("offline")))
    assert not w.refresh(T0)                     # no internet, no cache
    w.local_climate = lambda: (24.0, 38.0)       # DHT11: min / max of the last 24 h
    fc = w.forecast(T0)
    assert len(fc.et0_mm) >= 36 and sum(fc.rain_mm) == 0
    day = hargreaves_et0_day(24, 38, w.location["lat"], 105)       # T0 = 15 April = day 105
    assert sum(fc.et0_mm[:24]) == pytest.approx(day, rel=0.03)
    w.local_climate = None                       # no DHT11 either -> typical clear-day shape
    w._climate_memo = None
    assert sum(WeatherService(db).forecast(T0).et0_mm[:24]) > 0


def test_ideal_range_adapts_to_hot_days():
    cool = config.thresholds("loam", "potato", etc_mm_day=2.0)
    hot = config.thresholds("loam", "potato", etc_mm_day=8.0)
    base = config.thresholds("loam", "potato")
    assert hot["min_pct"] > base["min_pct"] > cool["min_pct"]     # water sooner on hot days
    assert hot["target_pct"] == base["target_pct"] == cool["target_pct"]
    assert config.depletion_fraction("spinach", 12.0) == pytest.approx(0.10)   # FAO lower limit


def test_controller_reports_weather_adjusted_range(predictor):
    rig = Rig(predictor)
    warm(rig, hours=0.2)
    z = rig.zone(1)
    assert "etc_mm_day" in z["thresholds"] and z["thresholds"]["etc_mm_day"] > 0
    expect = config.thresholds(z["soil"], z["crop"], z["thresholds"]["etc_mm_day"])
    assert z["thresholds"]["min_pct"] == pytest.approx(expect["min_pct"], abs=0.05)   # etc is rounded


def test_stopping_early_records_only_the_real_pump_time(predictor):
    rig = Rig(predictor, mode="manual")
    warm(rig, hours=0.2)
    rig.farm.set_moisture(1, 20)
    rig.run(40)
    rig.ctl.start_session(1, rig.now)           # first pulse is commanded for several seconds
    commanded = rig.db.pump_events(0)[-1]["seconds"]
    assert commanded >= 5
    rig.run(3)
    rig.ctl.stop_session(1, rig.now)
    ev = rig.db.pump_events(0)[-1]
    assert ev["seconds"] == pytest.approx(3, abs=1.1) and ev["note"] == "stopped early"
    assert ev["liters"] == pytest.approx(ev["seconds"] * 1.0 / 60, rel=0.01)


def test_heartbeat_carries_what_the_lcd_shows(predictor):
    import json
    rig = Rig(predictor)
    warm(rig, hours=0.2)
    rig.ctl.update_zone(2, crop="millet")
    v = rig.ctl.lcd_view(rig.now)
    z1 = rig.zone(1)
    assert v["mode"] == "auto" and len(v["z"]) == 2
    assert v["z"][0]["m"] == round(z1["moisture"])                        # same number as the dashboard
    assert v["z"][0]["lo"] == round(z1["thresholds"]["min_pct"]) and v["z"][0]["hi"] > v["z"][0]["lo"]
    assert v["z"][1]["c"] == "Bajra" and v["z"][0]["f"] is False         # "Pearl millet (bajra)" -> "Bajra"
    assert len(json.dumps({"t": round(rig.now), **v})) < 300                 # ESP32 MQTT buffer is 512 B
    rig.ctl.update_zone(2, crop="wheat")
