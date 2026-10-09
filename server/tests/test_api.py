import json
import math

import pytest
from fastapi.testclient import TestClient
from test_system import Rig, warm

from jaldrishti_server.app import create_app


@pytest.fixture(scope="module")
def rig(predictor):
    r = Rig(predictor)
    warm(r, hours=3.1)
    return r


@pytest.fixture(scope="module")
def client(rig):
    app = create_app(rig.ctl, clock=lambda: rig.now, run_loops=False)
    with TestClient(app) as c:
        yield c


def no_nan(obj):
    """The dashboard needs strict JSON - no NaN/Infinity anywhere."""
    if isinstance(obj, float):
        assert math.isfinite(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            no_nan(v)
    elif isinstance(obj, list):
        for v in obj:
            no_nan(v)


def test_state_is_complete_and_strict_json(client):
    s = client.get("/api/state").json()
    no_nan(s)
    json.dumps(s, allow_nan=False)
    assert len(s["zones"]) == 2 and s["device"]["online"] and s["mode"] == "auto"
    z = s["zones"][0]
    for key in ("moisture", "thresholds", "status", "decision", "forecast", "ai", "crop_label"):
        assert key in z
    assert [h["h"] for h in z["forecast"]["horizons"]] == [1, 3, 6, 12]
    assert s["weather"]["source"] in ("live", "cached", "simulated")


def test_history_and_decisions(client):
    h = client.get("/api/history?hours=2").json()
    assert set(h["zones"]) == {"1", "2"} and len(h["zones"]["1"]) > 10
    assert client.get("/api/decisions?limit=5").json()


def test_options(client):
    o = client.get("/api/options").json()
    assert "wheat" in o["crops"] and "rain" in o["scenarios"] and o["simulated_device"]


def test_zone_config_validation(client, rig):
    assert client.post("/api/zones/2/config", json={"crop": "moon_cactus"}).status_code == 400
    assert client.post("/api/zones/2/config", json={"depth_mm": 5}).status_code == 400
    assert client.post("/api/zones/9/config", json={"crop": "wheat"}).status_code == 404
    r = client.post("/api/zones/2/config", json={"crop": "mustard", "name": "Sarson"})
    assert r.status_code == 200 and r.json()["crop"] == "mustard"
    client.post("/api/zones/2/config", json={"crop": "wheat", "name": "Zone 02"})


def test_manual_irrigate_and_stop(client, rig):
    r = client.post("/api/zones/1/irrigate", json={"target_pct": 70})
    assert r.status_code == 200 and r.json()["started"]
    rig.run(3)
    assert rig.ctl.zones[1].session.active
    assert client.post("/api/zones/1/stop").json()["stopped"]
    assert not rig.ctl.zones[1].session.active
    assert client.post("/api/zones/1/irrigate", json={"seconds": 9999}).status_code == 422


def test_simulate_rain_endpoint(client, rig):
    s = client.post("/api/weather/simulate?scenario=rain").json()
    assert s["source"] == "simulated" and s["rain_next_6h_mm"] > 10
    assert client.post("/api/weather/simulate?scenario=tornado").status_code == 400
    s = client.post("/api/weather/simulate").json()
    assert s["simulation"] is None


def test_mode_switch(client):
    assert client.post("/api/mode/manual").json()["mode"] == "manual"
    assert client.post("/api/mode/banana").status_code == 400
    assert client.post("/api/mode/auto").json()["mode"] == "auto"


def test_calibration(client, rig):
    before = dict(rig.ctl.zones[2].cfg)
    rig.farm.set_moisture(2, 3)                       # sensor in dry soil
    rig.run(12)
    r = client.post("/api/zones/2/calibrate/dry").json()
    assert abs(r["raw_dry"] - rig.farm.raw_dry[1]) < 80
    assert client.post("/api/zones/2/calibrate/mud").status_code == 400
    # wet point too close to dry -> refused with a clear message
    bad = client.post("/api/zones/2/calibrate/wet")
    assert bad.status_code == 400 and "300" in bad.json()["detail"]
    # the proper flow: move the sensor into soaked soil, then press "Wet"
    rig.farm.set_moisture(2, 100)
    rig.run(12)
    r = client.post("/api/zones/2/calibrate/wet").json()
    assert abs(r["raw_wet"] - rig.farm.raw_wet[1]) < 80 and r["calibrated"]
    rig.run(12)
    assert abs(client.get("/api/state").json()["zones"][1]["moisture"] - 100) < 4
    rig.ctl.update_zone(2, raw_dry=before["raw_dry"], raw_wet=before["raw_wet"], calibrated=1)
    rig.farm.set_moisture(2, 55)


def test_sim_endpoints_and_websocket(client, rig):
    assert client.post("/api/sim/tank/7").json()["tank_l"] == 7
    assert client.post("/api/sim/zones/2/blocked/true").json()["blocked"][1] is True
    client.post("/api/sim/zones/2/blocked/false")
    with client.websocket_connect("/ws") as ws:
        data = ws.receive_json()
        assert len(data["zones"]) == 2


def test_share_wifi_and_server_ip(client):
    o = client.get("/api/options").json()
    assert o["share_wifi"]["ssid"] and "server_ip" in o
    assert client.post("/api/share/wifi", json={"ssid": "TanmayPi", "password": "secret123"}).json()["ssid"] == "TanmayPi"
    assert client.get("/api/options").json()["share_wifi"] == {"ssid": "TanmayPi", "password": "secret123"}
    assert client.post("/api/share/wifi", json={"ssid": ""}).status_code == 422


def test_root_page(client):
    assert client.get("/").status_code == 200


def test_every_route_runs_on_the_event_loop(client):
    """Plain `def` routes run in a worker thread and share the SQLite connection across
    threads (seen on the Pi: sqlite3.InterfaceError). Every route must be async."""
    import inspect
    from fastapi.routing import APIRoute, APIWebSocketRoute
    routes = [r for r in client.app.routes if isinstance(r, (APIRoute, APIWebSocketRoute))]
    assert len(routes) > 15
    sync = [r.path for r in routes if not inspect.iscoroutinefunction(r.endpoint)]
    assert sync == [], f"routes that would run in a thread: {sync}"
