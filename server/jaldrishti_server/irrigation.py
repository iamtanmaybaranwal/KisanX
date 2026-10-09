"""Pulse irrigation for one zone.

    PULSE (pump on for a few seconds, timed by the ESP32 itself)
      -> SOAK (wait for the water to reach the sensor)
      -> measure -> learn pump response -> next pulse / DONE / FAULT

Every pump command is TIMED, so if the Pi crashes mid-session the ESP32
switches the pump off by itself. No "turn off" message is ever required.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import settings


@dataclass
class Session:
    zone: int
    target_pct: float
    m_start: float
    planned_s: float
    source: str = "auto"              # auto | manual
    state: str = "PULSE_DUE"          # PULSE_DUE -> PUMPING -> SOAKING -> ... -> DONE | FAULT
    pumped_s: float = 0.0
    pulses: int = 0
    phase_until: float = 0.0
    pulse_s: float = 0.0
    m_pulse_start: float = 0.0
    end_reason: str = ""
    log: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state not in ("DONE", "FAULT")

    def remaining_budget(self) -> float:
        cap = min(settings.MAX_SESSION_PUMP_S, max(self.planned_s * 1.5, self.planned_s + 10))
        return max(cap - self.pumped_s, 0.0)


def step(sess: Session, now: float, m_now: float | None, gain: float, send_pump, on_pulse_result):
    """Advance the session. `send_pump(zone, seconds)` actuates; `on_pulse_result(seconds, rise)`
    lets the caller learn the pump response. Returns the session (mutated)."""
    if not sess.active:
        return sess

    if sess.state == "PULSE_DUE":
        if m_now is None:
            return _finish(sess, "FAULT", "sensor_lost")
        if m_now >= sess.target_pct - settings.TARGET_TOLERANCE_PCT:
            return _finish(sess, "DONE", "target_reached")
        budget = sess.remaining_budget()
        if budget < 1.0:
            return _finish(sess, "DONE", "time_limit")
        need = (sess.target_pct - m_now) / max(gain, 1e-3)
        sess.pulse_s = float(min(settings.PULSE_MAX_S, max(need, 2.0), budget))
        sess.m_pulse_start = m_now
        send_pump(sess.zone, sess.pulse_s)
        sess.pulses += 1
        sess.state = "PUMPING"
        sess.phase_until = now + sess.pulse_s
        sess.log.append({"t": now, "pulse_s": round(sess.pulse_s, 1), "m": round(m_now, 2)})
        return sess

    if sess.state == "PUMPING" and now >= sess.phase_until:
        sess.pumped_s += sess.pulse_s
        sess.state = "SOAKING"
        sess.phase_until = now + settings.SOAK_S
        return sess

    if sess.state == "SOAKING" and now >= sess.phase_until:
        if m_now is None:
            return _finish(sess, "FAULT", "sensor_lost")
        rise = m_now - sess.m_pulse_start
        on_pulse_result(sess.pulse_s, rise)
        total_rise = m_now - sess.m_start
        if sess.pumped_s >= settings.NO_RISE_CHECK_S and total_rise < settings.NO_RISE_MIN_PCT:
            return _finish(sess, "FAULT", "no_rise")
        sess.state = "PULSE_DUE"
        return step(sess, now, m_now, gain, send_pump, on_pulse_result)
    return sess


def _finish(sess: Session, state: str, reason: str) -> Session:
    sess.state = state
    sess.end_reason = reason
    return sess
