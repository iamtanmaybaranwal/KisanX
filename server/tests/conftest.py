import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jaldrishti_ml.predictor import IST, MoisturePredictor  # noqa: E402

from jaldrishti_server.weather_service import _default_et0  # noqa: E402

T0 = datetime(2025, 4, 15, 6, 0, tzinfo=IST).timestamp()     # hot pre-monsoon morning


def fake_open_meteo(rain_at: dict | None = None):
    """Deterministic Open-Meteo replacement: 3 days around T0, dry unless rain_at {hour_index: mm}."""
    rain_at = rain_at or {}

    def fetch(lat, lon):
        start = datetime.fromtimestamp(T0, IST).replace(hour=0)
        start = start.replace(day=start.day - 1)
        times, rain, prob, et0, temp, rh = [], [], [], [], [], []
        for i in range(24 * 4):
            t = start.timestamp() + i * 3600
            hour = (i % 24)
            times.append(datetime.fromtimestamp(t, IST).strftime("%Y-%m-%dT%H:%M"))
            r = rain_at.get(i, 0.0)
            rain.append(r)
            prob.append(90 if r else 5)
            et0.append(_default_et0(hour))
            temp.append(30.0)
            rh.append(40.0)
        return {"hourly": {"time": times, "precipitation": rain, "precipitation_probability": prob,
                           "et0_fao_evapotranspiration": et0, "temperature_2m": temp,
                           "relative_humidity_2m": rh}}
    return fetch


@pytest.fixture(scope="session")
def predictor():
    return MoisturePredictor()
