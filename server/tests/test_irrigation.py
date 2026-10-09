from jaldrishti_server import irrigation, settings


class FakeZone:
    """Moisture rises `gain` %/s of pumping, visible after the soak."""
    def __init__(self, m, gain, blocked=False):
        self.m, self.gain, self.blocked = m, gain, blocked
        self.cmds, self.learned = [], []

    def send(self, zone, seconds):
        self.cmds.append(seconds)
        if not self.blocked:
            self.m += self.gain * seconds

    def learn(self, s, rise):
        self.learned.append((s, rise))


def run(sess, zone, gain_est=0.5, max_t=2000):
    t = 0.0
    while sess.active and t < max_t:
        irrigation.step(sess, t, zone.m, gain_est, zone.send, zone.learn)
        t += 1
    return t


def test_reaches_target_in_pulses_without_overshoot():
    z = FakeZone(30, gain=0.4)
    s = irrigation.Session(zone=3, target_pct=50, m_start=30, planned_s=50)
    run(s, z)
    assert s.state == "DONE" and s.end_reason == "target_reached"
    assert all(c <= settings.PULSE_MAX_S for c in z.cmds)
    assert z.m >= 50 - settings.TARGET_TOLERANCE_PCT and z.m < 50 + 0.4 * settings.PULSE_MAX_S
    assert z.learned and all(r > 0 for _, r in z.learned)


def test_blocked_pipe_is_detected():
    z = FakeZone(30, gain=0.4, blocked=True)
    s = irrigation.Session(zone=1, target_pct=50, m_start=30, planned_s=50)
    run(s, z)
    assert (s.state, s.end_reason) == ("FAULT", "no_rise")
    assert sum(z.cmds) <= settings.NO_RISE_CHECK_S + settings.PULSE_MAX_S


def test_budget_limits_total_pumping():
    z = FakeZone(10, gain=0.05)                           # very weak pump
    s = irrigation.Session(zone=2, target_pct=60, m_start=10, planned_s=200)
    run(s, z)
    assert s.end_reason == "time_limit"
    assert sum(z.cmds) <= settings.MAX_SESSION_PUMP_S + 1e-6


def test_sensor_loss_aborts():
    z = FakeZone(30, gain=0.4)
    s = irrigation.Session(zone=1, target_pct=50, m_start=30, planned_s=40)
    irrigation.step(s, 0, 30, 0.5, z.send, z.learn)
    t = 0
    while s.active and t < 200:
        t += 1
        irrigation.step(s, t, None, 0.5, z.send, z.learn)
    assert (s.state, s.end_reason) == ("FAULT", "sensor_lost")


def test_already_wet_does_nothing():
    z = FakeZone(52, gain=0.4)
    s = irrigation.Session(zone=1, target_pct=50, m_start=52, planned_s=10)
    run(s, z)
    assert s.end_reason == "target_reached" and z.cmds == []
