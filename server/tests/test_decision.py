import pytest

from jaldrishti_server import settings
from jaldrishti_server.decision import ZoneInputs, decide

THR = {"min_pct": 40.0, "target_pct": 55.0, "wp_pct": 11.0, "fc_pct": 60.0}


def fc(m_now, m6, until=None):
    return {"m_now": m_now, "hours_until_min": until,
            "horizons": [{"h": 1, "moisture": m_now}, {"h": 3, "moisture": m_now},
                         {"h": 6, "moisture": m6}, {"h": 12, "moisture": m6}]}


def inp(m, **kw):
    base = dict(m_now=m, thresholds=THR, forecast=None if m is None else fc(m, m - 2), pump_gain=0.5)
    base.update(kw)
    return ZoneInputs(**base)


def test_fault_wins():
    d = decide(inp(20, fault="no_rise"))
    assert d.action == "FAULT" and "टंकी" in d.reason_hi


def test_no_sensor():
    assert decide(inp(None)).action == "NO_SENSOR"


def test_session_running():
    assert decide(inp(30, session_active=True)).action == "WATERING"


def test_enough_water():
    d = decide(inp(56))
    assert (d.action, d.code) == ("SKIP", "enough_water")


def test_ok_with_time_until_need():
    d = decide(inp(45, forecast=fc(45, 41, until=7.5)))
    assert (d.action, d.code) == ("SKIP", "ok_for_now")
    assert "about 8 h" in d.reason_en and d.details["hours_until_min"] == 7.5   # rounded for farmers


def test_ok_no_need_within_horizon():
    assert decide(inp(45, forecast=fc(45, 43, until=None))).code == "ok_12h"


def test_dry_irrigates_to_target():
    d = decide(inp(30))
    assert (d.action, d.code) == ("IRRIGATE", "dry")
    assert d.details["goal_pct"] == 55.0
    assert d.seconds == pytest.approx((55 - 30) / 0.5)
    assert d.details["liters"] == pytest.approx(d.seconds * settings.DEFAULT_FLOW_L_PER_MIN / 60, abs=0.01)


def test_pre_emptive_irrigation_when_crossing_soon():
    d = decide(inp(42, forecast=fc(42, 37, until=1.5)))
    assert (d.action, d.code) == ("IRRIGATE", "dry_soon")


def test_rain_postpones_when_ai_predicts_recovery():
    d = decide(inp(35, forecast=fc(35, 48), fc_rain_6h=12, fc_prob_6h=90))
    assert (d.action, d.code) == ("POSTPONE", "rain_expected")
    assert "बारिश" in d.reason_hi


def test_rain_too_small_still_irrigates():
    d = decide(inp(35, forecast=fc(35, 36), fc_rain_6h=3, fc_prob_6h=80))
    assert d.action == "IRRIGATE" and "too little" in d.reason_en


def test_unlikely_rain_does_not_postpone():
    d = decide(inp(35, forecast=fc(35, 48), fc_rain_6h=12, fc_prob_6h=30))
    assert d.action == "IRRIGATE"


def test_critically_dry_gets_light_water_even_with_rain():
    d = decide(inp(15, forecast=fc(15, 45), fc_rain_6h=12, fc_prob_6h=90))
    assert (d.action, d.code) == ("IRRIGATE", "critical_light")
    assert d.details["goal_pct"] == THR["min_pct"]           # only a top-up, rain does the rest


def test_cooldown_waits():
    assert decide(inp(30, cooling_down=True)).action == "WAIT"


def test_seconds_capped():
    d = decide(inp(5, pump_gain=0.01))
    assert d.seconds == settings.MAX_SESSION_PUMP_S


def test_no_forecast_still_safe():
    """Without the AI (e.g. no history yet) the plain threshold rule still works."""
    assert decide(inp(30, forecast=None)).action == "IRRIGATE"
    assert decide(inp(45, forecast=None)).code == "ok_12h"
