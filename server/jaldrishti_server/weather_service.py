"""Live weather for the decision engine.

* Fetches the free Open-Meteo forecast (hourly rain, rain probability, ET0,
  temperature, humidity) incl. the past 24 h (observed rain), every 30 min.
* The last good forecast is kept in the database, so after a restart without
  internet the Pi still has it; the predictor pads anything missing.
* "Simulate rain" (demo switch) overlays a storm on the next hours.
"""
from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from jaldrishti_ml.predictor import IST, Forecast

from . import settings

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_KEY = "weather_cache"
SIM_KEY = "weather_simulation"

SCENARIOS = {
    # hours from now -> (rain mm/h, probability %)
    "rain": {"label": "Heavy rain in 2 h", "hours": {1: (0.2, 70), 2: (3.0, 90), 3: (6.0, 95),
                                                     4: (5.0, 90), 5: (2.0, 80), 6: (0.5, 60)}},
    "light_rain": {"label": "Light drizzle", "hours": {2: (0.3, 55), 3: (0.4, 60), 4: (0.2, 50)}},
    "clear": {"label": "Clear and hot", "hours": {h: (0.0, 0) for h in range(0, 24)}},
}


def fetch_open_meteo(lat: float, lon: float, timeout: float = 20) -> dict:
    query = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "Asia/Kolkata",
        "hourly": "precipitation,precipitation_probability,et0_fao_evapotranspiration,"
                  "temperature_2m,relative_humidity_2m",
        "past_days": 1, "forecast_days": 3,
    })
    with urllib.request.urlopen(f"{FORECAST_URL}?{query}", timeout=timeout) as resp:
        return json.load(resp)


def extraterrestrial_radiation(lat_deg: float, day_of_year: int) -> float:
    """Ra in MJ m-2 day-1 (FAO-56 eq. 21)."""
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    delta = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    return 24 * 60 / math.pi * 0.0820 * dr * (ws * math.sin(phi) * math.sin(delta)
                                              + math.cos(phi) * math.cos(delta) * math.sin(ws))


def hargreaves_et0_day(tmin: float, tmax: float, lat_deg: float, day_of_year: int) -> float:
    """Daily reference ET (mm) from temperature only - FAO-56 eq. 52 (Hargreaves)."""
    tmean = (tmin + tmax) / 2
    ra_mm = 0.408 * extraterrestrial_radiation(lat_deg, day_of_year)
    return max(0.0, 0.0023 * (tmean + 17.8) * math.sqrt(max(tmax - tmin, 0.0)) * ra_mm)


# share of a day's ET in each hour (zero at night, sine-shaped by day); sums to 1
_SHAPE = [max(0.0, math.sin(math.pi * (h - 6) / 12)) for h in range(24)]
DAY_SHAPE = [v / sum(_SHAPE) for v in _SHAPE]


