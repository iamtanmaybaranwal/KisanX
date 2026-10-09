"""Web server: REST API + live WebSocket + the dashboard files.

Everything runs in ONE asyncio event loop (control loop, API, WebSocket), so the
controller and the database are only ever touched from one thread. Every route is
`async def` ON PURPOSE: FastAPI runs plain `def` routes in a worker thread, and
sharing the SQLite connection across threads caused "InterfaceError: bad parameter
or other API misuse" (tests/test_api.py guards this). The only other thread is the
weather download, which does not touch the database.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import settings
from .controller import Controller
from .simdevice import SimLink
from .weather_service import SCENARIOS

log = logging.getLogger("jaldrishti")


class ZoneUpdate(BaseModel):
    name: str | None = Field(None, max_length=30)
    crop: str | None = None
    stage: str | None = None
    soil: str | None = None
    depth_mm: float | None = None
    flow_l_per_min: float | None = None
    rain_exposed: bool | None = None
    enabled: bool | None = None


class IrrigateRequest(BaseModel):
    target_pct: float | None = Field(None, ge=0, le=100)
    seconds: float | None = Field(None, gt=0, le=settings.MAX_SESSION_PUMP_S)


class ShareWifi(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=32)
    password: str = Field("", max_length=63)


def server_ip() -> str | None:
    """The Pi's address on the farm network (for the "open dashboard" QR code)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class Location(BaseModel):
    name: str = Field(..., max_length=60)
    lat: float
    lon: float


