from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from jaldrishti_ml import config, features, weather
from jaldrishti_ml.adaptation import PumpResponse, ZoneAdapter
from jaldrishti_ml.predictor import IST, Forecast, MoisturePredictor, ZoneSetup, hours_until


class Recorder:
    """Stands in for a trained model: remembers its input, predicts -1 %."""
    def __init__(self):
        self.X = None

    def predict(self, X):
        self.X = X
        return np.full(len(X), -1.0)


def fake_predictor():
    p = MoisturePredictor.__new__(MoisturePredictor)
    p.horizons = list(config.HORIZONS_H)
    p.models = {h: Recorder() for h in p.horizons}
    p.band = {h: 0.5 * h for h in p.horizons}
    p.target_mode = "delta"
    return p


def scenario(now):
    zone = ZoneSetup("potting_mix", "tomato", "mid", 100)
    t_now = now.timestamp()
    ts = np.arange(t_now - 4 * 3600, t_now + 1, 30.0)          # every 30 s, 4 h
    vals = 40 + 0.5 * (t_now - ts) / 3600                       # drying 0.5 %/h
    rng = np.random.default_rng(0)
    fc = Forecast(start=now.replace(minute=0, second=0, microsecond=0),
                  rain_mm=rng.exponential(0.3, 24), rain_prob=rng.uniform(0, 100, 24),
                  et0_mm=rng.uniform(0, 0.5, 24))
    return zone, list(zip(ts, vals)), fc


def test_inference_features_match_training_definition():
    now = datetime(2024, 5, 10, 14, 37, tzinfo=IST)
    zone, readings, fc = scenario(now)
    p = fake_predictor()
    out = p.predict(zone, readings, now, 7.5, fc)

    soil, crop = config.SOILS["potting_mix"], config.CROPS["tomato"]
    rain_s = weather.expand_hourly_to_steps(np.array(fc.rain_mm), True)
    prob_s = weather.expand_hourly_to_steps(np.array(fc.rain_prob), False)
    et0_s = weather.expand_hourly_to_steps(np.array(fc.et0_mm), True)
    for h in config.HORIZONS_H:
        r, pr, e = features.window_aggregates(rain_s, prob_s, et0_s, [2], h * 4)  # 14:30 -> step 2
        X = p.models[h].X
        f = dict(zip(features.FEATURES, X[0]))
        # soil drying 0.5 %/h -> both slope features must be exactly -0.5
        assert f["d_1h"] == pytest.approx(-0.5, abs=0.01)
        assert f["d_3h"] == pytest.approx(-0.5, abs=0.01)
        assert f["fc_rain_mm"] == pytest.approx(r[0], rel=1e-5)
        assert f["fc_rain_prob"] == pytest.approx(pr[0], rel=1e-5)
        assert f["fc_et0_mm"] == pytest.approx(e[0], rel=1e-5)
        assert f["fc_pct"] == pytest.approx(soil.fc_pct, rel=1e-5)
        assert f["kc"] == pytest.approx(crop.kc_mid) and f["depth_mm"] == 100
        assert f["hours_since_water"] == pytest.approx(7.5)
        # physics feature must equal the shared training-time rollout
        nom = features.nominal_params(soil.theta_wp, soil.theta_fc, soil.theta_sat, soil.drain_tau_h,
                                      100, crop.kc_mid, crop.p)
        ph = features.physics_deltas(nom, out["m_now"], lambda k: et0_s[2 + k], lambda k: rain_s[2 + k], [h])[h]
        assert f["phys_delta"] == pytest.approx(float(ph), abs=2e-3)
    assert out["m_now"] == pytest.approx(40.0, abs=0.1)
    assert [hz["moisture"] for hz in out["horizons"]] == pytest.approx([out["m_now"] - 1] * 4, abs=0.01)
    assert out["history_ok"] and out["forecast_padded_hours"] == 0


def test_timezone_handling_is_consistent():
    """Same instant given as IST, UTC or naive-IST must give identical features."""
    p = fake_predictor()
    now_ist = datetime(2024, 5, 10, 14, 37, tzinfo=IST)
    zone, readings, fc = scenario(now_ist)
    p.predict(zone, readings, now_ist, 3, fc)
    a = p.models[6].X.copy()
    p.predict(zone, readings, now_ist.astimezone(timezone.utc), 3, fc)
    b = p.models[6].X.copy()
    p.predict(zone, readings, now_ist.replace(tzinfo=None), 3, fc)
    c = p.models[6].X.copy()
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(a, c)


def test_short_forecast_is_padded_and_missing_history_is_nan():
    now = datetime(2024, 5, 10, 22, 5, tzinfo=IST)
    zone, readings, fc = scenario(now)
    fc = Forecast(fc.start, fc.rain_mm[:3], fc.rain_prob[:3], fc.et0_mm[:3])
    readings = [r for r in readings if r[0] > now.timestamp() - 1800]      # only 30 min history
    p = fake_predictor()
    out = p.predict(zone, readings, now, 1, fc)
    assert out["forecast_padded_hours"] > 0 and not out["history_ok"]
    f = dict(zip(features.FEATURES, p.models[1].X[0]))
    assert np.isnan(f["d_1h"]) and np.isnan(f["d_3h"])


