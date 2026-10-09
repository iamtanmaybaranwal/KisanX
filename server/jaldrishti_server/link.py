"""Link between the Pi and the ESP32.

MQTT protocol (the ESP32 firmware implements exactly this):

  ESP32 -> Pi   jal/sensors    every 2 s
      {"raw": [r1, r2],                  12-bit ADC of the soil sensors (one per zone)
       "temp": 31.2, "rh": 48.0,         DHT22 (null if not fitted)
       "pumps": [0, 1],                  relay states
       "mode": "remote" | "fallback",    fallback = Pi silent, ESP32 on its own
       "uptime": 1234, "fw": "1.0"}
  ESP32 -> Pi   jal/status     "online" / "offline" (retained, last-will)
  Pi -> ESP32   jal/cmd        {"zone": 2, "seconds": 5.0, "id": 17}   timed pump run
                               {"zone": 2, "seconds": 0}                stop now
                               {"all_off": true}
  Pi -> ESP32   jal/heartbeat  {"t": 1730000000, "mode": "auto",   every 5 s
                                "z": [{"c": "Tomato", "m": 45, "lo": 36, "hi": 62, "f": false}, ...]}
                               "z" = what the ESP32's 16x2 LCD shows per zone: crop, moisture %,
                               ideal range, fault. Optional - the ESP32 only needs the message.

Every pump command is timed; the ESP32 caps it at 120 s and switches all
pumps off if no heartbeat arrives for 30 s.
"""
from __future__ import annotations

import json
import queue

TOPIC_SENSORS = "jal/sensors"
TOPIC_STATUS = "jal/status"
TOPIC_CMD = "jal/cmd"
TOPIC_HEARTBEAT = "jal/heartbeat"


class Link:
    """Interface used by the controller."""

    def poll(self, now: float) -> list[dict]:
        raise NotImplementedError

    def send_pump(self, zone: int, seconds: float, cmd_id: int) -> None:
        raise NotImplementedError

    def send_all_off(self) -> None:
        raise NotImplementedError

    def send_heartbeat(self, now: float, display: dict | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def kind(self) -> str:
        return type(self).__name__


class MqttLink(Link):
    """Talks to a real (or simulated) ESP32 through an MQTT broker."""

    def __init__(self, host: str = "localhost", port: int = 1883, client_id: str = "jaldrishti-pi"):
        import paho.mqtt.client as mqtt

        self._q: queue.Queue = queue.Queue()
        self.host, self.port = host, port
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 10)
        self.client.connect_async(host, port, keepalive=20)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.subscribe([(TOPIC_SENSORS, 0), (TOPIC_STATUS, 1)])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            if msg.topic == TOPIC_SENSORS:
                self._q.put(json.loads(msg.payload))
            elif msg.topic == TOPIC_STATUS:
                self._q.put({"status": msg.payload.decode(errors="replace")})
        except (ValueError, UnicodeDecodeError):
            pass                                    # ignore garbage, never crash the loop

    def poll(self, now: float) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    def _pub(self, topic, payload, qos=1):
        self.client.publish(topic, json.dumps(payload), qos=qos)

    def send_pump(self, zone, seconds, cmd_id):
        self._pub(TOPIC_CMD, {"zone": zone, "seconds": round(float(seconds), 2), "id": cmd_id})

    def send_all_off(self):
        self._pub(TOPIC_CMD, {"all_off": True})

    def send_heartbeat(self, now, display=None):
        self._pub(TOPIC_HEARTBEAT, {"t": round(now), **(display or {})}, qos=0)

    def close(self):
        try:
            self.send_all_off()
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