def create_app(controller: Controller, clock=time.time, run_loops: bool = True) -> FastAPI:
    weather = controller.weather
    clients: set[WebSocket] = set()

    async def control_loop():
        while True:
            try:
                controller.tick(clock())
            except Exception:                       # never let one bad tick stop the farm
                log.exception("control tick failed")
            await asyncio.sleep(0.5)

    async def weather_loop():
        while True:
            now = clock()
            if weather.due(now):
                weather.last_attempt = now
                loc = weather.location
                result = await asyncio.to_thread(weather.fetch, loc)
                weather.store(result, loc, clock())
            await asyncio.sleep(30)

    async def push_loop():
        while True:
            await asyncio.sleep(2)
            if not clients:
                continue
            data = controller.snapshot(clock())
            for ws in list(clients):
                try:
                    await ws.send_json(data)
                except Exception:
                    clients.discard(ws)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(f()) for f in (control_loop, weather_loop, push_loop)] if run_loops else []
        yield
        for t in tasks:
            t.cancel()
        controller.save_state()
        controller.link.send_all_off()
        controller.link.close()

    app = FastAPI(title="Jal-Drishti", lifespan=lifespan)

    def zone_or_404(zone: int):
        if zone not in controller.zones:
            raise HTTPException(404, f"no zone {zone}")

    def guarded(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc))

    # ---- read ----------------------------------------------------------------------------
    @app.get("/api/state")
    async def state():
        return controller.snapshot(clock())

    @app.get("/api/history")
    async def history(hours: float = 24, zone: int | None = None):
        since = clock() - min(max(hours, 0.1), 24 * 30) * 3600
        zones = [zone] if zone else list(controller.zones)
        for z in zones:
            zone_or_404(z)
        step = max(1, int(hours * 3600 / settings.STORE_PERIOD_S / 600))   # <= ~600 points per zone
        return {"zones": {str(z): controller.db.readings(z, since)[::step] for z in zones},
                "env": controller.db.env_history(since)[::max(1, step // 6)],
                "pumps": controller.db.pump_events(since)}

    @app.get("/api/decisions")
    async def decisions(limit: int = 50, zone: int | None = None):
        return controller.db.decisions(min(limit, 500), zone)

    @app.get("/api/options")
    async def options():
        from jaldrishti_ml import config
        return {"crops": {k: c.label for k, c in config.CROPS.items()}, "stages": list(config.STAGES),
                "soils": dict(config.SOIL_LABELS), "regions": config.REGIONS,
                "scenarios": {k: v["label"] for k, v in SCENARIOS.items()},
                "simulated_device": isinstance(controller.link, SimLink),
                "share_wifi": controller.db.get("share_wifi", settings.DEFAULT_SHARE_WIFI),
                "server_ip": server_ip()}

    @app.post("/api/share/wifi")
    async def share_wifi(w: ShareWifi):
        controller.db.put("share_wifi", {"ssid": w.ssid, "password": w.password})
        return controller.db.get("share_wifi")

    # ---- control -----------------------------------------------------------------------------
    @app.post("/api/mode/{mode}")
    async def set_mode(mode: str):
        guarded(controller.set_mode, mode)
        return {"mode": controller.mode}

    @app.post("/api/zones/{zone}/irrigate")
    async def irrigate(zone: int, req: IrrigateRequest | None = None):
        zone_or_404(zone)
        req = req or IrrigateRequest()
        return guarded(controller.start_session, zone, clock(), target=req.target_pct,
                       planned_s=req.seconds, source="manual")

    @app.post("/api/zones/{zone}/stop")
    async def stop(zone: int):
        zone_or_404(zone)
        controller.stop_session(zone, clock())
        return {"stopped": True}

    @app.post("/api/zones/{zone}/reset_fault")
    async def reset_fault(zone: int):
        zone_or_404(zone)
        controller.reset_fault(zone)
        return {"fault": None}

    @app.post("/api/zones/{zone}/config")
    async def config_zone(zone: int, upd: ZoneUpdate):
        zone_or_404(zone)
        fields = {k: (int(v) if isinstance(v, bool) else v) for k, v in upd.model_dump().items() if v is not None}
        return guarded(controller.update_zone, zone, **fields)

    @app.post("/api/zones/{zone}/calibrate/{point}")
    async def calibrate(zone: int, point: str):
        zone_or_404(zone)
        return guarded(controller.calibrate, zone, point, clock())

    @app.post("/api/weather/simulate")
    async def simulate(scenario: str | None = None):
        guarded(weather.simulate, scenario, clock())
        for z in controller.zones.values():           # react immediately on stage
            controller._predict(z, clock())
            controller._decide(z, clock(), controller.device_online(clock()))
        return weather.summary(clock())

    @app.post("/api/weather/location")
    async def location(loc: Location):
        guarded(weather.set_location, loc.name, loc.lat, loc.lon)
        weather.last_attempt = 0
        return weather.location

    # ---- simulated farm only (demo / testing without hardware) -----------------------------------
    def farm():
        if not isinstance(controller.link, SimLink):
            raise HTTPException(400, "only available with the simulated farm (--sim)")
        return controller.link.farm

    @app.get("/api/sim")
    async def sim_state():
        return farm().state()

    @app.post("/api/sim/zones/{zone}/moisture/{pct}")
    async def sim_moisture(zone: int, pct: float):
        zone_or_404(zone)
        farm().set_moisture(zone, min(max(pct, 0), 100))
        return farm().state()

    @app.post("/api/sim/tank/{liters}")
    async def sim_tank(liters: float):
        farm().tank_l = max(liters, 0.0)
        return farm().state()

    @app.post("/api/sim/zones/{zone}/blocked/{on}")
    async def sim_block(zone: int, on: bool):
        zone_or_404(zone)
        farm().blocked[zone - 1] = on
        return farm().state()

    @app.post("/api/sim/zones/{zone}/unplugged/{on}")
    async def sim_unplug(zone: int, on: bool):
        zone_or_404(zone)
        farm().unplugged[zone - 1] = on
        return farm().state()

    # ---- live feed -------------------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_json(controller.snapshot(clock()))
            while True:
                await ws.receive_text()                  # keep-alive; client may send pings
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    # ---- dashboard -----------------------------------------------------------------------------------
    if settings.DASHBOARD_DIR.exists():
        app.mount("/", StaticFiles(directory=settings.DASHBOARD_DIR, html=True), name="dashboard")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def placeholder():
            return ("<h2>Jal-Drishti server is running</h2><p>Dashboard not built yet. "
                    "API: <a href='/api/state'>/api/state</a> · <a href='/docs'>/docs</a></p>")

    return app