def test_no_recent_reading_raises():
    now = datetime(2024, 5, 10, 12, 0, tzinfo=IST)
    zone, readings, fc = scenario(now)
    old = [r for r in readings if r[0] < now.timestamp() - 3600]
    with pytest.raises(ValueError):
        fake_predictor().predict(zone, old, now, 3, fc)


def test_bad_zone_config_rejected():
    with pytest.raises(ValueError):
        ZoneSetup("moon_dust", "tomato", "mid", 100)
    with pytest.raises(ValueError):
        ZoneSetup("loam", "tomato", "flowering", 100)


def test_hours_until():
    hz = [{"h": 1, "moisture": 40}, {"h": 3, "moisture": 36}, {"h": 6, "moisture": 30}, {"h": 12, "moisture": 25}]
    assert hours_until(41, hz, 50) == 0.0
    assert hours_until(41, hz, 33) == pytest.approx(4.5)
    assert hours_until(41, hz, 20) is None


def test_adapter_learns_slow_drying_zone():
    rng = np.random.default_rng(0)
    ad = ZoneAdapter()
    for _ in range(300):
        pred = rng.uniform(-6, 0)
        obs = 0.45 * pred + 0.1 + rng.normal(0, 0.15)            # zone dries 2x slower
        ad.update(6, pred, obs)
    s = ad.stats(6)
    assert s["correction_active"] and abs(s["a"] - 0.45) < 0.1
    assert ad.adjust(6, -4.0) == pytest.approx(-1.7, abs=0.4)
    back = ZoneAdapter.from_dict(ad.to_dict())                     # persistence
    assert back.adjust(6, -4.0) == pytest.approx(ad.adjust(6, -4.0))


def test_adapter_does_not_hurt_good_model():
    rng = np.random.default_rng(1)
    ad = ZoneAdapter()
    errs_raw, errs_used = [], []
    for _ in range(200):
        pred = rng.uniform(-6, 0)
        obs = pred + rng.normal(0, 0.3)                           # model already right
        errs_used.append(abs(ad.adjust(3, pred) - obs))
        errs_raw.append(abs(pred - obs))
        ad.update(3, pred, obs)
    assert np.mean(errs_used) <= np.mean(errs_raw) * 1.05


def test_pump_response():
    pr = PumpResponse(prior_gain=0.5)
    assert not pr.update(1, 5)          # too short
    assert not pr.update(10, 0.1)       # no rise -> flagged
    for _ in range(10):
        pr.update(10, 8.0)
    assert pr.gain == pytest.approx(0.8, rel=0.01)
    assert pr.seconds_for(16) == pytest.approx(20, rel=0.01)
    assert pr.seconds_for(1000) == 120
    assert PumpResponse.from_dict(pr.to_dict()).gain == pr.gain


def test_residual_mode_adds_physics_back():
    now = datetime(2024, 5, 10, 14, 37, tzinfo=IST)
    zone, readings, fc = scenario(now)
    p = fake_predictor()
    p.target_mode = "residual"
    out = p.predict(zone, readings, now, 2, fc)
    for hz in out["horizons"]:
        expected = min(max(out["m_now"] - 1 + hz["physics_delta"], 0), 100)
        assert hz["moisture"] == pytest.approx(expected, abs=0.02)


def test_adapter_ignores_rain_forecast_windows():
    """A false-alarm rain forecast must not teach the zone a correction."""
    rng = np.random.default_rng(2)
    ad = ZoneAdapter()
    for _ in range(400):
        pred = rng.uniform(1, 4)                      # model expected rain to wet the soil
        assert not ad.update(12, pred, -1.0, fc_rain_mm=5.0)   # it never came
    assert ad.stats(12)["checks"] == 0 and not ad.active(12)
    for _ in range(300):
        pred = rng.uniform(-6, 0)
        ad.update(12, pred, 0.5 * pred + rng.normal(0, 0.1))
    assert ad.active(12)
    assert ad.adjust(12, -4.0) == pytest.approx(-2.0, abs=0.4)
    assert ad.adjust(12, 3.0, fc_rain_mm=6.0) == 3.0  # rain forecast: model output untouched


def test_single_reading_stamped_exactly_now_is_used():
    """Regression: a reading at time.time() with sub-microsecond digits was dropped."""
    now_ts = 1759771918.5534123456
    now = datetime.fromtimestamp(now_ts, IST)
    zone, _, fc = scenario(now)
    out = fake_predictor().predict(zone, [(now_ts, 47.0)], now, 5, fc)
    assert out["m_now"] == 47.0 and not out["history_ok"]
