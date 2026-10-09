import numpy as np
import pytest

from jaldrishti_ml import config, features, weather
from jaldrishti_ml.soil_model import ZoneParams, step


def zones(n=500, seed=0):
    rng = np.random.default_rng(seed)
    wp = rng.uniform(0.04, 0.22, n)
    fc = wp + rng.uniform(0.05, 0.3, n)
    return ZoneParams(theta_wp=wp, theta_fc=fc, theta_sat=fc + rng.uniform(0.08, 0.25, n),
                      drain_tau_h=rng.uniform(1, 30, n), depth_mm=rng.uniform(50, 600, n),
                      kc=rng.uniform(0.3, 1.3, n), p=rng.uniform(0.2, 0.65, n),
                      exposure=rng.uniform(0.2, 1.4, n), rain_capture=rng.uniform(0.8, 1, n)), rng


def test_mass_balance_and_bounds():
    prm, rng = zones()
    theta = rng.uniform(prm.theta_ad, prm.theta_sat)
    for _ in range(500):
        et0, rain = rng.exponential(0.1, 500), rng.exponential(0.5, 500) * (rng.random(500) < 0.1)
        irr = rng.exponential(5, 500) * (rng.random(500) < 0.02)
        new, et, drain, runoff = step(theta, prm, et0, rain, irr, 0.25)
        lhs = (new - theta) * prm.depth_mm
        rhs = rain * prm.rain_capture + irr - et - drain - runoff
        np.testing.assert_allclose(lhs, rhs, atol=1e-9)
        assert np.all(new >= prm.theta_ad - 1e-12) and np.all(new <= prm.theta_sat + 1e-12)
        assert np.all(et >= -1e-12) and np.all(drain >= 0) and np.all(runoff >= 0)
        theta = new


def test_no_weather_below_fc_is_static():
    prm, rng = zones()
    theta = rng.uniform(prm.theta_ad, prm.theta_fc)
    new, *_ = step(theta, prm, 0.0, 0.0, 0.0, 0.25)
    np.testing.assert_allclose(new, theta)


def test_drainage_relaxes_to_field_capacity():
    prm, _ = zones()
    theta = prm.theta_sat.copy()
    for _ in range(4 * 24 * 20):                    # 20 days, no ET
        theta, *_ = step(theta, prm, 0.0, 0.0, 0.0, 0.25)
    np.testing.assert_allclose(theta, prm.theta_fc, atol=1e-3)


def test_drying_slows_when_stressed():
    prm, _ = zones()
    wet = prm.theta_fc.copy()
    dry = prm.theta_wp + 0.05 * (prm.theta_fc - prm.theta_wp)
    loss_wet = wet - step(wet, prm, 0.5, 0, 0, 0.25)[0]
    loss_dry = dry - step(dry, prm, 0.5, 0, 0, 0.25)[0]
    assert np.all(loss_dry < loss_wet)


def test_pct_roundtrip_and_thresholds_order():
    for name, soil in config.SOILS.items():
        assert soil.theta_ad < soil.theta_wp < soil.theta_fc < soil.theta_sat, name
        assert soil.to_pct(soil.theta_ad) == pytest.approx(0)
        assert soil.to_pct(soil.theta_sat) == pytest.approx(100)
        assert soil.from_pct(soil.to_pct(0.2)) == pytest.approx(0.2)
        for crop in config.CROPS:
            t = config.thresholds(name, crop)
            assert t["wp_pct"] < t["min_pct"] < t["target_pct"] < t["fc_pct"] < 100


def test_window_aggregates_match_bruteforce():
    rng = np.random.default_rng(1)
    rain, prob, et0 = rng.exponential(1, 300), rng.uniform(0, 100, 300), rng.exponential(0.1, 300)
    starts = rng.integers(0, 300 - 48, 50)
    r, p, e = features.window_aggregates(rain, prob, et0, starts, 48)
    for i, s in enumerate(starts):
        assert r[i] == pytest.approx(rain[s:s + 48].sum())
        assert e[i] == pytest.approx(et0[s:s + 48].sum())
        assert p[i] == pytest.approx(prob[s:s + 48].max())
    with pytest.raises(ValueError):
        features.window_aggregates(rain, prob, et0, [290], 48)


def test_expand_hourly_preserves_totals():
    h = np.array([1.0, 2.0, 0.0, 4.0])
    s = weather.expand_hourly_to_steps(h, True)
    assert len(s) == 4 * config.STEPS_PER_HOUR and s.sum() == pytest.approx(h.sum())
    assert np.all(weather.expand_hourly_to_steps(h, False)[:4] == 1.0)


def test_simulated_forecast_is_sane():
    rng = np.random.default_rng(3)
    rain = rng.exponential(2, 5000) * (rng.random(5000) < 0.05)
    et0 = rng.uniform(0, 0.6, 5000)
    fr, fp, fe = weather.simulate_forecast(rain, et0, rng)
    assert fr.min() >= 0 and fe.min() >= 0 and 0 <= fp.min() and fp.max() <= 100
    assert np.corrcoef(fr, rain)[0, 1] > 0.3       # informative but imperfect
    assert fp[fr > 1].mean() > fp[fr == 0].mean() + 30