class WeatherService:
    def __init__(self, db, fetcher=fetch_open_meteo):
        self.db = db
        self.fetcher = fetcher
        # set by the controller: () -> (tmin, tmax) of the last 24 h from the DHT11, or None
        self.local_climate = None
        self._memo: tuple[float, list] | None = None     # parsed forecast, re-read every 30 s
        self._climate_memo: tuple[float, object] | None = None
        self.last_error: str | None = None
        self.last_attempt: float = 0.0

    # ---- location / simulation settings -----------------------------------------------
    @property
    def location(self) -> dict:
        return self.db.get("location", settings.DEFAULT_LOCATION)

    def set_location(self, name: str, lat: float, lon: float) -> None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("bad coordinates")
        self.db.put("location", {"name": name, "lat": lat, "lon": lon})
        self.db.put(CACHE_KEY, None)
        self._invalidate()

    def simulate(self, scenario: str | None, now: float) -> None:
        if scenario is not None and scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.db.put(SIM_KEY, None if scenario is None else {"scenario": scenario, "since": now})

    @property
    def simulation(self) -> dict | None:
        return self.db.get(SIM_KEY)

    # ---- fetching (called from a background thread) -----------------------------------------
    def fetch(self, loc: dict) -> dict | Exception:
        """Network part only (safe to run in a worker thread - touches no database)."""
        try:
            data = self.fetcher(loc["lat"], loc["lon"])
            hourly = data["hourly"]
            n = len(hourly["time"])
            for k in ("precipitation", "precipitation_probability", "et0_fao_evapotranspiration",
                      "temperature_2m", "relative_humidity_2m"):
                if len(hourly[k]) != n:
                    raise ValueError(f"bad forecast: {k}")
            return hourly
        except Exception as exc:          # offline: keep using the cache
            return exc

    def store(self, result: dict | Exception, loc: dict, now: float) -> bool:
        """Database part (call from the main thread)."""
        if isinstance(result, Exception):
            self.last_error = f"{type(result).__name__}: {result}"
            return False
        self.db.put(CACHE_KEY, {"fetched_at": now, "location": loc, "hourly": result})
        self._invalidate()
        self.last_error = None
        return True

    def refresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self.last_attempt = now
        loc = self.location
        return self.store(self.fetch(loc), loc, now)

    def due(self, now: float) -> bool:
        cache = self.db.get(CACHE_KEY)
        fresh = cache and now - cache["fetched_at"] < settings.WEATHER_REFRESH_S
        return not fresh and now - self.last_attempt > 120

    # ---- what the controller uses ----------------------------------------------------------------
    def _invalidate(self) -> None:
        self._memo = None

    def _hours(self) -> list[dict]:
        if self._memo and time.monotonic() - self._memo[0] < 30:
            return self._memo[1]
        hours = self._parse_hours()
        self._memo = (time.monotonic(), hours)
        return hours

    def _parse_hours(self) -> list[dict]:
        cache = self.db.get(CACHE_KEY)
        if not cache:
            return []
        h = cache["hourly"]

        def num(v, default=0.0):
            return default if v is None else float(v)

        return [{"time": datetime.fromisoformat(t).replace(tzinfo=IST),
                 "rain": num(h["precipitation"][i]),
                 "prob": num(h["precipitation_probability"][i]),
                 "et0": num(h["et0_fao_evapotranspiration"][i], 0.1),
                 "temp": None if h["temperature_2m"][i] is None else float(h["temperature_2m"][i]),
                 "rh": None if h["relative_humidity_2m"][i] is None else float(h["relative_humidity_2m"][i])}
                for i, t in enumerate(h["time"])]

    def forecast(self, now: float) -> Forecast:
        """Hourly forecast from the current hour on (simulation applied)."""
        now_dt = datetime.fromtimestamp(now, IST)
        start = now_dt.replace(minute=0, second=0, microsecond=0)
        future = [x for x in self._hours() if x["time"] >= start]
        if future and future[0]["time"] == start:
            rain = [x["rain"] for x in future]
            prob = [x["prob"] for x in future]
            et0 = [x["et0"] for x in future]
        else:                                  # no forecast at all (offline since boot)
            rain, prob, et0 = [], [], []
        if len(et0) < 36:                      # offline / stale: fill the gap without the internet
            fill = self._offline_et0(start, len(et0), 36 - len(et0))
            et0 += fill
            rain += [0.0] * len(fill)
            prob += [0.0] * len(fill)
        sim = self.simulation
        if sim:
            hours = SCENARIOS[sim["scenario"]]["hours"]
            # keep the storm anchored to when the button was pressed
            shift = int((now - sim["since"]) // 3600)
            for k, (r, p) in hours.items():
                i = k - shift
                if 0 <= i:
                    while len(rain) <= i:
                        rain.append(0.0); prob.append(0.0); et0.append(_default_et0((start.hour + len(et0)) % 24))
                    rain[i], prob[i] = r, p
                    et0[i] = min(et0[i], 0.05) if r > 0 else et0[i]
        return Forecast(start=start, rain_mm=rain, rain_prob=prob, et0_mm=et0)

    def _offline_et0(self, start: datetime, first: int, n: int) -> list[float]:
        """Hourly ET0 for hours [first, first+n) after `start`. Uses the DHT11's last 24 h
        (Hargreaves, FAO-56 eq. 52) when available, else a typical clear-day shape."""
        climate = self._climate()
        out = []
        for i in range(first, first + n):
            t = start + timedelta(hours=i)
            if climate:
                daily = hargreaves_et0_day(climate[0], climate[1], self.location["lat"], t.timetuple().tm_yday)
                out.append(round(daily * DAY_SHAPE[t.hour], 4))
            else:
                out.append(_default_et0(t.hour))
        return out

    def _climate(self):
        if not self.local_climate:
            return None
        if self._climate_memo and time.monotonic() - self._climate_memo[0] < 600:
            return self._climate_memo[1]
        value = self.local_climate()
        self._climate_memo = (time.monotonic(), value)
        return value

    def et0_next_24h(self, now: float) -> float:
        """Forecast reference ET over the next 24 h (mm) - for the day's crop water use."""
        return float(sum(self.forecast(now).et0_mm[:24]))

    def next_hours(self, now: float, hours: int = 6) -> dict:
        fc = self.forecast(now)
        r = list(fc.rain_mm[:hours]); p = list(fc.rain_prob[:hours])
        return {"rain_mm": round(sum(r), 2), "prob_max": round(max(p) if p else 0.0, 1)}

    def observed_rain_events(self, since: float, now: float) -> list[float]:
        """Epoch times of past hours with real rain >= wet-event threshold."""
        from jaldrishti_ml import config
        out = []
        for x in self._hours():
            t = x["time"].timestamp()
            if since <= t <= now - 3600 and x["rain"] >= config.WET_EVENT_RAIN_MM_PER_HOUR:
                out.append(t)
        return out

    def summary(self, now: float) -> dict:
        cache = self.db.get(CACHE_KEY)
        now_dt = datetime.fromtimestamp(now, IST).replace(minute=0, second=0, microsecond=0)
        cur = next((x for x in self._hours() if x["time"] == now_dt), None)
        nxt = self.next_hours(now, 6)
        fc = self.forecast(now)
        sim = self.simulation
        if sim:
            source = "simulated"
        elif cache is None:
            source = "none"
        elif now - cache["fetched_at"] > 2 * settings.WEATHER_REFRESH_S:
            source = "cached"
        else:
            source = "live"
        return {
            "source": source,
            "age_min": None if cache is None else round((now - cache["fetched_at"]) / 60),
            "location": self.location,
            "temp_c": cur["temp"] if cur else None,
            "rh_pct": cur["rh"] if cur else None,
            "rain_next_6h_mm": nxt["rain_mm"],
            "rain_prob_next_6h": nxt["prob_max"],
            "simulation": SCENARIOS[sim["scenario"]]["label"] if sim else None,
            "error": self.last_error,
            "hourly": [{"t": (now_dt + timedelta(hours=i)).isoformat(), "rain": r, "prob": p}
                       for i, (r, p) in enumerate(zip(fc.rain_mm[:12], fc.rain_prob[:12]))],
        }


def _default_et0(hour: int) -> float:
    """Typical clear-day ET0 shape (mm/h), used only when no forecast exists at all."""
    import math
    return round(max(0.0, math.sin(math.pi * (hour - 6) / 12)) * 0.5, 3)
