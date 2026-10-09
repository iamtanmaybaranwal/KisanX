"""Decision engine: what should one zone do right now?

Pure function - no I/O - so every rule is unit-tested.

Actions
    IRRIGATE  start a pulse-irrigation session (seconds = planned pump time)
    POSTPONE  dry, but forecast rain will refill it (AI-checked) -> wait
    SKIP      enough water now and for the next LOOKAHEAD_H hours
    WATERING  a session is already running
    WAIT      just irrigated - letting water spread before judging again
    FAULT     pump locked out (e.g. tank empty / pipe blocked)
    NO_SENSOR no valid reading
Every decision carries a short reason in English and Hindi for Farmer Mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import settings


@dataclass
class ZoneInputs:
    m_now: float | None                 # current moisture %, None if no valid reading
    thresholds: dict                    # min_pct, target_pct, wp_pct, fc_pct
    forecast: dict | None = None        # predictor output (adjusted horizons) or None
    fc_rain_6h: float = 0.0             # forecast rain mm, next 6 h
    fc_prob_6h: float = 0.0             # max rain probability %, next 6 h
    fault: str | None = None
    session_active: bool = False
    cooling_down: bool = False
    pump_gain: float = 0.5              # learned % points per pump-second
    flow_l_per_min: float = settings.DEFAULT_FLOW_L_PER_MIN


@dataclass
class Decision:
    action: str
    code: str
    reason_en: str
    reason_hi: str
    seconds: float = 0.0
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"action": self.action, "code": self.code, "seconds": round(self.seconds, 1),
                "reason_en": self.reason_en, "reason_hi": self.reason_hi, "details": self.details}


FAULT_TEXT = {
    "no_rise": ("Pump ran but soil did not get wetter - tank empty or pipe blocked? Check and reset.",
                "पंप चला पर मिट्टी गीली नहीं हुई - टंकी खाली या पाइप बंद? जाँच कर रीसेट करें।"),
    "device_offline": ("Controller (ESP32) not responding - pumps are off for safety.",
                       "कंट्रोलर (ESP32) जवाब नहीं दे रहा - सुरक्षा के लिए पंप बंद हैं।"),
}


def _h(x: float) -> str:
    return f"{x:.0f}" if x >= 2 else f"{x:.1f}"


def horizon(forecast: dict | None, h: int) -> dict | None:
    if not forecast:
        return None
    return next((x for x in forecast["horizons"] if x["h"] == h), None)


def decide(z: ZoneInputs) -> Decision:
    thr = z.thresholds
    if z.fault:
        en, hi = FAULT_TEXT.get(z.fault, (f"Fault: {z.fault}", f"खराबी: {z.fault}"))
        return Decision("FAULT", z.fault, en, hi)
    if z.m_now is None:
        return Decision("NO_SENSOR", "no_sensor", "No reading from the soil sensor - check the wire.",
                        "मिट्टी सेंसर से रीडिंग नहीं मिल रही - तार जाँचें।")
    m = z.m_now
    if z.session_active:
        return Decision("WATERING", "watering", f"Watering now ({m:.0f}% -> target {thr['target_pct']:.0f}%).",
                        f"सिंचाई चल रही है ({m:.0f}% -> लक्ष्य {thr['target_pct']:.0f}%)।")
    if m >= thr["target_pct"]:
        return Decision("SKIP", "enough_water", f"Enough water ({m:.0f}%). No irrigation needed.",
                        f"पर्याप्त नमी ({m:.0f}%)। पानी की ज़रूरत नहीं।")

    until = z.forecast.get("hours_until_min") if z.forecast else None
    need = m < thr["min_pct"] or (until is not None and until <= settings.LOOKAHEAD_H)
    if not need:
        if until is None:
            return Decision("SKIP", "ok_12h", f"Moisture OK ({m:.0f}%). No water needed in the next 12 h.",
                            f"नमी ठीक है ({m:.0f}%)। अगले 12 घंटे पानी की ज़रूरत नहीं।",
                            details={"hours_until_min": None})
        return Decision("SKIP", "ok_for_now",
                        f"Moisture OK ({m:.0f}%). Will need water in about {_h(until)} h.",
                        f"नमी ठीक है ({m:.0f}%)। लगभग {_h(until)} घंटे में पानी चाहिए।",
                        details={"hours_until_min": until})

    # --- the zone needs water: will the rain do the job? -------------------------------
    h6 = horizon(z.forecast, 6)
    pred6 = h6["moisture"] if h6 else None
    rain_likely = z.fc_rain_6h >= settings.RAIN_POSTPONE_MM and z.fc_prob_6h >= settings.RAIN_POSTPONE_PROB
    recovers = pred6 is not None and pred6 >= thr["min_pct"]
    critical = m < thr["wp_pct"] + settings.CRITICAL_FRACTION * (thr["min_pct"] - thr["wp_pct"])
    details = {"pred_6h": pred6, "fc_rain_6h": round(z.fc_rain_6h, 1), "fc_prob_6h": round(z.fc_prob_6h),
               "hours_until_min": until, "critical": critical}

    if rain_likely and recovers and not critical:
        return Decision("POSTPONE", "rain_expected",
                        f"Dry ({m:.0f}%) but rain expected ({z.fc_rain_6h:.0f} mm, {z.fc_prob_6h:.0f}%). "
                        f"AI predicts soil will recover to {pred6:.0f}% - irrigation postponed.",
                        f"सूखा है ({m:.0f}%) पर बारिश की संभावना ({z.fc_prob_6h:.0f}%)। "
                        f"AI के अनुसार नमी {pred6:.0f}% तक आएगी - सिंचाई टाली गई।", details=details)

    if z.cooling_down:
        return Decision("WAIT", "soaking", "Just watered - letting the water spread before checking again.",
                        "अभी पानी दिया - दोबारा जाँचने से पहले पानी फैलने दे रहे हैं।", details=details)

    # how much: light top-up to min if rain will come anyway, else refill to target
    goal = thr["min_pct"] if (rain_likely and recovers) else thr["target_pct"]
    deficit = max(goal - m, 0.0)
    seconds = min(deficit / max(z.pump_gain, 1e-3), settings.MAX_SESSION_PUMP_S)
    liters = seconds * z.flow_l_per_min / 60.0
    details.update({"goal_pct": round(goal, 1), "deficit_pct": round(deficit, 1), "liters": round(liters, 2)})
    if rain_likely and recovers:
        return Decision("IRRIGATE", "critical_light",
                        f"Critically dry ({m:.0f}%). Light watering now even though rain is expected.",
                        f"बहुत सूखा ({m:.0f}%)। बारिश की संभावना है फिर भी थोड़ा पानी दे रहे हैं।",
                        seconds=seconds, details=details)
    if m < thr["min_pct"]:
        code, en, hi = ("dry", f"Soil is dry ({m:.0f}% < {thr['min_pct']:.0f}%).",
                        f"मिट्टी सूखी है ({m:.0f}% < {thr['min_pct']:.0f}%)।")
    else:
        code, en, hi = ("dry_soon", f"Will be too dry in {_h(until)} h ({m:.0f}% now).",
                        f"{_h(until)} घंटे में बहुत सूखा हो जाएगा (अभी {m:.0f}%)।")
    if rain_likely and not recovers:
        en += " Rain forecast is too little to help."
        hi += " अनुमानित बारिश काफ़ी नहीं है।"
    return Decision("IRRIGATE", code,
                    f"{en} Water ~{seconds:.0f} s (~{liters:.1f} L) to reach {goal:.0f}%.",
                    f"{hi} ~{seconds:.0f} सेकंड (~{liters:.1f} लीटर) पानी, लक्ष्य {goal:.0f}%।",
                    seconds=seconds, details=details)
